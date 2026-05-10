"""Host-capacity snapshot, attached to every ramp result.

Captures the host's CPU/RAM/disk/FD envelope plus Docker version
and runtime list. Without this, a ramp result on a different
machine can't be compared (or fed to the calculator) — the
per-session coefficients are meaningful only relative to the
host they were measured on.

Pure stdlib + the `docker` SDK (already a project dep). On
non-Linux hosts /proc isn't available so several fields are
None — that's fine, the harness still works locally for
smoke testing on macOS even though the real numbers come from
a Linux deploy box.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import resource
import shutil
import socket
import time
from pathlib import Path
from typing import Any


def _read_meminfo() -> dict[str, int]:
    """Returns kib values from /proc/meminfo, or {} on non-Linux."""
    out: dict[str, int] = {}
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                k, _, rest = line.partition(":")
                parts = rest.strip().split()
                if parts and parts[-1].lower() == "kb":
                    out[k] = int(parts[0])
    except FileNotFoundError:
        pass
    return out


def _read_loadavg() -> tuple[float, float, float] | None:
    try:
        return os.getloadavg()
    except (OSError, AttributeError):
        return None


def _read_fd_capacity() -> dict[str, Any]:
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    out: dict[str, Any] = {"rlimit_soft": soft, "rlimit_hard": hard}
    try:
        with open("/proc/sys/fs/file-max") as f:
            out["sys_fs_file_max"] = int(f.read().strip())
        with open("/proc/sys/fs/file-nr") as f:
            allocated, _, _ = f.read().split()
            out["sys_fs_file_nr_allocated"] = int(allocated)
    except (FileNotFoundError, ValueError):
        pass
    return out


def _disk_free(path: str) -> dict[str, int] | None:
    try:
        usage = shutil.disk_usage(path)
        return {"total": usage.total, "free": usage.free, "used": usage.used}
    except OSError:
        return None


def _docker_info() -> dict[str, Any]:
    """Best-effort: queries the docker daemon. Empty dict if docker
    isn't available — the test still has API-side numbers."""
    out: dict[str, Any] = {}
    try:
        import docker  # type: ignore[import-not-found]

        client = docker.from_env(timeout=5)
        version = client.version()
        info = client.info()
        out = {
            "version": version.get("Version"),
            "api_version": version.get("ApiVersion"),
            "runtimes": sorted((info.get("Runtimes") or {}).keys()),
            "default_runtime": info.get("DefaultRuntime"),
            "storage_driver": info.get("Driver"),
            "ncpu": info.get("NCPU"),
            "mem_total": info.get("MemTotal"),
            "data_root": info.get("DockerRootDir"),
            "userns_remap": info.get("SecurityOptions")
            and any("userns" in s for s in (info.get("SecurityOptions") or [])),
        }
    except Exception as e:  # pragma: no cover — diagnostic only.
        out = {"error": f"{type(e).__name__}: {e}"}
    return out


def capture_host_baseline() -> dict[str, Any]:
    """One-shot host snapshot. Safe to call from a fixture."""
    mem = _read_meminfo()
    docker_root = None
    di = _docker_info()
    if isinstance(di, dict):
        docker_root = di.get("data_root")

    return {
        "captured_at": time.time(),
        "captured_at_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "hostname": socket.gethostname(),
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "python_version": platform.python_version(),
        },
        "cpu": {
            "logical_count": os.cpu_count(),
            "loadavg_1_5_15": _read_loadavg(),
        },
        "memory_kib": {
            "total": mem.get("MemTotal"),
            "available": mem.get("MemAvailable"),
            "free": mem.get("MemFree"),
            "buffers": mem.get("Buffers"),
            "cached": mem.get("Cached"),
        },
        "disk": {
            "root": _disk_free("/"),
            "docker_data_root": _disk_free(docker_root) if docker_root else None,
        },
        "fd_capacity": _read_fd_capacity(),
        "docker": di,
    }


def _main() -> int:
    parser = argparse.ArgumentParser(description="Capture host baseline JSON.")
    parser.add_argument("--out", default="-", help="Output path or '-' for stdout.")
    args = parser.parse_args()

    baseline = capture_host_baseline()
    text = json.dumps(baseline, indent=2)
    if args.out == "-":
        print(text)
    else:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
