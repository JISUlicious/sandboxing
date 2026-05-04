"""Startup reconciliation tests (slice 6a). ARCH-051.

Three scenarios:
  1. A row stuck in DESTROYING gets finished (rm container/volume +
     transition to DESTROYED).
  2. A RUNNING row whose container is gone gets marked STOPPED so the
     next exec returns InvalidState rather than docker NotFound.
  3. A RUNNING row whose container is still present is a no-op.
"""

import json

import aiosqlite


async def _force_status(db_path, session_id, status):
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "UPDATE sessions SET status = ? WHERE id = ?",
            (status, session_id),
        )
        await db.commit()


async def test_finishes_stuck_destroying(authed, fake_docker, settings, service):
    sid = authed.post("/v1/sessions", json={}).json()["session_id"]
    container_id = fake_docker.created_containers[0][0]

    # Force the row into DESTROYING (simulate a crash mid-destroy).
    await _force_status(settings.db_path, sid, "DESTROYING")

    summary = await service.reconcile_on_startup()

    assert summary["finished_destroy"] == 1
    assert container_id in fake_docker.removed_containers
    # GET on a destroyed session returns 404 — confirms transition went through.
    assert authed.get(f"/v1/sessions/{sid}").status_code == 404


async def test_orphans_running_with_missing_container(authed, fake_docker, service):
    sid = authed.post("/v1/sessions", json={}).json()["session_id"]
    container_id = fake_docker.created_containers[0][0]
    fake_docker._missing_containers = {container_id}

    summary = await service.reconcile_on_startup()

    assert summary["orphaned"] == 1
    # Volume preserved; status now STOPPED.
    r = authed.get(f"/v1/sessions/{sid}")
    assert r.status_code == 200
    assert r.json()["status"] == "STOPPED"


async def test_noop_when_container_present(authed, fake_docker, service):
    sid = authed.post("/v1/sessions", json={}).json()["session_id"]
    fake_docker._missing_containers = set()

    summary = await service.reconcile_on_startup()

    assert summary["finished_destroy"] == 0
    assert summary["orphaned"] == 0
    assert authed.get(f"/v1/sessions/{sid}").json()["status"] == "RUNNING"


async def test_emits_audit_records(authed, fake_docker, settings, service):
    sid = authed.post("/v1/sessions", json={}).json()["session_id"]
    container_id = fake_docker.created_containers[0][0]
    fake_docker._missing_containers = {container_id}

    await service.reconcile_on_startup()

    audit_lines = settings.audit_log_path.read_text().splitlines()
    reconciled = [
        json.loads(line) for line in audit_lines if json.loads(line)["kind"] == "session.reconciled"
    ]
    assert len(reconciled) == 1
    assert reconciled[0]["session"] == sid
    assert reconciled[0]["payload"]["action"] == "orphaned_missing_container"
