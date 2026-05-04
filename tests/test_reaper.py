"""Reaper tests (slice 4). SPEC §6 idle-stop + hard-destroy."""

import time

import aiosqlite
import pytest

from api.reaper import Reaper


def _create(authed) -> str:
    return authed.post("/v1/sessions", json={}).json()["session_id"]


async def _force_timestamps(
    db_path,
    session_id: str,
    *,
    last_activity_ms: int | None = None,
    created_ms: int | None = None,
) -> None:
    """Backdate a session's timestamps so reaper sees it as a candidate."""
    async with aiosqlite.connect(db_path) as db:
        if last_activity_ms is not None:
            await db.execute(
                "UPDATE sessions SET last_activity_at = ? WHERE id = ?",
                (last_activity_ms, session_id),
            )
        if created_ms is not None:
            await db.execute(
                "UPDATE sessions SET created_at = ? WHERE id = ?",
                (created_ms, session_id),
            )
        await db.commit()


def _reaper(client) -> Reaper:
    return client.app.state.reaper


@pytest.mark.asyncio
async def test_tick_idle_stops_old_running_session(authed, fake_docker, settings, client):
    sid = _create(authed)
    # Backdate last_activity 30 minutes (idle threshold is 15).
    old_ms = int(time.time() * 1000) - 30 * 60 * 1000
    await _force_timestamps(settings.db_path, sid, last_activity_ms=old_ms)

    await _reaper(client).tick()

    r = authed.get(f"/v1/sessions/{sid}")
    assert r.status_code == 200
    assert r.json()["status"] == "STOPPED"
    # docker.stop_container was called by the reap path.
    assert any(cid for cid, _ in fake_docker.stopped)


@pytest.mark.asyncio
async def test_tick_does_not_stop_recent_session(authed, settings, client):
    sid = _create(authed)
    # No backdating — session is fresh.
    await _reaper(client).tick()
    r = authed.get(f"/v1/sessions/{sid}")
    assert r.json()["status"] == "RUNNING"


@pytest.mark.asyncio
async def test_tick_destroys_expired_session(authed, fake_docker, settings, client):
    sid = _create(authed)
    # Backdate created_at 25 hours (TTL is 24h).
    old_ms = int(time.time() * 1000) - 25 * 60 * 60 * 1000
    await _force_timestamps(settings.db_path, sid, created_ms=old_ms, last_activity_ms=old_ms)

    await _reaper(client).tick()

    # GET on a destroyed session returns 404 (SPEC-200).
    r = authed.get(f"/v1/sessions/{sid}")
    assert r.status_code == 404
    assert len(fake_docker.removed_containers) == 1
    assert len(fake_docker.removed_volumes) == 1


@pytest.mark.asyncio
async def test_tick_idempotent_on_already_stopped(authed, settings, client, fake_docker):
    sid = _create(authed)
    authed.post(f"/v1/sessions/{sid}/stop")
    # Backdate so the row is "idle".
    old_ms = int(time.time() * 1000) - 30 * 60 * 1000
    await _force_timestamps(settings.db_path, sid, last_activity_ms=old_ms)

    pre_stop_count = len(fake_docker.stopped)
    await _reaper(client).tick()
    # Row is already STOPPED; reap_stop early-returns; no extra docker.stop.
    assert len(fake_docker.stopped) == pre_stop_count


@pytest.mark.asyncio
async def test_tick_refreshes_status_gauge(authed, settings, client):
    _create(authed)
    _create(authed)
    await _reaper(client).tick()
    from api import metrics

    running = metrics.sessions_by_status.labels(status="RUNNING")._value.get()
    assert running >= 2
