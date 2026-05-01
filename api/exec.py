"""Exec dispatch (slices 2 + 3). SPEC-201, SPEC-202, SPEC-203, SPEC-301."""

from __future__ import annotations

import asyncio
import base64
import logging
import threading
import time
from collections.abc import AsyncIterator
from typing import Any

from api import metrics
from api.audit import AuditEmitter
from api.docker_client import (
    TIMEOUT_EXIT_CODE,
    DockerClient,
    _append_capped,
)
from api.errors import ExecTimeout, InvalidArgument, InvalidState, SessionNotFound
from api.models import ExecRequest, ExecResponse
from api.registry import Registry, SessionRow

log = logging.getLogger("sandbox.exec")

# SPEC-201: env keys we own and must not be overridden.
FORBIDDEN_ENV_KEYS = frozenset({"HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY"})

# SPEC §6 tenant max for exec timeout.
TENANT_MAX_TIMEOUT_S = 600

# SPEC-201 stdin limit when sent inline as UTF-8.
STDIN_INLINE_LIMIT_BYTES = 1 * 1024 * 1024


class ExecService:
    def __init__(
        self,
        *,
        registry: Registry,
        docker: DockerClient,
        audit: AuditEmitter,
    ) -> None:
        self.registry = registry
        self.docker = docker
        self.audit = audit

    # ----- non-streaming -----

    async def run(self, session_id: str, tenant_id: str, req: ExecRequest) -> ExecResponse:
        session = await self._prepare(session_id, tenant_id, req)
        timeout_s = self._effective_timeout(req, session)
        env = req.env or {}
        stdin_bytes = self._encode_stdin(req)
        assert session.container_id is not None

        out = await asyncio.to_thread(
            self.docker.exec_in_container,
            container_id=session.container_id,
            argv=req.argv,
            env=env,
            timeout_s=timeout_s,
            stdin_bytes=stdin_bytes,
        )

        await self.audit.emit(
            kind="session.exec",
            tenant=tenant_id,
            session=session_id,
            payload={
                "argv": req.argv,
                "env_keys": sorted((req.env or {}).keys()),
                "timeout_s": timeout_s,
                "stdin_size": len(stdin_bytes) if stdin_bytes else 0,
            },
            result="timeout" if out.exit_code == TIMEOUT_EXIT_CODE else "ok",
            duration_ms=out.duration_ms,
        )

        result_label = "timeout" if out.exit_code == TIMEOUT_EXIT_CODE else "ok"
        metrics.exec_duration_seconds.labels(result=result_label).observe(out.duration_ms / 1000.0)

        if out.exit_code == TIMEOUT_EXIT_CODE:
            raise ExecTimeout()

        return ExecResponse(
            stdout=out.stdout.decode("utf-8", errors="replace"),
            stderr=out.stderr.decode("utf-8", errors="replace"),
            exit_code=out.exit_code,
            duration_ms=out.duration_ms,
            effective_timeout_s=timeout_s,
            truncated=bool(out.truncated_streams),
            truncated_streams=out.truncated_streams,
        )

    # ----- streaming (SPEC-202) -----

    @staticmethod
    def validate_stream_request(req: ExecRequest) -> None:
        """Stream-specific pre-check; call from the route handler before
        opening the response body so HTTPException turns into a plain 4xx
        rather than getting buried inside a half-flushed SSE stream."""
        if req.stdin is not None:
            raise InvalidArgument("stdin is not yet supported on /exec/stream (use /exec)")

    async def run_stream(
        self, session_id: str, tenant_id: str, req: ExecRequest
    ) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        """Yields SSE-shaped events: ("stdout"|"stderr"|"truncated"|"result", payload).

        stdin is NOT supported on the streaming endpoint in slice 3 — combining
        socket-mode stdin with live demuxing is a slice 4 deliverable. Inline
        stdin on the synchronous /exec is fine.
        """
        self.validate_stream_request(req)
        session = await self._prepare(session_id, tenant_id, req)
        timeout_s = self._effective_timeout(req, session)
        env = req.env or {}
        assert session.container_id is not None

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[tuple[str, bytes | int] | None] = asyncio.Queue()
        SENTINEL = None

        def producer() -> None:
            try:
                for event in self.docker.exec_stream_in_container(
                    container_id=session.container_id,  # type: ignore[arg-type]
                    argv=req.argv,
                    env=env,
                    timeout_s=timeout_s,
                ):
                    asyncio.run_coroutine_threadsafe(queue.put(event), loop)
            except Exception as exc:  # noqa: BLE001
                log.exception("exec stream producer crashed: %s", exc)
                asyncio.run_coroutine_threadsafe(queue.put(("error", str(exc).encode())), loop)
            finally:
                asyncio.run_coroutine_threadsafe(queue.put(SENTINEL), loop)

        threading.Thread(target=producer, daemon=True).start()

        stdout = bytearray()
        stderr = bytearray()
        truncated: set[str] = set()
        announced: set[str] = set()
        exit_code = 0
        start_ns = time.monotonic_ns()

        while True:
            event = await queue.get()
            if event is SENTINEL:
                break
            kind, payload = event
            if kind in ("stdout", "stderr"):
                assert isinstance(payload, (bytes, bytearray))
                pre = kind in truncated
                _append_capped(stdout if kind == "stdout" else stderr, payload, kind, truncated)
                if not pre:
                    yield (
                        kind,
                        {"chunk_b64": base64.b64encode(payload).decode()},
                    )
                if kind in truncated and kind not in announced:
                    yield ("truncated", {"stream": kind})
                    announced.add(kind)
            elif kind == "exit":
                assert isinstance(payload, int)
                exit_code = payload
            elif kind == "error":
                assert isinstance(payload, (bytes, bytearray))
                yield ("error", {"message": payload.decode("utf-8", errors="replace")})

        duration_ms = (time.monotonic_ns() - start_ns) // 1_000_000

        timed_out = exit_code == TIMEOUT_EXIT_CODE
        result_payload = ExecResponse(
            stdout=stdout.decode("utf-8", errors="replace"),
            stderr=stderr.decode("utf-8", errors="replace"),
            exit_code=-1 if timed_out else exit_code,
            duration_ms=duration_ms,
            effective_timeout_s=timeout_s,
            truncated=bool(truncated),
            truncated_streams=sorted(truncated),
        ).model_dump()
        if timed_out:
            result_payload["error"] = "exec_timeout"

        await self.audit.emit(
            kind="session.exec.stream",
            tenant=tenant_id,
            session=session_id,
            payload={
                "argv": req.argv,
                "env_keys": sorted((req.env or {}).keys()),
                "timeout_s": timeout_s,
            },
            result="timeout" if timed_out else "ok",
            duration_ms=duration_ms,
        )

        yield ("result", result_payload)

    # ----- helpers -----

    async def _prepare(self, session_id: str, tenant_id: str, req: ExecRequest) -> SessionRow:
        session = await self.registry.get(session_id, tenant_id)
        if session is None:
            raise SessionNotFound()
        self._validate(req)
        if session.status not in ("RUNNING", "IDLE", "STOPPED"):
            raise InvalidState(f"cannot exec on session in status {session.status}")
        if session.status in ("STOPPED", "IDLE"):
            assert session.container_id is not None
            start_ns = time.monotonic_ns()
            await asyncio.to_thread(self.docker.start_container, session.container_id)
            await self.registry.transition(session_id, "RUNNING")
            metrics.resume_seconds.observe((time.monotonic_ns() - start_ns) / 1_000_000_000)
            session = await self.registry.get(session_id, tenant_id)
            assert session is not None
        return session

    @staticmethod
    def _validate(req: ExecRequest) -> None:
        if not req.argv:
            raise InvalidArgument("argv must be a non-empty list")
        if req.env:
            forbidden = FORBIDDEN_ENV_KEYS & set(req.env.keys())
            if forbidden:
                raise InvalidArgument(f"env keys forbidden by SPEC-201: {sorted(forbidden)}")

    @staticmethod
    def _effective_timeout(req: ExecRequest, session: SessionRow) -> int:
        requested = req.timeout_s if req.timeout_s is not None else session.limits.exec_timeout_s
        return max(1, min(requested, TENANT_MAX_TIMEOUT_S))

    @staticmethod
    def _encode_stdin(req: ExecRequest) -> bytes | None:
        if req.stdin is None:
            return None
        encoded = req.stdin.encode("utf-8")
        if len(encoded) > STDIN_INLINE_LIMIT_BYTES:
            raise InvalidArgument(
                f"stdin exceeds {STDIN_INLINE_LIMIT_BYTES // (1024 * 1024)} MiB "
                "inline limit (SPEC-201)"
            )
        return encoded
