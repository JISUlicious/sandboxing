"""Slice 13c — activity-pin policy tests.

The discovery from Phase 1: prior to 13c, `last_activity_at` was bumped
ONLY by state transitions. On a stable RUNNING session, exec / file /
process ops did not bump activity, so the reaper would idle-stop the
session after `idle_stop_minutes` from create, regardless of workload.

After 13c, mutating ops and data-consumption reads bump activity. Pure
observation (status GETs, listings) deliberately doesn't.

These tests assert pin/no-pin per route under both flag states:
- `pin_on_activity=True` (default): new behaviour, mutations and reads pin.
- `pin_on_activity=False`: legacy behaviour, only transitions pin.
"""

from __future__ import annotations

import base64
import time

import pytest


def _activity_before_and_after(authed, sid: str, op_callable) -> tuple[int, int]:
    """Read last_activity_at, force a back-date so the bump is
    observable, then run the op and read last_activity_at again.
    Returns (before_ms, after_ms)."""
    before = authed.get(f"/v1/sessions/{sid}").json()["last_activity_at"]
    # Backdate the row in the registry so a same-millisecond touch
    # would be observable.
    import asyncio

    import aiosqlite

    backdate = before - 60_000  # 1 minute earlier

    async def _backdate() -> None:
        # Reach the test app's DB via the client.
        path = authed.app.state.settings.db_path
        async with aiosqlite.connect(path) as db:
            await db.execute(
                "UPDATE sessions SET last_activity_at = ? WHERE id = ?",
                (backdate, sid),
            )
            await db.commit()

    asyncio.run(_backdate())
    op_callable()
    after = authed.get(f"/v1/sessions/{sid}").json()["last_activity_at"]
    return backdate, after


# ----- mutations + reads that SHOULD pin -----


def test_exec_on_running_pins(authed, fake_docker):
    """The headline fix: exec on an already-RUNNING session bumps
    last_activity_at. Before 13c this didn't happen and busy sessions
    got idle-reaped."""
    sid = authed.post("/v1/sessions", json={}).json()["session_id"]
    before, after = _activity_before_and_after(
        authed,
        sid,
        lambda: authed.post(f"/v1/sessions/{sid}/exec", json={"argv": ["/bin/true"]}),
    )
    assert after > before, "exec should bump last_activity_at"


def test_exec_stream_pins(authed, fake_docker):
    sid = authed.post("/v1/sessions", json={}).json()["session_id"]
    fake_docker.stream_exec_scripts.append([("stdout", b"x"), ("exit", 0)])
    before, after = _activity_before_and_after(
        authed,
        sid,
        lambda: authed.post(f"/v1/sessions/{sid}/exec/stream", json={"argv": ["/bin/true"]}),
    )
    assert after > before


def test_file_write_pins(authed, fake_docker):
    sid = authed.post("/v1/sessions", json={}).json()["session_id"]
    payload = base64.b64encode(b"hello").decode("ascii")
    before, after = _activity_before_and_after(
        authed,
        sid,
        lambda: authed.post(
            f"/v1/sessions/{sid}/files",
            json={"path": "x.txt", "content_b64": payload, "mode": 0o640},
        ),
    )
    assert after > before


def test_file_read_pins(authed, fake_docker):
    sid = authed.post("/v1/sessions", json={}).json()["session_id"]
    fake_docker.get_archive_responses["/workspace/x.txt"] = (b"hi", 0o644)
    before, after = _activity_before_and_after(
        authed,
        sid,
        lambda: authed.get(f"/v1/sessions/{sid}/files/x.txt"),
    )
    assert after > before


def test_file_delete_pins(authed, fake_docker):
    sid = authed.post("/v1/sessions", json={}).json()["session_id"]

    # File ops: simulate `test -e` ok, `test -d` non-zero (it's a file),
    # then rm succeeds. Without this, delete returns 400 (is-dir without
    # ?recursive=true) and never reaches the bump call.
    def handler(argv):
        if argv[:2] == ["/usr/bin/test", "-d"]:
            return (b"", b"", 1)
        return (b"", b"", 0)

    fake_docker.simple_exec_handler = handler
    before, after = _activity_before_and_after(
        authed,
        sid,
        lambda: authed.delete(f"/v1/sessions/{sid}/files/x.txt"),
    )
    assert after > before


def test_process_start_pins(authed, fake_docker):
    sid = authed.post("/v1/sessions", json={}).json()["session_id"]
    before, after = _activity_before_and_after(
        authed,
        sid,
        lambda: authed.post(f"/v1/sessions/{sid}/processes", json={"argv": ["/bin/sleep", "1"]}),
    )
    assert after > before


def test_process_delete_pins(authed, fake_docker):
    sid = authed.post("/v1/sessions", json={}).json()["session_id"]
    pid = authed.post(f"/v1/sessions/{sid}/processes", json={"argv": ["/bin/sleep", "1"]}).json()[
        "process_id"
    ]
    before, after = _activity_before_and_after(
        authed,
        sid,
        lambda: authed.delete(f"/v1/sessions/{sid}/processes/{pid}"),
    )
    assert after > before


# ----- observations that should NOT pin -----


def test_get_session_does_not_pin(authed, fake_docker):
    sid = authed.post("/v1/sessions", json={}).json()["session_id"]
    before, after = _activity_before_and_after(
        authed,
        sid,
        lambda: authed.get(f"/v1/sessions/{sid}"),
    )
    assert after == before, "pure status GET should not pin"


def test_file_list_does_not_pin(authed, fake_docker):
    sid = authed.post("/v1/sessions", json={}).json()["session_id"]
    fake_docker.simple_exec_handler = lambda argv: (b"", b"", 0)
    before, after = _activity_before_and_after(
        authed,
        sid,
        lambda: authed.get(f"/v1/sessions/{sid}/files"),
    )
    assert after == before


def test_process_list_does_not_pin(authed, fake_docker):
    sid = authed.post("/v1/sessions", json={}).json()["session_id"]
    before, after = _activity_before_and_after(
        authed,
        sid,
        lambda: authed.get(f"/v1/sessions/{sid}/processes"),
    )
    assert after == before


def test_process_get_does_not_pin(authed, fake_docker):
    sid = authed.post("/v1/sessions", json={}).json()["session_id"]
    pid = authed.post(f"/v1/sessions/{sid}/processes", json={"argv": ["/bin/sleep", "1"]}).json()[
        "process_id"
    ]
    before, after = _activity_before_and_after(
        authed,
        sid,
        lambda: authed.get(f"/v1/sessions/{sid}/processes/{pid}"),
    )
    assert after == before


# ----- flag-off (legacy) behaviour -----


@pytest.fixture
def authed_pin_off(settings, fake_docker, monkeypatch):
    """Build a parallel TestClient with pin_on_activity=False so we can
    verify the legacy behaviour is recoverable."""
    from fastapi.testclient import TestClient

    from api.audit import AuditEmitter
    from api.config import Settings
    from api.registry import Registry
    from api.server import create_app
    from api.sessions import SessionService

    # Fresh settings + DB so the two clients don't share state.
    off_settings = Settings(
        dev_mode=True,
        api_token="test-token",
        db_path=settings.db_path.parent / "off.db",
        audit_log_path=settings.audit_log_path.parent / "off.log",
        pin_on_activity=False,
    )
    fake_docker._settings = off_settings
    service = SessionService(
        settings=off_settings,
        registry=Registry(off_settings.db_path),
        docker=fake_docker,
        audit=AuditEmitter(off_settings.audit_log_path),
    )
    app = create_app(off_settings, service=service, start_reaper=False)
    with TestClient(app) as c:
        c.headers.update({"Authorization": "Bearer test-token"})
        yield c


def test_flag_off_exec_does_not_pin(authed_pin_off, fake_docker):
    """When SANDBOX_PIN_ON_ACTIVITY=False, exec on RUNNING does not
    bump — restoring the pre-13c semantic."""
    sid = authed_pin_off.post("/v1/sessions", json={}).json()["session_id"]
    before, after = _activity_before_and_after(
        authed_pin_off,
        sid,
        lambda: authed_pin_off.post(f"/v1/sessions/{sid}/exec", json={"argv": ["/bin/true"]}),
    )
    assert after == before, "with pin_on_activity=False, exec must NOT pin"


# ----- registry-level invariants -----


async def test_touch_activity_no_op_on_stopped(settings, service):
    """Touching a STOPPED row leaves last_activity_at unchanged —
    semantic guard so the touch path can't reach into terminal-ish
    rows."""
    await service.registry.init()
    row = await service.create(tenant_id="default", limits=None)
    await service.stop(row.id, "default")
    row_after_stop = await service.registry.get(row.id, "default")
    activity_after_stop = row_after_stop.last_activity_at

    # Sleep one tick so the next now_ms differs.
    time.sleep(0.005)
    await service.registry.touch_activity(row.id)

    row_after_touch = await service.registry.get(row.id, "default")
    assert row_after_touch.last_activity_at == activity_after_stop
