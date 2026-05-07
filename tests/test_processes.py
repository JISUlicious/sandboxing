"""Background-process service tests (slice 11b).

Drives `POST /v1/sessions/{sid}/processes` and friends through the
TestClient. The fake docker client tracks processes in memory; tests
flip them to "exited" via `fake_docker.simulate_process_exit(ospid)`.
"""

from __future__ import annotations


def _start(authed, sid, **kw):
    body = {"argv": kw.get("argv", ["sleep", "60"])}
    for f in ("name", "env", "cwd"):
        if f in kw:
            body[f] = kw[f]
    r = authed.post(f"/v1/sessions/{sid}/processes", json=body)
    return r


def _create_session(authed, **limits):
    body = {}
    if limits:
        body["limits"] = limits
    r = authed.post("/v1/sessions", json=body)
    assert r.status_code == 201, r.text
    return r.json()["session_id"]


# ---------------------------------------------------------------------
# Lifecycle round-trip
# ---------------------------------------------------------------------


def test_start_returns_running(authed):
    sid = _create_session(authed)
    r = _start(authed, sid, argv=["sleep", "10"], name="napper")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["state"] == "RUNNING"
    assert body["argv"] == ["sleep", "10"]
    assert body["name"] == "napper"
    assert body["exit_code"] is None


def test_list_includes_started_process(authed):
    sid = _create_session(authed)
    p = _start(authed, sid).json()
    r = authed.get(f"/v1/sessions/{sid}/processes")
    assert r.status_code == 200
    pids = [e["process_id"] for e in r.json()["entries"]]
    assert p["process_id"] in pids


def test_get_refreshes_state_on_exit(authed, fake_docker):
    sid = _create_session(authed)
    p = _start(authed, sid).json()
    # Simulate the underlying process exiting cleanly.
    ospid = fake_docker.spawn_supervised_calls[-1]["ospid"]
    fake_docker.simulate_process_exit(ospid, exit_code=0)
    r = authed.get(f"/v1/sessions/{sid}/processes/{p['process_id']}")
    body = r.json()
    assert body["state"] == "EXITED"
    assert body["exit_code"] == 0


def test_delete_kills_running_process(authed, fake_docker):
    sid = _create_session(authed)
    p = _start(authed, sid).json()
    ospid = fake_docker.spawn_supervised_calls[-1]["ospid"]
    r = authed.delete(f"/v1/sessions/{sid}/processes/{p['process_id']}")
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "EXITED"
    # Real signal was sent (SIGTERM at minimum).
    sigs = [s for cid, op, s in fake_docker.signal_pid_calls if op == ospid]
    assert 15 in sigs
    # Row is gone afterwards. v0.1.8: 404 process_not_found, NOT
    # 400 invalid_argument — "this resource doesn't exist" isn't a
    # malformed request.
    r2 = authed.get(f"/v1/sessions/{sid}/processes/{p['process_id']}")
    assert r2.status_code == 404
    assert r2.json()["detail"]["code"] == "process_not_found"


def test_delete_already_exited_is_idempotent(authed, fake_docker):
    sid = _create_session(authed)
    p = _start(authed, sid).json()
    ospid = fake_docker.spawn_supervised_calls[-1]["ospid"]
    fake_docker.simulate_process_exit(ospid, exit_code=0)
    r = authed.delete(f"/v1/sessions/{sid}/processes/{p['process_id']}")
    assert r.status_code == 200
    assert r.json()["state"] == "EXITED"


# ---------------------------------------------------------------------
# Concurrency cap
# ---------------------------------------------------------------------


def test_max_processes_caps_concurrent_starts(authed):
    sid = _create_session(authed, max_processes=2)
    r1 = _start(authed, sid)
    r2 = _start(authed, sid)
    r3 = _start(authed, sid)
    assert r1.status_code == 201
    assert r2.status_code == 201
    assert r3.status_code == 429
    assert r3.json()["detail"]["code"] == "limit_exceeded"


# ---------------------------------------------------------------------
# Cross-tenant isolation
# ---------------------------------------------------------------------


async def test_cross_tenant_process_isolation(authed, client, settings, service):
    from api.auth import TokenAuthenticator, generate_token_plaintext

    sid = _create_session(authed)
    p = _start(authed, sid).json()

    # Issue a token for tenant alice.
    authn = TokenAuthenticator(settings=settings, registry=service.registry)
    await service.registry.create_tenant("alice", "Alice's team")
    plaintext = generate_token_plaintext()
    await authn.issue_initial_token("alice", plaintext)

    client.headers["Authorization"] = f"Bearer {plaintext}"
    # Alice can't list default-tenant's processes.
    r = client.get(f"/v1/sessions/{sid}/processes")
    assert r.status_code == 404
    # Or fetch by id.
    r = client.get(f"/v1/sessions/{sid}/processes/{p['process_id']}")
    assert r.status_code == 404


# ---------------------------------------------------------------------
# Idle-stop guard
# ---------------------------------------------------------------------


async def test_idle_reaper_skips_session_with_running_process(authed, settings, service):
    from api.reaper import Reaper

    sid = _create_session(authed)
    _start(authed, sid).json()

    # Backdate last_activity_at so the session looks idle.
    import aiosqlite

    async with aiosqlite.connect(settings.db_path) as db:
        await db.execute("UPDATE sessions SET last_activity_at = 0 WHERE id = ?", (sid,))
        await db.commit()

    reaper = Reaper(settings=settings, registry=service.registry, sessions=service)
    await reaper.tick()

    # Session must still be RUNNING — the running process kept it
    # off the idle-stop list.
    async with aiosqlite.connect(settings.db_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT status FROM sessions WHERE id = ?", (sid,))
        row = await cur.fetchone()
    assert row is not None
    assert row["status"] == "RUNNING"


# ---------------------------------------------------------------------
# Destroy reaps processes
# ---------------------------------------------------------------------


def test_session_destroy_kills_running_processes(authed, fake_docker):
    sid = _create_session(authed)
    _start(authed, sid).json()
    ospid = fake_docker.spawn_supervised_calls[-1]["ospid"]

    r = authed.delete(f"/v1/sessions/{sid}")
    assert r.status_code == 204

    sigs = [s for cid, op, s in fake_docker.signal_pid_calls if op == ospid]
    assert 9 in sigs  # SIGKILL fired during the destroy hook.


# ---------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------


def test_absolute_cwd_rejected(authed):
    sid = _create_session(authed)
    r = _start(authed, sid, argv=["true"], cwd="/etc")
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "invalid_argument"


def test_dotdot_cwd_rejected(authed):
    sid = _create_session(authed)
    r = _start(authed, sid, argv=["true"], cwd="../escape")
    assert r.status_code == 400


# Slice 11c: log streaming SSE


def test_log_stream_emits_sse_chunks(authed, fake_docker):
    import base64
    import json as _json

    sid = _create_session(authed)
    p = _start(authed, sid).json()
    spawn = fake_docker.spawn_supervised_calls[-1]
    container_id = fake_docker.created_containers[-1][0]
    fake_docker.write_log_in_container(container_id, spawn["log_path"], "hello\n")

    r = authed.get(f"/v1/sessions/{sid}/processes/{p['process_id']}/logs")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    body = r.text
    assert "event: log" in body
    data_line = next(line for line in body.splitlines() if line.startswith("data:"))
    payload = _json.loads(data_line.removeprefix("data:").strip())
    assert base64.b64decode(payload["chunk_b64"]) == b"hello\n"


# Slice 11c: cross-cutting ExecResponse fields


def test_exec_response_includes_truncation_cap_and_resume(authed):
    sid = _create_session(authed)
    body = authed.post(f"/v1/sessions/{sid}/exec", json={"argv": ["echo", "hi"]}).json()
    assert body["effective_truncation_cap_bytes"] == 8 * 1024 * 1024
    assert body["resume_latency_ms"] == 0


def test_resume_latency_populated_on_stopped_session(authed):
    sid = _create_session(authed)
    authed.post(f"/v1/sessions/{sid}/stop")
    body = authed.post(f"/v1/sessions/{sid}/exec", json={"argv": ["echo", "hi"]}).json()
    assert body["resume_latency_ms"] >= 0


# Slice 11c: InvalidPath sub-codes


def test_invalid_path_sub_codes(authed):
    sid = _create_session(authed)
    r = authed.post(
        f"/v1/sessions/{sid}/files",
        json={"path": "/etc/passwd", "content_b64": "Cg=="},
    )
    assert r.status_code == 400
    assert r.json()["detail"]["sub_code"] == "absolute_path"

    r = authed.post(
        f"/v1/sessions/{sid}/files",
        json={"path": "../../tmp/foo", "content_b64": "Cg=="},
    )
    assert r.status_code == 400
    assert r.json()["detail"]["sub_code"] == "escaped_workspace"

    # workspace_root is hard to trigger via HTTP because URL
    # normalization eats `/.`-style segments before they reach the
    # route handler. Coverage exists at the unit level via the
    # _resolve_workspace_path helper.


# ---------------------------------------------------------------------
# v0.1.8 customer-audit regressions
# ---------------------------------------------------------------------


def test_exit_code_populated_after_clean_exit(authed, fake_docker):
    """Bug #10: state=EXITED but exit_code=null was caused by the bash
    supervisor `exec`-ing argv after setting `trap ... EXIT` — the
    trap-bearing bash was gone before the child exited, so exit_path
    was never written. The fixed supervisor backgrounds the child,
    `wait`s, and writes $? to exit_path right after the child dies.
    Locks down the contract: state=EXITED → exit_code is the actual
    integer the child returned."""
    sid = _create_session(authed)
    p = _start(authed, sid).json()
    ospid = fake_docker.spawn_supervised_calls[-1]["ospid"]
    fake_docker.simulate_process_exit(ospid, exit_code=7)
    r = authed.get(f"/v1/sessions/{sid}/processes/{p['process_id']}")
    body = r.json()
    assert body["state"] == "EXITED"
    assert body["exit_code"] == 7, "exit_code must reflect child's actual return value"


def test_missing_process_returns_404_process_not_found(authed):
    """Bug #11: looking up a process that doesn't exist (or was deleted)
    returned `400 invalid_argument`; should be `404 process_not_found`."""
    sid = _create_session(authed)
    r = authed.get(f"/v1/sessions/{sid}/processes/01ABCDEFGHIJKLMNOP1234567X")
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "process_not_found"


def test_per_field_limits_violation_returns_400(authed):
    """Bug #13: per-field request limits exceeding tenant caps returned
    `429 limit_exceeded`. SPEC-100 says it must be 400 — bad client
    input, not rate limiting. The `limit_exceeded` code is preserved
    so existing client error handling still matches."""
    r = authed.post("/v1/sessions", json={"limits": {"exec_timeout_s": 99999}})
    assert r.status_code == 400
    body = r.json()
    assert body["detail"]["code"] == "limit_exceeded"
    assert "exec_timeout_s" in body["detail"]["message"]
    assert "exceeds tenant max" in body["detail"]["message"]


def test_concurrency_cap_still_returns_429(authed):
    """Companion to the bug #13 fix — make sure the *legitimate* 429
    case (tenant concurrency cap, retry-might-help) didn't regress to
    400 in the rename. v0.1.0 test_limit_exceeded_returns_429 covers
    this in test_lifecycle.py too; we keep one here so the split is
    visible in both files."""
    # Concurrency cap is hard to hit in this fixture (default 50);
    # just verify the existing per-session process cap, which uses the
    # same LimitExceeded class, still returns 429.
    sid = _create_session(authed, max_processes=1)
    _start(authed, sid)  # 1st succeeds
    r = _start(authed, sid)  # 2nd hits the cap
    assert r.status_code == 429
    assert r.json()["detail"]["code"] == "limit_exceeded"
