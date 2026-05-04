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
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import time
from typing import TYPE_CHECKING

from ulid import ULID

from api.errors import Unauthorized

if TYPE_CHECKING:
    from api.config import Settings
    from api.registry import Registry

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
        """Resolve a bearer token to its tenant_id, or raise Unauthorized.

        Steps:
        1. Hash the bearer with the pepper.
        2. Look up the row by hash (single indexed query).
        3. Reject if not found.
        4. Reject if revoked_at is set and ≤ now (fully revoked).
        5. Otherwise return tenant_id (NULL revoked_at or in-grace).
        """
        digest = hash_token(bearer, self._settings.token_pepper)
        row = await self._registry.lookup_token(digest)
        if row is None:
            raise Unauthorized()
        _token_id, tenant_id, revoked_at = row
        if revoked_at is not None:
            now = int(time.time() * 1000)
            if revoked_at <= now:
                raise Unauthorized()
        return tenant_id

    async def issue_initial_token(self, tenant_id: str, plaintext: str) -> str:
        """Used by the bootstrap path and the CLI. Hashes `plaintext`,
        stores it under `tenant_id`, returns the new row's token_id."""
        token_id = str(ULID())
        digest = hash_token(plaintext, self._settings.token_pepper)
        await self._registry.insert_token(
            token_id=token_id,
            tenant_id=tenant_id,
            hash_=digest,
        )
        return token_id

    async def rotate(self, tenant_id: str, current_token_id: str) -> tuple[str, int]:
        """Issue a new token for `tenant_id`, mark `current_token_id`
        revoked_at = now + grace. Returns (new_plaintext, grace_seconds)."""
        plaintext = generate_token_plaintext()
        new_id = str(ULID())
        digest = hash_token(plaintext, self._settings.token_pepper)
        await self._registry.insert_token(token_id=new_id, tenant_id=tenant_id, hash_=digest)
        grace = self._settings.token_grace_seconds
        revoke_at = int(time.time() * 1000) + grace * 1000
        await self._registry.revoke_token(current_token_id, revoke_at_ms=revoke_at)
        return plaintext, grace


async def bootstrap_default_tenant(
    *, settings: Settings, registry: Registry, auth: TokenAuthenticator
) -> bool:
    """Idempotent: if no tenants exist AND `settings.api_token` is set,
    create the 'default' tenant and seed it with a token that hashes
    to that bearer. Returns True iff bootstrap actually ran.

    Existing single-token deployments upgrade transparently — the env
    variable they've been using continues to authenticate."""
    if await registry.count_tenants() > 0:
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
    return True
