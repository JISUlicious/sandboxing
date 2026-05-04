"""Integration tests against a real Docker daemon. Slice 8c.

Run via:
    uv run pytest -m integration

These tests skip cleanly when:
- The Docker daemon isn't reachable (no `/var/run/docker.sock` /
  `DOCKER_HOST` set up).
- The sandbox-runtime image isn't built locally.

CI (`.github/workflows/ci.yml`) builds the image and runs this suite on
ubuntu-latest. They catch docker-py kwarg drift and similar wiring
regressions that the mocked unit tests can't surface.
"""

from __future__ import annotations

import os
import time

import docker
import pytest
from docker.errors import DockerException, ImageNotFound

from api.audit import AuditEmitter
from api.auth import TokenAuthenticator
from api.config import Settings
from api.docker_client import DockerClient
from api.registry import Registry
from api.sessions import SessionService

pytestmark = pytest.mark.integration


def _docker_available() -> bool:
    try:
        client = docker.from_env()
        client.ping()
        return True
    except DockerException:
        return False


def _image_available(tag: str) -> bool:
    try:
        docker.from_env().images.get(tag)
        return True
    except (DockerException, ImageNotFound):
        return False


SANDBOX_IMAGE = os.environ.get("SANDBOX_INTEG_IMAGE", "sandbox-runtime:latest")


@pytest.fixture
def integ_settings(tmp_path):
    if not _docker_available():
        pytest.skip("Docker daemon not reachable")
    if not _image_available(SANDBOX_IMAGE):
        pytest.skip(f"{SANDBOX_IMAGE} not built locally")

    return Settings(
        dev_mode=True,
        api_token="integration-token",
        token_pepper="integration-pepper",
        db_path=tmp_path / "test.db",
        audit_log_path=tmp_path / "audit.log",
        sandbox_image=SANDBOX_IMAGE,
        # No quota in CI — `quota_volume_base` left at default; the
        # default-Docker volume layout is fine for integration tests.
        quota_volume_base=tmp_path / "irrelevant-not-used",
        quota_setup_cmd="",
        quota_teardown_cmd="",
        # Don't set HTTP_PROXY in the sandbox env — there's no Squid
        # running in CI.
        egress_proxy_url="",
    )


@pytest.fixture
async def integ_service(integ_settings):
    docker_client = DockerClient(integ_settings)
    docker_client.ensure_network()
    registry = Registry(integ_settings.db_path)
    await registry.init()
    audit = AuditEmitter(integ_settings.audit_log_path)
    auth = TokenAuthenticator(settings=integ_settings, registry=registry)
    await registry.create_tenant("integ", "integ")
    await auth.issue_initial_token("integ", "integration-token")

    service = SessionService(
        settings=integ_settings, registry=registry, docker=docker_client, audit=audit
    )
    yield service
    # Best-effort cleanup of any sessions the test left behind.
    import contextlib

    rows = await registry.list_non_terminal()
    for row in rows:
        with contextlib.suppress(Exception):
            await service.reap_destroy(row, reason="integ-cleanup")


async def test_full_lifecycle_against_real_docker(integ_service):
    """Create → exec echo → destroy. Exercises every bit of plumbing
    the unit tests mock at the DockerClient boundary."""
    row = await integ_service.create("integ", limits=None)
    assert row.status == "RUNNING"
    assert row.container_id is not None

    # Wait briefly for the container to be fully up so exec doesn't
    # race the start.
    time.sleep(0.5)

    out = (
        await __import__("api.exec", fromlist=["ExecService"])
        .ExecService(
            registry=integ_service.registry,
            docker=integ_service.docker,
            audit=integ_service.audit,
        )
        .run(
            row.id,
            "integ",
            __import__("api.models", fromlist=["ExecRequest"]).ExecRequest(argv=["echo", "hello"]),
        )
    )
    assert out.stdout.strip() == "hello"
    assert out.exit_code == 0

    await integ_service.destroy(row.id, "integ")
    fresh = await integ_service.registry.get(row.id, "integ")
    assert fresh is None  # DESTROYED → 404 via SPEC-200


async def test_hardening_flags_applied_on_real_container(integ_service):
    """Verify the canonical ARCH-021 flag-set actually lands on the
    container that Docker creates. Catches docker-py renaming of any
    of the kwargs (the kind of breakage the FakeDockerClient can't see)."""
    row = await integ_service.create("integ", limits=None)

    info = docker.from_env().containers.get(row.container_id).attrs
    host = info["HostConfig"]

    assert host["ReadonlyRootfs"] is True
    assert host["CapDrop"] == ["ALL"]
    sec_opts = host.get("SecurityOpt") or []
    assert "no-new-privileges:true" in sec_opts
    assert "seccomp=unconfined" in sec_opts
    assert info["Config"]["User"] == "10001:10001"
    assert info["Config"]["WorkingDir"] == "/workspace"

    await integ_service.destroy(row.id, "integ")
