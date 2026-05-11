"""Regression tests for the canonical hardening flag-set (SPEC-401 / ARCH-021)."""

from unittest.mock import MagicMock, patch

from api.config import Settings
from api.docker_client import DockerClient, _parse_docker_iso, hardening_flags
from api.models import Limits

# ----- _parse_docker_iso (slice 13a) -----
#
# Docker is inconsistent across resource kinds:
# - Containers report `Created` in UTC with `Z` and nanoseconds.
# - Volumes report `CreatedAt` using the daemon's local timezone
#   and an explicit offset, without sub-second precision.
# The integration test on a +09:00 host caught the bug where the
# original parser silently forced UTC and produced timestamps 9h
# in the future — age calculations went negative and the reaper
# treated freshly-created orphans as "under grace".


def test_parse_docker_iso_utc_z_with_nanoseconds():
    """Container-style timestamp: UTC, Z suffix, nanoseconds."""
    # 2024-05-10T07:39:27 UTC → epoch 1715326767
    ts = _parse_docker_iso("2024-05-10T07:39:27.123456789Z")
    assert ts == 1715326767.123456


def test_parse_docker_iso_local_offset_no_subsecond():
    """Volume-style timestamp: local TZ with offset, no nanos.
    Korea (+09:00) at 13:08:41 is UTC 04:08:41 — they must produce
    the same epoch."""
    local = _parse_docker_iso("2024-05-10T13:08:41+09:00")
    utc_equivalent = _parse_docker_iso("2024-05-10T04:08:41Z")
    assert local == utc_equivalent


def test_parse_docker_iso_naive_treated_as_utc():
    """Strings without any tz info should be assumed UTC, matching
    the prior behaviour for the fallback case."""
    assert _parse_docker_iso("2024-05-10T07:39:27") == _parse_docker_iso("2024-05-10T07:39:27Z")


def test_parse_docker_iso_empty_and_garbage():
    """Empty/garbage strings return 0.0 so the reaper treats them as
    'very old' — the grace window is then the actual safety knob."""
    assert _parse_docker_iso("") == 0.0
    assert _parse_docker_iso("not-an-iso-string") == 0.0


def test_hardening_flags_canonical_production():
    limits = Limits()
    flags = hardening_flags(
        session_id="s1",
        tenant_id="t1",
        volume_name="v1",
        limits=limits,
        image="img:1",
        network="net1",
        dev_mode=False,
    )
    # Each assertion below corresponds to a SPEC or ARCH ID. Adjusting any
    # of these without bumping the docs will break this test on purpose.
    assert flags["runtime"] == "runsc"  # SPEC-400
    assert flags["read_only"] is True  # SPEC-401
    assert flags["user"] == "10001:10001"  # ARCH-032
    assert flags["working_dir"] == "/workspace"  # SPEC-108
    assert flags["cap_drop"] == ["ALL"]  # SPEC-401
    assert "no-new-privileges:true" in flags["security_opt"]  # SPEC-401
    assert "seccomp=unconfined" in flags["security_opt"]  # ARCH-021 commentary
    assert "userns_mode" not in flags  # SPEC-401 (daemon default)
    assert flags["pids_limit"] == limits.pids  # SPEC §6
    assert flags["mem_limit"] == f"{limits.memory_mib}m"  # SPEC §6
    assert flags["nano_cpus"] == limits.vcpu * 1_000_000_000  # SPEC §6
    assert flags["network"] == "net1"  # SPEC-402
    assert flags["entrypoint"] == ["/usr/bin/sleep", "infinity"]  # ARCH-021
    assert flags["tmpfs"]["/tmp"] == "size=256m,mode=1777,noexec,nosuid,nodev"  # SPEC-401
    assert flags["volumes"] == {"v1": {"bind": "/workspace", "mode": "rw"}}  # ARCH-050
    env = flags["environment"]
    assert env["HTTP_PROXY"] == "http://proxy:3128"  # SPEC-403
    assert env["HTTPS_PROXY"] == "http://proxy:3128"
    assert env["HOME"] == "/workspace"  # SPEC-108
    assert env["USER"] == "agent"
    labels = flags["labels"]
    assert labels["sandbox.session_id"] == "s1"
    assert labels["sandbox.tenant_id"] == "t1"


def test_wrap_with_timeout_omits_preserve_status():
    """Regression: --preserve-status makes GNU `timeout` return the inner
    program's signal-exit code (143 for SIGTERM) instead of 124 on timeout,
    which silently breaks the exec_timeout → 408 mapping."""
    from api.docker_client import _wrap_with_timeout

    cmd = _wrap_with_timeout(["sleep", "30"], 2)
    assert cmd == ["/usr/bin/timeout", "2", "sleep", "30"]
    assert "--preserve-status" not in cmd


def test_hardening_flags_dev_mode_omits_runtime():
    flags = hardening_flags(
        session_id="s1",
        tenant_id="t1",
        volume_name="v1",
        limits=Limits(),
        image="img:1",
        network="net1",
        dev_mode=True,
    )
    # SPEC-302: dev mode bypasses SPEC-400; everything else stays hardened.
    assert "runtime" not in flags
    assert flags["read_only"] is True
    assert flags["cap_drop"] == ["ALL"]


def test_create_session_applies_canonical_flags(authed, fake_docker):
    authed.post("/v1/sessions", json={})
    assert len(fake_docker.created_containers) == 1
    _, flags = fake_docker.created_containers[0]
    assert flags["read_only"] is True
    assert flags["user"] == "10001:10001"
    assert flags["cap_drop"] == ["ALL"]
    # Tests run with dev_mode=True per conftest, so runtime is omitted.
    assert "runtime" not in flags


def _docker_client_with_fake_engine(settings: Settings) -> DockerClient:
    client = DockerClient(settings)
    fake_engine = MagicMock()
    fake_engine.volumes.create = MagicMock()
    client._client = fake_engine
    return client


def test_create_volume_chowns_bind_when_uid_set(tmp_path):
    """SPEC-401: with bind_volume_uid set, per-session bind dirs are
    chown'd to the userns-remap subuid + chmod'd 0700."""
    base = tmp_path / "volumes"
    settings = Settings(
        api_token="t",
        quota_volume_base=base,
        bind_volume_uid=110001,
    )
    client = _docker_client_with_fake_engine(settings)

    chown_calls: list[tuple[str, int, int]] = []
    chmod_calls: list[tuple[str, int]] = []

    def fake_chown(path, uid, gid):
        chown_calls.append((str(path), uid, gid))

    def fake_chmod(self, mode):
        chmod_calls.append((str(self), mode))

    with (
        patch("api.docker_client.os.chown", fake_chown),
        patch.object(type(base), "chmod", fake_chmod),
    ):
        client.create_volume("vol-x", "session-x", "tenant-x")

    assert chown_calls == [(str(base / "session-x"), 110001, 110001)]
    assert chmod_calls == [(str(base / "session-x"), 0o700)]


def test_create_volume_falls_back_to_0777_when_chown_fails(tmp_path):
    """When the underlying filesystem refuses chown (SMB without forceuid,
    NFS without idmapping, FUSE), the create_volume path must NOT propagate
    the OSError — it logs a warning and falls back to mode 0o777 so the
    session can still come up. The operator's mount options are the real
    fix; this is just so a 500 ISE on session create becomes a working
    session + a log line."""
    base = tmp_path / "volumes"
    settings = Settings(
        api_token="t",
        quota_volume_base=base,
        bind_volume_uid=110001,
    )
    client = _docker_client_with_fake_engine(settings)

    chown_calls: list[tuple[str, int, int]] = []
    chmod_calls: list[tuple[str, int]] = []

    def fake_chown(path, uid, gid):
        chown_calls.append((str(path), uid, gid))
        raise OSError(1, "Operation not permitted")

    def fake_chmod(self, mode):
        chmod_calls.append((str(self), mode))

    with (
        patch("api.docker_client.os.chown", fake_chown),
        patch.object(type(base), "chmod", fake_chmod),
    ):
        client.create_volume("vol-x", "session-x", "tenant-x")

    # chown was attempted (and failed), chmod 0o777 fallback ran.
    assert chown_calls == [(str(base / "session-x"), 110001, 110001)]
    assert chmod_calls == [(str(base / "session-x"), 0o777)]


def test_create_volume_falls_back_to_0777_when_uid_unset(tmp_path):
    """Back-compat: without bind_volume_uid, the legacy 0777 stopgap stays."""
    base = tmp_path / "volumes"
    settings = Settings(api_token="t", quota_volume_base=base, bind_volume_uid=None)
    client = _docker_client_with_fake_engine(settings)

    chown_calls: list = []
    chmod_calls: list[tuple[str, int]] = []

    def fake_chown(*args, **kwargs):
        chown_calls.append((args, kwargs))

    def fake_chmod(self, mode):
        chmod_calls.append((str(self), mode))

    with (
        patch("api.docker_client.os.chown", fake_chown),
        patch.object(type(base), "chmod", fake_chmod),
    ):
        client.create_volume("vol-x", "session-x", "tenant-x")

    assert chown_calls == []
    assert chmod_calls == [(str(base / "session-x"), 0o777)]
