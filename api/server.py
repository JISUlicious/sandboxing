"""FastAPI app for the sandbox control plane.

OpenAPI metadata (tags, summaries, declared error responses) lives at
the top of the module and is wired into each route decorator. The
schema is served at `/openapi.json`; Swagger UI at `/docs`; ReDoc at
`/redoc`.
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
    ErrorResponse,
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

# ----- OpenAPI metadata -----

API_DESCRIPTION = """
Control plane for an LLM-agent sandbox service. See
[SPECIFICATION.md](https://github.com/JISUlicious/sandboxing/blob/main/SPECIFICATION.md)
and
[ARCHITECTURE.md](https://github.com/JISUlicious/sandboxing/blob/main/ARCHITECTURE.md)
for the full contract.

Every session is a long-lived, hardened container. Filesystem state in
`/workspace` persists across exec calls and across stop/resume; process
state does not (SPEC-002). All endpoints under `/v1/sessions` require a
bearer token in the `Authorization` header.
"""

TAGS_METADATA = [
    {"name": "Sessions", "description": "Lifecycle: create, get, stop, resume, destroy."},
    {
        "name": "Exec",
        "description": "Run commands inside a session. Sync and Server-Sent-Events streaming.",
    },
    {"name": "Files", "description": "Read, write, list, and delete files under `/workspace`."},
    {"name": "Operations", "description": "Liveness, readiness, and Prometheus metrics."},
]

# Reusable response declarations. The keys are integer status codes;
# FastAPI mounts them under `responses=` on each route so the OpenAPI
# schema shows error shapes alongside the success path.
ERR_BAD_REQUEST = {
    400: {
        "model": ErrorResponse,
        "description": "Validation failure: `invalid_argument`, `invalid_path`, etc.",
    }
}
ERR_UNAUTHORIZED = {
    401: {
        "model": ErrorResponse,
        "description": "Missing or invalid bearer token.",
    }
}
ERR_NOT_FOUND_SESSION = {
    404: {
        "model": ErrorResponse,
        "description": (
            "Session not found, not owned by the calling tenant, or already DESTROYED (SPEC-200)."
        ),
    }
}
ERR_NOT_FOUND_FILE = {
    404: {
        "model": ErrorResponse,
        "description": "Session or file not found (`session_not_found` / `file_not_found`).",
    }
}
ERR_TIMEOUT = {
    408: {
        "model": ErrorResponse,
        "description": "Exec exceeded its wall-clock budget (`exec_timeout`). SPEC-201.",
    }
}
ERR_CONFLICT = {
    409: {
        "model": ErrorResponse,
        "description": "Session is in a state that doesn't allow this operation (`invalid_state`).",
    }
}
ERR_RATE_LIMIT = {
    429: {
        "model": ErrorResponse,
        "description": (
            "Tenant concurrency or per-field limit exceeded (`limit_exceeded`). SPEC §6."
        ),
    }
}


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

    app = FastAPI(
        title="Sandbox Service",
        version="0.1.0",
        description=API_DESCRIPTION,
        openapi_tags=TAGS_METADATA,
        lifespan=lifespan,
    )
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

    # ----- operations -----

    @app.get(
        "/healthz",
        tags=["Operations"],
        summary="Liveness probe",
        description=(
            "Returns 200 as long as the process is up. Does not check Docker — see `/readyz`."
        ),
    )
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get(
        "/readyz",
        tags=["Operations"],
        summary="Readiness probe",
        description=(
            "Returns `{docker: true}` once the daemon is reachable. Use as "
            "a load-balancer / orchestrator readiness check."
        ),
    )
    async def readyz() -> dict[str, bool]:
        return {"docker": service_.docker.health()}

    @app.get(
        "/metrics",
        tags=["Operations"],
        summary="Prometheus metrics",
        description=(
            "Prometheus text exposition (SPEC-503). No auth — bind to an "
            "internal port or restrict via reverse proxy in production."
        ),
        response_class=Response,
        responses={
            200: {
                "content": {"text/plain": {}},
                "description": "Metrics in Prometheus exposition format.",
            }
        },
    )
    async def metrics_endpoint() -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    # ----- sessions: lifecycle -----

    @app.post(
        "/v1/sessions",
        response_model=SessionResponse,
        status_code=201,
        tags=["Sessions"],
        summary="Create a session",
        description=(
            "Creates a long-lived sandbox session under the calling tenant. "
            "Starts in `RUNNING` with an empty `/workspace` (SPEC-101). "
            "`limits` are merged with tenant-default values; per-field caps "
            "are enforced (SPEC-100, SPEC-300)."
        ),
        responses={**ERR_UNAUTHORIZED, **ERR_RATE_LIMIT},
    )
    async def create_session(
        req: CreateSessionRequest, tenant: str = Depends(auth)
    ) -> SessionResponse:
        return _to_response(await service_.create(tenant, req.limits))

    @app.get(
        "/v1/sessions/{session_id}",
        response_model=SessionResponse,
        tags=["Sessions"],
        summary="Get a session's status and limits",
        responses={**ERR_UNAUTHORIZED, **ERR_NOT_FOUND_SESSION},
    )
    async def get_session(session_id: str, tenant: str = Depends(auth)) -> SessionResponse:
        return _to_response(await service_.get(session_id, tenant))

    @app.post(
        "/v1/sessions/{session_id}/stop",
        response_model=SessionResponse,
        tags=["Sessions"],
        summary="Idle-stop a session",
        description=(
            "Stops the underlying container; the volume and `/workspace` "
            "contents are retained (SPEC-104). Resume implicitly via "
            "`/exec` or any file op, or explicitly via `/resume`."
        ),
        responses={**ERR_UNAUTHORIZED, **ERR_NOT_FOUND_SESSION, **ERR_CONFLICT},
    )
    async def stop_session(session_id: str, tenant: str = Depends(auth)) -> SessionResponse:
        return _to_response(await service_.stop(session_id, tenant))

    @app.post(
        "/v1/sessions/{session_id}/resume",
        response_model=SessionResponse,
        tags=["Sessions"],
        summary="Resume a stopped session",
        description=(
            "Restarts the container for a `STOPPED` or `IDLE` session. "
            "Process state from before the stop is gone — only filesystem "
            "state persists (ARCH §3.4)."
        ),
        responses={**ERR_UNAUTHORIZED, **ERR_NOT_FOUND_SESSION, **ERR_CONFLICT},
    )
    async def resume_session(session_id: str, tenant: str = Depends(auth)) -> SessionResponse:
        return _to_response(await service_.resume(session_id, tenant))

    @app.delete(
        "/v1/sessions/{session_id}",
        status_code=204,
        tags=["Sessions"],
        summary="Destroy a session",
        description=(
            "Removes the container and its `/workspace` volume. Irreversible "
            "(SPEC-105). The destroy is multi-step (DESTROYING → DESTROYED) "
            "and idempotent on partial failure (ARCH-051)."
        ),
        responses={**ERR_UNAUTHORIZED, **ERR_NOT_FOUND_SESSION},
    )
    async def destroy_session(session_id: str, tenant: str = Depends(auth)) -> None:
        await service_.destroy(session_id, tenant)

    # ----- exec -----

    @app.post(
        "/v1/sessions/{session_id}/exec",
        response_model=ExecResponse,
        tags=["Exec"],
        summary="Run a command (synchronous)",
        description=(
            "Runs `argv` inside the session and returns stdout/stderr/exit "
            "after the process exits or `timeout_s` is hit. Output is "
            "capped at 8 MiB per stream independently (SPEC-203); "
            "`truncated_streams` lists any that hit the cap. Stdin must be "
            "UTF-8 ≤ 1 MiB; binary stdin is a future enhancement.\n\n"
            "STOPPED / IDLE sessions are transparently resumed before the "
            "exec runs (SPEC-104). `HTTP_PROXY`, `HTTPS_PROXY`, and "
            "`NO_PROXY` are forbidden in `env` (SPEC-201)."
        ),
        responses={
            **ERR_UNAUTHORIZED,
            **ERR_NOT_FOUND_SESSION,
            **ERR_BAD_REQUEST,
            **ERR_TIMEOUT,
            **ERR_CONFLICT,
        },
    )
    async def exec_session(
        session_id: str, req: ExecRequest, tenant: str = Depends(auth)
    ) -> ExecResponse:
        return await exec_service_.run(session_id, tenant, req)

    @app.post(
        "/v1/sessions/{session_id}/exec/stream",
        tags=["Exec"],
        summary="Run a command (Server-Sent Events streaming)",
        description=(
            "Same contract as `/exec` but streams output live as SSE events.\n\n"
            "**Event types** (each is an SSE frame with `event:` + `data:`):\n"
            '- `event: stdout` / `event: stderr` — `{"chunk_b64": "<base64>"}`. '
            "Decoded chunks reconstruct the original byte stream in order. "
            "Once the per-stream 8 MiB cap is hit, no further chunks of that "
            "stream are emitted.\n"
            '- `event: truncated` — `{"stream": "stdout"|"stderr"}`. '
            "Emitted exactly once per affected stream when the cap is "
            "reached.\n"
            "- `event: result` — final ExecResponse-shaped payload "
            "(matches the synchronous `/exec` response, SPEC-201/202).\n"
            '- `event: error` — `{"message": "..."}`. The producer '
            "thread crashed; the client should treat this as a 5xx.\n\n"
            "Stdin is rejected with 400 — combining stdin with live demuxed "
            "streaming is a future slice. OpenAPI tooling does not generate "
            "useful clients for SSE; consume with a streaming HTTP client."
        ),
        response_class=StreamingResponse,
        responses={
            200: {
                "content": {"text/event-stream": {}},
                "description": "SSE stream of stdout/stderr/truncated/result events.",
            },
            **ERR_UNAUTHORIZED,
            **ERR_NOT_FOUND_SESSION,
            **ERR_BAD_REQUEST,
            **ERR_CONFLICT,
        },
    )
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

    # ----- files -----

    @app.post(
        "/v1/sessions/{session_id}/files",
        status_code=201,
        tags=["Files"],
        summary="Write a file",
        description=(
            "Writes a single file into `/workspace`. `content_b64` is "
            "base64-encoded for binary safety. Path is validated against "
            "traversal (`..`, absolute paths, NUL bytes) per SPEC-107. "
            "Parent directories are created as needed. STOPPED / IDLE "
            "sessions are transparently resumed (ARCH §3.3)."
        ),
        responses={
            201: {
                "description": "File written.",
                "content": {
                    "application/json": {
                        "example": {
                            "path": "/workspace/notes.txt",
                            "size": 14,
                            "mode": 420,
                        }
                    }
                },
            },
            **ERR_UNAUTHORIZED,
            **ERR_NOT_FOUND_SESSION,
            **ERR_BAD_REQUEST,
        },
    )
    async def write_file(
        session_id: str, req: FileWriteRequest, tenant: str = Depends(auth)
    ) -> dict[str, object]:
        return await file_service_.write(session_id, tenant, req)

    @app.get(
        "/v1/sessions/{session_id}/files",
        response_model=FileListResponse,
        tags=["Files"],
        summary="List files in a directory",
        description=(
            "Lists immediate children of `dir` (relative to `/workspace`; "
            "default is `/workspace` itself)."
        ),
        responses={**ERR_UNAUTHORIZED, **ERR_NOT_FOUND_FILE},
    )
    async def list_files(
        session_id: str,
        dir: str = Query(
            default="", description="Path relative to `/workspace`. Empty = list `/workspace`."
        ),
        tenant: str = Depends(auth),
    ) -> FileListResponse:
        return await file_service_.list_dir(session_id, tenant, dir)

    @app.get(
        "/v1/sessions/{session_id}/files/{path:path}",
        tags=["Files"],
        summary="Read a file",
        description=(
            "Returns the file's raw bytes as `application/octet-stream`. "
            "The unix mode is exposed in the `X-File-Mode` response header. "
            "Reading a directory returns 400; missing path returns 404."
        ),
        response_class=Response,
        responses={
            200: {
                "description": "Raw file bytes.",
                "content": {"application/octet-stream": {}},
                "headers": {
                    "X-File-Mode": {
                        "description": "Octal file mode (e.g., `0o640`).",
                        "schema": {"type": "string"},
                    }
                },
            },
            **ERR_UNAUTHORIZED,
            **ERR_NOT_FOUND_FILE,
            **ERR_BAD_REQUEST,
        },
    )
    async def read_file(session_id: str, path: str, tenant: str = Depends(auth)) -> Response:
        content, mode = await file_service_.read(session_id, tenant, path)
        # Raw bytes; clients that want JSON can base64 the result themselves.
        return Response(
            content=content,
            media_type="application/octet-stream",
            headers={"X-File-Mode": oct(mode)},
        )

    @app.delete(
        "/v1/sessions/{session_id}/files/{path:path}",
        status_code=204,
        tags=["Files"],
        summary="Delete a file or directory",
        description=(
            "Deletes a file under `/workspace`. Directories require "
            "`?recursive=true`; deleting `/workspace` itself is rejected "
            "(SPEC-107). Missing paths return 404."
        ),
        responses={
            **ERR_UNAUTHORIZED,
            **ERR_NOT_FOUND_FILE,
            **ERR_BAD_REQUEST,
        },
    )
    async def delete_file(
        session_id: str,
        path: str,
        recursive: bool = Query(
            default=False, description="Required to delete a non-empty directory."
        ),
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
