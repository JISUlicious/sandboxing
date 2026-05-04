"""Audit fail-closed tests (slice 5a). ARCH §7.

Failure injection works by subclassing AuditEmitter so `_write_line`
can be flipped between succeeding and raising OSError.
"""

import json

import pytest

from api.audit import AuditEmitter
from api.errors import AuditUnhealthy


class FailingAuditEmitter(AuditEmitter):
    def __init__(self, *args, **kw):
        super().__init__(*args, **kw)
        self.fail_primary = False
        self.fail_fallback = False

    def _write_line(self, target, line):
        if target == self.path and self.fail_primary:
            raise OSError("simulated primary failure")
        if target == self.fallback_path and self.fail_fallback:
            raise OSError("simulated fallback failure")
        super()._write_line(target, line)


@pytest.fixture
def emitter(tmp_path):
    return FailingAuditEmitter(
        tmp_path / "audit.log",
        fallback_path=tmp_path / "audit.fallback.jsonl",
        buffer_timeout_s=0.05,  # tiny budget for tests
    )


# ----- happy path -----


async def test_emit_writes_to_primary_when_healthy(emitter):
    await emitter.emit(kind="x.test", tenant="t", session="s")
    assert emitter.is_healthy
    assert emitter.buffered_count == 0
    lines = emitter.path.read_text().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["kind"] == "x.test"


# ----- failure path -----


async def test_emit_failure_marks_unhealthy_and_buffers(emitter):
    emitter.fail_primary = True
    await emitter.emit(kind="x.test", tenant="t")
    assert not emitter.is_healthy
    assert emitter.buffered_count == 1
    assert not emitter.path.exists()  # primary never opened (open() raised)


async def test_precheck_raises_when_unhealthy(emitter):
    emitter.fail_primary = True
    await emitter.emit(kind="x.test", tenant="t")
    with pytest.raises(AuditUnhealthy):
        emitter.precheck()


async def test_precheck_passes_when_healthy(emitter):
    emitter.precheck()  # does not raise


# ----- recovery -----


async def test_recovery_drains_buffer(emitter):
    emitter.fail_primary = True
    for i in range(3):
        await emitter.emit(kind=f"x.{i}", tenant="t")
    assert emitter.buffered_count == 3

    emitter.fail_primary = False
    await emitter.emit(kind="x.recover", tenant="t")
    assert emitter.is_healthy
    assert emitter.buffered_count == 0

    # Primary now contains 3 buffered + 1 recover = 4 lines.
    lines = emitter.path.read_text().splitlines()
    assert len(lines) == 4
    kinds = [json.loads(line)["kind"] for line in lines]
    assert kinds == ["x.0", "x.1", "x.2", "x.recover"]


# ----- fallback timeout -----


async def test_fallback_flush_after_buffer_timeout(emitter):
    import time

    emitter.fail_primary = True
    await emitter.emit(kind="x.first", tenant="t")
    assert emitter.buffered_count == 1
    assert not emitter.fallback_path.exists()

    # Sleep past buffer_timeout_s (0.05 in fixture).
    time.sleep(0.1)
    await emitter.emit(kind="x.second", tenant="t")

    # Both records flushed to fallback; buffer cleared.
    assert emitter.buffered_count == 0
    fallback_lines = emitter.fallback_path.read_text().splitlines()
    assert len(fallback_lines) == 2


# ----- maintenance_tick -----


async def test_maintenance_tick_recovers(emitter):
    emitter.fail_primary = True
    await emitter.emit(kind="x.test", tenant="t")
    assert not emitter.is_healthy

    emitter.fail_primary = False
    await emitter.maintenance_tick()
    assert emitter.is_healthy
    # Heartbeat + the previously buffered record.
    lines = emitter.path.read_text().splitlines()
    assert any("audit.heartbeat" in line for line in lines)
    assert any("x.test" in line for line in lines)


async def test_maintenance_tick_noop_when_healthy(emitter):
    await emitter.maintenance_tick()
    # Healthy, so no heartbeat written.
    assert not emitter.path.exists() or emitter.path.read_text() == ""


# ----- API integration: 503 on mutations when unhealthy -----


def test_mutations_return_503_when_audit_unhealthy(authed, service):
    """Wire up via the existing app with the existing audit emitter,
    flip it unhealthy by hand, and verify mutations are rejected."""
    # Grease the wheels: create one healthy session first so subsequent
    # mutations have something to act on.
    sid = authed.post("/v1/sessions", json={}).json()["session_id"]

    # Force unhealthy.
    service.audit._unhealthy_since_monotonic = 0.0

    r = authed.post("/v1/sessions", json={})
    assert r.status_code == 503
    assert r.json()["detail"]["code"] == "audit_unhealthy"

    r = authed.post(f"/v1/sessions/{sid}/exec", json={"argv": ["true"]})
    assert r.status_code == 503

    r = authed.delete(f"/v1/sessions/{sid}")
    assert r.status_code == 503

    # Reads are unaffected.
    r = authed.get(f"/v1/sessions/{sid}")
    assert r.status_code == 200


def test_readyz_reports_audit_unhealthy(client, service):
    service.audit._unhealthy_since_monotonic = 0.0
    r = client.get("/readyz")
    assert r.json() == {"docker": True, "audit": False}
