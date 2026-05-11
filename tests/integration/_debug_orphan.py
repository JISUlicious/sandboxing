"""One-off diagnostic for the orphan reaper integration test failure.

Run on the deploy host:

    uv run python -m tests.integration._debug_orphan

Prints exactly what the reaper sees at each step so we can pinpoint
why the past-grace volume isn't being reaped. Underscore-prefixed
so pytest doesn't collect it.
"""

from __future__ import annotations

import asyncio
import time

import docker as docker_sdk

from api.config import Settings
from api.docker_client import DockerClient, _parse_docker_iso
from api.orphan_reaper import LABEL_KEY, OrphanReaper
from api.registry import Registry


class _Known:
    pass


async def main() -> None:
    sid = f"debug-{int(time.time() * 1_000_000)}"
    vol_name = f"sandbox-{sid}"

    settings = Settings(
        dev_mode=True,
        api_token="debug",
        db_path="/tmp/_orphan_debug.db",
        audit_log_path="/tmp/_orphan_debug.log",
        orphan_reap_grace_s=2,
        orphan_reap_interval_s=3600,
        orphan_reap_max_per_tick=10,
    )

    print(f"[1] test_sid = {sid!r}")
    print(f"    vol_name = {vol_name!r}")

    raw = docker_sdk.from_env()
    raw.volumes.create(
        name=vol_name,
        labels={"sandbox.session_id": sid, "sandbox.tenant_id": "debug"},
    )
    print(f"[2] volume created. direct inspect:")
    v = raw.volumes.get(vol_name)
    print(f"    v.name      = {v.name!r}")
    print(f"    v.attrs     = {v.attrs!r}")

    dc = DockerClient(settings)
    print(f"[3] list_volumes_with_label('sandbox.session_id') →")
    items = dc.list_volumes_with_label(LABEL_KEY)
    print(f"    total returned = {len(items)}")
    matches = [
        i for i in items if (i.get("labels") or {}).get(LABEL_KEY) == sid
    ]
    print(f"    matches for test_sid = {len(matches)}")
    if matches:
        m = matches[0]
        print(f"    matched item = {m!r}")
        print(f"    created_epoch_s = {m.get('created_epoch_s')!r}")
        print(f"    label session_id = {(m.get('labels') or {}).get(LABEL_KEY)!r}")
        print(f"    type of label value = {type((m.get('labels') or {}).get(LABEL_KEY)).__name__}")
    else:
        print(f"    !! test volume NOT in list response — listing skipped it !!")
        print(f"    all returned names: {[i.get('name') for i in items]}")

    print(f"[4] sleeping {settings.orphan_reap_grace_s + 2}s...")
    await asyncio.sleep(settings.orphan_reap_grace_s + 2)

    print(f"[5] re-listing after wait:")
    items = dc.list_volumes_with_label(LABEL_KEY)
    matches = [
        i for i in items if (i.get("labels") or {}).get(LABEL_KEY) == sid
    ]
    if matches:
        m = matches[0]
        now = time.time()
        created = float(m.get("created_epoch_s") or 0.0)
        age = now - created if created > 0 else float("inf")
        print(f"    created_epoch_s = {created!r}")
        print(f"    now = {now!r}")
        print(f"    age = {age!r}")
        print(f"    grace = {settings.orphan_reap_grace_s}")
        print(f"    will be skipped under grace? {age < settings.orphan_reap_grace_s}")
    else:
        print(f"    !! test volume vanished from list !!")

    print(f"[6] running OrphanReaper.tick() with scoped registry stub...")
    registry = Registry(settings.db_path)
    await registry.init()

    async def scoped_get_unscoped(s):
        if s == sid:
            print(f"    [stub] get_unscoped({s!r}) → None (orphaned)")
            return None
        return _Known()

    registry.get_unscoped = scoped_get_unscoped  # type: ignore[method-assign]

    from api.audit import AuditEmitter

    audit = AuditEmitter(settings.audit_log_path)
    reaper = OrphanReaper(settings=settings, registry=registry, docker=dc, audit=audit)
    await reaper.tick()

    print(f"[7] post-tick check — does volume still exist?")
    try:
        v = raw.volumes.get(vol_name)
        print(f"    !! STILL EXISTS !! attrs CreatedAt = {v.attrs.get('CreatedAt')!r}")
    except docker_sdk.errors.NotFound:
        print(f"    GONE — reaper worked as expected")

    print(f"[8] cleanup")
    try:
        raw.volumes.get(vol_name).remove(force=True)
        print(f"    removed by hand")
    except docker_sdk.errors.NotFound:
        print(f"    already gone")


if __name__ == "__main__":
    asyncio.run(main())
