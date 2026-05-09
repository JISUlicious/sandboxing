"""Idempotency-Key replay cache (slice 11a).

Mutating endpoints honor an optional `Idempotency-Key` request
header. The first mutating request with a given key under a tenant
runs normally; the response (status + body + content-type) is
cached. Replays of the same key within the TTL return the cached
response verbatim instead of executing a second time.

A small per-key asyncio lock prevents two concurrent requests with
the same key from racing — the second request waits for the first
to finish and returns the same body. This is the Stripe-style
"key reused for an in-flight request" guarantee.

The cache key is `(tenant_id, key)` so a key reused under a
different tenant is allowed (it's a separate logical operation).

Cache rows expire after `Settings.idempotency_ttl_s` (default 24h).
The reaper sweeps expired rows on its normal tick.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

from api.config import Settings
from api.registry import Registry

log = logging.getLogger("sandbox.idempotency")

# HTTP methods that mutate state. GET / HEAD / OPTIONS are skipped —
# replaying them has no observable difference.
_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# Status codes worth caching. 4xx/5xx are not cached (they're
# transient or retryable; replaying a 500 to bypass a recovery is
# the wrong behaviour).
_CACHEABLE_STATUS_RANGE = range(200, 300)


class IdempotencyMiddleware(BaseHTTPMiddleware):
    """Wraps the FastAPI app to honor `Idempotency-Key` on mutations.

    Order matters: this middleware must run AFTER auth has resolved
    `request.state.tenant_id` (or set the equivalent), because the
    cache is scoped per-tenant. In `api.server.create_app` the
    middleware is registered after the auth bridge for that reason.
    For HTTP routes the resolution happens in the per-route
    `Depends(auth)`; this middleware runs *before* the dependency,
    so we re-resolve the bearer here using the same authenticator
    the routes use. That's a small redundant lookup, but it keeps
    the middleware self-contained.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        settings: Settings,
        registry: Registry,
        authenticate: Callable[[str], Awaitable[str]],
    ) -> None:
        super().__init__(app)
        self._settings = settings
        self._registry = registry
        self._authenticate = authenticate
        # Per-key in-flight lock map. A request acquires the lock for
        # its (tenant, key) pair; concurrent replays wait, then read
        # the now-cached response on the second pass through dispatch.
        self._inflight: dict[tuple[str, str], asyncio.Lock] = {}
        self._inflight_meta_lock = asyncio.Lock()

    async def _lock_for(self, tenant_id: str, key: str) -> asyncio.Lock:
        async with self._inflight_meta_lock:
            cache_key = (tenant_id, key)
            lock = self._inflight.get(cache_key)
            if lock is None:
                lock = asyncio.Lock()
                self._inflight[cache_key] = lock
            return lock

    async def _release_inflight(self, tenant_id: str, key: str) -> None:
        async with self._inflight_meta_lock:
            self._inflight.pop((tenant_id, key), None)

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        # Skip non-mutating methods entirely.
        if request.method not in _MUTATING_METHODS:
            return await call_next(request)

        key = request.headers.get("idempotency-key")
        if not key:
            return await call_next(request)

        # Resolve the tenant. Bearer-less / invalid-bearer requests
        # fall through to the route's auth dependency, which returns
        # 401 — we don't want the middleware to short-circuit them
        # with a confusing 401 of its own shape.
        authorization = request.headers.get("authorization") or ""
        if not authorization.startswith("Bearer "):
            return await call_next(request)
        try:
            tenant_id = await self._authenticate(authorization.removeprefix("Bearer ").strip())
        except Exception:
            return await call_next(request)

        route_template = _route_template(request)

        # Fast path: cache hit.
        cached = await self._registry.lookup_idempotency(tenant_id=tenant_id, key=key)
        if cached is not None:
            cached_route, status, body, content_type = cached
            if cached_route != route_template:
                return _route_mismatch_response(cached_route, route_template)
            return _replay_response(status, body, content_type)

        # Slow path: take the per-key lock, re-check, run the request,
        # cache the response if it's a 2xx mutation.
        lock = await self._lock_for(tenant_id, key)
        async with lock:
            cached = await self._registry.lookup_idempotency(tenant_id=tenant_id, key=key)
            if cached is not None:
                cached_route, status, body, content_type = cached
                if cached_route != route_template:
                    return _route_mismatch_response(cached_route, route_template)
                return _replay_response(status, body, content_type)

            response = await call_next(request)

            if response.status_code in _CACHEABLE_STATUS_RANGE:
                # SSE streaming responses cannot be safely cached:
                # `_read_body_bytes` drains the response body to compute
                # a Content-Length, which forces Starlette to send the
                # entire payload in one batch (no Transfer-Encoding:
                # chunked, no incremental delivery). That defeats the
                # whole point of /exec/stream — clients see all SSE
                # frames bunch at the end of execution.
                #
                # And replay semantics for a real-time stream are
                # awkward anyway: a frozen byte-snapshot of "what
                # would have streamed" doesn't preserve inter-chunk
                # timing. Operators using Idempotency-Key on a
                # streaming endpoint get exactly-once execution on the
                # first call (no in-flight duplicate via the lock
                # above) but no cached replay on retry — they have to
                # re-run, which for a streaming endpoint is fine since
                # outputs are observability, not state mutation.
                response_ct = response.headers.get("content-type", "")
                if response_ct.startswith("text/event-stream"):
                    log.debug(
                        "idempotency: skipping cache for streaming response (%s %s)",
                        request.method, route_template,
                    )
                else:
                    body_bytes, content_type = await _read_body_bytes(response)
                    await self._registry.store_idempotency(
                        tenant_id=tenant_id,
                        key=key,
                        route_template=route_template,
                        status_code=response.status_code,
                        body_json=body_bytes.decode("utf-8") if body_bytes else "",
                        content_type=content_type,
                        ttl_s=self._settings.idempotency_ttl_s,
                    )
                    # We consumed the streaming body to cache it; rebuild
                    # a Response so downstream Starlette ASGI sends it.
                    response = Response(
                        content=body_bytes,
                        status_code=response.status_code,
                        headers=dict(response.headers),
                        media_type=content_type or None,
                    )
            await self._release_inflight(tenant_id, key)
            return response


def _route_template(request: Request) -> str:
    """Stable identifier for the route shape — `/v1/sessions/{session_id}`
    rather than the literal session id. Falls back to the raw path if
    the request never reached a route (e.g., 404 paths)."""
    route = request.scope.get("route")
    template = getattr(route, "path", None)
    return template or request.url.path


async def _read_body_bytes(response: Response) -> tuple[bytes, str]:
    """Drain a Starlette streaming Response into bytes for caching.
    StreamingResponse iterators can only be consumed once, so we
    rebuild a non-streaming Response upstream."""
    if hasattr(response, "body") and isinstance(response.body, (bytes, bytearray)):
        # Plain Response — body is already in memory.
        return bytes(response.body), response.headers.get("content-type", "")
    chunks: list[bytes] = []
    async for chunk in response.body_iterator:  # type: ignore[attr-defined]
        if isinstance(chunk, str):
            chunks.append(chunk.encode("utf-8"))
        else:
            chunks.append(chunk)
    return b"".join(chunks), response.headers.get("content-type", "")


def _replay_response(status: int, body: str, content_type: str) -> Response:
    return Response(
        content=body.encode("utf-8") if body else b"",
        status_code=status,
        media_type=content_type or None,
        headers={"Idempotent-Replay": "true"},
    )


def _route_mismatch_response(stored_route: str, attempted_route: str) -> Response:
    return JSONResponse(
        {
            "detail": {
                "code": "idempotency_route_mismatch",
                "message": (
                    f"Idempotency-Key was first used against {stored_route!r}; "
                    f"replay attempted against {attempted_route!r}"
                ),
            }
        },
        status_code=409,
    )
