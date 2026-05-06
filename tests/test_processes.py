"""Background-process service tests (slice 11b).

Drives `POST /v1/sessions/{sid}/processes` and friends through the
TestClient. The fake docker client tracks processes in memory; tests
flip them to "exited" via `fake_docker.simulate_process_exit(ospid)`.
"""

from __future__ import annotations


def _start(authed, sid, **kw):
    body = {"argv": kw.get("argv", ["sleep", "60"])}
    for f in ("name", "env", "cwd", "restart_policy"):
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
    # Row is gone afterwards.
    r2 = authed.get(f"/v1/sessions/{sid}/processes/{p['process_id']}")
    assert r2.status_code == 400
    assert r2.json()["detail"]["code"] == "invalid_argument"


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
