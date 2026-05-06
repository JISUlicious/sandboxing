"""Session lifecycle service. Ties registry + docker; owns the locks."""

from __future__ import annotations

import asyncio
import logging
import time

from ulid import ULID

from api import metrics, quota
from api.audit import AuditEmitter
from api.config import Settings
from api.docker_client import DockerClient
from api.errors import InvalidState, LimitExceeded, SessionNotFound
from api.models import Limits
from api.registry import Registry, SessionRow

log = logging.getLogger("sandbox.sessions")

# SPEC §6 tenant maxes.
_TENANT_MAX = {
    "vcpu": 4,
    "memory_mib": 8192,
    "workspace_mib": 10240,
    "pids": 1024,
    "nofile": 4096,
    "exec_timeout_s": 600,
}


class SessionService:
    def __init__(
        self,
        *,
        settings: Settings,
        registry: Registry,
        docker: DockerClient,
        audit: AuditEmitter,
    ) -> None:
        self.settings = settings
        self.registry = registry
        self.docker = docker
        self.audit = audit
        self._locks_meta = asyncio.Lock()
        self._locks: dict[str, asyncio.Lock] = {}
        # Slice 11b: optional callable invoked at the top of
        # `_destroy_locked` to clean up per-session resources owned
        # by other services (today: ProcessService.reap_session_processes).
        # `None` while the SessionService is the only collaborator.
        self._destroy_hook = None  # type: ignore[var-annotated]

    def set_destroy_hook(self, hook) -> None:
        """Register a coroutine called as `await hook(SessionRow)` at
        the start of session destroy. Used by ProcessService to
        SIGKILL background processes before the container is
        removed. Replacing an existing hook is intentional —
        composition is single-collaborator for v1."""
        self._destroy_hook = hook

    async def _lock_for(self, session_id: str) -> asyncio.Lock:
        async with self._locks_meta:
            lock = self._locks.get(session_id)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[session_id] = lock
            return lock

    # ----- public API -----

    async def create(self, tenant_id: str, limits: Limits | None) -> SessionRow:
        self.audit.precheck()
        n = await self.registry.count_concurrent(tenant_id)
        if n >= self.settings.tenant_max_concurrent:
            raise LimitExceeded(
                f"tenant has {n} concurrent sessions (max {self.settings.tenant_max_concurrent})"
            )
        limits = limits or self._default_limits()
        self._validate_limits(limits)

        session_id = str(ULID())
        volume_name = f"sandbox-vol-{session_id}"
        await self.registry.insert(
            session_id=session_id,
            tenant_id=tenant_id,
            volume_name=volume_name,
            limits=limits,
        )
        start_ns = time.monotonic_ns()
        try:
            await asyncio.to_thread(self.docker.create_volume, volume_name, session_id, tenant_id)
            # SPEC-302: hook for the operator's xfs_quota / prjquota
            # script. No-op in dev mode (cmd is empty by default).
            await quota.run_setup(
                cmd=self.settings.quota_setup_cmd,
                session_id=session_id,
                tenant_id=tenant_id,
                volume_name=volume_name,
                volume_base=self.settings.quota_volume_base,
                workspace_mib=limits.workspace_mib,
            )
            container_id = await asyncio.to_thread(
                self.docker.create_container,
                session_id=session_id,
                tenant_id=tenant_id,
                volume_name=volume_name,
                limits=limits,
            )
            await self.registry.set_container(session_id, container_id)
            await asyncio.to_thread(self.docker.start_container, container_id)
            await self.registry.transition(session_id, "RUNNING")
        except Exception:
            log.exception("create failed for %s", session_id)
            metrics.sessions_lifecycle_total.labels(transition="create", reason="error").inc()
            # Best-effort rollback. Full reconcile lands in slice 5.
            await asyncio.to_thread(self.docker.remove_volume, volume_name)
            try:
                await self.registry.transition(session_id, "DESTROYING")
                await self.registry.transition(session_id, "DESTROYED")
            except Exception:
                log.exception("rollback transition failed for %s", session_id)
            raise

        metrics.session_create_seconds.observe((time.monotonic_ns() - start_ns) / 1_000_000_000)
        metrics.sessions_lifecycle_total.labels(transition="create", reason="api").inc()
        row = await self.registry.get(session_id, tenant_id)
        assert row is not None
        await self.audit.emit(
            kind="session.create",
            tenant=tenant_id,
            session=session_id,
            payload={"limits": limits.model_dump()},
        )
        return row

    async def get(self, session_id: str, tenant_id: str) -> SessionRow:
        row = await self.registry.get(session_id, tenant_id)
        if row is None:
            raise SessionNotFound()
        return row

    async def stop(self, session_id: str, tenant_id: str) -> SessionRow:
        self.audit.precheck()
        lock = await self._lock_for(session_id)
        async with lock:
            row = await self.registry.get(session_id, tenant_id)
            if row is None:
                raise SessionNotFound()
            if row.status not in ("RUNNING", "IDLE"):
                raise InvalidState(f"cannot stop session in status {row.status}")
            assert row.container_id is not None
            await asyncio.to_thread(self.docker.stop_container, row.container_id)
            await self.registry.transition(session_id, "STOPPED")
        await self.audit.emit(kind="session.stop", tenant=tenant_id, session=session_id)
        return await self.get(session_id, tenant_id)

    async def resume(self, session_id: str, tenant_id: str) -> SessionRow:
        self.audit.precheck()
        lock = await self._lock_for(session_id)
        async with lock:
            row = await self.registry.get(session_id, tenant_id)
            if row is None:
                raise SessionNotFound()
            if row.status not in ("STOPPED", "IDLE"):
                raise InvalidState(f"cannot resume session in status {row.status}")
            assert row.container_id is not None
            start_ns = time.monotonic_ns()
            await asyncio.to_thread(self.docker.start_container, row.container_id)
            await self.registry.transition(session_id, "RUNNING")
            metrics.resume_seconds.observe((time.monotonic_ns() - start_ns) / 1_000_000_000)
        metrics.sessions_lifecycle_total.labels(transition="resume", reason="api").inc()
        await self.audit.emit(kind="session.resume", tenant=tenant_id, session=session_id)
        return await self.get(session_id, tenant_id)

    async def destroy(self, session_id: str, tenant_id: str) -> None:
        """Multi-step destroy ordering per ARCH-051."""
        self.audit.precheck()
        lock = await self._lock_for(session_id)
        async with lock:
            row = await self.registry.get(session_id, tenant_id)
            if row is None:
                raise SessionNotFound()
            await self._destroy_locked(row, reason="api")

    async def reap_stop(self, row: SessionRow, *, reason: str) -> None:
        """Tenant-agnostic stop used by the reaper. Idempotent w.r.t. status.

        Caller is the reaper; takes the per-session lock the same way the
        public API does so an exec in flight blocks the reaper rather than
        racing.
        """
        lock = await self._lock_for(row.id)
        async with lock:
            current = await self.registry.get_unscoped(row.id)
            if current is None or current.status not in ("RUNNING", "IDLE"):
                return
            assert current.container_id is not None
            await asyncio.to_thread(self.docker.stop_container, current.container_id)
            await self.registry.transition(row.id, "STOPPED")
        metrics.sessions_lifecycle_total.labels(transition="stop", reason=reason).inc()
        await self.audit.emit(
            kind="session.stop",
            tenant=row.tenant_id,
            session=row.id,
            payload={"reason": reason},
        )

    async def reap_destroy(self, row: SessionRow, *, reason: str) -> None:
        """Tenant-agnostic destroy used by the reaper for hard-TTL expiry."""
        lock = await self._lock_for(row.id)
        async with lock:
            current = await self.registry.get_unscoped(row.id)
            if current is None or current.status in ("DESTROYING", "DESTROYED"):
                return
            await self._destroy_locked(current, reason=reason)

    async def _destroy_locked(self, row: SessionRow, *, reason: str) -> None:
        """Shared destroy body assuming the per-session lock is already held."""
        await self.registry.transition(row.id, "DESTROYING")  # step 1
        try:
            # Slice 11b: reap background processes BEFORE container
            # removal. Hook is set by lifespan when ProcessService
            # is constructed; None for tests / minimal embeddings.
            if self._destroy_hook is not None:
                try:
                    await self._destroy_hook(row)
                except Exception:
                    log.exception("destroy_hook failed for %s; continuing", row.id)
            if row.container_id:
                await asyncio.to_thread(self.docker.remove_container, row.container_id)  # step 2
            # Quota teardown before volume removal — the operator script
            # can free its project ID with the directory still present.
            await quota.run_teardown(
                cmd=self.settings.quota_teardown_cmd,
                session_id=row.id,
                tenant_id=row.tenant_id,
                volume_name=row.volume_name,
                volume_base=self.settings.quota_volume_base,
            )
            await asyncio.to_thread(self.docker.remove_volume, row.volume_name)  # step 3
        finally:
            await self.registry.transition(row.id, "DESTROYED")  # step 4
        metrics.sessions_lifecycle_total.labels(transition="destroy", reason=reason).inc()
        await self.audit.emit(
            kind="session.destroy",
            tenant=row.tenant_id,
            session=row.id,
            payload={"reason": reason},
        )

    # ----- reconciliation (slice 6a) -----

    async def reconcile_on_startup(self) -> dict[str, int]:
        """Walk the registry on boot, finish stuck destroys, mark
        orphaned sessions STOPPED. The promise made in ARCH-051 that
        the spec docs reference but slice 1 punted on.

        Returns a small summary dict of counts so the caller (lifespan
        log line) can report what happened.
        """
        log.info("reconcile_on_startup: starting sweep")
        finished_destroy = 0
        orphaned = 0

        # 1. Finish any DESTROYING rows. Each docker call is already
        # idempotent on NotFound, so this works whether the failure
        # was before, during, or after the docker calls.
        for row in await self.registry.list_pending_destroy():
            try:
                if row.container_id:
                    await asyncio.to_thread(self.docker.remove_container, row.container_id)
                await quota.run_teardown(
                    cmd=self.settings.quota_teardown_cmd,
                    session_id=row.id,
                    tenant_id=row.tenant_id,
                    volume_name=row.volume_name,
                    volume_base=self.settings.quota_volume_base,
                )
                await asyncio.to_thread(self.docker.remove_volume, row.volume_name)
                await self.registry.transition(row.id, "DESTROYED")
                finished_destroy += 1
                await self.audit.emit(
                    kind="session.reconciled",
                    tenant=row.tenant_id,
                    session=row.id,
                    payload={"action": "finish_destroy"},
                )
            except Exception:
                log.exception("reconcile: finish_destroy failed for %s", row.id)

        # 2. Find orphaned non-terminal rows (container is gone but the
        # registry still says CREATING/RUNNING/IDLE/STOPPED). Mark them
        # STOPPED so the next exec gets a clean InvalidState rather
        # than docker NotFound. Volume is preserved.
        for row in await self.registry.list_non_terminal():
            if not row.container_id:
                # CREATING that never got a container — treat as orphaned.
                await self.registry.transition_orphaned(row.id)
                orphaned += 1
                await self.audit.emit(
                    kind="session.reconciled",
                    tenant=row.tenant_id,
                    session=row.id,
                    payload={"action": "orphaned_no_container"},
                )
                continue
            container_present = await asyncio.to_thread(
                self.docker.container_exists, row.container_id
            )
            if not container_present and row.status != "STOPPED":
                await self.registry.transition_orphaned(row.id)
                orphaned += 1
                await self.audit.emit(
                    kind="session.reconciled",
                    tenant=row.tenant_id,
                    session=row.id,
                    payload={"action": "orphaned_missing_container"},
                )

        log.info(
            "reconcile_on_startup: done (finished_destroy=%d orphaned=%d)",
            finished_destroy,
            orphaned,
        )
        return {"finished_destroy": finished_destroy, "orphaned": orphaned}

    # ----- helpers -----

    def _default_limits(self) -> Limits:
        s = self.settings
        return Limits(
            vcpu=s.default_vcpu,
            memory_mib=s.default_memory_mib,
            workspace_mib=s.default_workspace_mib,
            pids=s.default_pids,
            nofile=s.default_nofile,
            exec_timeout_s=s.default_exec_timeout_s,
        )

    @staticmethod
    def _validate_limits(limits: Limits) -> None:
        for field, cap in _TENANT_MAX.items():
            if getattr(limits, field) > cap:
                raise LimitExceeded(f"{field} exceeds tenant max ({cap})")
