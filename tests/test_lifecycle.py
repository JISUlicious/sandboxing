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


def test_create_normalizes_workspace_perms_after_start(authed, fake_docker):
    """v0.1.7: SessionService.create runs DockerClient.normalize_workspace_perms
    after start_container so /workspace is agent-owned even when the host
    fs silently dropped our bind-side chown (e.g., Apple-served SMB)."""
    r = authed.post("/v1/sessions", json={})
    assert r.status_code == 201, r.text
    cid = fake_docker.created_containers[0][0]
    # Both calls happened, in order: start then normalize.
    assert fake_docker.started == [cid]
    assert fake_docker.workspace_perm_calls == [cid]


def test_image_not_found_returns_structured_503(authed, fake_docker):
    """Issue #9 from the e2e — when docker-py raises ImageNotFound the
    control plane previously surfaced a plain-text 500. After the
    exception handler ships, the response is a structured 503 with the
    `image_not_found` code so clients can distinguish 'transient daemon
    issue, retry once you've pulled the image' from a programming bug."""
    import docker.errors as docker_errors

    def boom(**_kw: object) -> str:
        raise docker_errors.ImageNotFound("image ghcr.io/x/y:tag not found locally")

    fake_docker.create_container = boom  # type: ignore[method-assign]
    r = authed.post("/v1/sessions", json={})
    assert r.status_code == 503, r.text
    body = r.json()
    assert body["detail"]["code"] == "image_not_found"
    assert "not found" in body["detail"]["message"].lower()


def test_docker_api_error_returns_structured_503(authed, fake_docker):
    """Other docker.errors.APIError subclasses (daemon down, network
    issues, conflict, etc.) also surface as 503 with `docker_api_error`."""
    import docker.errors as docker_errors

    def boom(**_kw: object) -> str:
        raise docker_errors.APIError("Bad Gateway from daemon")

    fake_docker.create_container = boom  # type: ignore[method-assign]
    r = authed.post("/v1/sessions", json={})
    assert r.status_code == 503, r.text
    assert r.json()["detail"]["code"] == "docker_api_error"


def test_per_field_limits_violation_returns_400(authed):
    """SPEC-100: per-field request limits exceeding tenant caps are
    400, not 429. (Pre-v0.1.8 this asserted 429 — the customer audit
    Bug #13 flagged it as wrong against SPEC-100, since 429 implies
    "retry might help" but a malformed body never will.) Code stays
    `limit_exceeded` so existing error handling still matches."""
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
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "limit_exceeded"
    assert "vcpu" in r.json()["detail"]["message"]


# ----- Slice 13b — destroy / idle-stop ETAs in SessionResponse -----


def test_create_response_includes_etas(authed, settings):
    """A freshly-created RUNNING session has both ETAs populated:
    idle_stop_at = last_activity_at + idle_stop_minutes,
    hard_destroy_at = last_activity_at + hard_destroy_hours."""
    r = authed.post("/v1/sessions", json={})
    assert r.status_code == 201, r.text
    body = r.json()

    assert body["status"] == "RUNNING"
    assert body["idle_stop_at"] is not None
    assert body["hard_destroy_at"] is not None
    expected_idle = body["last_activity_at"] + settings.idle_stop_minutes * 60_000
    expected_hard = body["last_activity_at"] + settings.hard_destroy_hours * 3_600_000
    assert body["idle_stop_at"] == expected_idle
    assert body["hard_destroy_at"] == expected_hard


def test_idle_stop_at_is_null_for_stopped(authed, settings):
    """STOPPED sessions still have hard_destroy_at (TTL applies) but
    no idle_stop_at (the reaper doesn't idle-stop a stopped row)."""
    sid = authed.post("/v1/sessions", json={}).json()["session_id"]
    r = authed.post(f"/v1/sessions/{sid}/stop")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "STOPPED"
    assert body["idle_stop_at"] is None
    assert body["hard_destroy_at"] is not None
    expected_hard = body["last_activity_at"] + settings.hard_destroy_hours * 3_600_000
    assert body["hard_destroy_at"] == expected_hard


def test_idle_stop_at_returns_after_resume(authed, settings):
    """Resuming a STOPPED session brings idle_stop_at back."""
    sid = authed.post("/v1/sessions", json={}).json()["session_id"]
    authed.post(f"/v1/sessions/{sid}/stop")
    r = authed.post(f"/v1/sessions/{sid}/resume")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "RUNNING"
    assert body["idle_stop_at"] is not None


def test_etas_shift_forward_after_activity(authed, settings):
    """Mutating ops bump last_activity_at; both ETAs shift forward by
    the same delta (they're derived from last_activity_at)."""
    sid = authed.post("/v1/sessions", json={}).json()["session_id"]
    r1 = authed.get(f"/v1/sessions/{sid}")
    a1 = r1.json()["last_activity_at"]
    eta1_idle = r1.json()["idle_stop_at"]
    eta1_hard = r1.json()["hard_destroy_at"]

    # Trigger a mutating op (exec). The fake docker exec defaults to
    # exit 0 + empty stdout.
    r_exec = authed.post(f"/v1/sessions/{sid}/exec", json={"argv": ["/bin/true"]})
    assert r_exec.status_code == 200

    r2 = authed.get(f"/v1/sessions/{sid}")
    a2 = r2.json()["last_activity_at"]
    eta2_idle = r2.json()["idle_stop_at"]
    eta2_hard = r2.json()["hard_destroy_at"]

    # Both ETAs shifted by exactly the activity delta. If
    # last_activity_at didn't move (unlikely but possible on
    # sub-millisecond exec), the ETAs shouldn't have moved either.
    delta = a2 - a1
    assert eta2_idle - eta1_idle == delta
    assert eta2_hard - eta1_hard == delta
