"""End-to-end lifecycle tests against the FastAPI app with a faked Docker."""


def test_health_no_auth(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_readyz_reports_docker_and_audit(client):
    r = client.get("/readyz")
    assert r.status_code == 200
    assert r.json() == {"docker": True, "audit": True}


def test_create_requires_auth(client):
    r = client.post("/v1/sessions", json={})
    assert r.status_code == 401
    assert r.json()["detail"]["code"] == "unauthorized"


def test_create_get_stop_resume_destroy_happy_path(authed, fake_docker):
    r = authed.post("/v1/sessions", json={})
    assert r.status_code == 201, r.text
    body = r.json()
    sid = body["session_id"]
    assert body["status"] == "RUNNING"
    assert body["tenant_id"] == "default"
    assert body["limits"]["vcpu"] == 2

    # docker side-effects (volume + container created + started)
    assert len(fake_docker.created_volumes) == 1
    assert len(fake_docker.created_containers) == 1
    assert len(fake_docker.started) == 1

    r = authed.get(f"/v1/sessions/{sid}")
    assert r.status_code == 200
    assert r.json()["status"] == "RUNNING"

    r = authed.post(f"/v1/sessions/{sid}/stop")
    assert r.status_code == 200
    assert r.json()["status"] == "STOPPED"
    assert len(fake_docker.stopped) == 1

    r = authed.post(f"/v1/sessions/{sid}/resume")
    assert r.status_code == 200
    assert r.json()["status"] == "RUNNING"
    assert len(fake_docker.started) == 2

    r = authed.delete(f"/v1/sessions/{sid}")
    assert r.status_code == 204
    assert len(fake_docker.removed_containers) == 1
    assert len(fake_docker.removed_volumes) == 1

    # SPEC-200: destroyed sessions return 404, not 410 / 409.
    r = authed.get(f"/v1/sessions/{sid}")
    assert r.status_code == 404


def test_stop_when_already_stopped_returns_409(authed):
    sid = authed.post("/v1/sessions", json={}).json()["session_id"]
    authed.post(f"/v1/sessions/{sid}/stop")
    r = authed.post(f"/v1/sessions/{sid}/stop")
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "invalid_state"


def test_resume_when_running_returns_409(authed):
    sid = authed.post("/v1/sessions", json={}).json()["session_id"]
    r = authed.post(f"/v1/sessions/{sid}/resume")
    assert r.status_code == 409


def test_unknown_session_returns_404(authed):
    r = authed.get("/v1/sessions/nonexistent")
    assert r.status_code == 404


def test_limit_exceeded_returns_429(authed):
    r = authed.post(
        "/v1/sessions",
        json={
            "limits": {
                "vcpu": 100,  # tenant max is 4
                "memory_mib": 256,
                "workspace_mib": 256,
                "pids": 256,
                "nofile": 1024,
                "exec_timeout_s": 60,
            }
        },
    )
    assert r.status_code == 429
    assert r.json()["detail"]["code"] == "limit_exceeded"
