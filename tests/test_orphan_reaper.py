"""Orphan reaper tests (slice 13a).

Stages orphan containers and volumes on the FakeDockerClient and
asserts the reaper's behaviour:
- Items within the grace window are not reaped.
- Items whose `sandbox.session_id` label matches a registry row are
  not reaped.
- Per-tick cap is respected across the two passes (containers first).
- Audit emit shape is correct.
- Container-then-volume ordering is preserved.
"""

from __future__ import annotations

import time

import pytest

from api.audit import AuditEmitter
from api.orphan_reaper import LABEL_KEY, OrphanReaper
from api.registry import Registry


def _orphan_container(name: str, *, age_s: float, session_id: str, tenant_id: str = "default"):
    return {
        "name": name,
        "id": name,
        "created_epoch_s": time.time() - age_s,
        "labels": {LABEL_KEY: session_id, "sandbox.tenant_id": tenant_id},
    }


def _orphan_volume(name: str, *, age_s: float, session_id: str, tenant_id: str = "default"):
    return {
        "name": name,
        "created_epoch_s": time.time() - age_s,
        "labels": {LABEL_KEY: session_id, "sandbox.tenant_id": tenant_id},
    }


@pytest.fixture
async def reaper_under_test(settings, fake_docker):
    """Build a fresh registry + AuditEmitter so the test owns its own
    artifacts; uses the package `settings`/`fake_docker` fixtures."""
    registry = Registry(settings.db_path)
    await registry.init()
    audit = AuditEmitter(settings.audit_log_path)
    reaper = OrphanReaper(
        settings=settings,
        registry=registry,
        docker=fake_docker,
        audit=audit,
    )
    yield reaper, registry, audit


async def test_under_grace_not_reaped(reaper_under_test, fake_docker, settings):
    """Resources younger than the grace window are skipped, even if
    the registry doesn't know about them — they may still be mid-create."""
    reaper, _, _ = reaper_under_test
    settings.orphan_reap_grace_s = 60
    fake_docker.orphan_containers = [
        _orphan_container("sandbox-young", age_s=5, session_id="never-created")
    ]
    fake_docker.orphan_volumes = [_orphan_volume("vol-young", age_s=5, session_id="never-created")]
    fake_docker.removed_containers = []
    fake_docker.removed_volumes = []

    await reaper.tick()

    assert fake_docker.removed_containers == []
    assert fake_docker.removed_volumes == []


async def test_registry_known_not_reaped(reaper_under_test, fake_docker, service, settings):
    """Resources whose session_id corresponds to a registry row are
    owned by the main reaper. Don't touch them even if old."""
    reaper, registry, _ = reaper_under_test
    settings.orphan_reap_grace_s = 5
    # Create a real session via the existing service so the registry
    # has a row with this session_id.
    real = await service.create(tenant_id="default", limits=None)
    fake_docker.orphan_containers = [
        _orphan_container(f"sandbox-{real.id}", age_s=600, session_id=real.id)
    ]
    fake_docker.removed_containers = []

    # The reaper uses its own registry instance; share the same path
    # so its `get_unscoped` sees the row.
    reaper._registry = service.registry  # ensure same DB

    await reaper.tick()

    assert fake_docker.removed_containers == []


async def test_orphan_past_grace_reaped(reaper_under_test, fake_docker, settings):
    """The actual win condition: old + unknown to registry → reaped."""
    reaper, _, _ = reaper_under_test
    settings.orphan_reap_grace_s = 5
    fake_docker.orphan_containers = [
        _orphan_container("sandbox-orph-1", age_s=600, session_id="orph-1")
    ]
    fake_docker.orphan_volumes = [_orphan_volume("vol-orph-2", age_s=600, session_id="orph-2")]
    fake_docker.removed_containers = []
    fake_docker.removed_volumes = []

    await reaper.tick()

    assert fake_docker.removed_containers == ["sandbox-orph-1"]
    assert fake_docker.removed_volumes == ["vol-orph-2"]


async def test_per_tick_cap(reaper_under_test, fake_docker, settings):
    """Stage 25 orphans with cap=10; assert 10 reaped, 15 left."""
    reaper, _, _ = reaper_under_test
    settings.orphan_reap_grace_s = 5
    settings.orphan_reap_max_per_tick = 10
    fake_docker.orphan_containers = [
        _orphan_container(f"sandbox-c{i}", age_s=600, session_id=f"c{i}") for i in range(15)
    ]
    fake_docker.orphan_volumes = [
        _orphan_volume(f"v{i}", age_s=600, session_id=f"v{i}") for i in range(10)
    ]
    fake_docker.removed_containers = []
    fake_docker.removed_volumes = []

    await reaper.tick()

    # Containers come first; cap of 10 exhausts before we get to volumes.
    assert len(fake_docker.removed_containers) == 10
    assert fake_docker.removed_volumes == []


async def test_container_first_ordering(reaper_under_test, fake_docker, settings):
    """When both lists have items, container-pass runs before volume-pass.
    Verified by the relative order of remove_* calls on the fake."""
    reaper, _, _ = reaper_under_test
    settings.orphan_reap_grace_s = 5

    call_order: list[tuple[str, str]] = []

    def _rec_container(name):
        call_order.append(("container", name))

    def _rec_volume(name):
        call_order.append(("volume", name))

    fake_docker.remove_container = _rec_container
    fake_docker.remove_volume = _rec_volume
    fake_docker.orphan_containers = [_orphan_container("sandbox-c1", age_s=600, session_id="c1")]
    fake_docker.orphan_volumes = [_orphan_volume("v1", age_s=600, session_id="v1")]

    await reaper.tick()

    assert call_order == [("container", "sandbox-c1"), ("volume", "v1")]


async def test_audit_emit_shape(reaper_under_test, fake_docker, settings):
    """The audit row includes resource_type, name, label_session_id,
    age_seconds — what an operator needs to investigate a sweep."""
    reaper, _, audit = reaper_under_test
    settings.orphan_reap_grace_s = 5
    fake_docker.orphan_containers = [
        _orphan_container("sandbox-c-audit", age_s=1234, session_id="aud-1", tenant_id="alice")
    ]

    await reaper.tick()
    # AuditEmitter writes synchronously inside emit() when healthy.

    text = settings.audit_log_path.read_text()
    assert "orphan.reap" in text
    assert "sandbox-c-audit" in text
    assert "aud-1" in text
    assert "alice" in text
    # age_seconds is approximate (test took some wall time) so just
    # confirm it landed in a plausible range.
    import json

    record = next(json.loads(line) for line in text.splitlines() if "orphan.reap" in line)
    payload = record.get("payload") or {}
    assert payload.get("resource_type") == "container"
    assert payload.get("label_session_id") == "aud-1"
    assert 1200 < payload.get("age_seconds", 0) < 1400
