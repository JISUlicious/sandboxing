"""FastAPI app for the sandbox control plane.

OpenAPI metadata (tags, summaries, declared error responses) lives at
the top of the module and is wired into each route decorator. The
schema is served at `/openapi.json`; Swagger UI at `/docs`; ReDoc at
`/redoc`.
"""

from __future__ import annotations

import asyncio
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
from api.auth import TokenAuthenticator, bootstrap_default_tenant
from api.config import Settings, settings
from api.docker_client import DockerClient
from api.errors import InvalidArgument, Unauthorized
from api.exec import ExecService
from api.files import FileService
from api.idempotency import IdempotencyMiddleware
from api.mcp_server import attach_to_fastapi as mcp_attach
from api.mcp_server import build_mcp, mcp_lifespan_context
from api.models import (
    CreateSessionRequest,
    CreateTenantRequest,
    DeleteTenantResponse,
    ErrorResponse,
    ExecRequest,
    ExecResponse,
    FileListResponse,
    FileWriteRequest,
    IssueTokenRequest,
    IssueTokenResponse,
    ProcessListResponse,
    ProcessResponse,
    RotateTokenResponse,
    SessionResponse,
    StartProcessRequest,
    TenantLimits,
    TenantListResponse,
    TenantResponse,
    TenantUsageResponse,
    UpdateTenantRequest,
)
from api.processes import ProcessService
from api.reaper import Reaper
from api.registry import Registry, SessionRow
from api.sampler import SessionSampler
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
    {
        "name": "Processes",
        "description": (
            "Start / stop / inspect long-running background processes inside a "
            "session. Slice 11b — survives across exec calls (SPEC-...)."
        ),
    },
    {"name": "Tenants", "description": "Token rotation and other tenant-scoped admin."},
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
        audit=AuditEmitter(
            s.audit_log_path,
            fallback_path=s.audit_fallback_log_path,
            buffer_timeout_s=s.audit_buffer_timeout_s,
        ),
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
    sampler_ = SessionSampler(
        settings=settings_,
        registry=service_.registry,
        docker=service_.docker,
        audit=service_.audit,
    )
    authn_ = TokenAuthenticator(settings=settings_, registry=service_.registry)
    process_service_ = ProcessService(
        settings=settings_,
        registry=service_.registry,
        docker=service_.docker,
        audit=service_.audit,
        sessions=service_,
    )
    # Slice 11b: SessionService.destroy reaps background processes
    # before container removal via this hook (avoids a circular
    # SessionService ↔ ProcessService dep).
    service_.set_destroy_hook(process_service_.reap_session_processes)
    mcp_ = build_mcp(
        sessions=service_,
        exec_service=exec_service_,
        file_service=file_service_,
        process_service=process_service_,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        # SPEC-302: refuse non-loopback bind in dev mode.
        if settings_.dev_mode and settings_.bind_host not in ("127.0.0.1", "localhost", "::1"):
            raise RuntimeError("dev mode forbids non-loopback bind (SPEC-302)")
        if not settings_.api_token:
            raise RuntimeError("SANDBOX_API_TOKEN must be set")
        # SPEC-401: warn loudly when bind volumes fall back to 0777 in
        # production. Dev mode opts out (no quota_volume_base means no
        # bind, no chmod path).
        if (
            not settings_.dev_mode
            and str(settings_.quota_volume_base)
            and settings_.bind_volume_uid is None
        ):
            log.warning(
                "SANDBOX_BIND_VOLUME_UID not set; per-session volumes will "
                "fall back to mode 0777 (SPEC-401 footgun). Compute via: "
                "awk -F: '$1==\"dockremap\"{print $2 + 10000}' /etc/subuid"
            )
        await service_.registry.init()
        # Bootstrap the default tenant from settings.api_token if no
        # tenants exist yet (transparent upgrade for single-token
        # deployments).
        await bootstrap_default_tenant(settings=settings_, registry=service_.registry, auth=authn_)
        # SPEC-400 (with SPEC-302 dev-mode bypass).
        service_.docker.ensure_runtime()
        if service_.docker.health():
            service_.docker.ensure_network()
            # ARCH-051 reconciliation: heal orphaned rows from any prior
            # crash before serving traffic so the next exec doesn't
            # surface docker NotFound.
            try:
                await service_.reconcile_on_startup()
            except Exception:
                log.exception("startup reconciliation failed; continuing anyway")
        else:
            log.warning("docker daemon not reachable; lifecycle calls will fail")
        if start_reaper:
            await reaper_.start()
            await sampler_.start()
        # Compose the FastMCP session manager into our lifespan so the
        # mounted /mcp endpoint is live for the duration of the app.
        async with mcp_lifespan_context(mcp_):
            try:
                yield
            finally:
                await sampler_.stop()
                await reaper_.stop()

    app = FastAPI(
        title="Sandbox Service",
        # Track the release tag. Bump on every spec-affecting change so
        # consumers pinned against an older version can detect drift.
        version="0.1.6",
        description=API_DESCRIPTION,
        openapi_tags=TAGS_METADATA,
        lifespan=lifespan,
    )
    app.state.service = service_
    app.state.settings = settings_
    app.state.reaper = reaper_
    app.state.sampler = sampler_
    app.state.mcp = mcp_
    app.state.processes = process_service_

    # Polish the OpenAPI schema FastAPI generates so it aligns with
    # what the running app actually does: declare the Idempotency-Key
    # header (slice 11a) + Idempotent-Replay response header, declare
    # bearerAuth + per-route security, fix the requestBody on the
    # raw-body file POST, declare a servers placeholder. None of this
    # changes runtime behaviour — purely contract documentation.
    _install_openapi_polish(app)

    # Slice 8e: TLS-readiness — when running behind a reverse proxy
    # that terminates TLS (Caddy / nginx in deploy/tls/*.example),
    # honor X-Forwarded-{Proto,For,Host}. Off by default so direct
    # callers can't spoof those headers.
    if settings_.trust_proxy_headers:
        from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

        app.add_middleware(ProxyHeadersMiddleware, trusted_hosts="*")

    # Translate docker-py engine errors into structured 503 responses.
    # Without these handlers, ImageNotFound / APIError surface as plain-
    # text "500 Internal Server Error" with no parseable body — the
    # consumer team's e2e flagged this when the runtime image hadn't
    # been pulled. 503 (vs 500) reflects "the daemon refused us, the
    # request itself is fine — retry once the image is pulled / the
    # daemon is healthy".
    import docker.errors as _docker_errors
    from fastapi.responses import JSONResponse as _JSONResponse

    @app.exception_handler(_docker_errors.ImageNotFound)
    async def _image_not_found_handler(_request: Request, exc: _docker_errors.ImageNotFound):
        log.warning("docker ImageNotFound: %s", exc)
        return _JSONResponse(
            status_code=503,
            content={
                "detail": {
                    "code": "image_not_found",
                    "message": str(exc).splitlines()[0] if str(exc) else "image not found",
                }
            },
        )

    # Order matters: ImageNotFound is a subclass of APIError, so its
    # handler must be registered first. Starlette dispatches the most-
    # specific match.
    @app.exception_handler(_docker_errors.APIError)
    async def _docker_api_error_handler(_request: Request, exc: _docker_errors.APIError):
        log.warning("docker APIError: %s", exc)
        return _JSONResponse(
            status_code=503,
            content={
                "detail": {
                    "code": "docker_api_error",
                    "message": str(exc).splitlines()[0] if str(exc) else "docker daemon error",
                }
            },
        )

    # Slice 11a: Idempotency-Key replay cache for mutating routes.
    # Registered AFTER ProxyHeadersMiddleware so the resolved client
    # IP is correct for any future per-IP caching, and BEFORE the
    # routing-level auth dependency so the middleware can re-resolve
    # the bearer to a tenant_id (cache scope is per-tenant).
    app.add_middleware(
        IdempotencyMiddleware,
        settings=settings_,
        registry=service_.registry,
        authenticate=authn_.authenticate,
    )

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

    async def auth(authorization: str | None = Header(default=None)) -> str:
        if not authorization or not authorization.startswith("Bearer "):
            raise Unauthorized()
        token = authorization.removeprefix("Bearer ").strip()
        return await authn_.authenticate(token)

    async def auth_full(authorization: str | None = Header(default=None)):
        """Slice 12: scope-aware auth dependency. Returns AuthContext."""
        if not authorization or not authorization.startswith("Bearer "):
            raise Unauthorized()
        token = authorization.removeprefix("Bearer ").strip()
        return await authn_.authenticate_full(token)

    def require_scope(scope: str):
        """Dependency factory: a route declares
        `Depends(require_scope('exec'))`. Raises 403 forbidden_scope
        when the bearer's scopes don't include `scope`. Admin tokens
        and tokens with `scopes is None` (back-compat) pass."""
        from api.auth import has_scope as _has_scope

        async def _dep(authorization: str | None = Header(default=None)) -> str:
            ctx = await auth_full(authorization)
            if not _has_scope(ctx, scope):
                from api.auth import ForbiddenScope

                raise ForbiddenScope(scope)
            return ctx.tenant_id

        return _dep

    async def require_admin(authorization: str | None = Header(default=None)) -> str:
        """Slice 12: admin-only routes. Returns the admin tenant_id."""
        if not settings_.admin_token:
            from fastapi import HTTPException

            raise HTTPException(
                status_code=503,
                detail={
                    "code": "admin_disabled",
                    "message": (
                        "SANDBOX_ADMIN_TOKEN is not set; admin endpoints are "
                        "disabled. Set the env var and restart to enable."
                    ),
                },
            )
        ctx = await auth_full(authorization)
        if not ctx.is_admin:
            raise Unauthorized()
        return ctx.tenant_id

    async def auth_with_token_id(
        authorization: str | None = Header(default=None),
    ) -> tuple[str, str]:
        """Same as `auth` but also returns the matched token_id, used
        by the rotation endpoint so it can revoke the caller's
        current token without a second lookup."""
        if not authorization or not authorization.startswith("Bearer "):
            raise Unauthorized()
        token = authorization.removeprefix("Bearer ").strip()
        from api.auth import hash_token

        digest = hash_token(token, settings_.token_pepper)
        row = await service_.registry.lookup_token(digest)
        if row is None:
            raise Unauthorized()
        token_id, tenant_id, revoked_at, _scopes = row
        if revoked_at is not None:
            import time as _time

            if revoked_at <= int(_time.time() * 1000):
                raise Unauthorized()
        return tenant_id, token_id

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
            "Returns the health of subsystems the API needs to serve "
            "mutations: `docker` for the daemon, `audit` for the "
            "fail-closed audit log (ARCH §7). All-true means the service "
            "will accept new lifecycle / exec / file calls."
        ),
    )
    async def readyz() -> dict[str, bool]:
        return {
            "docker": service_.docker.health(),
            "audit": service_.audit.is_healthy,
        }

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
        req: CreateSessionRequest, tenant: str = Depends(require_scope("session_create"))
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
    async def destroy_session(
        session_id: str, tenant: str = Depends(require_scope("session_destroy"))
    ) -> None:
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
        session_id: str, req: ExecRequest, tenant: str = Depends(require_scope("exec"))
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
        session_id: str, req: ExecRequest, tenant: str = Depends(require_scope("exec"))
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
        session_id: str,
        req: FileWriteRequest,
        tenant: str = Depends(require_scope("file_write")),
    ) -> dict[str, object]:
        return await file_service_.write(session_id, tenant, req)

    @app.post(
        "/v1/sessions/{session_id}/files/{path:path}",
        status_code=201,
        tags=["Files"],
        summary="Write a file (path-in-URL, raw body)",
        description=(
            "Writes a single file into `/workspace`. Mirrors the "
            "`GET /files/{path}` and `DELETE /files/{path}` shapes: "
            "the path is in the URL and the body is the raw file "
            "content (`application/octet-stream`). Use this when the "
            "caller has bytes-in-hand — for base64-encoded JSON, see "
            "the collection-level `POST /files`. Path validation, "
            "parent-dir creation, and STOPPED-session resume are "
            "identical to the JSON variant."
        ),
        responses={
            201: {
                "description": "File written.",
                "content": {
                    "application/json": {
                        "example": {
                            "path": "/workspace/sub/notes.txt",
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
    async def write_file_raw(
        session_id: str,
        path: str,
        request: Request,
        mode: int = Query(default=0o640, ge=0, le=0o777),
        tenant: str = Depends(require_scope("file_write")),
    ) -> dict[str, object]:
        body = await request.body()
        return await file_service_.write_raw(session_id, tenant, path, body, mode)

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
        tenant: str = Depends(require_scope("file_read")),
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
    async def read_file(
        session_id: str, path: str, tenant: str = Depends(require_scope("file_read"))
    ) -> Response:
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
        tenant: str = Depends(require_scope("file_delete")),
    ) -> None:
        await file_service_.delete(session_id, tenant, path, recursive=recursive)

    # ----- tenants: token rotation (slice 7) -----

    @app.post(
        "/v1/tenants/me/tokens/rotate",
        response_model=RotateTokenResponse,
        tags=["Tenants"],
        summary="Rotate the calling tenant's bearer token",
        description=(
            "Issues a new bearer token for the calling tenant and "
            "marks the current token revoked-at = now + grace seconds. "
            "Both tokens authenticate during the grace window so "
            "callers can read the response and switch over without "
            "downtime. After the window the old token returns 401. "
            "SPEC-405."
        ),
        responses={**ERR_UNAUTHORIZED},
    )
    async def rotate_token(
        auth_pair: tuple[str, str] = Depends(auth_with_token_id),
    ) -> RotateTokenResponse:
        tenant_id, current_token_id = auth_pair
        new_plaintext, grace = await authn_.rotate(tenant_id, current_token_id)
        return RotateTokenResponse(
            token=new_plaintext,
            old_token_grace_seconds=grace,
            tenant_id=tenant_id,
        )

    # ----- tenant management API (slice 12, admin-scoped) -----

    def _tenant_dict_to_response(tenant: dict) -> TenantResponse:
        return TenantResponse(
            tenant_id=tenant["id"],
            display_name=tenant["display_name"],
            created_at=tenant["created_at"],
            limits=TenantLimits(
                max_concurrency=tenant.get("max_concurrency"),
                max_workspace_gib=tenant.get("max_workspace_gib"),
                max_exec_timeout_s=tenant.get("max_exec_timeout_s"),
            ),
            egress_allowlist=tenant.get("egress_allowlist"),
            active_token_count=tenant.get("active_token_count", 0),
        )

    @app.post(
        "/v1/tenants",
        response_model=TenantResponse,
        status_code=201,
        tags=["Tenants"],
        summary="Create a tenant (admin)",
        responses={**ERR_BAD_REQUEST, **ERR_UNAUTHORIZED},
    )
    async def create_tenant(
        req: CreateTenantRequest,
        _admin: str = Depends(require_admin),
    ) -> TenantResponse:
        tid = req.name
        existing = await service_.registry.get_tenant_full(tid)
        if existing is not None:
            raise InvalidArgument(f"tenant '{tid}' already exists")
        await service_.registry.create_tenant(
            tid,
            req.display_name or tid,
            max_concurrency=req.limits.max_concurrency if req.limits else None,
            max_workspace_gib=req.limits.max_workspace_gib if req.limits else None,
            max_exec_timeout_s=req.limits.max_exec_timeout_s if req.limits else None,
            egress_allowlist=req.egress_allowlist,
        )
        tenant = await service_.registry.get_tenant_full(tid)
        assert tenant is not None
        tenant["active_token_count"] = 0
        await service_.audit.emit(
            kind="tenant.create", tenant=tid, session=None, payload={"by": "admin"}
        )
        return _tenant_dict_to_response(tenant)

    @app.get(
        "/v1/tenants",
        response_model=TenantListResponse,
        tags=["Tenants"],
        summary="List tenants (admin)",
        responses={**ERR_UNAUTHORIZED},
    )
    async def list_tenants(
        _admin: str = Depends(require_admin),
    ) -> TenantListResponse:
        rows = await service_.registry.list_tenants_full()
        entries: list[TenantResponse] = []
        for row in rows:
            row["active_token_count"] = await service_.registry.count_active_tokens(row["id"])
            entries.append(_tenant_dict_to_response(row))
        return TenantListResponse(entries=entries)

    @app.get(
        "/v1/tenants/{tenant_id}",
        response_model=TenantResponse,
        tags=["Tenants"],
        summary="Get a tenant (admin)",
        responses={**ERR_UNAUTHORIZED},
    )
    async def get_tenant(
        tenant_id: str,
        _admin: str = Depends(require_admin),
    ) -> TenantResponse:
        tenant = await service_.registry.get_tenant_full(tenant_id)
        if tenant is None:
            from fastapi import HTTPException

            raise HTTPException(
                status_code=404,
                detail={"code": "tenant_not_found", "message": tenant_id},
            )
        tenant["active_token_count"] = await service_.registry.count_active_tokens(tenant_id)
        return _tenant_dict_to_response(tenant)

    @app.patch(
        "/v1/tenants/{tenant_id}",
        response_model=TenantResponse,
        tags=["Tenants"],
        summary="Update a tenant's limits / display_name / allowlist (admin)",
        responses={**ERR_UNAUTHORIZED, **ERR_BAD_REQUEST},
    )
    async def update_tenant(
        tenant_id: str,
        req: UpdateTenantRequest,
        _admin: str = Depends(require_admin),
    ) -> TenantResponse:
        tenant = await service_.registry.get_tenant_full(tenant_id)
        if tenant is None:
            from fastapi import HTTPException

            raise HTTPException(
                status_code=404,
                detail={"code": "tenant_not_found", "message": tenant_id},
            )
        kw: dict = {}
        if req.display_name is not None:
            kw["display_name"] = req.display_name
        if req.limits is not None:
            kw["max_concurrency"] = req.limits.max_concurrency
            kw["max_workspace_gib"] = req.limits.max_workspace_gib
            kw["max_exec_timeout_s"] = req.limits.max_exec_timeout_s
        if req.egress_allowlist is not None:
            kw["egress_allowlist"] = req.egress_allowlist
        await service_.registry.update_tenant(tenant_id, **kw)
        tenant = await service_.registry.get_tenant_full(tenant_id)
        assert tenant is not None
        tenant["active_token_count"] = await service_.registry.count_active_tokens(tenant_id)
        return _tenant_dict_to_response(tenant)

    @app.delete(
        "/v1/tenants/{tenant_id}",
        response_model=DeleteTenantResponse,
        tags=["Tenants"],
        summary="Delete a tenant (admin)",
        description=(
            "Revokes ALL tokens for the tenant immediately, marks all "
            "non-terminal sessions DESTROYING (the reaper will finish "
            "them on its next tick), and drops the tenants row. "
            "Irreversible."
        ),
        responses={**ERR_UNAUTHORIZED},
    )
    async def delete_tenant(
        tenant_id: str,
        _admin: str = Depends(require_admin),
    ) -> DeleteTenantResponse:
        tenant = await service_.registry.get_tenant_full(tenant_id)
        if tenant is None:
            from fastapi import HTTPException

            raise HTTPException(
                status_code=404,
                detail={"code": "tenant_not_found", "message": tenant_id},
            )
        revoked = await service_.registry.revoke_all_tenant_tokens(tenant_id)
        # Schedule existing non-terminal sessions for destroy on the
        # next reaper tick rather than blocking the request.
        non_terminal = await service_.registry.list_non_terminal()
        sessions_destroyed = 0
        for s in non_terminal:
            if s.tenant_id != tenant_id:
                continue
            try:
                await service_.reap_destroy(s, reason="tenant_deleted")
                sessions_destroyed += 1
            except Exception:
                log.exception("reap_destroy failed for %s", s.id)
        await service_.registry.delete_tenant(tenant_id)
        await service_.audit.emit(
            kind="tenant.delete",
            tenant=tenant_id,
            session=None,
            payload={"sessions_destroyed": sessions_destroyed, "tokens_revoked": revoked},
        )
        return DeleteTenantResponse(
            tenant_id=tenant_id,
            sessions_destroyed=sessions_destroyed,
            tokens_revoked=revoked,
        )

    @app.post(
        "/v1/tenants/{tenant_id}/tokens",
        response_model=IssueTokenResponse,
        status_code=201,
        tags=["Tenants"],
        summary="Issue a token for a tenant (admin)",
        responses={**ERR_BAD_REQUEST, **ERR_UNAUTHORIZED},
    )
    async def issue_token(
        tenant_id: str,
        req: IssueTokenRequest,
        _admin: str = Depends(require_admin),
    ) -> IssueTokenResponse:
        tenant = await service_.registry.get_tenant_full(tenant_id)
        if tenant is None:
            from fastapi import HTTPException

            raise HTTPException(
                status_code=404,
                detail={"code": "tenant_not_found", "message": tenant_id},
            )
        from api.auth import generate_token_plaintext

        plaintext = generate_token_plaintext()
        token_id = await authn_.issue_initial_token(
            tenant_id,
            plaintext,
            scopes=list(req.scopes) if req.scopes is not None else None,
            note=req.note,
        )
        info = await service_.registry.get_token_by_id(token_id)
        assert info is not None
        await service_.audit.emit(
            kind="tenant.token.issue",
            tenant=tenant_id,
            session=None,
            payload={"token_id": token_id, "scopes": req.scopes, "note": req.note},
        )
        return IssueTokenResponse(
            token_id=token_id,
            token=plaintext,
            tenant_id=tenant_id,
            scopes=info["scopes"],
            issued_at=info["issued_at"],
        )

    @app.delete(
        "/v1/tenants/{tenant_id}/tokens/{token_id}",
        tags=["Tenants"],
        summary="Revoke one token (admin)",
        status_code=204,
        responses={**ERR_UNAUTHORIZED},
    )
    async def revoke_token_route(
        tenant_id: str,
        token_id: str,
        _admin: str = Depends(require_admin),
    ) -> Response:
        info = await service_.registry.get_token_by_id(token_id)
        if info is None or info["tenant_id"] != tenant_id:
            from fastapi import HTTPException

            raise HTTPException(
                status_code=404,
                detail={"code": "token_not_found", "message": token_id},
            )
        # Slice 12: admin revocation is immediate (no grace), unlike
        # the self-rotate path which carries a `token_grace_seconds`
        # window.
        await service_.registry.revoke_token(token_id, revoke_at_ms=int(time.time() * 1000))
        await service_.audit.emit(
            kind="tenant.token.revoke",
            tenant=tenant_id,
            session=None,
            payload={"token_id": token_id},
        )
        return Response(status_code=204)

    @app.get(
        "/v1/tenants/{tenant_id}/usage",
        response_model=TenantUsageResponse,
        tags=["Tenants"],
        summary="Tenant usage snapshot (admin or owner)",
        responses={**ERR_UNAUTHORIZED},
    )
    async def get_tenant_usage(
        tenant_id: str,
        authorization: str | None = Header(default=None),
    ) -> TenantUsageResponse:
        # Admin OR owner. Resolve full context to know which.
        ctx = await auth_full(authorization)
        if not ctx.is_admin and ctx.tenant_id != tenant_id:
            raise Unauthorized()
        tenant = await service_.registry.get_tenant_full(tenant_id)
        if tenant is None:
            from fastapi import HTTPException

            raise HTTPException(
                status_code=404,
                detail={"code": "tenant_not_found", "message": tenant_id},
            )
        concurrent = await service_.registry.count_concurrent(tenant_id)
        max_conc = tenant.get("max_concurrency") or settings_.tenant_max_concurrent
        active_tokens = await service_.registry.count_active_tokens(tenant_id)
        return TenantUsageResponse(
            tenant_id=tenant_id,
            concurrent_sessions=concurrent,
            max_concurrency=max_conc,
            workspace_bytes=None,  # Quota tracking is filesystem-side; not populated here.
            active_token_count=active_tokens,
        )

    # ----- background processes (slice 11b) -----

    @app.post(
        "/v1/sessions/{session_id}/processes",
        tags=["Processes"],
        summary="Start a background process",
        status_code=201,
        response_model=ProcessResponse,
        responses={
            **ERR_BAD_REQUEST,
            **ERR_UNAUTHORIZED,
            **ERR_NOT_FOUND_SESSION,
            **ERR_CONFLICT,
            **ERR_RATE_LIMIT,
        },
    )
    async def start_process(
        session_id: str,
        req: StartProcessRequest,
        tenant_id: str = Depends(require_scope("processes")),
    ) -> ProcessResponse:
        return await process_service_.start(session_id=session_id, tenant_id=tenant_id, req=req)

    @app.get(
        "/v1/sessions/{session_id}/processes",
        tags=["Processes"],
        summary="List a session's processes",
        response_model=ProcessListResponse,
        responses={**ERR_UNAUTHORIZED, **ERR_NOT_FOUND_SESSION},
    )
    async def list_processes(
        session_id: str, tenant_id: str = Depends(require_scope("processes"))
    ) -> ProcessListResponse:
        entries = await process_service_.list(session_id=session_id, tenant_id=tenant_id)
        return ProcessListResponse(entries=entries)

    @app.get(
        "/v1/sessions/{session_id}/processes/{process_id}",
        tags=["Processes"],
        summary="Get one process",
        response_model=ProcessResponse,
        responses={**ERR_BAD_REQUEST, **ERR_UNAUTHORIZED, **ERR_NOT_FOUND_SESSION},
    )
    async def get_process(
        session_id: str, process_id: str, tenant_id: str = Depends(require_scope("processes"))
    ) -> ProcessResponse:
        return await process_service_.get(
            session_id=session_id, tenant_id=tenant_id, process_id=process_id
        )

    @app.delete(
        "/v1/sessions/{session_id}/processes/{process_id}",
        tags=["Processes"],
        summary="Stop and delete a process",
        response_model=ProcessResponse,
        responses={**ERR_BAD_REQUEST, **ERR_UNAUTHORIZED, **ERR_NOT_FOUND_SESSION},
    )
    async def delete_process(
        session_id: str, process_id: str, tenant_id: str = Depends(require_scope("processes"))
    ) -> ProcessResponse:
        return await process_service_.delete(
            session_id=session_id, tenant_id=tenant_id, process_id=process_id
        )

    @app.get(
        "/v1/sessions/{session_id}/processes/{process_id}/logs",
        tags=["Processes"],
        summary="Tail a process's combined stdout+stderr log over SSE",
        description=(
            "Server-Sent Events stream of the process's combined "
            "stdout+stderr log. Each SSE frame is `event: log` with a "
            '`data: {"chunk_b64": "<base64>"}` payload — base64 keeps '
            "binary log content (ANSI escapes, NULs) round-tripping "
            "cleanly. The stream stays open until the client "
            "disconnects or the underlying process exits.\n\n"
            "OpenAPI tooling does not generate useful clients for SSE; "
            "consume with a streaming HTTP client (`httpx`, `curl -N`, "
            "etc.)."
        ),
        response_class=StreamingResponse,
        responses={
            200: {
                "content": {"text/event-stream": {}},
                "description": "SSE stream of `log` events with base64-encoded chunks.",
            },
            **ERR_BAD_REQUEST,
            **ERR_UNAUTHORIZED,
            **ERR_NOT_FOUND_SESSION,
        },
    )
    async def stream_process_logs(
        session_id: str, process_id: str, tenant_id: str = Depends(require_scope("processes"))
    ) -> StreamingResponse:
        # Validate the (session, process) pair before opening the SSE
        # body so 404 / 400 is a plain HTTP error, not a half-flushed
        # stream.
        session_row = await service_.get(session_id, tenant_id)
        proc_row = await service_.registry.get_process(
            session_id=session_id, process_id=process_id, tenant_id=tenant_id
        )
        if proc_row is None:
            from api.errors import InvalidArgument as _InvalidArgument

            raise _InvalidArgument(f"process_id {process_id} not found in session")

        async def event_iter() -> AsyncIterator[bytes]:
            # Bridge the sync docker-py iterator into asyncio via a
            # thread + Queue (same pattern exec/stream uses).
            loop = asyncio.get_running_loop()
            queue: asyncio.Queue[bytes | None] = asyncio.Queue()
            SENTINEL = None

            def producer() -> None:
                try:
                    for chunk in process_service_.stream_log_iter(session_row, proc_row.log_path):
                        asyncio.run_coroutine_threadsafe(queue.put(chunk), loop)
                except Exception as exc:  # noqa: BLE001
                    log.exception("log stream producer crashed: %s", exc)
                finally:
                    asyncio.run_coroutine_threadsafe(queue.put(SENTINEL), loop)

            import threading as _threading

            _threading.Thread(target=producer, daemon=True).start()
            while True:
                chunk = await queue.get()
                if chunk is SENTINEL:
                    break
                # Frame as one SSE 'log' event per chunk; data is the
                # raw bytes base64-encoded so binary log content (ANSI
                # escapes etc.) round-trips cleanly.
                import base64 as _base64

                payload = json.dumps({"chunk_b64": _base64.b64encode(chunk).decode()})
                yield f"event: log\ndata: {payload}\n\n".encode()

        return StreamingResponse(event_iter(), media_type="text/event-stream")

    # MCP Streamable HTTP endpoint at /mcp. Mounted LAST so all
    # explicit FastAPI routes above (incl. /healthz, /readyz,
    # /metrics, /v1/...) are matched before the catch-all mount.
    # Bearer auth is bridged via Starlette middleware on the sub-app.
    mcp_attach(fastapi_app=app, mcp=mcp_, authn=authn_)

    return app


_IDEMPOTENT_METHODS = ("post", "put", "patch", "delete")
_HTTP_METHODS = ("get", "post", "put", "patch", "delete")
# Public endpoints that don't require a bearer token. Anything else
# under the FastAPI app is bearer-protected at the route layer.
_PUBLIC_PATHS = frozenset({"/healthz", "/readyz", "/metrics"})


def _install_openapi_polish(app: FastAPI) -> None:
    """Wrap `app.openapi` so the generated schema documents:

    - bearerAuth security scheme + a global default + opt-outs on
      the public ops endpoints (alignment with the auth dependency
      every /v1/ route declares).
    - Idempotency-Key request header on every mutating /v1/ route
      and the Idempotent-Replay response header on their 2xx
      responses (slice 11a; the middleware sees the header, FastAPI's
      default generator doesn't).
    - requestBody for POST /v1/sessions/{sid}/files/{path}; the
      handler reads `await request.body()` directly so FastAPI can't
      introspect the shape.
    - A relative `servers` entry so SDK generators have a base URL.

    Cached on `app.openapi_schema` after the first call, matching
    FastAPI's standard pattern. Pure documentation — no runtime
    behaviour change.
    """
    from fastapi.openapi.utils import get_openapi

    def _augment(schema: dict) -> dict:
        components = schema.setdefault("components", {})

        # ----- security: bearerAuth + global default + public opt-out -----
        schemes = components.setdefault("securitySchemes", {})
        schemes.setdefault(
            "bearerAuth",
            {
                "type": "http",
                "scheme": "bearer",
                "bearerFormat": "opaque",
                "description": (
                    "Tenant-scoped opaque bearer token. Issued via "
                    "`POST /v1/tenants/{tenant_id}/tokens` (admin) or "
                    "bootstrapped from `SANDBOX_API_TOKEN`. Stored as "
                    "HMAC-SHA256(pepper, plaintext) at rest "
                    "(SPEC-405). Rotate via "
                    "`POST /v1/tenants/me/tokens/rotate`; previous "
                    "tokens authenticate during a 5-minute grace "
                    "window."
                ),
            },
        )
        schema.setdefault("security", [{"bearerAuth": []}])

        # ----- servers: relative URL placeholder -----
        # Operators front this with reverse proxies on operator-
        # specific URLs, so we ship a relative-URL placeholder; SDK
        # generators that respect `servers` will resolve against the
        # host the spec was fetched from.
        schema.setdefault(
            "servers",
            [
                {
                    "url": "/",
                    "description": "Current host (relative). Override per deployment.",
                }
            ],
        )

        # ----- IdempotencyKey parameter -----
        parameters = components.setdefault("parameters", {})
        parameters["IdempotencyKey"] = {
            "name": "Idempotency-Key",
            "in": "header",
            "required": False,
            "description": (
                "OPTIONAL but STRONGLY RECOMMENDED on every mutating "
                "call. The first request with a given key under the "
                "calling tenant runs normally; replays of the same "
                "(route, key) within the TTL (default 24h) return the "
                "cached response verbatim with the "
                "`Idempotent-Replay: true` response header. Replays "
                "against a different route for the same key return "
                "`409 idempotency_route_mismatch`. Suggested format: "
                "UUIDv4 (Stripe-style semantics; slice 11a)."
            ),
            "schema": {"type": "string", "maxLength": 64},
        }
        replay_header = {
            "description": (
                "Set to `true` when this response was replayed from "
                "the idempotency cache rather than freshly computed."
            ),
            "schema": {"type": "string", "enum": ["true"]},
        }
        ref = {"$ref": "#/components/parameters/IdempotencyKey"}

        # ----- per-path / per-operation polish -----
        for path, item in (schema.get("paths") or {}).items():
            # Public endpoints opt out of the global bearerAuth.
            if path in _PUBLIC_PATHS:
                for method in _HTTP_METHODS:
                    op = item.get(method)
                    if op is not None:
                        op["security"] = []
                continue

            # /v1/ routes get the Idempotency-Key + Idempotent-Replay.
            if path.startswith("/v1/"):
                for method in _IDEMPOTENT_METHODS:
                    op = item.get(method)
                    if not op:
                        continue
                    op.setdefault("parameters", [])
                    if ref not in op["parameters"]:
                        op["parameters"].append(ref)
                    for status, resp in (op.get("responses") or {}).items():
                        if not status.startswith("2"):
                            continue
                        headers = resp.setdefault("headers", {})
                        headers.setdefault("Idempotent-Replay", replay_header)

        # ----- POST /v1/sessions/{sid}/files/{path} requestBody -----
        # The handler reads `await request.body()` directly; FastAPI
        # can't infer the body shape, so we declare it explicitly.
        # Empty bodies remain valid (touch-like creation of a 0-byte
        # file), so requestBody is not marked required.
        files_path_post = (
            schema.get("paths", {}).get("/v1/sessions/{session_id}/files/{path}", {}).get("post")
        )
        if files_path_post is not None and "requestBody" not in files_path_post:
            files_path_post["requestBody"] = {
                "required": False,
                "description": (
                    "Raw file bytes. Empty body creates a zero-length file (touch-like)."
                ),
                "content": {
                    "application/octet-stream": {
                        "schema": {"type": "string", "format": "binary"},
                    },
                },
            }

        return schema

    def custom_openapi() -> dict:
        if app.openapi_schema:
            return app.openapi_schema
        schema = get_openapi(
            title=app.title,
            version=app.version,
            description=app.description,
            routes=app.routes,
            tags=app.openapi_tags,
        )
        app.openapi_schema = _augment(schema)
        return app.openapi_schema

    app.openapi = custom_openapi  # type: ignore[method-assign]


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
