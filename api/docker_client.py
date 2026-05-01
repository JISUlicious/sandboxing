"""Sole point of policy for Docker interactions.

Every container the service ever creates goes through `hardening_flags`,
which is the canonical materialization of ARCH-021 / SPEC-401. No code
outside this module talks to Docker directly.
"""

from __future__ import annotations

import io
import logging
import posixpath
import tarfile
import time
from dataclasses import dataclass, field
from typing import Any

import docker
from docker.errors import NotFound

from api.config import Settings
from api.models import Limits

log = logging.getLogger("sandbox.docker")

# SPEC-203: per-stream output cap.
OUTPUT_CAP_BYTES = 8 * 1024 * 1024
# `timeout` utility convention: exit 124 when the wall-clock budget is hit.
TIMEOUT_EXIT_CODE = 124


@dataclass
class ExecOutput:
    stdout: bytes
    stderr: bytes
    exit_code: int
    duration_ms: int
    truncated_streams: list[str] = field(default_factory=list)


def _append_capped(buf: bytearray, chunk: bytes, name: str, truncated: set[str]) -> None:
    """Append `chunk` to `buf` until OUTPUT_CAP_BYTES; mark `name` truncated.

    SPEC-203: each stream is capped *independently*; once capped, further
    bytes are discarded but the process continues to run.
    """
    if name in truncated:
        return
    remaining = OUTPUT_CAP_BYTES - len(buf)
    if remaining <= 0:
        truncated.add(name)
        return
    if len(chunk) <= remaining:
        buf.extend(chunk)
    else:
        buf.extend(chunk[:remaining])
        truncated.add(name)


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

    # ----- exec (slice 2) -----

    def exec_in_container(
        self,
        *,
        container_id: str,
        argv: list[str],
        env: dict[str, str],
        timeout_s: int,
    ) -> ExecOutput:
        """Run argv inside the container with a hard wall-clock cap.

        Output is collected with the SPEC-203 8 MiB per-stream cap. The
        process keeps running until natural exit or the `timeout` utility
        kills it; the response either way carries the truncation markers.
        """
        api = self.client.api
        # SPEC-201: deterministic timeout via coreutils' `timeout` (exit 124).
        wrapped = ["/usr/bin/timeout", "--preserve-status", str(timeout_s), *argv]
        exec_id = api.exec_create(
            container_id,
            cmd=wrapped,
            stdout=True,
            stderr=True,
            environment=env or {},
            workdir="/workspace",
            user="10001:10001",
        )["Id"]

        start_ns = time.monotonic_ns()
        stream = api.exec_start(exec_id, detach=False, stream=True, demux=True)

        stdout = bytearray()
        stderr = bytearray()
        truncated: set[str] = set()

        for stdout_chunk, stderr_chunk in stream:
            if stdout_chunk:
                _append_capped(stdout, stdout_chunk, "stdout", truncated)
            if stderr_chunk:
                _append_capped(stderr, stderr_chunk, "stderr", truncated)

        duration_ms = (time.monotonic_ns() - start_ns) // 1_000_000
        exit_code = int(api.exec_inspect(exec_id).get("ExitCode") or 0)
        return ExecOutput(
            stdout=bytes(stdout),
            stderr=bytes(stderr),
            exit_code=exit_code,
            duration_ms=duration_ms,
            truncated_streams=sorted(truncated),
        )

    def _exec_simple(
        self, container_id: str, argv: list[str], *, user: str = "10001:10001"
    ) -> tuple[bytes, bytes, int]:
        """Short utility exec — used by file list / delete helpers."""
        api = self.client.api
        exec_id = api.exec_create(
            container_id,
            cmd=argv,
            stdout=True,
            stderr=True,
            workdir="/workspace",
            user=user,
        )["Id"]
        out, err = api.exec_start(exec_id, detach=False, stream=False, demux=True)
        exit_code = int(api.exec_inspect(exec_id).get("ExitCode") or 0)
        return out or b"", err or b"", exit_code

    # ----- files (slice 2) -----

    def put_archive_file(
        self,
        *,
        container_id: str,
        abs_path: str,
        content: bytes,
        mode: int,
    ) -> None:
        """Write `content` to `abs_path` inside the container via tar stream.

        Parent directories are created (mkdir -p) before the put_archive
        call. Owner is hard-coded to UID/GID 10001 (the agent user) since
        the container runs as that UID; otherwise files would land
        owned by root and be unwritable.
        """
        parent = posixpath.dirname(abs_path)
        name = posixpath.basename(abs_path)
        # mkdir -p as the agent user so the dirs are owned correctly.
        _, _, rc = self._exec_simple(container_id, ["/bin/mkdir", "-p", "--", parent])
        if rc != 0:
            raise RuntimeError(f"mkdir -p {parent} failed (exit {rc})")

        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            info = tarfile.TarInfo(name=name)
            info.size = len(content)
            info.mode = mode & 0o777
            info.uid = 10001
            info.gid = 10001
            info.mtime = int(time.time())
            tar.addfile(info, io.BytesIO(content))

        self.client.api.put_archive(container_id, parent, buf.getvalue())

    def get_archive_file(self, *, container_id: str, abs_path: str) -> tuple[bytes, int]:
        """Read a single file from the container; returns (content, mode)."""
        try:
            stream, _ = self.client.api.get_archive(container_id, abs_path)
        except NotFound as exc:
            raise FileNotFoundError(abs_path) from exc

        buf = io.BytesIO()
        for chunk in stream:
            buf.write(chunk)
        buf.seek(0)
        with tarfile.open(fileobj=buf, mode="r") as tar:
            members = tar.getmembers()
            if not members:
                raise FileNotFoundError(abs_path)
            member = members[0]
            if member.isdir():
                raise IsADirectoryError(abs_path)
            f = tar.extractfile(member)
            if f is None:
                raise IsADirectoryError(abs_path)
            return f.read(), member.mode
