"""Capacity calculator for the sandbox service.

Reads the latest ramp result (or one named explicitly), applies a
linear sizing model, and prints a host recommendation in plain text.

Run after `pytest tests/load/test_ramp.py -m load` produces a
result file. There is no built-in default measurement: the
recommendation is only meaningful when sourced from a ramp run on
the operator's hardware. Without a result file we error with a
clear "run the ramp test first" message.

Linear model (per-session cost taken from the deepest clean ramp
level; baseline taken from host_baseline + control-plane samples):

    ram_gib   = baseline_gib + N * per_session_rss_gib * (1 + safety)
    cpu_cores = baseline_cores
                + ceil(N * active_fraction * per_session_cpu_pct / 100)
                  * (1 + safety)
    disk_gib  = N * 2 + audit_growth_per_day * 30 + 50
    fd_limit  = 1024 + N * 16

`active_fraction` weights how many sessions are running real load at
once. Default 0.5 — half active, half idle. RSS is paid by all
sessions (idle still has gVisor + agent + cached pages); CPU is
paid only by active ones.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

DEFAULT_RESULTS_DIR = Path("tests/load/results")
DEFAULT_AUDIT_GROWTH_MIB_PER_DAY = 50


def find_latest_result(results_dir: Path) -> Path | None:
    if not results_dir.exists():
        return None
    candidates = sorted(results_dir.glob("ramp_*.json"))
    return candidates[-1] if candidates else None


def load_result(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def _baseline_ram_gib(result: dict[str, Any]) -> float:
    """Approximate the *non-session* baseline RAM the host needs:
    control plane + reaper + sampler + Docker daemon + kernel headroom.
    Read from the first level's host_samples + cp_samples; these are
    sampled with sessions running, so we subtract the per-session cost
    we're about to multiply back in."""
    levels = result.get("levels") or []
    if not levels:
        return 1.0  # conservative.
    first = levels[0]
    cp = first.get("control_plane_samples") or []
    cp_rss_bytes = max((s.get("mem_rss_bytes") or 0) for s in cp) if cp else 0
    cp_gib = cp_rss_bytes / (1024**3)
    # Plus a small slack for daemon + kernel + page cache headroom.
    return round(cp_gib + 1.0, 2)


def _baseline_cores(result: dict[str, Any]) -> float:
    """Round-up baseline CPU: 1 core for control plane + 1 core for
    Docker daemon + sampler. We don't try to measure idle host CPU; the
    aim is "give the operator a number that matches a sensible cloud
    instance shape", and 2 cores baseline is consistent with that."""
    return 2.0


def _per_session_rss_gib(result: dict[str, Any]) -> float | None:
    coeff = result.get("session_cost_coefficients")
    if not coeff or coeff.get("avg_session_rss_mib") is None:
        return None
    return coeff["avg_session_rss_mib"] / 1024


def _per_session_cpu_pct(result: dict[str, Any]) -> float | None:
    coeff = result.get("session_cost_coefficients")
    if not coeff or coeff.get("avg_session_cpu_pct") is None:
        return None
    return coeff["avg_session_cpu_pct"]


def _suggest_host_shape(cpu: int, ram_gib: int) -> str:
    """Round each up to a familiar instance shape. Stays generic on
    purpose — we don't name cloud instance families."""

    def _round_up_pow2(x: int, choices: list[int]) -> int:
        for c in choices:
            if c >= x:
                return c
        return choices[-1]

    cpu_choices = [2, 4, 8, 16, 32, 48, 64, 96, 128]
    ram_choices = [4, 8, 16, 32, 64, 128, 192, 256, 384, 512]
    return f"~{_round_up_pow2(cpu, cpu_choices)} vCPU / {_round_up_pow2(ram_gib, ram_choices)} GiB"


def _format_tested_range(result: dict[str, Any]) -> str:
    last_clean = result.get("last_clean_level")
    deg = result.get("degradation_level")
    parts = []
    if last_clean is not None:
        parts.append(f"tested cleanly up to {last_clean}")
    if deg is not None:
        parts.append(f"degradation observed at {deg}")
    return "; ".join(parts) if parts else "no clean range recorded"


def _host_label(result: dict[str, Any]) -> str:
    hb = result.get("host_baseline") or {}
    docker = hb.get("docker") or {}
    cpu = docker.get("ncpu") or hb.get("cpu", {}).get("logical_count")
    mem_total = docker.get("mem_total")
    mem_gib = round(mem_total / (1024**3)) if mem_total else None
    runtime = docker.get("default_runtime") or "?"
    if cpu and mem_gib:
        return f"{cpu} vCPU / {mem_gib} GiB ({runtime})"
    return hb.get("hostname") or "unknown host"


def render(
    *,
    result: dict[str, Any],
    result_path: Path,
    n: int,
    active_fraction: float,
    safety: float,
    audit_growth_mib_per_day: int,
) -> str:
    rss_gib = _per_session_rss_gib(result)
    cpu_pct = _per_session_cpu_pct(result)
    if rss_gib is None or cpu_pct is None:
        return (
            f"ERROR: result file {result_path} has no per-session resource "
            "coefficients (avg_session_rss_mib / avg_session_cpu_pct).\n"
            "Re-run with local docker reachable from the harness so "
            "per-container stats can be collected.\n"
        )

    base_ram_gib = _baseline_ram_gib(result)
    base_cores = _baseline_cores(result)

    ram_gib = base_ram_gib + n * rss_gib * (1 + safety)
    cpu_cores = base_cores + math.ceil(n * active_fraction * cpu_pct / 100.0) * (1 + safety)
    disk_gib = n * 2 + (audit_growth_mib_per_day / 1024) * 30 + 50
    fd_limit = 1024 + n * 16

    cpu_int = int(math.ceil(cpu_cores))
    ram_int = int(math.ceil(ram_gib))
    disk_int = int(math.ceil(disk_gib))
    # Round fd_limit up to a power of 2 — easier to set in /etc/security.
    fd_pow2 = 1
    while fd_pow2 < fd_limit:
        fd_pow2 *= 2

    return (
        "Sandbox capacity recommendation\n"
        "================================\n\n"
        f"Workload: {n} concurrent sessions, "
        f"{int(active_fraction * 100)}% active.\n\n"
        f"Resources needed (with {int(safety * 100)}% safety margin):\n"
        f"  CPU      : {cpu_int} cores\n"
        f"  RAM      : {ram_int} GiB\n"
        f"  Disk     : {disk_int} GiB\n"
        f"  FD limit : {fd_pow2}\n\n"
        f"Recommended host: {_suggest_host_shape(cpu_int, ram_int)} "
        f"/ {disk_int} GiB SSD.\n\n"
        f"Source: {result_path}\n"
        f"  measured on host: {_host_label(result)}\n"
        f"  per-session RSS: {rss_gib * 1024:.0f} MiB; CPU at load: "
        f"{cpu_pct:.1f}%\n"
        f"  ramp coverage: {_format_tested_range(result)}\n"
        + (
            f"  NOTE: target N={n} is above tested ceiling "
            f"({result.get('last_clean_level')}); "
            "numbers are extrapolated — re-run the ramp at higher "
            "--max-sessions for tighter sizing.\n"
            if (result.get("last_clean_level") is not None and n > result["last_clean_level"])
            else ""
        )
    )


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Sandbox host sizing calculator.")
    p.add_argument(
        "--concurrent-sessions",
        type=int,
        required=True,
        help="Target concurrent sandbox sessions.",
    )
    p.add_argument(
        "--active-fraction",
        type=float,
        default=0.5,
        help="Fraction of sessions running active load at once. Default 0.5.",
    )
    p.add_argument(
        "--safety-margin",
        type=float,
        default=0.25,
        help="Multiplier added to per-session RAM and CPU. Default 0.25 (25%%).",
    )
    p.add_argument(
        "--audit-growth-mib-per-day",
        type=int,
        default=DEFAULT_AUDIT_GROWTH_MIB_PER_DAY,
        help=f"Audit log growth estimate. Default {DEFAULT_AUDIT_GROWTH_MIB_PER_DAY} MiB/day.",
    )
    p.add_argument(
        "--results",
        default="latest",
        help="Path to a ramp_*.json file, or 'latest' to pick the newest in --results-dir.",
    )
    p.add_argument(
        "--results-dir",
        type=Path,
        default=DEFAULT_RESULTS_DIR,
        help="Directory holding ramp_*.json files. Default tests/load/results.",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])

    if args.results == "latest":
        path = find_latest_result(args.results_dir)
        if path is None:
            print(
                f"ERROR: no ramp_*.json files in {args.results_dir}.\n"
                "Run the ramp test first:\n"
                "  uv run pytest tests/load/test_ramp.py -m load "
                "--base-url=... --token=...",
                file=sys.stderr,
            )
            return 2
    else:
        path = Path(args.results)
        if not path.exists():
            print(f"ERROR: {path} does not exist.", file=sys.stderr)
            return 2

    result = load_result(path)
    print(
        render(
            result=result,
            result_path=path,
            n=args.concurrent_sessions,
            active_fraction=args.active_fraction,
            safety=args.safety_margin,
            audit_growth_mib_per_day=args.audit_growth_mib_per_day,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
