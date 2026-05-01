"""Tests for the exec endpoint (slice 2). SPEC-201 / SPEC-203 / SPEC-301."""

from api.docker_client import TIMEOUT_EXIT_CODE, ExecOutput


def _create(authed) -> str:
    return authed.post("/v1/sessions", json={}).json()["session_id"]


def test_exec_happy_path(authed, fake_docker):
    sid = _create(authed)
    fake_docker.exec_responses.append(
        ExecOutput(stdout=b"hello\n", stderr=b"", exit_code=0, duration_ms=12)
    )
    r = authed.post(
        f"/v1/sessions/{sid}/exec",
        json={"argv": ["echo", "hello"]},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["stdout"] == "hello\n"
    assert body["exit_code"] == 0
    assert body["truncated"] is False
    assert body["effective_timeout_s"] == 60  # default exec timeout

    call = fake_docker.exec_calls[0]
    assert call["argv"] == ["echo", "hello"]
    assert call["timeout_s"] == 60


def test_exec_clamps_timeout_to_tenant_max(authed, fake_docker):
    sid = _create(authed)
    fake_docker.exec_responses.append(
        ExecOutput(stdout=b"", stderr=b"", exit_code=0, duration_ms=1)
    )
    r = authed.post(
        f"/v1/sessions/{sid}/exec",
        json={"argv": ["true"], "timeout_s": 10_000},
    )
    assert r.status_code == 200
    assert r.json()["effective_timeout_s"] == 600
    assert fake_docker.exec_calls[0]["timeout_s"] == 600


def test_exec_empty_argv_is_400(authed):
    sid = _create(authed)
    r = authed.post(f"/v1/sessions/{sid}/exec", json={"argv": []})
    assert r.status_code == 422  # pydantic validation rejects min_length=1


def test_exec_stdin_not_yet_supported(authed):
    sid = _create(authed)
    r = authed.post(
        f"/v1/sessions/{sid}/exec",
        json={"argv": ["cat"], "stdin": "hi"},
    )
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "invalid_argument"


def test_exec_forbidden_env_keys_rejected(authed):
    sid = _create(authed)
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY"):
        r = authed.post(
            f"/v1/sessions/{sid}/exec",
            json={"argv": ["true"], "env": {key: "evil"}},
        )
        assert r.status_code == 400, key
        assert r.json()["detail"]["code"] == "invalid_argument"


def test_exec_timeout_returns_408(authed, fake_docker):
    sid = _create(authed)
    fake_docker.exec_responses.append(
        ExecOutput(stdout=b"", stderr=b"", exit_code=TIMEOUT_EXIT_CODE, duration_ms=60_000)
    )
    r = authed.post(f"/v1/sessions/{sid}/exec", json={"argv": ["sleep", "999"]})
    assert r.status_code == 408
    assert r.json()["detail"]["code"] == "exec_timeout"


def test_exec_truncation_propagates(authed, fake_docker):
    sid = _create(authed)
    fake_docker.exec_responses.append(
        ExecOutput(
            stdout=b"x" * 10,
            stderr=b"",
            exit_code=0,
            duration_ms=20,
            truncated_streams=["stdout"],
        )
    )
    r = authed.post(f"/v1/sessions/{sid}/exec", json={"argv": ["yes"]})
    assert r.status_code == 200
    body = r.json()
    assert body["truncated"] is True
    assert body["truncated_streams"] == ["stdout"]


def test_exec_transparently_resumes_stopped_session(authed, fake_docker):
    sid = _create(authed)
    authed.post(f"/v1/sessions/{sid}/stop")
    fake_docker.exec_responses.append(
        ExecOutput(stdout=b"ok\n", stderr=b"", exit_code=0, duration_ms=5)
    )
    r = authed.post(f"/v1/sessions/{sid}/exec", json={"argv": ["echo", "ok"]})
    assert r.status_code == 200
    # Container was started twice: once at create, once after the stop.
    # The fake docker tracks each start call.
    assert len(fake_docker.started) >= 2


def test_exec_on_destroyed_session_is_404(authed):
    sid = _create(authed)
    authed.delete(f"/v1/sessions/{sid}")
    r = authed.post(f"/v1/sessions/{sid}/exec", json={"argv": ["true"]})
    assert r.status_code == 404


def test_output_cap_helper_caps_independently():
    """Whitebox: per-stream cap applies to stdout and stderr independently."""
    from api.docker_client import OUTPUT_CAP_BYTES, _append_capped

    truncated: set[str] = set()
    stdout = bytearray()
    stderr = bytearray()
    _append_capped(stdout, b"x" * (OUTPUT_CAP_BYTES + 100), "stdout", truncated)
    _append_capped(stderr, b"y" * 1000, "stderr", truncated)
    assert len(stdout) == OUTPUT_CAP_BYTES
    assert "stdout" in truncated
    assert len(stderr) == 1000
    assert "stderr" not in truncated
