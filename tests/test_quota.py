"""Tests for the quota hook plumbing (slice 5d). SPEC-302.

The hooks shell out to operator-provided scripts; these tests
substitute a /bin/true-like script and assert the env vars the control
plane passes are correct, without exercising xfs_quota itself.
"""

import os
import stat

from api.audit import AuditEmitter
from api.config import Settings
from api.docker_client import hardening_flags  # noqa: F401  (import shape)
from api.registry import Registry
from api.sessions import SessionService


def _write_recorder_script(path, kind="setup"):
    """Write a tiny script that appends its env vars to a known file
    and exits 0. Lets the test inspect what the control plane passed."""
    log_path = path.parent / f"{kind}.log"
    path.write_text(
        f"""#!/usr/bin/env bash
{{
  echo "kind={kind}"
  echo "SESSION_ID=$SESSION_ID"
  echo "TENANT_ID=$TENANT_ID"
  echo "VOLUME_NAME=$VOLUME_NAME"
  echo "VOLUME_PATH=$VOLUME_PATH"
  echo "VOLUME_BASE=$VOLUME_BASE"
  echo "WORKSPACE_MIB=${{WORKSPACE_MIB:-unset}}"
}} >> {log_path}
"""
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP)
    return log_path


async def test_setup_and_teardown_hooks_fire(tmp_path, fake_docker):
    setup_script = tmp_path / "setup.sh"
    teardown_script = tmp_path / "teardown.sh"
    setup_log = _write_recorder_script(setup_script, kind="setup")
    teardown_log = _write_recorder_script(teardown_script, kind="teardown")

    settings = Settings(
        dev_mode=True,
        api_token="t",
        db_path=tmp_path / "test.db",
        audit_log_path=tmp_path / "audit.log",
        quota_setup_cmd=str(setup_script),
        quota_teardown_cmd=str(teardown_script),
        quota_volume_base=tmp_path / "volumes",
    )
    service = SessionService(
        settings=settings,
        registry=Registry(settings.db_path),
        docker=fake_docker,
        audit=AuditEmitter(settings.audit_log_path),
    )
    await service.registry.init()

    row = await service.create("default", limits=None)
    assert setup_log.exists()
    setup_text = setup_log.read_text()
    assert f"SESSION_ID={row.id}" in setup_text
    assert f"VOLUME_NAME={row.volume_name}" in setup_text
    assert f"WORKSPACE_MIB={row.limits.workspace_mib}" in setup_text
    assert "VOLUME_BASE=" + str(tmp_path / "volumes") in setup_text

    await service.destroy(row.id, "default")
    assert teardown_log.exists()
    teardown_text = teardown_log.read_text()
    assert f"SESSION_ID={row.id}" in teardown_text
    # Teardown deliberately does NOT receive WORKSPACE_MIB (the size is
    # only relevant at setup); the recorder script sees the unset default.
    assert "WORKSPACE_MIB=unset" in teardown_text


async def test_no_op_when_unconfigured(tmp_path, fake_docker):
    """With empty quota_*_cmd settings, create + destroy must not crash."""
    settings = Settings(
        dev_mode=True,
        api_token="t",
        db_path=tmp_path / "test.db",
        audit_log_path=tmp_path / "audit.log",
        # quota_setup_cmd / quota_teardown_cmd intentionally left blank
    )
    service = SessionService(
        settings=settings,
        registry=Registry(settings.db_path),
        docker=fake_docker,
        audit=AuditEmitter(settings.audit_log_path),
    )
    await service.registry.init()
    row = await service.create("default", limits=None)
    await service.destroy(row.id, "default")


async def test_hook_failure_doesnt_kill_create(tmp_path, fake_docker):
    """A failing setup script must not block session create — the
    control plane logs and continues. SPEC-302 documents that quotas
    in dev mode are advisory."""
    failing = tmp_path / "fail.sh"
    failing.write_text("#!/usr/bin/env bash\nexit 7\n")
    failing.chmod(0o755)

    settings = Settings(
        dev_mode=True,
        api_token="t",
        db_path=tmp_path / "test.db",
        audit_log_path=tmp_path / "audit.log",
        quota_setup_cmd=str(failing),
    )
    service = SessionService(
        settings=settings,
        registry=Registry(settings.db_path),
        docker=fake_docker,
        audit=AuditEmitter(settings.audit_log_path),
    )
    await service.registry.init()

    row = await service.create("default", limits=None)
    assert row.status == "RUNNING"
    # Cleanup the env so other tests aren't affected.
    os.environ.pop("SESSION_ID", None)
