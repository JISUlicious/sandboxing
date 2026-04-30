"""Session lifecycle service. Ties registry + docker; owns the locks."""

from __future__ import annotations

import asyncio
import logging

from ulid import ULID

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

    async def _lock_for(self, session_id: str) -> asyncio.Lock:
        async with self._locks_meta:
            lock = self._locks.get(session_id)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[session_id] = lock
            return lock

    # ----- public API -----

    async def create(self, tenant_id: str, limits: Limits | None) -> SessionRow:
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
        try:
            await asyncio.to_thread(self.docker.create_volume, volume_name, session_id, tenant_id)
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
            # Best-effort rollback. Full reconcile lands in slice 5.
            await asyncio.to_thread(self.docker.remove_volume, volume_name)
            try:
                await self.registry.transition(session_id, "DESTROYING")
                await self.registry.transition(session_id, "DESTROYED")
            except Exception:
                log.exception("rollback transition failed for %s", session_id)
            raise

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
        lock = await self._lock_for(session_id)
        async with lock:
            row = await self.registry.get(session_id, tenant_id)
            if row is None:
                raise SessionNotFound()
            if row.status not in ("STOPPED", "IDLE"):
                raise InvalidState(f"cannot resume session in status {row.status}")
            assert row.container_id is not None
            await asyncio.to_thread(self.docker.start_container, row.container_id)
            await self.registry.transition(session_id, "RUNNING")
        await self.audit.emit(kind="session.resume", tenant=tenant_id, session=session_id)
        return await self.get(session_id, tenant_id)

    async def destroy(self, session_id: str, tenant_id: str) -> None:
        """Multi-step destroy ordering per ARCH-051."""
        lock = await self._lock_for(session_id)
        async with lock:
            row = await self.registry.get(session_id, tenant_id)
            if row is None:
                raise SessionNotFound()
            await self.registry.transition(session_id, "DESTROYING")  # step 1
            try:
                if row.container_id:
                    await asyncio.to_thread(
                        self.docker.remove_container, row.container_id
                    )  # step 2
                await asyncio.to_thread(self.docker.remove_volume, row.volume_name)  # step 3
            finally:
                await self.registry.transition(session_id, "DESTROYED")  # step 4
        await self.audit.emit(kind="session.destroy", tenant=tenant_id, session=session_id)

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
