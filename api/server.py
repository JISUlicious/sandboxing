"""FastAPI app for the sandbox control plane.

Slice 1 endpoints: session lifecycle (create/get/stop/resume/destroy),
plus health and readiness probes.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, Query, Request, Response
from fastapi.responses import StreamingResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from api import metrics
from api.audit import AuditEmitter
from api.config import Settings, settings
from api.docker_client import DockerClient
from api.errors import Unauthorized
from api.exec import ExecService
from api.files import FileService
from api.models import (
    CreateSessionRequest,
    ExecRequest,
    ExecResponse,
    FileListResponse,
    FileWriteRequest,
    SessionResponse,
)
from api.reaper import Reaper
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
    start_reaper: bool = True,
) -> FastAPI:
    settings_ = s or settings
    service_ = service or _build_service(settings_)
    # Slice 2: exec + files share the same registry / docker / audit.
    exec_service_ = ExecService(
        registry=service_.registry, docker=service_.docker, audit=service_.audit
    )
    file_service_ = FileService(
        registry=service_.registry, docker=service_.docker, audit=service_.audit
    )
    reaper_ = Reaper(settings=settings_, registry=service_.registry, sessions=service_)

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
        if start_reaper:
            await reaper_.start()
        try:
            yield
        finally:
            await reaper_.stop()

    app = FastAPI(title="Sandbox Service", version="0.1.0", lifespan=lifespan)
    app.state.service = service_
    app.state.settings = settings_
    app.state.reaper = reaper_

    @app.middleware("http")
    async def metrics_middleware(request: Request, call_next):
        start = time.monotonic()
        response = await call_next(request)
        # Use the templated route path to avoid label cardinality blowup
        # from session_id/path segments. /metrics itself is excluded so
        # scrapes don't poison the histogram.
        route = request.scope.get("route")
        path = getattr(route, "path", request.url.path) if route else request.url.path
        if path != "/metrics":
            metrics.api_requests_total.labels(
                method=request.method, path=path, status=response.status_code
            ).inc()
            metrics.api_request_duration_seconds.labels(method=request.method, path=path).observe(
                time.monotonic() - start
            )
        return response

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

    @app.get("/metrics")
    async def metrics_endpoint() -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

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

    # ----- exec (slice 2) -----

    @app.post("/v1/sessions/{session_id}/exec", response_model=ExecResponse)
    async def exec_session(
        session_id: str, req: ExecRequest, tenant: str = Depends(auth)
    ) -> ExecResponse:
        return await exec_service_.run(session_id, tenant, req)

    @app.post("/v1/sessions/{session_id}/exec/stream")
    async def exec_session_stream(
        session_id: str, req: ExecRequest, tenant: str = Depends(auth)
    ) -> StreamingResponse:
        # Pre-flight validation runs synchronously so a bad request gets
        # a clean 400 instead of being buried inside a half-flushed SSE.
        exec_service_.validate_stream_request(req)

        async def sse() -> AsyncIterator[bytes]:
            async for event_type, payload in exec_service_.run_stream(session_id, tenant, req):
                data = json.dumps(payload, separators=(",", ":"))
                yield f"event: {event_type}\ndata: {data}\n\n".encode()

        return StreamingResponse(
            sse(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # ----- files (slice 2) -----

    @app.post("/v1/sessions/{session_id}/files", status_code=201)
    async def write_file(
        session_id: str, req: FileWriteRequest, tenant: str = Depends(auth)
    ) -> dict[str, object]:
        return await file_service_.write(session_id, tenant, req)

    @app.get("/v1/sessions/{session_id}/files", response_model=FileListResponse)
    async def list_files(
        session_id: str,
        dir: str = Query(default=""),
        tenant: str = Depends(auth),
    ) -> FileListResponse:
        return await file_service_.list_dir(session_id, tenant, dir)

    @app.get("/v1/sessions/{session_id}/files/{path:path}")
    async def read_file(session_id: str, path: str, tenant: str = Depends(auth)) -> Response:
        content, mode = await file_service_.read(session_id, tenant, path)
        # Return raw bytes so callers can handle binary content; clients
        # that want JSON can base64 the result themselves.
        return Response(
            content=content,
            media_type="application/octet-stream",
            headers={"X-File-Mode": oct(mode)},
        )

    @app.delete("/v1/sessions/{session_id}/files/{path:path}", status_code=204)
    async def delete_file(
        session_id: str,
        path: str,
        recursive: bool = Query(default=False),
        tenant: str = Depends(auth),
    ) -> None:
        await file_service_.delete(session_id, tenant, path, recursive=recursive)

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
