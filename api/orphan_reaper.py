"""Orphan-resource sweeper (slice 13a).

Catches Docker volumes and containers labelled `sandbox.session_id`
that the registry doesn't know about. Three scenarios produce such
orphans: a mid-create crash between `create_volume` and the registry
insert, a registry restore from an older backup, or an operator who
hand-edited the SQLite DB. The main reaper (`api/reaper.py`) handles
the inverse direction — registry rows whose Docker resources are
gone — but never sees Docker-side leaks because it iterates the
registry, not Docker.

**SAFETY — pass the real production registry.** This reaper concludes
a Docker resource is orphaned when `Registry.get_unscoped(session_id)`
returns None. If you build an `OrphanReaper` against a fresh / empty
registry (e.g., a tmp_path test fixture) and call `tick()` on a host
with real labelled containers and volumes, *every* labelled resource
will look orphaned and the reaper will start destroying real
production data. The per-tick cap mitigates blast radius but does
not prevent the loss.

Acceptable construction:
  - Live service: `OrphanReaper(registry=service.registry, ...)`. Safe.
  - Integration tests: stub `registry.get_unscoped` to return non-None
    for any session id you don't own — only the test's own
    sid should ever be "unknown." See
    `tests/integration/test_orphan_reaper_real.py` for the pattern.

Two passes per tick, in this order:

1. Containers — `docker ps -a --filter label=sandbox.session_id` →
   for each, look up the session_id in the registry; if absent and
   the container's `Created` timestamp is older than the grace
   window, `docker rm` it.
2. Volumes — `docker volume ls --filter label=sandbox.session_id` →
   same filter; `docker volume rm` matching orphans.

Order matters: Docker refuses `volume rm` on a volume mounted by
any container, even an orphaned one. Sweeping containers first
unblocks volumes for the same tick (or, if Docker's release
timing is slow, the next).

Safety:
- Grace window (default 1h) avoids the create-path race and
  absorbs startup inconsistency before the existing reconciler
  finishes its pass.
- Per-tick cap (default 10) so a registry wipe or stale-backup
  restore can't shred everything in one sweep. The audit emit
  preserves a forensic record.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from api import metrics

if TYPE_CHECKING:
    from api.audit import AuditEmitter
    from api.config import Settings
    from api.docker_client import DockerClient
    from api.registry import Registry

log = logging.getLogger("sandbox.orphan-reaper")

LABEL_KEY = "sandbox.session_id"


class OrphanReaper:
    def __init__(
        self,
        *,
        settings: Settings,
        registry: Registry,
        docker: DockerClient,
        audit: AuditEmitter,
    ) -> None:
        self._settings = settings
        self._registry = registry
        self._docker = docker
        self._audit = audit
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()

    async def start(self) -> None:
        if self._settings.orphan_reap_interval_s <= 0:
            log.info("orphan reaper disabled (interval <= 0)")
            return
        if self._task is not None:
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run(), name="sandbox-orphan-reaper")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stop_event.set()
        try:
            await asyncio.wait_for(self._task, timeout=5.0)
        except TimeoutError:
            self._task.cancel()
        self._task = None

    async def _run(self) -> None:
        log.info(
            "orphan reaper started: interval=%ds grace=%ds max_per_tick=%d",
            self._settings.orphan_reap_interval_s,
            self._settings.orphan_reap_grace_s,
            self._settings.orphan_reap_max_per_tick,
        )
        while not self._stop_event.is_set():
            try:
                await self.tick()
            except Exception:
                log.exception("orphan reaper tick crashed")
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self._settings.orphan_reap_interval_s,
                )
                break
            except TimeoutError:
                pass
        log.info("orphan reaper stopped")

    async def tick(self) -> None:
        """One sweep: containers first (so they release volumes they
        hold), then volumes. Each pass shares the same per-tick cap
        — the goal is to bound total destructive work, not per-kind
        work."""
        now = time.time()
        grace = self._settings.orphan_reap_grace_s
        budget = self._settings.orphan_reap_max_per_tick

        # ---- pass 1: containers ----
        try:
            containers = await asyncio.to_thread(self._docker.list_containers_with_label, LABEL_KEY)
        except Exception:
            log.exception("orphan reaper: container list failed")
            containers = []
        budget = await self._sweep(
            kind="container",
            items=containers,
            now=now,
            grace=grace,
            budget=budget,
            remove=lambda name: asyncio.to_thread(self._docker.remove_container, name),
        )
        if budget <= 0:
            return

        # ---- pass 2: volumes ----
        try:
            volumes = await asyncio.to_thread(self._docker.list_volumes_with_label, LABEL_KEY)
        except Exception:
            log.exception("orphan reaper: volume list failed")
            volumes = []
        await self._sweep(
            kind="volume",
            items=volumes,
            now=now,
            grace=grace,
            budget=budget,
            remove=lambda name: asyncio.to_thread(self._docker.remove_volume, name),
        )

    async def _sweep(
        self,
        *,
        kind: str,
        items: list[dict],
        now: float,
        grace: float,
        budget: int,
        remove,
    ) -> int:
        """Walk `items`; for each, decide reap / skip / leave alone.
        Returns the remaining budget so the caller can carry it into
        the next pass."""
        for item in items:
            if budget <= 0:
                metrics.orphan_reap_total.labels(kind=kind, result="skipped").inc()
                continue
            session_id = (item.get("labels") or {}).get(LABEL_KEY) or ""
            if not session_id:
                # Label present but value missing — keep counting under
                # "skipped" so the operator sees something is off.
                metrics.orphan_reap_total.labels(kind=kind, result="skipped").inc()
                continue

            # Registry-known? Then the main reaper owns this row.
            row = await self._registry.get_unscoped(session_id)
            if row is not None:
                continue

            # Within grace? Could still be mid-create.
            created = float(item.get("created_epoch_s") or 0.0)
            age = now - created if created > 0 else float("inf")
            if age < grace:
                metrics.orphan_reap_total.labels(kind=kind, result="skipped").inc()
                continue

            name = item.get("name") or ""
            payload = {
                "resource_type": kind,
                "name": name,
                "label_session_id": session_id,
                "age_seconds": int(age) if age != float("inf") else None,
            }
            try:
                await remove(name)
                await self._audit.emit(
                    kind="orphan.reap",
                    # Tenant label may be missing on hand-crafted orphans
                    # (e.g., operator-created bare volumes); record a
                    # sentinel so the audit row is still well-shaped.
                    tenant=(item.get("labels") or {}).get("sandbox.tenant_id") or "unknown",
                    session=session_id,
                    payload=payload,
                )
                metrics.orphan_reap_total.labels(kind=kind, result="ok").inc()
                budget -= 1
                log.info(
                    "reaped orphan %s name=%s session_id=%s age_s=%s",
                    kind,
                    name,
                    session_id,
                    payload["age_seconds"],
                )
            except Exception:
                log.exception("orphan reap %s name=%s failed", kind, name)
                metrics.orphan_reap_total.labels(kind=kind, result="error").inc()
        return budget


__all__ = ["OrphanReaper", "LABEL_KEY"]
