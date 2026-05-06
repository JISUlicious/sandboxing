"""Bearer-token authentication for the sandbox control plane.

Slice 7: tokens are stored as `HMAC-SHA256(pepper, plaintext)` so the
DB never holds plaintext. HMAC (not Argon2) is the right primitive
here because tokens are 32 bytes of randomness — slow KDFs buy
nothing against a brute-force attacker on that input space, and HMAC
lets us index the lookup column for O(1) authentication.

Rotation grace window: a rotated token's row gets `revoked_at = now +
token_grace_seconds`. Both the new and old tokens authenticate during
that window so callers have time to switch over without an outage.
After the window closes the old token returns 401.

Bootstrap: on startup, if the tenants table is empty AND
`settings.api_token` is set, the lifespan creates a tenant `default`
and seeds a token whose hash matches the env value. This keeps
existing single-token deployments working without manual migration.

Slice 12 — admin token + scoped tokens:
- A separate `settings.admin_token`, when set, authenticates against
  the synthetic tenant `__admin__` (created on first use) with the
  full scope set. Admin endpoints under `/v1/tenants` require this.
- Tokens carry an optional `scopes` list; `None` means "all scopes"
  (back-compat for tokens issued before slice 12). The
  `AuthContext` returned by `authenticate_full` carries scopes so
  per-route scope checks can run.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ulid import ULID

from api.errors import SandboxError, Unauthorized

if TYPE_CHECKING:
    from api.config import Settings
    from api.registry import Registry


ADMIN_TENANT_ID = "__admin__"
"""Synthetic tenant id reserved for the admin token. Carries no
sessions / volumes; only exists so the admin token has a row to live
under and the same revoke / rotate machinery applies."""


@dataclass
class AuthContext:
    """Resolved bearer token. `scopes is None` ⇒ all scopes (back-
    compat). `is_admin` is True only when the bearer matched
    `Settings.admin_token` (or a token issued for the synthetic
    `__admin__` tenant)."""

    tenant_id: str
    token_id: str
    scopes: list[str] | None
    is_admin: bool


class ForbiddenScope(SandboxError):
    def __init__(self, required: str) -> None:
        super().__init__(403, "forbidden_scope", f"token lacks required scope: {required}")


log = logging.getLogger("sandbox.auth")


def hash_token(plaintext: str, pepper: str) -> str:
    """Stable hash of a bearer token. Same (plaintext, pepper) pair
    always produces the same hex digest, so the `tokens.hash` column
    can be a unique index. Pepper rotation invalidates every token —
    don't rotate it casually."""
    return hmac.new(
        pepper.encode("utf-8"),
        plaintext.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def generate_token_plaintext() -> str:
    """32 bytes of randomness, hex-encoded — 64 ASCII chars. Suitable
    as a bearer token; cryptographically secure."""
    return secrets.token_hex(32)


class TokenAuthenticator:
    def __init__(self, *, settings: Settings, registry: Registry) -> None:
        self._settings = settings
        self._registry = registry

    async def authenticate(self, bearer: str) -> str:
        """Back-compat wrapper: resolve a bearer to its tenant_id, or
        raise Unauthorized. Scope-aware callers should use
        `authenticate_full` instead so they have access to the token's
        scopes + admin flag."""
        ctx = await self.authenticate_full(bearer)
        return ctx.tenant_id

    async def authenticate_full(self, bearer: str) -> AuthContext:
        """Resolve a bearer token to its full AuthContext, or raise
        Unauthorized.

        Steps:
        1. Hash the bearer with the pepper.
        2. Look up the row by hash (single indexed query).
        3. Reject if not found / fully revoked.
        4. Mark `is_admin=True` iff the row's tenant_id is the
           synthetic `__admin__` tenant. Admin tokens always carry
           the full scope set (we don't restrict them).
        5. Return the context with scopes (None ⇒ all-scopes).
        """
        digest = hash_token(bearer, self._settings.token_pepper)
        row = await self._registry.lookup_token(digest)
        if row is None:
            raise Unauthorized()
        token_id, tenant_id, revoked_at, scopes = row
        if revoked_at is not None:
            now = int(time.time() * 1000)
            if revoked_at <= now:
                raise Unauthorized()
        return AuthContext(
            tenant_id=tenant_id,
            token_id=token_id,
            scopes=scopes,  # None → all scopes
            is_admin=(tenant_id == ADMIN_TENANT_ID),
        )

    async def issue_initial_token(
        self,
        tenant_id: str,
        plaintext: str,
        *,
        scopes: list[str] | None = None,
        note: str | None = None,
    ) -> str:
        """Used by the bootstrap path and the CLI. Hashes `plaintext`,
        stores it under `tenant_id`, returns the new row's token_id."""
        token_id = str(ULID())
        digest = hash_token(plaintext, self._settings.token_pepper)
        await self._registry.insert_token(
            token_id=token_id,
            tenant_id=tenant_id,
            hash_=digest,
            scopes=scopes,
            note=note,
        )
        return token_id

    async def rotate(self, tenant_id: str, current_token_id: str) -> tuple[str, int]:
        """Issue a new token for `tenant_id`, mark `current_token_id`
        revoked_at = now + grace. Returns (new_plaintext, grace_seconds).
        The replacement carries the same scopes as the rotated token
        (so rotation doesn't silently widen privilege)."""
        plaintext = generate_token_plaintext()
        new_id = str(ULID())
        digest = hash_token(plaintext, self._settings.token_pepper)
        # Carry over scopes from the rotated token so a rotation never
        # silently widens privilege.
        existing = await self._registry.get_token_by_id(current_token_id)
        existing_scopes = existing["scopes"] if existing else None
        existing_note = existing["note"] if existing else None
        await self._registry.insert_token(
            token_id=new_id,
            tenant_id=tenant_id,
            hash_=digest,
            scopes=existing_scopes,
            note=existing_note,
        )
        grace = self._settings.token_grace_seconds
        revoke_at = int(time.time() * 1000) + grace * 1000
        await self._registry.revoke_token(current_token_id, revoke_at_ms=revoke_at)
        return plaintext, grace


def has_scope(ctx: AuthContext, required: str) -> bool:
    """`scopes is None` is the back-compat 'all scopes' shorthand.
    Empty list explicitly grants nothing."""
    if ctx.is_admin:
        return True
    if ctx.scopes is None:
        return True
    return required in ctx.scopes


async def bootstrap_default_tenant(
    *, settings: Settings, registry: Registry, auth: TokenAuthenticator
) -> bool:
    """Idempotent: if no tenants exist AND `settings.api_token` is set,
    create the 'default' tenant and seed it with a token that hashes
    to that bearer. Returns True iff bootstrap actually ran.

    Existing single-token deployments upgrade transparently — the env
    variable they've been using continues to authenticate."""
    if await registry.count_tenants() > 0:
        await _bootstrap_admin_token(settings=settings, registry=registry, auth=auth)
        return False
    if not settings.api_token:
        log.warning(
            "no tenants exist and SANDBOX_API_TOKEN not set; "
            "service will reject all requests until a tenant is created"
        )
        return False
    await registry.create_tenant("default", "default")
    await auth.issue_initial_token("default", settings.api_token)
    log.info(
        "bootstrap: created tenant 'default' from SANDBOX_API_TOKEN "
        "(env value continues to authenticate)"
    )
    await _bootstrap_admin_token(settings=settings, registry=registry, auth=auth)
    return True


async def _bootstrap_admin_token(
    *, settings: Settings, registry: Registry, auth: TokenAuthenticator
) -> None:
    """Slice 12 — seed the synthetic `__admin__` tenant + token from
    `settings.admin_token` if set. Idempotent: re-running with the
    same token hash is a no-op (UNIQUE constraint on tokens.hash);
    rotating the env value adds a second admin token (operator can
    revoke the old one via /v1/tenants/__admin__/tokens/{kid})."""
    if not settings.admin_token:
        return
    digest = hash_token(settings.admin_token, settings.token_pepper)
    existing = await registry.lookup_token(digest)
    if existing is not None:
        return
    await registry.create_tenant(ADMIN_TENANT_ID, "admin (synthetic)")
    await auth.issue_initial_token(
        ADMIN_TENANT_ID, settings.admin_token, note="bootstrap from SANDBOX_ADMIN_TOKEN"
    )
    log.info("bootstrap: seeded admin token (tenant=%s)", ADMIN_TENANT_ID)
