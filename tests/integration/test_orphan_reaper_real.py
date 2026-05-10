"""Integration test for the orphan reaper (slice 13a).

Verifies the reaper actually removes a real Docker volume/container
when the registry doesn't know about it. Skip cleanly when Docker
isn't reachable.

Run on the deploy host:
    uv run pytest -m integration tests/integration/test_orphan_reaper_real.py -v
"""

from __future__ import annotations

import asyncio
import time

import docker
import pytest
from docker.errors import DockerException, NotFound

from api.audit import AuditEmitter
from api.config import Settings
from api.docker_client import DockerClient
from api.orphan_reaper import OrphanReaper
from api.registry import Registry

pytestmark = pytest.mark.integration


def _docker_available() -> bool:
    try:
        docker.from_env().ping()
        return True
    except DockerException:
        return False


@pytest.fixture
def integ_settings(tmp_path):
    if not _docker_available():
        pytest.skip("Docker daemon not reachable")
    return Settings(
        dev_mode=True,
        api_token="orphan-integ-token",
        db_path=tmp_path / "orphan.db",
        audit_log_path=tmp_path / "orphan-audit.log",
        # Aggressive grace so the test doesn't wait an hour.
        orphan_reap_grace_s=2,
        orphan_reap_interval_s=3600,  # we drive tick() directly
        orphan_reap_max_per_tick=10,
    )


@pytest.fixture
async def real_reaper(integ_settings):
    docker_client = DockerClient(integ_settings)
    registry = Registry(integ_settings.db_path)
    await registry.init()
    audit = AuditEmitter(integ_settings.audit_log_path)
    reaper = OrphanReaper(
        settings=integ_settings, registry=registry, docker=docker_client, audit=audit
    )
    yield reaper, docker_client


async def test_real_orphan_volume_reaped(real_reaper, integ_settings):
    """Stage a real Docker volume with the sandbox.session_id label, no
    registry row, age past grace; assert reaper.tick() removes it."""
    reaper, dc = real_reaper
    sid = f"integ-orph-{int(time.time())}"
    vol_name = f"sandbox-integ-orph-{int(time.time())}"

    raw = docker.from_env()
    raw.volumes.create(
        name=vol_name,
        labels={"sandbox.session_id": sid, "sandbox.tenant_id": "integ"},
    )

    # Confirm the volume is visible before we tick.
    assert raw.volumes.get(vol_name) is not None

    # Wait past grace.
    await asyncio.sleep(integ_settings.orphan_reap_grace_s + 1)

    await reaper.tick()

    # Volume should be gone now.
    with pytest.raises(NotFound):
        raw.volumes.get(vol_name)


async def test_under_grace_volume_kept(real_reaper, integ_settings):
    """Stage a real Docker volume but tick before grace expires.
    The volume must survive — the reaper is conservative on young items."""
    reaper, dc = real_reaper
    sid = f"integ-young-{int(time.time())}"
    vol_name = f"sandbox-integ-young-{int(time.time())}"

    raw = docker.from_env()
    raw.volumes.create(
        name=vol_name,
        labels={"sandbox.session_id": sid, "sandbox.tenant_id": "integ"},
    )

    try:
        # Tick immediately — well under the 2s grace.
        await reaper.tick()

        # Volume should still exist.
        assert raw.volumes.get(vol_name) is not None
    finally:
        # Cleanup so the test doesn't leak volumes.
        with pytest.raises(NotFound) if False else _ignore_not_found():
            raw.volumes.get(vol_name).remove(force=True)


def _ignore_not_found():
    import contextlib

    return contextlib.suppress(NotFound)
