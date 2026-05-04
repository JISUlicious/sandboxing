"""Sole point of policy for Docker interactions.

Every container the service ever creates goes through `hardening_flags`,
which is the canonical materialization of ARCH-021 / SPEC-401. No code
outside this module talks to Docker directly.
"""

from __future__ import annotations

import io
import logging
import os
import posixpath
import socket as _socket
import tarfile
import time
from collections.abc import Iterator
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


def _wrap_with_timeout(argv: list[str], timeout_s: int) -> list[str]:
    """Prepend coreutils' `timeout` so the wall-clock budget is enforced
    by the kernel rather than by the control plane.

    NOTE: do NOT pass `--preserve-status`. With it, GNU `timeout`
    returns the inner program's signal-exit code (e.g. 143 for SIGTERM)
    instead of 124 on timeout, which breaks the exec_timeout mapping.
    See `TIMEOUT_EXIT_CODE` and the `ExecTimeout` raise in `ExecService`.
    """
    return ["/usr/bin/timeout", str(timeout_s), *argv]


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
    proxy_url: str = "http://proxy:3128",
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
            "HTTPS_PROXY": proxy_url,
            "HTTP_PROXY": proxy_url,
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
        labels = {
            "sandbox.session_id": session_id,
            "sandbox.tenant_id": tenant_id,
        }
        bind_base = self._settings.quota_volume_base
        # When a quota volume base is configured, bind the Docker volume
        # to a per-session directory there. This is what makes
        # SPEC-302's XFS project quota actually apply — Docker's default
        # volume location (/var/lib/docker/volumes) usually isn't on
        # the prjquota-enabled filesystem.
        if str(bind_base):
            bind_path = bind_base / session_id
            bind_path.mkdir(parents=True, exist_ok=True)
            # SPEC-401: with userns-remap, container UID 10001 lands on
            # a host subuid (e.g. 110001). Chown the bind to that UID +
            # 0700 so the workspace is writable to the agent only.
            uid = self._settings.bind_volume_uid
            if uid is not None:
                os.chown(bind_path, uid, uid)
                bind_path.chmod(0o700)
            else:
                # Dev / non-userns-remap fallback. The lifespan logs a
                # one-time warning recommending bind_volume_uid.
                bind_path.chmod(0o777)
            self.client.volumes.create(
                name=volume_name,
                driver="local",
                driver_opts={
                    "type": "none",
                    "device": str(bind_path),
                    "o": "bind",
                },
                labels=labels,
            )
        else:
            self.client.volumes.create(name=volume_name, labels=labels)

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
            proxy_url=self._settings.egress_proxy_url,
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

    def container_exists(self, container_id: str) -> bool:
        """Used by startup reconciliation (slice 6a). Returns False if
        the container has been removed (e.g. across a daemon restart);
        any other docker error is treated as 'present' to avoid
        falsely orphaning a session whose container is fine."""
        try:
            self.client.containers.get(container_id)
            return True
        except NotFound:
            return False
        except Exception:
            return True

    def container_stats(self, container_id: str) -> dict[str, Any]:
        """Single-shot stats snapshot (slice 6b). docker-py's
        `container.stats(stream=False)` returns one dict; we extract
        the fields the sampler actually uses to avoid pinning callers
        to docker's full payload shape."""
        try:
            raw = self.client.containers.get(container_id).stats(stream=False)
        except NotFound:
            return {}

        cpu = raw.get("cpu_stats", {}) or {}
        precpu = raw.get("precpu_stats", {}) or {}
        cpu_total = (cpu.get("cpu_usage") or {}).get("total_usage", 0)
        precpu_total = (precpu.get("cpu_usage") or {}).get("total_usage", 0)
        sys_total = cpu.get("system_cpu_usage", 0)
        presys_total = precpu.get("system_cpu_usage", 0)
        online_cpus = cpu.get("online_cpus") or 1
        cpu_delta = cpu_total - precpu_total
        sys_delta = sys_total - presys_total
        cpu_percent = (cpu_delta / sys_delta) * online_cpus * 100.0 if sys_delta > 0 else 0.0

        mem = raw.get("memory_stats", {}) or {}
        # Docker reports `usage` minus the cached pages — match what
        # `docker stats` shows in the CLI.
        mem_usage = mem.get("usage", 0)
        cache = (mem.get("stats") or {}).get("cache", 0)
        mem_used = max(0, mem_usage - cache)
        mem_limit = mem.get("limit", 0) or 0

        blkio = raw.get("blkio_stats", {}) or {}
        bytes_io = blkio.get("io_service_bytes_recursive") or []
        read_bytes = sum(e["value"] for e in bytes_io if e.get("op") in ("Read", "read"))
        write_bytes = sum(e["value"] for e in bytes_io if e.get("op") in ("Write", "write"))

        return {
            "cpu_percent": round(cpu_percent, 2),
            "memory_bytes": int(mem_used),
            "memory_limit_bytes": int(mem_limit),
            "blkio_read_bytes": int(read_bytes),
            "blkio_write_bytes": int(write_bytes),
        }

    # ----- exec (slice 2) -----

    def exec_in_container(
        self,
        *,
        container_id: str,
        argv: list[str],
        env: dict[str, str],
        timeout_s: int,
        stdin_bytes: bytes | None = None,
    ) -> ExecOutput:
        """Run argv inside the container with a hard wall-clock cap.

        Output is collected with the SPEC-203 8 MiB per-stream cap. The
        process keeps running until natural exit or the `timeout` utility
        kills it; the response either way carries the truncation markers.

        When `stdin_bytes` is provided, the exec is started in socket
        mode and the input is sent before reading framed output.
        """
        if stdin_bytes is not None:
            return self._exec_with_stdin(
                container_id=container_id,
                argv=argv,
                env=env,
                timeout_s=timeout_s,
                stdin_bytes=stdin_bytes,
            )

        api = self.client.api
        # SPEC-201: deterministic timeout via coreutils' `timeout` (exit 124).
        wrapped = _wrap_with_timeout(argv, timeout_s)
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

    def _exec_with_stdin(
        self,
        *,
        container_id: str,
        argv: list[str],
        env: dict[str, str],
        timeout_s: int,
        stdin_bytes: bytes,
    ) -> ExecOutput:
        """Exec with stdin via socket mode, reading docker's framed stream."""
        from docker.utils.socket import frames_iter

        api = self.client.api
        wrapped = _wrap_with_timeout(argv, timeout_s)
        exec_id = api.exec_create(
            container_id,
            cmd=wrapped,
            stdin=True,
            stdout=True,
            stderr=True,
            environment=env or {},
            workdir="/workspace",
            user="10001:10001",
        )["Id"]

        start_ns = time.monotonic_ns()
        sock = api.exec_start(exec_id, detach=False, stream=False, socket=True)
        raw = getattr(sock, "_sock", sock)
        try:
            raw.sendall(stdin_bytes)
            # Half-close the write side so the inner process sees EOF on stdin.
            raw.shutdown(_socket.SHUT_WR)
        except OSError:
            # Container may have closed early; fall through to drain output.
            pass

        stdout = bytearray()
        stderr = bytearray()
        truncated: set[str] = set()
        for stream_type, payload in frames_iter(sock, tty=False):
            if not payload:
                continue
            if stream_type == 1:
                _append_capped(stdout, payload, "stdout", truncated)
            elif stream_type == 2:
                _append_capped(stderr, payload, "stderr", truncated)

        duration_ms = (time.monotonic_ns() - start_ns) // 1_000_000
        exit_code = int(api.exec_inspect(exec_id).get("ExitCode") or 0)
        return ExecOutput(
            stdout=bytes(stdout),
            stderr=bytes(stderr),
            exit_code=exit_code,
            duration_ms=duration_ms,
            truncated_streams=sorted(truncated),
        )

    def exec_stream_in_container(
        self,
        *,
        container_id: str,
        argv: list[str],
        env: dict[str, str],
        timeout_s: int,
    ) -> Iterator[tuple[str, bytes | int]]:
        """Sync generator yielding live exec events.

        Events:
        - ('stdout', bytes) — chunk of stdout (raw, uncapped — caller decides).
        - ('stderr', bytes) — chunk of stderr.
        - ('exit', exit_code) — terminal event with the process exit code.

        The caller (typically `ExecService.run_stream`) is responsible
        for applying the SPEC-203 cap and for emitting SSE frames.
        """
        api = self.client.api
        wrapped = _wrap_with_timeout(argv, timeout_s)
        exec_id = api.exec_create(
            container_id,
            cmd=wrapped,
            stdout=True,
            stderr=True,
            environment=env or {},
            workdir="/workspace",
            user="10001:10001",
        )["Id"]

        stream = api.exec_start(exec_id, detach=False, stream=True, demux=True)
        for stdout_chunk, stderr_chunk in stream:
            if stdout_chunk:
                yield ("stdout", stdout_chunk)
            if stderr_chunk:
                yield ("stderr", stderr_chunk)

        exit_code = int(api.exec_inspect(exec_id).get("ExitCode") or 0)
        yield ("exit", exit_code)

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
