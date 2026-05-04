"""Per-session resource sampler (slice 6b). SPEC-501.

Walks RUNNING / IDLE sessions every `resource_sample_interval_s`,
captures cpu / memory / blkio via the Docker stats API, and writes
one audit record per session per sweep
(`kind="session.sample"`). Aggregate Prometheus signals
(`sandbox_resource_samples_total`, `sandbox_resource_sample_duration_seconds`)
let operators tell whether the sampler itself is healthy without
exposing per-session-id label cardinality.
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

log = logging.getLogger("sandbox.sampler")


class SessionSampler:
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
        if self._settings.resource_sample_interval_s <= 0:
            log.info("resource sampler disabled (interval <= 0)")
            return
        if self._task is not None:
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run(), name="sandbox-sampler")

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
        interval = self._settings.resource_sample_interval_s
        log.info("resource sampler started: interval=%ds", interval)
        while not self._stop_event.is_set():
            try:
                await self.tick()
            except Exception:
                log.exception("sampler tick crashed")
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=interval)
                break
            except TimeoutError:
                pass
        log.info("resource sampler stopped")

    async def tick(self) -> None:
        """One sampling sweep — invoked periodically by `_run`, also
        directly from tests."""
        start = time.monotonic()
        # Reuse list_idle_running with a threshold of "now" to get every
        # row currently in RUNNING or IDLE.
        from api.registry import now_ms

        rows = await self._registry.list_idle_running(now_ms() + 1)
        for row in rows:
            if not row.container_id:
                continue
            try:
                stats = await asyncio.to_thread(self._docker.container_stats, row.container_id)
                if not stats:
                    # Container vanished mid-tick — let reconciliation
                    # handle it on next boot, or the reaper notice via
                    # exec failure. The sample is just skipped.
                    metrics.resource_samples_total.labels(result="error").inc()
                    continue
                await self._audit.emit(
                    kind="session.sample",
                    tenant=row.tenant_id,
                    session=row.id,
                    payload=stats,
                )
                metrics.resource_samples_total.labels(result="ok").inc()
            except Exception:
                log.exception("sample failed for %s", row.id)
                metrics.resource_samples_total.labels(result="error").inc()

        metrics.resource_sample_duration_seconds.observe(time.monotonic() - start)
