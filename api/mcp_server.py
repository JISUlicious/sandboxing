"""MCP server (Streamable HTTP) mounted on the FastAPI app at `/mcp`.

Each MCP tool is a thin wrapper around an existing
SessionService / ExecService / FileService method; the tenant_id
flows from the bearer token through a ContextVar set by the
auth-bridge middleware. See the MCP plan for the design rationale
(transport, auth, tool surface). Streamable HTTP is configured
stateless + json-response per the 2026 scaling guidance.
"""

from __future__ import annotations

import base64
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar

from fastapi import FastAPI
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request as StarletteRequest
from starlette.responses import JSONResponse

from api.auth import TokenAuthenticator
from api.errors import SandboxError, Unauthorized
from api.exec import ExecService
from api.files import FileService
from api.models import (
    ExecRequest,
    ExecResponse,
    FileListResponse,
    FileWriteRequest,
    Limits,
    SessionResponse,
)
from api.registry import SessionRow
from api.sessions import SessionService

log = logging.getLogger("sandbox.mcp")

# Per-request tenant id, set by the auth bridge before the tool
# handler runs. Reads inside tool handlers see the value because
# asyncio inherits ContextVars at Task creation time and updates
# propagate within the Task chain.
_current_tenant: ContextVar[str] = ContextVar("sandbox_mcp_tenant_id")


def _row_to_response(row: SessionRow) -> SessionResponse:
    """Mirror of `api.server._to_response`. Duplicated here to keep
    the import direction one-way (mcp_server imports from server-
    adjacent modules but server imports from mcp_server, not the
    other way around)."""
    return SessionResponse(
        session_id=row.id,
        status=row.status,
        tenant_id=row.tenant_id,
        limits=row.limits,
        created_at=row.created_at,
        last_activity_at=row.last_activity_at,
    )


def _tenant_id() -> str:
    try:
        return _current_tenant.get()
    except LookupError as exc:
        # Reachable only if the auth-bridge middleware didn't run —
        # programming error, not a user-facing condition.
        raise RuntimeError("MCP tool invoked without tenant context") from exc


def _surface_error(exc: SandboxError) -> RuntimeError:
    """Translate our HTTPException-rooted SandboxError types into a
    plain Python exception with just the human-readable message —
    avoids leaking the FastAPI machinery into MCP tool-error output."""
    detail = exc.detail if isinstance(exc.detail, dict) else {}
    msg = detail.get("message") or str(exc)
    code = detail.get("code") or "error"
    return RuntimeError(f"{code}: {msg}")


class _MCPAuthMiddleware(BaseHTTPMiddleware):
    """Bridge bearer-token auth from the existing TokenAuthenticator
    into the MCP request scope. Sets `_current_tenant` so the tool
    handlers see the resolved tenant_id; rejects with HTTP 401
    otherwise."""

    def __init__(self, app, *, authn: TokenAuthenticator) -> None:
        super().__init__(app)
        self._authn = authn

    async def dispatch(self, request: StarletteRequest, call_next):
        # Only enforce auth on the MCP endpoint itself. The sub-app
        # is mounted at the FastAPI root as a catch-all, so unknown
        # paths fall through here too — let them pass to the sub-
        # app's natural 404 instead of returning 401, which would
        # confuse clients that hit a typo'd URL.
        if request.url.path != "/mcp":
            return await call_next(request)
        authorization = request.headers.get("authorization") or ""
        if not authorization.startswith("Bearer "):
            return JSONResponse(
                {"detail": {"code": "unauthorized", "message": "missing bearer"}},
                status_code=401,
            )
        token = authorization.removeprefix("Bearer ").strip()
        try:
            tenant_id = await self._authn.authenticate(token)
        except Unauthorized:
            return JSONResponse(
                {"detail": {"code": "unauthorized", "message": "invalid bearer"}},
                status_code=401,
            )
        ctx_token = _current_tenant.set(tenant_id)
        try:
            return await call_next(request)
        finally:
            _current_tenant.reset(ctx_token)


def build_mcp(
    *,
    sessions: SessionService,
    exec_service: ExecService,
    file_service: FileService,
) -> FastMCP:
    """Construct the FastMCP server with the v1 tool catalogue
    registered as closures over the live service instances.

    `streamable_http_path='/'` makes the sub-app expose its endpoint
    at the mount root; mount this on the parent FastAPI at `/mcp`
    so the public URL is `/mcp` (not `/mcp/mcp`).
    """
    mcp = FastMCP(
        name="sandbox",
        instructions=(
            "Lifecycle, exec, and file tools for an LLM agent sandbox. "
            "Create a session, run commands inside it, read/write its "
            "/workspace, then destroy it when done. /workspace persists "
            "across exec calls and across stop/resume."
        ),
        stateless_http=True,
        json_response=True,
        # Sub-app exposes its endpoint at `/mcp` internally; we mount
        # the sub-app at `/` on the parent so the public URL stays
        # `/mcp` (mounting at `/mcp` would redirect-loop because of
        # how Starlette / FastAPI handles trailing slashes on Mount
        # routes).
        streamable_http_path="/mcp",
        # The SDK's DNS-rebinding protection rejects requests whose
        # Host header isn't in `allowed_hosts`. The default empty
        # list rejects everything except localhost variants; that's
        # too strict for our deployment shape, where an operator-
        # configured reverse proxy fronts the service. Our existing
        # bearer-token auth already neutralises browser-based DNS-
        # rebinding (the attacker's page would need a valid token).
        # Operators who need stricter posture can revisit this; for
        # v1 we turn the SDK check off and rely on the reverse-proxy
        # + token model.
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=False,
        ),
    )

    # ----- lifecycle (5 tools) -----

    @mcp.tool(
        name="session_create",
        description=(
            "Create a fresh sandbox session. Returns a SessionResponse "
            "with the new session_id and RUNNING status. Use this "
            "whenever the agent needs an isolated environment."
        ),
    )
    async def session_create(limits: Limits | None = None) -> SessionResponse:
        try:
            row = await sessions.create(_tenant_id(), limits)
        except SandboxError as exc:
            raise _surface_error(exc) from exc
        return _row_to_response(row)

    @mcp.tool(
        name="session_get",
        description="Fetch a session's status, limits, and timestamps.",
    )
    async def session_get(session_id: str) -> SessionResponse:
        try:
            row = await sessions.get(session_id, _tenant_id())
        except SandboxError as exc:
            raise _surface_error(exc) from exc
        return _row_to_response(row)

    @mcp.tool(
        name="session_stop",
        description=(
            "Stop the session's container while keeping its /workspace "
            "volume. Use to pause without losing state; resume later."
        ),
    )
    async def session_stop(session_id: str) -> SessionResponse:
        try:
            row = await sessions.stop(session_id, _tenant_id())
        except SandboxError as exc:
            raise _surface_error(exc) from exc
        return _row_to_response(row)

    @mcp.tool(
        name="session_resume",
        description=(
            "Restart the container for a STOPPED session. /workspace "
            "is preserved across stop+resume."
        ),
    )
    async def session_resume(session_id: str) -> SessionResponse:
        try:
            row = await sessions.resume(session_id, _tenant_id())
        except SandboxError as exc:
            raise _surface_error(exc) from exc
        return _row_to_response(row)

    @mcp.tool(
        name="session_destroy",
        description=(
            "Permanently delete the session, its container, and its "
            "/workspace volume. Idempotent."
        ),
    )
    async def session_destroy(session_id: str) -> dict[str, bool]:
        try:
            await sessions.destroy(session_id, _tenant_id())
        except SandboxError as exc:
            raise _surface_error(exc) from exc
        return {"ok": True}

    # ----- exec (1 tool, sync only for v1) -----

    @mcp.tool(
        name="exec",
        description=(
            "Run a command inside a session and wait for completion. "
            "argv is the program + args (no shell). Returns "
            "stdout/stderr/exit_code. STOPPED sessions auto-resume on "
            "first exec."
        ),
    )
    async def exec_(session_id: str, req: ExecRequest) -> ExecResponse:
        try:
            return await exec_service.run(session_id, _tenant_id(), req)
        except SandboxError as exc:
            raise _surface_error(exc) from exc

    # ----- files (4 tools) -----

    @mcp.tool(
        name="file_write",
        description=(
            "Write a file to /workspace. content_b64 is base64-encoded "
            "for binary safety. Creates parent directories as needed."
        ),
    )
    async def file_write(session_id: str, req: FileWriteRequest) -> dict[str, object]:
        try:
            return await file_service.write(session_id, _tenant_id(), req)
        except SandboxError as exc:
            raise _surface_error(exc) from exc

    @mcp.tool(
        name="file_read",
        description=(
            "Read a file from /workspace. Returns content_b64 (base64) "
            "and the file mode."
        ),
    )
    async def file_read(session_id: str, path: str) -> dict[str, object]:
        try:
            content, mode = await file_service.read(session_id, _tenant_id(), path)
        except SandboxError as exc:
            raise _surface_error(exc) from exc
        return {"content_b64": base64.b64encode(content).decode(), "mode": mode}

    @mcp.tool(
        name="file_list",
        description="List entries under /workspace (or a subdirectory).",
    )
    async def file_list(session_id: str, subdir: str = "") -> FileListResponse:
        try:
            return await file_service.list_dir(session_id, _tenant_id(), subdir)
        except SandboxError as exc:
            raise _surface_error(exc) from exc

    @mcp.tool(
        name="file_delete",
        description=(
            "Delete a file or directory under /workspace. Pass "
            "recursive=True to delete a non-empty directory."
        ),
    )
    async def file_delete(
        session_id: str, path: str, recursive: bool = False
    ) -> dict[str, bool]:
        try:
            await file_service.delete(session_id, _tenant_id(), path, recursive)
        except SandboxError as exc:
            raise _surface_error(exc) from exc
        return {"ok": True}

    return mcp


def attach_to_fastapi(
    *,
    fastapi_app: FastAPI,
    mcp: FastMCP,
    authn: TokenAuthenticator,
) -> None:
    """Mount the MCP Streamable HTTP sub-app at `/mcp`, with bearer-
    token auth bridged into a ContextVar. Caller is responsible for
    composing `mcp_lifespan_context(mcp)` into the parent app's
    lifespan so the FastMCP session manager runs."""
    sub_app = mcp.streamable_http_app()
    sub_app.add_middleware(_MCPAuthMiddleware, authn=authn)
    # Mount the sub-app at `/` (catch-all) rather than at `/mcp`. The
    # sub-app's *internal* route is `/mcp`, so the public URL is
    # `/mcp`. Mounting at `/mcp` would have Starlette strip the prefix
    # and the sub-app would redirect-loop. The parent's existing
    # routes (`/v1/sessions/...`, `/healthz`, etc.) match before this
    # fallback because they're more specific.
    fastapi_app.mount("/", sub_app, name="mcp")


@asynccontextmanager
async def mcp_lifespan_context(mcp: FastMCP) -> AsyncIterator[None]:
    """Drive the FastMCP session manager from FastAPI's lifespan.

    Nest this inside the parent app's lifespan so the manager
    starts before traffic and shuts down cleanly with the app.
    """
    async with mcp.session_manager.run():
        yield
