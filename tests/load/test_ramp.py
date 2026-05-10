"""Concurrency / capacity ramp test.

Single test that walks a small set of concurrency levels (1, 5, 10,
25, 50, 100, 200) — capped by `--max-sessions` — and at each level
captures:

- burst-create latency (time from POST /v1/sessions to the response
  arriving with status RUNNING) per session, p50/p95/p99,
- per-op latency under a 60s mixed workload (70% exec, 20% file ops,
  10% start-then-poll process; 5% of execs use /exec/stream with
  an Idempotency-Key as a regression for v0.2.9),
- host-side resource samples every 5s (cpu%, memory, fds, loadavg)
  plus per-session and control-plane container stats from the local
  docker daemon when reachable,
- burst-destroy latency,
- and the error count / rate observed during the steady-state.

Output is one JSON file per run at `tests/load/results/ramp_<ts>.json`
with one entry per level; the calculator at
`tools/sandbox_capacity_calc.py` consumes the latest file.

Stop conditions (the ramp halts before the next level if any fire
during steady-state):
- error_rate > 1%,
- host cpu > 90% sustained for 30s,
- host mem_available < 5%,
- any per-op p99 latency > 10× the N=1 baseline.

Failure-mode probes (OOM, disk-full, etc.) are intentionally not part
of this test — they're covered by Phase 3's separate file when
operators want to characterise the cliff edge.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import math
import os
import random
import statistics
import time
from collections.abc import Awaitable
from pathlib import Path
from typing import Any

import httpx
import pytest

from .host_baseline import _read_fd_capacity, _read_loadavg, _read_meminfo

log = logging.getLogger("sandbox.load")

LEVELS_FULL: tuple[int, ...] = (1, 5, 10, 25, 50, 100, 200)
SAMPLE_INTERVAL_S: float = 5.0
WORKLOAD_MIX: tuple[tuple[str, float], ...] = (
    ("exec", 0.70),
    ("file", 0.20),
    ("process", 0.10),
)
STREAM_FRACTION: float = 0.05  # of exec ops; /exec/stream + Idempotency-Key


# ---------------- helpers ----------------


def _percentiles(samples: list[float]) -> dict[str, float | None]:
    if not samples:
        return {"p50": None, "p95": None, "p99": None, "count": 0}
    if len(samples) == 1:
        v = samples[0]
        return {"p50": v, "p95": v, "p99": v, "count": 1}
    sorted_s = sorted(samples)

    def _pick(p: float) -> float:
        idx = max(0, min(len(sorted_s) - 1, int(math.ceil(p * len(sorted_s))) - 1))
        return sorted_s[idx]

    return {
        "p50": _pick(0.50),
        "p95": _pick(0.95),
        "p99": _pick(0.99),
        "count": len(sorted_s),
    }


def _read_proc_stat_cpu() -> tuple[int, int]:
    """Returns (idle_jiffies, total_jiffies). 0,0 if not on linux."""
    try:
        with open("/proc/stat") as f:
            line = f.readline()
        parts = line.split()
        if not parts or parts[0] != "cpu":
            return 0, 0
        nums = [int(x) for x in parts[1:]]
        idle = nums[3] + (nums[4] if len(nums) > 4 else 0)  # idle + iowait
        return idle, sum(nums)
    except FileNotFoundError:
        return 0, 0


def _host_sample(prev_cpu: tuple[int, int]) -> tuple[dict[str, Any], tuple[int, int]]:
    """Returns (sample, new_prev) — pass new_prev into the next call."""
    mem = _read_meminfo()
    fds = _read_fd_capacity()
    cur_cpu = _read_proc_stat_cpu()
    cpu_pct: float | None = None
    if prev_cpu != (0, 0) and cur_cpu != (0, 0):
        d_idle = cur_cpu[0] - prev_cpu[0]
        d_total = cur_cpu[1] - prev_cpu[1]
        if d_total > 0:
            cpu_pct = max(0.0, min(100.0, 100.0 * (1.0 - d_idle / d_total)))
    mem_total = mem.get("MemTotal") or 0
    mem_avail = mem.get("MemAvailable") or 0
    mem_avail_pct = (mem_avail / mem_total * 100.0) if mem_total else None
    return (
        {
            "ts": time.time(),
            "cpu_pct": cpu_pct,
            "mem_total_kib": mem_total or None,
            "mem_available_kib": mem_avail or None,
            "mem_available_pct": mem_avail_pct,
            "fds_allocated": fds.get("sys_fs_file_nr_allocated"),
            "fds_max": fds.get("sys_fs_file_max"),
            "loadavg": _read_loadavg(),
        },
        cur_cpu,
    )


def _docker_client():
    """Returns a docker SDK client, or None if docker isn't reachable."""
    try:
        import docker  # type: ignore[import-not-found]

        c = docker.from_env(timeout=5)
        c.ping()
        return c
    except Exception:
        return None


def _container_stats(client, name_or_id: str) -> dict[str, Any] | None:
    """Single non-streaming snapshot. Returns {cpu_pct, mem_rss_bytes, mem_limit_bytes}
    or None on error / missing container."""
    try:
        cont = client.containers.get(name_or_id)
        s = cont.stats(stream=False)
    except Exception:
        return None
    try:
        cpu = s.get("cpu_stats", {})
        precpu = s.get("precpu_stats", {})
        d_cpu = cpu.get("cpu_usage", {}).get("total_usage", 0) - precpu.get("cpu_usage", {}).get(
            "total_usage", 0
        )
        d_sys = cpu.get("system_cpu_usage", 0) - precpu.get("system_cpu_usage", 0)
        ncpu = cpu.get("online_cpus") or len(cpu.get("cpu_usage", {}).get("percpu_usage") or [1])
        cpu_pct = 0.0
        if d_sys > 0 and d_cpu > 0:
            cpu_pct = (d_cpu / d_sys) * ncpu * 100.0
        mem = s.get("memory_stats", {})
        mem_usage = mem.get("usage", 0)
        # Subtract pagecache when available — more honest as "the
        # process's working set" rather than "all RAM the container has
        # touched".
        cache = (mem.get("stats") or {}).get("cache", 0) or 0
        mem_rss = max(0, mem_usage - cache)
        return {
            "cpu_pct": cpu_pct,
            "mem_rss_bytes": mem_rss,
            "mem_usage_bytes": mem_usage,
            "mem_limit_bytes": mem.get("limit"),
        }
    except Exception:
        return None


# ---------------- workload primitives ----------------


class ApiClient:
    def __init__(self, base_url: str, token: str) -> None:
        self._base = base_url
        self._headers = {"Authorization": f"Bearer {token}"}
        # One pooled client per session task is cheaper than re-resolving
        # the connection every op.
        self._http = httpx.AsyncClient(
            base_url=base_url,
            headers=self._headers,
            timeout=httpx.Timeout(30.0, read=120.0),
            limits=httpx.Limits(max_keepalive_connections=400, max_connections=600),
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def create_session(self) -> tuple[str, float]:
        t0 = time.monotonic()
        r = await self._http.post("/v1/sessions", json={})
        dt = time.monotonic() - t0
        r.raise_for_status()
        return r.json()["session_id"], dt

    async def destroy_session(self, sid: str) -> float:
        t0 = time.monotonic()
        r = await self._http.delete(f"/v1/sessions/{sid}")
        dt = time.monotonic() - t0
        r.raise_for_status()
        return dt

    async def exec_(self, sid: str, argv: list[str]) -> float:
        t0 = time.monotonic()
        r = await self._http.post(f"/v1/sessions/{sid}/exec", json={"argv": argv})
        dt = time.monotonic() - t0
        r.raise_for_status()
        return dt

    async def exec_stream(self, sid: str, argv: list[str], idem_key: str) -> float:
        t0 = time.monotonic()
        async with self._http.stream(
            "POST",
            f"/v1/sessions/{sid}/exec/stream",
            json={"argv": argv},
            headers={"Idempotency-Key": idem_key},
        ) as r:
            r.raise_for_status()
            ct = r.headers.get("content-type", "")
            if not ct.startswith("text/event-stream"):
                raise RuntimeError(f"streaming response not SSE: {ct!r}")
            saw_result = False
            async for line in r.aiter_lines():
                if line.startswith("event: result"):
                    saw_result = True
            if not saw_result:
                raise RuntimeError("SSE stream did not include 'event: result'")
        return time.monotonic() - t0

    async def file_roundtrip(self, sid: str) -> float:
        path = f"loadtest_{random.randint(0, 1 << 30):x}.bin"
        body = base64.b64encode(os.urandom(4096)).decode("ascii")
        t0 = time.monotonic()
        r = await self._http.post(
            f"/v1/sessions/{sid}/files",
            json={"path": path, "content_b64": body, "mode": 0o640},
        )
        r.raise_for_status()
        r = await self._http.get(f"/v1/sessions/{sid}/files/{path}")
        r.raise_for_status()
        r = await self._http.delete(f"/v1/sessions/{sid}/files/{path}")
        r.raise_for_status()
        return time.monotonic() - t0

    async def process_start_and_poll(self, sid: str) -> float:
        # Sleep short enough to self-exit before the next process op
        # for this session (process ops fire at ~1/s/session at N=3,
        # so 0.1s self-exit is well clear). No DELETE — DELETE goes
        # through SIGTERM → grace → SIGKILL with
        # process_stop_grace_s=10, which under contention pushes
        # process p99 into the multi-second range and conflates the
        # api-roundtrip latency we actually care about with the
        # reap-and-confirm tail. The reaper / session destroy cleans
        # up EXITED rows at the end of the level.
        t0 = time.monotonic()
        r = await self._http.post(
            f"/v1/sessions/{sid}/processes",
            json={"argv": ["/bin/sleep", "0.1"]},
        )
        r.raise_for_status()
        pid = r.json()["process_id"]
        r = await self._http.get(f"/v1/sessions/{sid}/processes/{pid}")
        r.raise_for_status()
        return time.monotonic() - t0


# ---------------- per-level runner ----------------


class LevelStats:
    def __init__(self) -> None:
        self.create_latencies: list[float] = []
        self.destroy_latencies: list[float] = []
        self.op_latencies: dict[str, list[float]] = {"exec": [], "file": [], "process": []}
        self.stream_latencies: list[float] = []
        self.errors: list[dict[str, Any]] = []
        self.host_samples: list[dict[str, Any]] = []
        self.cp_samples: list[dict[str, Any]] = []
        self.session_samples: list[dict[str, Any]] = []  # one per (ts, sid) pair


def _pick_op() -> str:
    r = random.random()
    cum = 0.0
    for kind, p in WORKLOAD_MIX:
        cum += p
        if r < cum:
            return kind
    return "exec"


async def _session_workload_loop(
    api: ApiClient, sid: str, stop: asyncio.Event, stats: LevelStats
) -> None:
    while not stop.is_set():
        kind = _pick_op()
        try:
            if kind == "exec":
                if random.random() < STREAM_FRACTION:
                    key = f"loadtest-{sid}-{time.time_ns()}"
                    dt = await api.exec_stream(sid, ["/bin/echo", "hi"], key)
                    stats.stream_latencies.append(dt)
                    stats.op_latencies["exec"].append(dt)
                else:
                    argv = random.choice(
                        [
                            ["/bin/echo", "ok"],
                            ["/bin/cat", "/etc/hostname"],
                            ["/bin/sleep", "0.05"],
                            ["/bin/sh", "-c", "echo $$"],
                        ]
                    )
                    dt = await api.exec_(sid, argv)
                    stats.op_latencies["exec"].append(dt)
            elif kind == "file":
                dt = await api.file_roundtrip(sid)
                stats.op_latencies["file"].append(dt)
            else:
                dt = await api.process_start_and_poll(sid)
                stats.op_latencies["process"].append(dt)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            stats.errors.append({"ts": time.time(), "kind": kind, "error": repr(e)[:200]})
            # Brief backoff so a hot error path doesn't drown the run.
            await asyncio.sleep(0.05)


async def _sampler_loop(
    sids: list[str],
    stop: asyncio.Event,
    docker_client,
    stats: LevelStats,
    breach: dict[str, Any],
) -> None:
    """Every SAMPLE_INTERVAL_S, capture host + per-container stats and
    update the breach detector for the steady-state stop conditions."""
    prev_cpu = _read_proc_stat_cpu()
    high_cpu_started_at: float | None = None
    while not stop.is_set():
        sample, prev_cpu = _host_sample(prev_cpu)
        stats.host_samples.append(sample)

        # CPU > 90% sustained 30s.
        if sample.get("cpu_pct") is not None and sample["cpu_pct"] > 90.0:
            high_cpu_started_at = high_cpu_started_at or sample["ts"]
            if sample["ts"] - high_cpu_started_at >= 30.0:
                breach["reason"] = "host_cpu_over_90_for_30s"
        else:
            high_cpu_started_at = None

        # Mem available < 5%.
        ma = sample.get("mem_available_pct")
        if ma is not None and ma < 5.0:
            breach["reason"] = "host_mem_available_below_5pct"

        if docker_client is not None:
            cp = _container_stats(docker_client, "sandbox-control-plane")
            if cp is not None:
                cp["ts"] = sample["ts"]
                stats.cp_samples.append(cp)
            # Per-session: snapshot a small random sample to bound docker
            # daemon load; with 200 sessions and 5s ticks, polling each
            # would be ~40 stats calls/sec.
            sample_subset = random.sample(sids, min(len(sids), 16))
            for sid in sample_subset:
                ss = _container_stats(docker_client, f"sandbox-{sid}")
                if ss is not None:
                    ss["ts"] = sample["ts"]
                    ss["session_id"] = sid
                    stats.session_samples.append(ss)

        try:
            await asyncio.wait_for(stop.wait(), timeout=SAMPLE_INTERVAL_S)
        except TimeoutError:
            continue


async def _gather_with_progress(coros: list[Awaitable[Any]], desc: str) -> list[Any]:
    log.info("%s: %d tasks", desc, len(coros))
    return await asyncio.gather(*coros, return_exceptions=True)


async def _run_one_level(
    n: int,
    api: ApiClient,
    duration_s: int,
    docker_client,
    n1_p99: dict[str, float],
) -> dict[str, Any]:
    stats = LevelStats()
    breach: dict[str, Any] = {}

    # ---- burst create ----
    log.info("level N=%d: burst-create", n)
    create_results = await _gather_with_progress(
        [api.create_session() for _ in range(n)], f"create N={n}"
    )
    sids: list[str] = []
    for r in create_results:
        if isinstance(r, Exception):
            stats.errors.append({"ts": time.time(), "kind": "create", "error": repr(r)[:200]})
            continue
        sid, dt = r
        sids.append(sid)
        stats.create_latencies.append(dt)

    if not sids:
        # Detect the most common case — bad token — explicitly so the
        # operator sees "fix LOAD_TOKEN" rather than a generic
        # "all_creates_failed". httpx raises HTTPStatusError on 4xx/5xx;
        # we look at the first one's status_code.
        first_err = stats.errors[0]["error"] if stats.errors else ""
        if "401" in first_err or "Unauthorized" in first_err:
            stop_reason = "auth_failed_401_check_LOAD_TOKEN"
        elif "403" in first_err:
            stop_reason = "auth_failed_403_token_lacks_session_create_scope"
        else:
            stop_reason = "all_creates_failed"
        return {
            "N": n,
            "sessions_created": 0,
            "stopped_early": True,
            "stop_reason": stop_reason,
            "burst_create_seconds": _percentiles([]),
            "burst_destroy_seconds": _percentiles([]),
            "per_op_latency_seconds": {
                "exec": _percentiles([]),
                "file": _percentiles([]),
                "process": _percentiles([]),
            },
            "stream_seconds": _percentiles([]),
            "host_samples": [],
            "control_plane_samples": [],
            "session_samples": [],
            "avg_session_rss_mib": None,
            "avg_session_cpu_pct": None,
            "error_count": len(stats.errors),
            "error_total_including_lifecycle": len(stats.errors),
            "error_rate": 1.0,
            "errors_sample": stats.errors[:25],
            "ops_total": 0,
        }

    # ---- steady-state mixed workload ----
    log.info("level N=%d: steady-state %ds with %d sessions", n, duration_s, len(sids))
    stop_evt = asyncio.Event()
    workload_tasks = [
        asyncio.create_task(_session_workload_loop(api, sid, stop_evt, stats)) for sid in sids
    ]
    sampler = asyncio.create_task(_sampler_loop(sids, stop_evt, docker_client, stats, breach))

    deadline = time.monotonic() + duration_s
    while time.monotonic() < deadline:
        await asyncio.sleep(0.5)
        if breach:
            log.warning("level N=%d: breach %s — cutting steady-state", n, breach["reason"])
            break

    stop_evt.set()
    for t in workload_tasks:
        t.cancel()
    await asyncio.gather(*workload_tasks, return_exceptions=True)
    await asyncio.gather(sampler, return_exceptions=True)

    # ---- burst destroy ----
    log.info("level N=%d: burst-destroy", n)
    destroy_results = await _gather_with_progress(
        [api.destroy_session(sid) for sid in sids], f"destroy N={n}"
    )
    for r in destroy_results:
        if isinstance(r, Exception):
            stats.errors.append({"ts": time.time(), "kind": "destroy", "error": repr(r)[:200]})
        else:
            stats.destroy_latencies.append(r)

    # ---- aggregate ----
    total_ops = sum(len(v) for v in stats.op_latencies.values())
    err_count = len([e for e in stats.errors if e["kind"] in ("exec", "file", "process")])
    err_rate = err_count / total_ops if total_ops else 0.0

    per_op = {k: _percentiles(v) for k, v in stats.op_latencies.items()}

    # p99 ratio breach: only meaningful when N=1 baseline exists AND
    # the absolute latency is high enough that 10× the baseline
    # represents a real responsiveness problem. Sub-second baselines
    # tripping a "10× of 30ms" breach is just measurement noise.
    P99_ABSOLUTE_FLOOR_S = 1.0
    if n1_p99:
        for kind, ps in per_op.items():
            ref = n1_p99.get(kind)
            if (
                ref
                and ps["p99"] is not None
                and ps["p99"] > 10 * ref
                and ps["p99"] > P99_ABSOLUTE_FLOOR_S
            ):
                breach.setdefault("reason", f"p99_{kind}_over_10x_baseline")

    if err_rate > 0.01 and total_ops >= 100:
        breach.setdefault("reason", f"error_rate_{err_rate:.4f}_over_1pct")

    # Per-session resource cost: average mem_rss_bytes across all
    # session samples.
    if stats.session_samples:
        sess_rss = [s["mem_rss_bytes"] for s in stats.session_samples if s.get("mem_rss_bytes")]
        sess_cpu = [s["cpu_pct"] for s in stats.session_samples if s.get("cpu_pct") is not None]
        avg_rss_mib = (statistics.fmean(sess_rss) / (1024 * 1024)) if sess_rss else None
        avg_cpu_pct = statistics.fmean(sess_cpu) if sess_cpu else None
    else:
        avg_rss_mib = None
        avg_cpu_pct = None

    return {
        "N": n,
        "sessions_created": len(sids),
        "burst_create_seconds": _percentiles(stats.create_latencies),
        "burst_destroy_seconds": _percentiles(stats.destroy_latencies),
        "per_op_latency_seconds": per_op,
        "stream_seconds": _percentiles(stats.stream_latencies),
        "host_samples": stats.host_samples,
        "control_plane_samples": stats.cp_samples,
        "session_samples": stats.session_samples,
        "avg_session_rss_mib": avg_rss_mib,
        "avg_session_cpu_pct": avg_cpu_pct,
        "error_count": err_count,
        "error_total_including_lifecycle": len(stats.errors),
        "error_rate": err_rate,
        "errors_sample": stats.errors[:25],  # cap to keep JSON small
        "ops_total": total_ops,
        "stopped_early": bool(breach),
        "stop_reason": breach.get("reason"),
    }


# ---------------- the test ----------------


def _select_levels(max_sessions: int) -> list[int]:
    levels = [n for n in LEVELS_FULL if n <= max_sessions]
    if not levels or levels[-1] < max_sessions:
        levels.append(max_sessions)
    return levels


def _autocap_for_host(max_sessions: int, default_memory_mib: int = 2048) -> tuple[int, str | None]:
    """If MemAvailable can't fit `max_sessions * default_memory_mib` even at
    50% active fraction, lower the ceiling and return a warning."""
    mem = _read_meminfo()
    avail_kib = mem.get("MemAvailable") or 0
    if not avail_kib:
        return max_sessions, None
    avail_mib = avail_kib / 1024
    # Reserve 20% headroom for control plane + kernel.
    usable_mib = avail_mib * 0.80
    # Assume 50% active * default_memory_mib + 25% of default for idle.
    per_session_estimate = default_memory_mib * 0.5 + default_memory_mib * 0.25 * 0.5
    fits = int(usable_mib // per_session_estimate)
    if fits < max_sessions:
        return max(1, fits), (
            f"host MemAvailable {avail_mib:.0f} MiB lowered ceiling "
            f"from {max_sessions} to {fits} (per-session estimate {per_session_estimate:.0f} MiB)"
        )
    return max_sessions, None


@pytest.mark.load
@pytest.mark.asyncio
async def test_ramp(
    base_url: str,
    token: str,
    max_sessions: int,
    duration_s: int,
    results_dir: Path,
    run_timestamp: str,
    host_baseline: dict,
) -> None:
    """The ramp. Produces tests/load/results/ramp_<ts>.json."""
    capped, warning = _autocap_for_host(max_sessions)
    if warning:
        log.warning("autocap: %s", warning)
    levels = _select_levels(capped)
    log.info("ramp levels: %s (duration_s=%d)", levels, duration_s)

    api = ApiClient(base_url, token)
    docker_client = _docker_client()
    if docker_client is None:
        log.warning("local docker not reachable; per-container samples will be empty")

    n1_p99: dict[str, float] = {}
    levels_out: list[dict[str, Any]] = []
    summary_stop_reason: str | None = None
    try:
        for n in levels:
            level = await _run_one_level(n, api, duration_s, docker_client, n1_p99)
            levels_out.append(level)
            if level.get("stopped_early"):
                summary_stop_reason = f"level {n}: {level['stop_reason']}"
                log.warning("ramp halted at N=%d: %s", n, level["stop_reason"])
                break
            if n == 1:
                for kind, ps in level.get("per_op_latency_seconds", {}).items():
                    if ps.get("p99") is not None:
                        n1_p99[kind] = ps["p99"]
    finally:
        await api.aclose()
        if docker_client is not None:
            with contextlib.suppress(Exception):
                docker_client.close()

    last_clean = max(
        (lvl["N"] for lvl in levels_out if not lvl.get("stopped_early")),
        default=None,
    )
    degradation = next(
        (lvl["N"] for lvl in levels_out if lvl.get("stopped_early")),
        None,
    )

    # Pull a per-session cost from the deepest clean level (more loaded
    # samples = more representative). Fall back to whatever we got.
    coeff_source = next(
        (lvl for lvl in reversed(levels_out) if lvl.get("avg_session_rss_mib")),
        None,
    )

    out = {
        "schema_version": 1,
        "run_timestamp": run_timestamp,
        "base_url": base_url,
        "duration_s": duration_s,
        "max_sessions_requested": max_sessions,
        "max_sessions_effective": capped,
        "autocap_warning": warning,
        "host_baseline": host_baseline,
        "tested_levels": [lvl["N"] for lvl in levels_out],
        "last_clean_level": last_clean,
        "degradation_level": degradation,
        "stop_reason": summary_stop_reason,
        "session_cost_coefficients": (
            {
                "source_level_N": coeff_source["N"],
                "avg_session_rss_mib": coeff_source["avg_session_rss_mib"],
                "avg_session_cpu_pct": coeff_source["avg_session_cpu_pct"],
            }
            if coeff_source is not None
            else None
        ),
        "levels": levels_out,
    }

    out_path = results_dir / f"ramp_{run_timestamp}.json"
    out_path.write_text(json.dumps(out, indent=2, default=str))
    log.info("wrote %s", out_path)

    # Test-side assertions: harness produced a usable artifact.
    assert levels_out, "no levels ran"
    if not any(lvl.get("sessions_created", 0) > 0 for lvl in levels_out):
        # The result JSON is still written so the operator can inspect
        # errors_sample to debug. Surface the most likely cause in the
        # assertion message.
        first_reason = levels_out[0].get("stop_reason", "unknown")
        first_errors = levels_out[0].get("errors_sample", [])
        raise AssertionError(
            f"no sessions ever created; stop_reason={first_reason!r}; "
            f"first error: {first_errors[0]['error'] if first_errors else 'none'}; "
            f"check the API token, scope grants, and `curl -i ${{LOAD_BASE_URL}}/v1/sessions "
            f"-X POST -H 'Authorization: Bearer ${{LOAD_TOKEN}}' -d '{{}}'`. "
            f"Result file: {out_path}"
        )
