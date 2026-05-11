"""Background-process service (slice 11b).

Long-running commands that survive across `exec` calls. The agent
starts a process, observes/stops it via four endpoints (+ MCP
tools, slice 11c). Process state lives in the SQLite registry —
the supervisor pattern in `DockerClient.spawn_supervised` writes
PID + exit-code files into the session's `/workspace`, and this
service polls them lazily on observation (no background watcher
loop; see slice plan for the trade-off).

All process IDs are session-scoped ULIDs distinct from the OS PID
the supervisor records. Clients should never rely on PID stability
across container restart.
"""

from __future__ import annotations

import asyncio
import logging
import time

from ulid import ULID

from api.audit import AuditEmitter
from api.config import Settings
from api.docker_client import DockerClient
from api.errors import (
    InvalidArgument,
    InvalidState,
    LimitExceeded,
    ProcessNotFound,
)
from api.models import ProcessResponse, StartProcessRequest
from api.registry import ProcessRow, Registry, now_ms
from api.sessions import SessionService

log = logging.getLogger("sandbox.processes")

_PROCESS_ROOT_REL = ".sandbox/processes"
"""Per-session in-`/workspace` directory holding pid/exit/log files."""


class ProcessService:
    def __init__(
        self,
        *,
        settings: Settings,
        registry: Registry,
        docker: DockerClient,
        audit: AuditEmitter,
        sessions: SessionService,
    ) -> None:
        self._settings = settings
        self._registry = registry
        self._docker = docker
        self._audit = audit
        self._sessions = sessions

    # ----- public API -----

    async def start(
        self, *, session_id: str, tenant_id: str, req: StartProcessRequest
    ) -> ProcessResponse:
        self._audit.precheck()
        if req.cwd is not None and not _is_safe_relative_cwd(req.cwd):
            raise InvalidArgument("cwd must be a relative path under /workspace")
        # Auto-resume STOPPED sessions like exec does — the agent
        # shouldn't need to track session state to start a process.
        session = await self._sessions.get(session_id, tenant_id)
        if session.status in ("STOPPED", "IDLE"):
            session = await self._sessions.resume(session_id, tenant_id)
        if session.status != "RUNNING":
            raise InvalidState(f"cannot start process in session status {session.status}")
        assert session.container_id is not None

        # Concurrency cap (per-session limit + tenant clamp).
        running = await self._registry.count_running_processes(session_id)
        cap = min(session.limits.max_processes, self._settings.tenant_max_processes)
        if running >= cap:
            raise LimitExceeded(f"session has {running} running processes (max {cap})")

        process_id = str(ULID())
        log_path = f"/workspace/{_PROCESS_ROOT_REL}/{process_id}.log"
        pid_path = f"/workspace/{_PROCESS_ROOT_REL}/{process_id}.pid"
        exit_path = f"/workspace/{_PROCESS_ROOT_REL}/{process_id}.exit"
        cwd = f"/workspace/{req.cwd}" if req.cwd else "/workspace"

        # Spawn detached. The supervisor writes `pid_path` BEFORE
        # `exec`-ing argv, so we can read it back ~50 ms later.
        await asyncio.to_thread(
            self._docker.spawn_supervised,
            container_id=session.container_id,
            argv=req.argv,
            env=req.env,
            cwd=cwd,
            pid_path=pid_path,
            exit_path=exit_path,
            log_path=log_path,
        )
        ospid = await self._read_pid_with_retry(session.container_id, pid_path)

        await self._registry.insert_process(
            process_id=process_id,
            session_id=session_id,
            tenant_id=tenant_id,
            name=req.name,
            argv=req.argv,
            cwd=req.cwd,
            restart_policy="never",
            ospid=ospid,
            log_path=log_path,
            exit_path=exit_path,
        )
        await self._audit.emit(
            kind="process.start",
            tenant=tenant_id,
            session=session_id,
            payload={
                "process_id": process_id,
                "argv": req.argv,
                "name": req.name,
            },
        )
        row = await self._registry.get_process(
            session_id=session_id, process_id=process_id, tenant_id=tenant_id
        )
        assert row is not None
        # Slice 13c — starting a process is a mutation; pin.
        await self._sessions.bump_activity(session_id)
        return _row_to_response(row)

    async def list(self, *, session_id: str, tenant_id: str) -> list[ProcessResponse]:
        # Confirm the session belongs to the tenant (404s otherwise).
        await self._sessions.get(session_id, tenant_id)
        rows = await self._registry.list_processes(session_id=session_id, tenant_id=tenant_id)
        # Lazy refresh of RUNNING rows. Throttled by `last_polled_at`
        # so a flurry of GETs doesn't hammer the docker daemon.
        refreshed: list[ProcessResponse] = []
        for row in rows:
            if row.state == "RUNNING":
                row = await self._maybe_refresh(row, session_id)
            refreshed.append(_row_to_response(row))
        return refreshed

    async def get(self, *, session_id: str, tenant_id: str, process_id: str) -> ProcessResponse:
        await self._sessions.get(session_id, tenant_id)
        row = await self._registry.get_process(
            session_id=session_id, process_id=process_id, tenant_id=tenant_id
        )
        if row is None:
            raise ProcessNotFound(process_id)
        if row.state == "RUNNING":
            row = await self._maybe_refresh(row, session_id, force=True)
        return _row_to_response(row)

    async def stop(
        self,
        *,
        session_id: str,
        tenant_id: str,
        process_id: str,
        grace_s: int | None = None,
    ) -> ProcessResponse:
        """SIGTERM, wait `grace_s` (default `process_stop_grace_s`),
        SIGKILL if still alive. Idempotent on already-EXITED rows."""
        self._audit.precheck()
        session = await self._sessions.get(session_id, tenant_id)
        row = await self._registry.get_process(
            session_id=session_id, process_id=process_id, tenant_id=tenant_id
        )
        if row is None:
            raise ProcessNotFound(process_id)
        if row.state == "EXITED" or row.ospid is None:
            return _row_to_response(row)
        assert session.container_id is not None
        grace = grace_s if grace_s is not None else self._settings.process_stop_grace_s
        await self._sigterm_then_sigkill(session.container_id, row.ospid, grace_s=grace)
        row = await self._maybe_refresh(row, session_id, force=True)
        await self._audit.emit(
            kind="process.stop",
            tenant=tenant_id,
            session=session_id,
            payload={"process_id": process_id, "exit_code": row.exit_code},
        )
        return _row_to_response(row)

    async def delete(self, *, session_id: str, tenant_id: str, process_id: str) -> ProcessResponse:
        """Stop (if running) AND drop the registry row.
        Equivalent to `stop` followed by registry delete."""
        snapshot = await self.stop(
            session_id=session_id, tenant_id=tenant_id, process_id=process_id
        )
        await self._registry.delete_process(session_id=session_id, process_id=process_id)
        # Slice 13c — deleting a background process is a mutation; pin.
        await self._sessions.bump_activity(session_id)
        return snapshot

    async def tail_logs(
        self,
        *,
        session_id: str,
        tenant_id: str,
        process_id: str,
        lines: int = 100,
    ) -> tuple[str, bool]:
        """Return (text, truncated_to_cap) for the last `lines` lines
        of the process's merged stdout+stderr log. Used by the
        non-streaming MCP `process_logs` tool."""
        session = await self._sessions.get(session_id, tenant_id)
        row = await self._registry.get_process(
            session_id=session_id, process_id=process_id, tenant_id=tenant_id
        )
        if row is None:
            raise ProcessNotFound(process_id)
        if session.container_id is None:
            return "", False
        return await asyncio.to_thread(
            self._docker.tail_text_in_container,
            session.container_id,
            row.log_path,
            lines=lines,
        )

    def stream_log_iter(self, session, log_path: str):
        """Sync generator yielding raw log chunks. Wrapped by the SSE
        route handler with `asyncio.to_thread`-style bridging. Returns
        the underlying iterator the route can iterate over with
        cancellation propagation."""
        return self._docker.stream_log_lines(session.container_id, log_path)

    async def reap_session_processes(self, session: object) -> int:
        """Called from `SessionService._destroy_locked` before container
        removal. SIGKILLs every RUNNING process owned by the session.
        Returns count reaped. Idempotent.
        """
        # Imported lazily to avoid a circular dep at module import.
        from api.registry import SessionRow

        if not isinstance(session, SessionRow):
            raise TypeError("reap_session_processes expects a SessionRow")
        if session.container_id is None:
            return 0
        rows = await self._registry.list_running_processes_unscoped(session.id)
        for row in rows:
            if row.ospid is not None:
                try:
                    await asyncio.to_thread(
                        self._docker.signal_pid, session.container_id, row.ospid, 9
                    )
                except Exception:
                    log.exception("SIGKILL %s failed", row.ospid)
            await self._registry.mark_process_exited(process_id=row.id, exit_code=137)
        return len(rows)

    # ----- internals -----

    async def _read_pid_with_retry(self, container_id: str, pid_path: str) -> int | None:
        """Poll the supervisor's pid file for up to ~1s. Bash is fast
        enough that this almost always returns on the first try."""
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            text = await asyncio.to_thread(
                self._docker.read_text_in_container, container_id, pid_path
            )
            if text:
                stripped = text.strip()
                if stripped.isdigit():
                    return int(stripped)
            await asyncio.sleep(0.05)
        log.warning("supervisor pid file %s never appeared", pid_path)
        return None

    async def _maybe_refresh(
        self, row: ProcessRow, session_id: str, *, force: bool = False
    ) -> ProcessRow:
        """If the OS process is gone, mark the row EXITED and read its
        exit code. Throttled by `process_watcher_interval_s` unless
        `force=True` (used by `get`/`stop` where the caller wants a
        definitive read)."""
        if row.ospid is None:
            return row
        if not force and row.last_polled_at is not None:
            elapsed = (now_ms() - row.last_polled_at) / 1000.0
            if elapsed < self._settings.process_watcher_interval_s:
                return row
        # Probe via the session container.
        session = await self._registry.get_unscoped(session_id)
        if session is None or session.container_id is None:
            return row
        alive = await asyncio.to_thread(self._docker.pid_alive, session.container_id, row.ospid)
        if alive:
            await self._registry.touch_process_polled(row.id)
            return row
        # Dead: read the exit file. There is a small race window between
        # the child process terminating and the bash supervisor writing
        # `$?` to exit_path (a few ms in practice). Retry with backoff
        # so we don't permanently mark exit_code=null when we caught
        # the supervisor mid-write. Once the row is in EXITED, _maybe_refresh
        # never re-runs, so getting this right on the first dead-detection
        # is critical.
        exit_code: int | None = None
        for attempt in range(5):
            exit_text = await asyncio.to_thread(
                self._docker.read_text_in_container, session.container_id, row.exit_path
            )
            if exit_text is not None:
                stripped = exit_text.strip()
                if stripped.isdigit() or (stripped.startswith("-") and stripped[1:].isdigit()):
                    exit_code = int(stripped)
                    break
            if attempt < 4:
                await asyncio.sleep(0.05 * (attempt + 1))  # 50, 100, 150, 200 ms
        await self._registry.mark_process_exited(process_id=row.id, exit_code=exit_code)
        refreshed = await self._registry.get_process(
            session_id=session_id, process_id=row.id, tenant_id=row.tenant_id
        )
        return refreshed or row

    async def _sigterm_then_sigkill(self, container_id: str, ospid: int, *, grace_s: int) -> None:
        await asyncio.to_thread(self._docker.signal_pid, container_id, ospid, 15)
        # Poll alive every 200ms up to grace_s.
        deadline = time.monotonic() + grace_s
        while time.monotonic() < deadline:
            alive = await asyncio.to_thread(self._docker.pid_alive, container_id, ospid)
            if not alive:
                return
            await asyncio.sleep(0.2)
        await asyncio.to_thread(self._docker.signal_pid, container_id, ospid, 9)


def _row_to_response(row: ProcessRow) -> ProcessResponse:
    return ProcessResponse(
        process_id=row.id,
        name=row.name,
        argv=row.argv,
        state=row.state,  # type: ignore[arg-type]
        exit_code=row.exit_code,
        started_at=row.started_at,
        exited_at=row.exited_at,
        last_output_at=row.last_output_at,
    )


def _is_safe_relative_cwd(cwd: str) -> bool:
    """Reject absolute paths and `..` escapes. The control plane joins
    the cwd onto `/workspace/`, so an absolute / escaping path would
    let a caller cd outside the workspace."""
    if not cwd or cwd.startswith("/") or cwd.startswith("~"):
        return False
    parts = [p for p in cwd.split("/") if p]
    return all(p != ".." for p in parts)
