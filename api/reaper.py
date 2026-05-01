"""Background reaper: idle-stop and hard-destroy. SPEC §6, ARCH §7.

The reaper runs as an asyncio task started by the FastAPI lifespan. It
acquires the same per-session locks as the public API, so an exec call
in flight will block the reaper rather than collide with it.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from api import metrics
from api.registry import Registry, SessionRow

if TYPE_CHECKING:
    from api.config import Settings
    from api.sessions import SessionService

log = logging.getLogger("sandbox.reaper")


class Reaper:
    def __init__(
        self,
        *,
        settings: Settings,
        registry: Registry,
        sessions: SessionService,
    ) -> None:
        self._settings = settings
        self._registry = registry
        self._sessions = sessions
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run(), name="sandbox-reaper")

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
            "reaper started: interval=%ds idle_stop=%dm hard_destroy=%dh",
            self._settings.reaper_interval_s,
            self._settings.idle_stop_minutes,
            self._settings.hard_destroy_hours,
        )
        while not self._stop_event.is_set():
            try:
                await self.tick()
            except Exception:
                log.exception("reaper tick crashed")
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self._settings.reaper_interval_s,
                )
                # If wait returns normally, stop_event was set.
                break
            except TimeoutError:
                pass
        log.info("reaper stopped")

    async def tick(self) -> None:
        """One reaper sweep: idle-stop, hard-destroy, refresh status gauge."""
        now_ms = int(time.time() * 1000)
        idle_ms = self._settings.idle_stop_minutes * 60 * 1000
        ttl_ms = self._settings.hard_destroy_hours * 60 * 60 * 1000

        idle_candidates = await self._registry.list_idle_running(now_ms - idle_ms)
        for row in idle_candidates:
            try:
                await self._sessions.reap_stop(row, reason="idle")
            except Exception:
                log.exception("reap_stop failed for %s", row.id)

        expired = await self._registry.list_expired(now_ms - ttl_ms)
        for row in expired:
            try:
                await self._sessions.reap_destroy(row, reason="ttl")
            except Exception:
                log.exception("reap_destroy failed for %s", row.id)

        await self._refresh_status_gauge()

    async def _refresh_status_gauge(self) -> None:
        counts = await self._registry.status_counts()
        for status in (
            "CREATING",
            "RUNNING",
            "IDLE",
            "STOPPED",
            "DESTROYING",
            "DESTROYED",
        ):
            metrics.sessions_by_status.labels(status=status).set(counts.get(status, 0))


# Re-exported symbol used by tests for monkeypatching.
__all__ = ["Reaper", "SessionRow"]
