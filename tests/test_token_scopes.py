"""Slice 12 — per-route scope enforcement tests.

A token issued with `scopes=["exec","file_read"]` should be able to
exec and read files on its tenant's sessions, but not write files
nor destroy sessions.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from api.config import Settings
from api.server import create_app
from api.sessions import SessionService


@pytest.fixture
def admin_settings(tmp_path) -> Settings:
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


def _issue_scoped_token(admin_client, scopes):
    admin_client.headers.update({"Authorization": "Bearer admin-token"})
    body = admin_client.post("/v1/tenants", json={"name": "scoped"}).json()
    assert body["tenant_id"] == "scoped"
    tok = admin_client.post("/v1/tenants/scoped/tokens", json={"scopes": scopes}).json()
    return tok["token"]


def test_back_compat_default_token_has_all_scopes(admin_client):
    """Tokens without an explicit scopes list (the bootstrap default
    tenant token, in particular) keep working on every endpoint."""
    admin_client.headers.update({"Authorization": "Bearer default-tenant-token"})
    sid = admin_client.post("/v1/sessions", json={}).json()["session_id"]
    assert (
        admin_client.post(f"/v1/sessions/{sid}/exec", json={"argv": ["echo", "hi"]}).status_code
        == 200
    )
    assert (
        admin_client.post(
            f"/v1/sessions/{sid}/files",
            json={"path": "x", "content_b64": "aGk="},
        ).status_code
        == 201
    )


def test_scoped_token_can_only_exec_and_read(admin_client):
    """A token limited to exec + file_read can do those things on its
    own tenant's session, but is forbidden on file_write / destroy."""
    plaintext = _issue_scoped_token(admin_client, ["exec", "file_read", "session_create"])
    admin_client.headers.update({"Authorization": f"Bearer {plaintext}"})

    # Create + exec succeed (session_create + exec are scoped in).
    sid = admin_client.post("/v1/sessions", json={}).json()["session_id"]
    assert admin_client.post(f"/v1/sessions/{sid}/exec", json={"argv": ["true"]}).status_code == 200

    # file_write is denied.
    r = admin_client.post(
        f"/v1/sessions/{sid}/files",
        json={"path": "x", "content_b64": "aGk="},
    )
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "forbidden_scope"
    assert "file_write" in r.json()["detail"]["message"]

    # session_destroy is denied.
    r = admin_client.delete(f"/v1/sessions/{sid}")
    assert r.status_code == 403


def test_empty_scopes_grants_nothing_protected(admin_client):
    """`scopes=[]` is the explicit "no scopes" form. The token can
    still pass auth on un-scoped routes (session_get, stop, resume)
    but no scope-protected route lets it through."""
    plaintext = _issue_scoped_token(admin_client, [])
    admin_client.headers.update({"Authorization": f"Bearer {plaintext}"})

    r = admin_client.post("/v1/sessions", json={})
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "forbidden_scope"


def test_processes_umbrella_scope(admin_client):
    """All process routes share one `processes` scope."""
    # Issue a token with everything BUT processes.
    plaintext = _issue_scoped_token(
        admin_client,
        ["session_create", "exec", "file_read", "file_write", "file_delete"],
    )
    admin_client.headers.update({"Authorization": f"Bearer {plaintext}"})

    sid = admin_client.post("/v1/sessions", json={}).json()["session_id"]
    r = admin_client.post(
        f"/v1/sessions/{sid}/processes",
        json={"argv": ["sleep", "10"]},
    )
    assert r.status_code == 403
    assert "processes" in r.json()["detail"]["message"]


def test_admin_token_passes_every_scope(admin_client):
    """Admin tokens are exempt from scope checks on the regular surface
    too (so an operator with the admin token can debug any endpoint)."""
    admin_client.headers.update({"Authorization": "Bearer admin-token"})
    # The admin tenant has its own __admin__ session pool — admin
    # tokens can create sessions there.
    r = admin_client.post("/v1/sessions", json={})
    assert r.status_code == 201, r.text


def test_session_get_stop_resume_unscoped(admin_client):
    """Session inspection + lifecycle (get, stop, resume) are not
    behind any scope — any tenant token can manage its own
    sessions."""
    plaintext = _issue_scoped_token(admin_client, ["session_create"])
    admin_client.headers.update({"Authorization": f"Bearer {plaintext}"})
    sid = admin_client.post("/v1/sessions", json={}).json()["session_id"]
    # get
    assert admin_client.get(f"/v1/sessions/{sid}").status_code == 200
    # stop
    assert admin_client.post(f"/v1/sessions/{sid}/stop").status_code == 200
    # resume
    assert admin_client.post(f"/v1/sessions/{sid}/resume").status_code == 200
