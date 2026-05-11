"""Integration test for the orphan reaper (slice 13a).

Verifies the reaper actually removes a real Docker volume when the
registry doesn't know about it. Skips cleanly when Docker isn't
reachable.

**SAFETY DESIGN.** The reaper concludes a Docker resource is orphaned
when `registry.get_unscoped(session_id)` returns None. If we ran the
reaper against a fresh empty `Registry` (in `tmp_path`), *every*
labelled container and volume on the host would look orphaned —
including any real production sessions. This is exactly what an
earlier version of this test did, and it destroyed real volumes.

To prevent that, the fixture below stubs `registry.get_unscoped` so
that **only the test's own session id** ever returns None. Every other
session id on the host is treated as registry-known and left alone.

Run on the deploy host:

    uv run pytest -m integration tests/integration/test_orphan_reaper_real.py -v
"""

from __future__ import annotations

import asyncio
import contextlib
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
        # Aggressive grace so the test doesn't wait an hour. 2s is
        # below the typical sleep used by the past-grace test (>= 4s).
        orphan_reap_grace_s=2,
        orphan_reap_interval_s=3600,  # we drive tick() directly
        orphan_reap_max_per_tick=10,
    )


@pytest.fixture
def test_sid():
    """Unique session id per test invocation, paired with the registry
    stub below so only this id is considered orphaned by the reaper."""
    return f"integ-orph-{int(time.time() * 1_000_000)}"


class _Known:
    """Sentinel returned by the stubbed `get_unscoped` to mark a row
    as 'registry knows about this; do not reap'. Any non-None object
    satisfies the reaper's check."""


@pytest.fixture
async def real_reaper(integ_settings, test_sid):
    docker_client = DockerClient(integ_settings)
    registry = Registry(integ_settings.db_path)
    await registry.init()

    # SAFETY: see module docstring. Only `test_sid` is unknown; every
    # other labelled resource on the host is treated as registry-known
    # so this test cannot destroy production data.
    async def scoped_get_unscoped(sid):  # type: ignore[no-untyped-def]
        if sid == test_sid:
            return None
        return _Known()

    registry.get_unscoped = scoped_get_unscoped  # type: ignore[method-assign]

    audit = AuditEmitter(integ_settings.audit_log_path)
    reaper = OrphanReaper(
        settings=integ_settings, registry=registry, docker=docker_client, audit=audit
    )
    yield reaper, docker_client


async def test_real_orphan_volume_reaped(real_reaper, integ_settings, test_sid):
    """Stage a real Docker volume with the sandbox.session_id label set
    to a sid the registry stub treats as unknown; wait past grace;
    assert reaper.tick() removes it."""
    _, _ = real_reaper  # fixture only, no destructuring needed
    reaper, _ = real_reaper
    vol_name = f"sandbox-{test_sid}"

    raw = docker.from_env()
    raw.volumes.create(
        name=vol_name,
        labels={"sandbox.session_id": test_sid, "sandbox.tenant_id": "integ"},
    )
    try:
        # Wait long enough that Docker's `CreatedAt` (1s granularity on
        # older daemons) reports an age comfortably past grace.
        await asyncio.sleep(integ_settings.orphan_reap_grace_s + 2)

        await reaper.tick()

        # Volume should be gone now.
        with pytest.raises(NotFound):
            raw.volumes.get(vol_name)
    finally:
        # Backstop: if the test failed before the reap, clean up by hand.
        with contextlib.suppress(NotFound):
            raw.volumes.get(vol_name).remove(force=True)


async def test_under_grace_volume_kept(real_reaper, integ_settings, test_sid):
    """Stage a real Docker volume but tick before grace expires. The
    reaper must leave it alone — it could still be mid-create."""
    reaper, _ = real_reaper
    vol_name = f"sandbox-{test_sid}"

    raw = docker.from_env()
    raw.volumes.create(
        name=vol_name,
        labels={"sandbox.session_id": test_sid, "sandbox.tenant_id": "integ"},
    )
    try:
        # Tick immediately — well under grace.
        await reaper.tick()

        # Volume should still exist.
        assert raw.volumes.get(vol_name) is not None
    finally:
        with contextlib.suppress(NotFound):
            raw.volumes.get(vol_name).remove(force=True)
