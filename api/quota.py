"""Quota hooks for /workspace volumes. SPEC-302.

The control plane doesn't talk to `xfs_quota` directly. Instead, it
invokes two operator-provided scripts at volume create / destroy time
(`quota_setup_cmd`, `quota_teardown_cmd`). The scripts receive the
session metadata as environment variables and return 0 on success.

This keeps the control plane filesystem-agnostic — the same hook works
for XFS project quotas, ext4 prjquota, or even no-op no-quota dev
mode. Sample implementations live in `deploy/xfs-quota-{setup,teardown}.sh.example`.

When either setting is empty (the dev-mode default) the corresponding
hook is a no-op; the rest of the lifecycle continues unchanged.
"""

from __future__ import annotations

import asyncio
import logging
import shlex
import subprocess
from pathlib import Path

log = logging.getLogger("sandbox.quota")


async def run_setup(
    *,
    cmd: str,
    session_id: str,
    tenant_id: str,
    volume_name: str,
    volume_base: Path,
    workspace_mib: int,
) -> None:
    if not cmd:
        return
    await _run_hook(
        cmd=cmd,
        kind="setup",
        env={
            "SESSION_ID": session_id,
            "TENANT_ID": tenant_id,
            "VOLUME_NAME": volume_name,
            "VOLUME_PATH": str(volume_base / session_id),
            "VOLUME_BASE": str(volume_base),
            "WORKSPACE_MIB": str(workspace_mib),
        },
    )


async def run_teardown(
    *,
    cmd: str,
    session_id: str,
    tenant_id: str,
    volume_name: str,
    volume_base: Path,
) -> None:
    if not cmd:
        return
    await _run_hook(
        cmd=cmd,
        kind="teardown",
        env={
            "SESSION_ID": session_id,
            "TENANT_ID": tenant_id,
            "VOLUME_NAME": volume_name,
            "VOLUME_PATH": str(volume_base / session_id),
            "VOLUME_BASE": str(volume_base),
        },
    )


async def _run_hook(*, cmd: str, kind: str, env: dict[str, str]) -> None:
    argv = shlex.split(cmd)
    log.info("quota %s: running %s with env=%s", kind, argv, sorted(env.keys()))
    result = await asyncio.to_thread(
        subprocess.run,
        argv,
        env={**env},
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        # Don't fail the whole session create on a quota issue — log,
        # surface in audit (caller's job), let the operator notice.
        # In dev this is benign; in prod it means the workspace is
        # unbounded which is documented in SPEC-302.
        log.warning(
            "quota %s exited %d: stderr=%r stdout=%r",
            kind,
            result.returncode,
            result.stderr.strip(),
            result.stdout.strip(),
        )
