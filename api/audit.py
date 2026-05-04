"""Append-only JSONL audit emitter with fail-closed semantics. ARCH-060,
ARCH §7.

Healthy path: best-effort write to `path`. Each emit attempt also
attempts to drain any in-memory buffer accumulated during a previous
unhealthy window.

Unhealthy path: a write failure (OSError) flips `is_healthy` to False,
queues the offending record into a memory buffer, and starts a 5 s
budget. While unhealthy:

- New API mutations are rejected with `503 audit_unhealthy` (callers
  invoke `precheck()` at the entry to lifecycle / exec / file ops).
- In-flight requests still complete; their audit emit calls land in
  the buffer.
- After `audit_buffer_timeout_s` of continuous failure, the buffer is
  flushed to a fallback file `audit.fallback.jsonl` so memory doesn't
  grow unbounded; operators reconcile it manually before clearing the
  alert.

Recovery: the next successful write drains the buffer back to the
primary log and flips `is_healthy` to True.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any

from api import metrics
from api.errors import AuditUnhealthy

log = logging.getLogger("sandbox.audit")


class AuditEmitter:
    def __init__(
        self,
        path: Path,
        *,
        fallback_path: Path | None = None,
        buffer_timeout_s: float = 5.0,
    ) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fallback_path = (
            Path(fallback_path)
            if fallback_path is not None
            else self.path.with_suffix(self.path.suffix + ".fallback.jsonl")
        )
        self.buffer_timeout_s = buffer_timeout_s

        self._lock = asyncio.Lock()
        self._buffer: list[str] = []
        self._unhealthy_since_monotonic: float | None = None

    @property
    def is_healthy(self) -> bool:
        return self._unhealthy_since_monotonic is None

    @property
    def buffered_count(self) -> int:
        return len(self._buffer)

    def precheck(self) -> None:
        """Raise AuditUnhealthy if the audit log is currently failing.

        Service methods that emit audit records call this at the entry,
        before any side effect, so failed mutations don't escape the
        record. Reads (which don't emit audit) skip the precheck.
        """
        if not self.is_healthy:
            raise AuditUnhealthy()

    async def emit(
        self,
        *,
        kind: str,
        tenant: str,
        session: str | None = None,
        actor: str | None = None,
        payload: Any = None,
        result: str = "ok",
        duration_ms: int | None = None,
    ) -> None:
        record = {
            "ts": int(time.time() * 1000),
            "kind": kind,
            "tenant": tenant,
            "session": session,
            "actor": actor,
            "payload": payload,
            "result": result,
            "duration_ms": duration_ms,
        }
        line = json.dumps(record, separators=(",", ":")) + "\n"
        async with self._lock:
            try:
                # Drain any backlog FIRST so the timeline stays ordered:
                # previously-buffered records land before this new one.
                await self._drain_buffer_locked()
                self._write_line(self.path, line)
            except OSError as exc:
                if self._unhealthy_since_monotonic is None:
                    self._unhealthy_since_monotonic = time.monotonic()
                    log.error("audit emit failed; entering fail-closed: %s", exc)
                self._buffer.append(line)
                self._maybe_flush_to_fallback_locked()
            else:
                self._unhealthy_since_monotonic = None
        metrics.audit_emit_total.labels(kind=kind).inc()

    async def maintenance_tick(self) -> None:
        """Periodic check the lifespan / reaper can call so the fallback
        flush still happens when no new audit calls come in."""
        async with self._lock:
            if self.is_healthy:
                return
            # Try one recovery attempt: write a small heartbeat record.
            heartbeat = (
                json.dumps(
                    {
                        "ts": int(time.time() * 1000),
                        "kind": "audit.heartbeat",
                        "tenant": "system",
                        "result": "recovered",
                    },
                    separators=(",", ":"),
                )
                + "\n"
            )
            try:
                self._write_line(self.path, heartbeat)
                await self._drain_buffer_locked()
                self._unhealthy_since_monotonic = None
                log.info("audit recovered after maintenance tick")
                return
            except OSError:
                self._maybe_flush_to_fallback_locked()

    def _write_line(self, target: Path, line: str) -> None:
        # Open + write + close synchronously; fsync would help durability
        # but is intentionally skipped here for v1 throughput. The
        # fail-closed branch fires on any OSError including ENOSPC.
        # Instance method (not @staticmethod) so tests can subclass
        # AuditEmitter and inject failures by overriding it.
        with open(target, "a", encoding="utf-8") as f:
            f.write(line)

    async def _drain_buffer_locked(self) -> None:
        """Replay buffered records to the primary log via _write_line so a
        test-injected (or real) failure propagates up to the caller. The
        records that made it to disk are removed from the buffer; the
        rest stay queued for the next attempt. Lock held by caller."""
        if not self._buffer:
            return
        drained = 0
        try:
            for line in self._buffer:
                self._write_line(self.path, line)
                drained += 1
        finally:
            del self._buffer[:drained]
        if drained:
            log.info("audit drained %d buffered records on recovery", drained)

    def _maybe_flush_to_fallback_locked(self) -> None:
        """If we've been unhealthy past the budget, dump buffer to the
        fallback file so memory doesn't grow without bound. Lock held by
        caller."""
        assert self._unhealthy_since_monotonic is not None
        elapsed = time.monotonic() - self._unhealthy_since_monotonic
        if elapsed < self.buffer_timeout_s:
            return
        if not self._buffer:
            return
        try:
            # The fallback file lives next to the audit log; if it can't
            # be written either, give up and warn — the buffer keeps
            # holding the records so a later recovery may still flush
            # them to the primary log.
            with open(self.fallback_path, "a", encoding="utf-8") as f:
                for line in self._buffer:
                    f.write(line)
            log.warning(
                "audit unhealthy %.1fs; flushed %d records to %s",
                elapsed,
                len(self._buffer),
                self.fallback_path,
            )
            self._buffer.clear()
        except OSError as exc:
            log.error("fallback audit write failed too: %s", exc)
