"""Sole point of policy for Docker interactions.

Every container the service ever creates goes through `hardening_flags`,
which is the canonical materialization of ARCH-021 / SPEC-401. No code
outside this module talks to Docker directly.
"""

from __future__ import annotations

import logging
from typing import Any

import docker
from docker.errors import NotFound

from api.config import Settings
from api.models import Limits

log = logging.getLogger("sandbox.docker")


def hardening_flags(
    *,
    session_id: str,
    tenant_id: str,
    volume_name: str,
    limits: Limits,
    image: str,
    network: str,
    dev_mode: bool,
) -> dict[str, Any]:
    """Canonical hardening kwargs for `containers.create`. ARCH-021."""
    flags: dict[str, Any] = {
        "image": image,
        "name": f"sandbox-{session_id}",
        "detach": True,
        "read_only": True,
        "tmpfs": {"/tmp": "size=256m,mode=1777,noexec,nosuid,nodev"},
        "volumes": {volume_name: {"bind": "/workspace", "mode": "rw"}},
        "user": "10001:10001",
        "working_dir": "/workspace",
        "cap_drop": ["ALL"],
        "security_opt": [
            "no-new-privileges:true",
            # runsc filters syscalls; layering runc's default seccomp on
            # top adds nothing useful (ARCH-021 commentary).
            "seccomp=unconfined",
        ],
        # userns_mode is intentionally omitted so the container inherits
        # the daemon's `userns-remap=default` mapping (SPEC-401).
        "pids_limit": limits.pids,
        "mem_limit": f"{limits.memory_mib}m",
        "nano_cpus": limits.vcpu * 1_000_000_000,
        "network": network,
        "environment": {
            "HTTPS_PROXY": "http://proxy:3128",
            "HTTP_PROXY": "http://proxy:3128",
            "NO_PROXY": "",
            "HOME": "/workspace",
            "USER": "agent",
        },
        "ulimits": [docker.types.Ulimit(name="nofile", soft=limits.nofile, hard=limits.nofile)],
        "entrypoint": ["/usr/bin/sleep", "infinity"],
        "labels": {
            "sandbox.session_id": session_id,
            "sandbox.tenant_id": tenant_id,
        },
    }
    # SPEC-400: `runtime=runsc` mandatory in production. SPEC-302 dev mode
    # falls back to the daemon's default runtime so the API is usable on
    # macOS / Windows / non-runsc Linux hosts.
    if not dev_mode:
        flags["runtime"] = "runsc"
    return flags


class DockerClient:
    def __init__(self, settings: Settings):
        self._settings = settings
        self._client: docker.DockerClient | None = None

    @property
    def client(self) -> docker.DockerClient:
        if self._client is None:
            self._client = docker.from_env()
        return self._client

    def health(self) -> bool:
        try:
            self.client.ping()
            return True
        except Exception:
            return False

    def ensure_runtime(self) -> None:
        """SPEC-400 enforcement, with SPEC-302 dev-mode bypass."""
        if self._settings.dev_mode:
            log.warning("dev mode: skipping runsc runtime check (SPEC-302)")
            return
        info = self.client.info() or {}
        runtimes = info.get("Runtimes") or {}
        if "runsc" not in runtimes:
            raise RuntimeError(
                "runsc runtime is not registered with the Docker daemon "
                "(required by SPEC-400). Set SANDBOX_DEV_MODE=1 for local dev."
            )

    def ensure_network(self) -> None:
        name = self._settings.network_name
        try:
            self.client.networks.get(name)
        except NotFound:
            log.info("creating network %s", name)
            self.client.networks.create(
                name,
                driver="bridge",
                # iptables rules + egress proxy land in slice 4. For
                # slice 1 we just create the dedicated bridge so traffic
                # doesn't ride the default `bridge` network.
                labels={"sandbox.managed": "true"},
            )

    def create_volume(self, volume_name: str, session_id: str, tenant_id: str) -> None:
        self.client.volumes.create(
            name=volume_name,
            labels={
                "sandbox.session_id": session_id,
                "sandbox.tenant_id": tenant_id,
            },
        )

    def remove_volume(self, volume_name: str) -> None:
        try:
            self.client.volumes.get(volume_name).remove(force=True)
        except NotFound:
            return  # idempotent (ARCH-051 reconcile)

    def create_container(
        self,
        *,
        session_id: str,
        tenant_id: str,
        volume_name: str,
        limits: Limits,
    ) -> str:
        flags = hardening_flags(
            session_id=session_id,
            tenant_id=tenant_id,
            volume_name=volume_name,
            limits=limits,
            image=self._settings.sandbox_image,
            network=self._settings.network_name,
            dev_mode=self._settings.dev_mode,
        )
        container = self.client.containers.create(**flags)
        return container.id

    def start_container(self, container_id: str) -> None:
        self.client.containers.get(container_id).start()

    def stop_container(self, container_id: str, timeout: int = 5) -> None:
        try:
            self.client.containers.get(container_id).stop(timeout=timeout)
        except NotFound:
            return  # idempotent

    def remove_container(self, container_id: str) -> None:
        try:
            self.client.containers.get(container_id).remove(force=True)
        except NotFound:
            return  # idempotent (ARCH-051 reconcile)
