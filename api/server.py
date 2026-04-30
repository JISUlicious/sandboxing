"""FastAPI app for the sandbox control plane.

Slice 1 endpoints: session lifecycle (create/get/stop/resume/destroy),
plus health and readiness probes.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header

from api.audit import AuditEmitter
from api.config import Settings, settings
from api.docker_client import DockerClient
from api.errors import Unauthorized
from api.models import CreateSessionRequest, SessionResponse
from api.registry import Registry, SessionRow
from api.sessions import SessionService

logging.basicConfig(
    level=logging.INFO,
    format='{"ts":"%(asctime)s","lvl":"%(levelname)s","logger":"%(name)s","msg":"%(message)s"}',
)
log = logging.getLogger("sandbox")


def _build_service(s: Settings) -> SessionService:
    return SessionService(
        settings=s,
        registry=Registry(s.db_path),
        docker=DockerClient(s),
        audit=AuditEmitter(s.audit_log_path),
    )


def _to_response(row: SessionRow) -> SessionResponse:
    return SessionResponse(
        session_id=row.id,
        status=row.status,
        tenant_id=row.tenant_id,
        limits=row.limits,
        created_at=row.created_at,
        last_activity_at=row.last_activity_at,
    )


def create_app(
    s: Settings | None = None,
    *,
    service: SessionService | None = None,
) -> FastAPI:
    settings_ = s or settings
    service_ = service or _build_service(settings_)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        # SPEC-302: refuse non-loopback bind in dev mode.
        if settings_.dev_mode and settings_.bind_host not in ("127.0.0.1", "localhost", "::1"):
            raise RuntimeError("dev mode forbids non-loopback bind (SPEC-302)")
        if not settings_.api_token:
            raise RuntimeError("SANDBOX_API_TOKEN must be set")
        await service_.registry.init()
        # SPEC-400 (with SPEC-302 dev-mode bypass).
        service_.docker.ensure_runtime()
        if service_.docker.health():
            service_.docker.ensure_network()
        else:
            log.warning("docker daemon not reachable; lifecycle calls will fail")
        yield

    app = FastAPI(title="Sandbox Service", version="0.1.0", lifespan=lifespan)
    app.state.service = service_
    app.state.settings = settings_

    def auth(authorization: str | None = Header(default=None)) -> str:
        if not authorization or not authorization.startswith("Bearer "):
            raise Unauthorized()
        token = authorization.removeprefix("Bearer ").strip()
        if token != settings_.api_token:
            raise Unauthorized()
        # Slice 1 is single-tenant; multi-tenant token store is slice 4+.
        return "default"

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz() -> dict[str, bool]:
        return {"docker": service_.docker.health()}

    @app.post("/v1/sessions", response_model=SessionResponse, status_code=201)
    async def create_session(
        req: CreateSessionRequest, tenant: str = Depends(auth)
    ) -> SessionResponse:
        return _to_response(await service_.create(tenant, req.limits))

    @app.get("/v1/sessions/{session_id}", response_model=SessionResponse)
    async def get_session(session_id: str, tenant: str = Depends(auth)) -> SessionResponse:
        return _to_response(await service_.get(session_id, tenant))

    @app.post("/v1/sessions/{session_id}/stop", response_model=SessionResponse)
    async def stop_session(session_id: str, tenant: str = Depends(auth)) -> SessionResponse:
        return _to_response(await service_.stop(session_id, tenant))

    @app.post("/v1/sessions/{session_id}/resume", response_model=SessionResponse)
    async def resume_session(session_id: str, tenant: str = Depends(auth)) -> SessionResponse:
        return _to_response(await service_.resume(session_id, tenant))

    @app.delete("/v1/sessions/{session_id}", status_code=204)
    async def destroy_session(session_id: str, tenant: str = Depends(auth)) -> None:
        await service_.destroy(session_id, tenant)

    return app


app = create_app()


def main() -> None:
    import uvicorn

    uvicorn.run(
        "api.server:app",
        host=settings.bind_host,
        port=settings.bind_port,
        reload=False,
    )


if __name__ == "__main__":
    main()
