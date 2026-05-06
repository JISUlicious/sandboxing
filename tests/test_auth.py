"""Multi-tenant auth + token rotation tests (slice 7). SPEC-405."""

import time

from api.auth import TokenAuthenticator, generate_token_plaintext, hash_token

# ----- hash function (unit) -----


def test_hash_token_is_deterministic_for_same_pepper():
    h1 = hash_token("hello", "pepper-A")
    h2 = hash_token("hello", "pepper-A")
    assert h1 == h2


def test_hash_token_changes_with_pepper():
    h1 = hash_token("hello", "pepper-A")
    h2 = hash_token("hello", "pepper-B")
    assert h1 != h2


def test_hash_token_changes_with_plaintext():
    h1 = hash_token("hello", "pepper")
    h2 = hash_token("world", "pepper")
    assert h1 != h2


def test_generate_token_plaintext_is_random_and_long_enough():
    a = generate_token_plaintext()
    b = generate_token_plaintext()
    assert a != b
    assert len(a) == 64  # 32 bytes hex


# ----- bootstrap + authenticate -----


def test_bootstrap_creates_default_tenant_and_authenticates(client, authed):
    # The `client` fixture goes through the lifespan, which bootstraps
    # tenant 'default' from settings.api_token. `authed` then sends the
    # same value as a Bearer token. If anything fails, /healthz works
    # but POST /v1/sessions returns 401.
    assert authed.post("/v1/sessions", json={}).status_code == 201


def test_authentication_rejects_wrong_token(client):
    client.headers.update({"Authorization": "Bearer wrong-token"})
    assert client.post("/v1/sessions", json={}).status_code == 401


def test_authentication_rejects_no_token(client):
    assert client.post("/v1/sessions", json={}).status_code == 401


# ----- rotation -----


def test_rotate_returns_new_token_with_grace(authed):
    r = authed.post("/v1/tenants/me/tokens/rotate")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "token" in body
    assert len(body["token"]) == 64
    assert body["old_token_grace_seconds"] > 0
    assert body["tenant_id"] == "default"


def test_old_token_still_works_during_grace(client):
    client.headers.update({"Authorization": "Bearer test-token"})
    r = client.post("/v1/tenants/me/tokens/rotate")
    new_token = r.json()["token"]

    # Old token (still in grace) — keep using it.
    assert client.post("/v1/sessions", json={}).status_code == 201
    # New token also works.
    client.headers.update({"Authorization": f"Bearer {new_token}"})
    assert client.post("/v1/sessions", json={}).status_code == 201


async def test_old_token_rejected_after_grace_window(client, service, settings):
    client.headers.update({"Authorization": "Bearer test-token"})
    r = client.post("/v1/tenants/me/tokens/rotate")
    new_token = r.json()["token"]

    # Force the grace window expired by rewriting revoked_at to the past.
    digest = hash_token("test-token", settings.token_pepper)
    row = await service.registry.lookup_token(digest)
    assert row is not None
    token_id, _, _, _ = row
    past = int(time.time() * 1000) - 1
    await service.registry.revoke_token(token_id, revoke_at_ms=past)

    # Old token now fails.
    assert client.post("/v1/sessions", json={}).status_code == 401
    # New token still good.
    client.headers.update({"Authorization": f"Bearer {new_token}"})
    assert client.post("/v1/sessions", json={}).status_code == 201


# ----- multi-tenant isolation -----


async def test_two_tenants_cannot_see_each_others_sessions(client, service, settings):
    # Tenant A is the bootstrap default; create tenant B by hand.
    authn = TokenAuthenticator(settings=settings, registry=service.registry)
    await service.registry.create_tenant("tenantB", "Tenant B")
    plaintext_b = generate_token_plaintext()
    await authn.issue_initial_token("tenantB", plaintext_b)

    # Tenant A creates a session.
    client.headers.update({"Authorization": "Bearer test-token"})
    sid_a = client.post("/v1/sessions", json={}).json()["session_id"]
    assert client.get(f"/v1/sessions/{sid_a}").status_code == 200

    # Tenant B sees 404 (existence-oracle parity, SPEC-200).
    client.headers.update({"Authorization": f"Bearer {plaintext_b}"})
    assert client.get(f"/v1/sessions/{sid_a}").status_code == 404
    # Tenant B's own session works fine.
    sid_b = client.post("/v1/sessions", json={}).json()["session_id"]
    assert client.get(f"/v1/sessions/{sid_b}").status_code == 200
    # And tenant A still can't see B's session.
    client.headers.update({"Authorization": "Bearer test-token"})
    assert client.get(f"/v1/sessions/{sid_b}").status_code == 404
