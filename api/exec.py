"""Exec dispatch (slice 2). SPEC-201, SPEC-202, SPEC-203, SPEC-301."""

from __future__ import annotations

import asyncio
import logging

from api.audit import AuditEmitter
from api.docker_client import TIMEOUT_EXIT_CODE, DockerClient
from api.errors import ExecTimeout, InvalidArgument, InvalidState, SessionNotFound
from api.models import ExecRequest, ExecResponse
from api.registry import Registry, SessionRow

log = logging.getLogger("sandbox.exec")

# SPEC-201: env keys we own and must not be overridden.
FORBIDDEN_ENV_KEYS = frozenset({"HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY"})

# SPEC §6 tenant max for exec timeout.
TENANT_MAX_TIMEOUT_S = 600


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

    async def run(self, session_id: str, tenant_id: str, req: ExecRequest) -> ExecResponse:
        session = await self.registry.get(session_id, tenant_id)
        if session is None:
            raise SessionNotFound()

        self._validate(req)

        if session.status not in ("RUNNING", "IDLE", "STOPPED"):
            raise InvalidState(f"cannot exec on session in status {session.status}")
        if session.status in ("STOPPED", "IDLE"):
            # Transparent resume per SPEC-104 / ARCH §3.2 step 3.
            # Resume latency is tracked separately (SPEC-504); not folded into
            # the exec response's duration_ms.
            assert session.container_id is not None
            await asyncio.to_thread(self.docker.start_container, session.container_id)
            await self.registry.transition(session_id, "RUNNING")
            session = await self.registry.get(session_id, tenant_id)
            assert session is not None

        timeout_s = self._effective_timeout(req, session)
        env = req.env or {}
        assert session.container_id is not None
        out = await asyncio.to_thread(
            self.docker.exec_in_container,
            container_id=session.container_id,
            argv=req.argv,
            env=env,
            timeout_s=timeout_s,
        )

        await self.audit.emit(
            kind="session.exec",
            tenant=tenant_id,
            session=session_id,
            payload={
                "argv": req.argv,
                "env_keys": sorted((req.env or {}).keys()),
                "timeout_s": timeout_s,
            },
            result="timeout" if out.exit_code == TIMEOUT_EXIT_CODE else "ok",
            duration_ms=out.duration_ms,
        )

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

    @staticmethod
    def _validate(req: ExecRequest) -> None:
        if not req.argv:
            raise InvalidArgument("argv must be a non-empty list")
        # Slice 2 omits stdin support; the lower-level socket plumbing lands
        # alongside the SSE streaming endpoint in slice 3.
        if req.stdin is not None:
            raise InvalidArgument("stdin is not yet supported (slice 3)")
        if req.env:
            forbidden = FORBIDDEN_ENV_KEYS & set(req.env.keys())
            if forbidden:
                raise InvalidArgument(f"env keys forbidden by SPEC-201: {sorted(forbidden)}")

    @staticmethod
    def _effective_timeout(req: ExecRequest, session: SessionRow) -> int:
        requested = req.timeout_s if req.timeout_s is not None else session.limits.exec_timeout_s
        return max(1, min(requested, TENANT_MAX_TIMEOUT_S))
