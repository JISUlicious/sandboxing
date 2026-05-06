"""Tenant management API tests (slice 12).

Covers admin auth, tenant CRUD, scoped token issuance, and the
usage-snapshot route. Scope enforcement on existing routes lives in
test_token_scopes.py.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from api.config import Settings
from api.server import create_app
from api.sessions import SessionService


@pytest.fixture
def admin_settings(tmp_path) -> Settings:
    """Settings with both `api_token` (default tenant) and
    `admin_token` set so the management API is available."""
    return Settings(
        dev_mode=True,
        api_token="default-tenant-token",
        admin_token="admin-token",
        db_path=tmp_path / "test.db",
        audit_log_path=tmp_path / "audit.log",
    )


@pytest.fixture
def admin_service(admin_settings, fake_docker):
    return SessionService(
        settings=admin_settings,
        registry=__import__("api.registry", fromlist=["Registry"]).Registry(admin_settings.db_path),
        docker=fake_docker,
        audit=__import__("api.audit", fromlist=["AuditEmitter"]).AuditEmitter(
            admin_settings.audit_log_path
        ),
    )


@pytest.fixture
def admin_client(admin_settings, admin_service):
    app = create_app(admin_settings, service=admin_service, start_reaper=False)
    with TestClient(app) as c:
        yield c


@pytest.fixture
def admin_authed(admin_client):
    admin_client.headers.update({"Authorization": "Bearer admin-token"})
    return admin_client


# ---------------------------------------------------------------------
# Admin gate
# ---------------------------------------------------------------------


def test_admin_routes_503_when_admin_token_unset(authed):
    """Default test fixture has no admin_token → admin routes are
    explicitly disabled."""
    r = authed.get("/v1/tenants")
    assert r.status_code == 503
    assert r.json()["detail"]["code"] == "admin_disabled"


def test_admin_routes_reject_non_admin_bearer(admin_client):
    """A regular tenant bearer must not reach admin routes."""
    admin_client.headers.update({"Authorization": "Bearer default-tenant-token"})
    r = admin_client.get("/v1/tenants")
    assert r.status_code == 401


def test_admin_routes_accept_admin_bearer(admin_authed):
    r = admin_authed.get("/v1/tenants")
    assert r.status_code == 200


# ---------------------------------------------------------------------
# Tenant CRUD
# ---------------------------------------------------------------------


def test_create_tenant(admin_authed):
    r = admin_authed.post(
        "/v1/tenants",
        json={
            "name": "acme",
            "display_name": "ACME Corp",
            "limits": {"max_concurrency": 10, "max_workspace_gib": 100},
            "egress_allowlist": ["github.com", "pypi.org"],
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["tenant_id"] == "acme"
    assert body["display_name"] == "ACME Corp"
    assert body["limits"]["max_concurrency"] == 10
    assert body["limits"]["max_workspace_gib"] == 100
    assert body["egress_allowlist"] == ["github.com", "pypi.org"]
    assert body["active_token_count"] == 0


def test_create_tenant_duplicate_400(admin_authed):
    admin_authed.post("/v1/tenants", json={"name": "acme"})
    r = admin_authed.post("/v1/tenants", json={"name": "acme"})
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "invalid_argument"


def test_list_tenants_includes_default(admin_authed):
    r = admin_authed.get("/v1/tenants")
    assert r.status_code == 200
    names = {t["tenant_id"] for t in r.json()["entries"]}
    # Bootstrap created `default` from api_token, plus admin ran the
    # admin-bootstrap path so `__admin__` exists too.
    assert "default" in names
    assert "__admin__" in names


def test_get_tenant(admin_authed):
    admin_authed.post("/v1/tenants", json={"name": "acme"})
    r = admin_authed.get("/v1/tenants/acme")
    assert r.status_code == 200
    assert r.json()["tenant_id"] == "acme"


def test_get_tenant_404(admin_authed):
    r = admin_authed.get("/v1/tenants/nonexistent")
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "tenant_not_found"


def test_update_tenant(admin_authed):
    admin_authed.post(
        "/v1/tenants",
        json={"name": "acme", "limits": {"max_concurrency": 5}},
    )
    r = admin_authed.patch(
        "/v1/tenants/acme",
        json={
            "display_name": "ACME Inc",
            "limits": {"max_concurrency": 25, "max_workspace_gib": 200},
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["display_name"] == "ACME Inc"
    assert body["limits"]["max_concurrency"] == 25
    assert body["limits"]["max_workspace_gib"] == 200


def test_delete_tenant(admin_authed):
    admin_authed.post("/v1/tenants", json={"name": "acme"})
    # Issue a token so we can confirm it gets revoked.
    tok = admin_authed.post("/v1/tenants/acme/tokens", json={}).json()
    assert tok["token"]
    r = admin_authed.delete("/v1/tenants/acme")
    assert r.status_code == 200
    body = r.json()
    assert body["tenant_id"] == "acme"
    assert body["tokens_revoked"] >= 1
    # Subsequent get_tenant must 404.
    assert admin_authed.get("/v1/tenants/acme").status_code == 404


# ---------------------------------------------------------------------
# Token issuance + scopes
# ---------------------------------------------------------------------


def test_issue_token_with_full_scopes_default(admin_authed):
    admin_authed.post("/v1/tenants", json={"name": "acme"})
    r = admin_authed.post("/v1/tenants/acme/tokens", json={"note": "team-bot"})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["token_id"]
    assert body["token"]
    assert body["tenant_id"] == "acme"
    # `scopes=None` ⇒ all-scopes back-compat default.
    assert body["scopes"] is None


def test_issue_token_with_explicit_scopes(admin_authed):
    admin_authed.post("/v1/tenants", json={"name": "acme"})
    r = admin_authed.post(
        "/v1/tenants/acme/tokens",
        json={"scopes": ["exec", "file_read"]},
    )
    assert r.status_code == 201
    assert r.json()["scopes"] == ["exec", "file_read"]


def test_revoke_token(admin_authed):
    admin_authed.post("/v1/tenants", json={"name": "acme"})
    tok = admin_authed.post("/v1/tenants/acme/tokens", json={}).json()
    r = admin_authed.delete(f"/v1/tenants/acme/tokens/{tok['token_id']}")
    assert r.status_code == 204
    # The plaintext can no longer authenticate.
    admin_authed.headers.update({"Authorization": f"Bearer {tok['token']}"})
    assert admin_authed.post("/v1/sessions", json={}).status_code == 401


def test_revoke_token_404_for_other_tenant(admin_authed):
    admin_authed.post("/v1/tenants", json={"name": "acme"})
    tok = admin_authed.post("/v1/tenants/acme/tokens", json={}).json()
    # Try to revoke acme's token under a different tenant id.
    r = admin_authed.delete(f"/v1/tenants/default/tokens/{tok['token_id']}")
    assert r.status_code == 404


# ---------------------------------------------------------------------
# Usage
# ---------------------------------------------------------------------


def test_tenant_usage_admin_can_read_any(admin_authed):
    admin_authed.post("/v1/tenants", json={"name": "acme", "limits": {"max_concurrency": 7}})
    r = admin_authed.get("/v1/tenants/acme/usage")
    assert r.status_code == 200
    body = r.json()
    assert body["tenant_id"] == "acme"
    assert body["max_concurrency"] == 7
    assert body["concurrent_sessions"] == 0


def test_tenant_usage_owner_can_read_own(admin_client, admin_authed):
    """Owner (non-admin) reads only their own tenant's usage."""
    admin_authed.post("/v1/tenants", json={"name": "acme"})
    tok = admin_authed.post("/v1/tenants/acme/tokens", json={}).json()
    admin_client.headers.update({"Authorization": f"Bearer {tok['token']}"})
    r = admin_client.get("/v1/tenants/acme/usage")
    assert r.status_code == 200


def test_tenant_usage_owner_cannot_read_other(admin_client, admin_authed):
    admin_authed.post("/v1/tenants", json={"name": "acme"})
    tok = admin_authed.post("/v1/tenants/acme/tokens", json={}).json()
    admin_authed.post("/v1/tenants", json={"name": "rival"})
    admin_client.headers.update({"Authorization": f"Bearer {tok['token']}"})
    r = admin_client.get("/v1/tenants/rival/usage")
    assert r.status_code == 401
