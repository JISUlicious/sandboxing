# Capacity & host sizing

How big a host do you need to run N concurrent sandbox sessions?
This page links the measurement harness to a sizing answer.

> **Status: scaffolding.** The numbers below are placeholders until
> the first ramp result lands. Re-run the harness on your hardware
> (see "Re-running on your hardware") and the calculator will quote
> measurements from your run, not these.

## TL;DR

1. SSH onto the host you want to size. Run the ramp against the
   loopback API:
   ```bash
   export LOAD_BASE_URL=http://127.0.0.1:8000
   # see tests/load/README.md → "Issuing a token" for the token.
   export LOAD_TOKEN=$(sudo grep -E '^SANDBOX_API_TOKEN=' /etc/sandbox/env | cut -d= -f2)
   uv run pytest tests/load/test_ramp.py -m load --max-sessions=100
   ```
2. Ask the calculator for a recommendation:
   ```bash
   uv run python -m tools.sandbox_capacity_calc --concurrent-sessions 50
   ```

The calculator prints CPU / RAM / disk / FD-limit numbers sourced
from your latest result file. The harness must run **on the same
host as the API** so `host_samples` and per-container Docker stats
reflect the box you're sizing — see the harness README for the why.

## What "concurrent" means

A concurrent session is one that is `RUNNING` or `IDLE` at the same
instant. Sessions you've created but `STOPPED` don't draw RAM (the
container is gone) — only the workspace volume's disk usage
persists.

Most operators have a mix of "active" sessions (running an exec
right now) and "idle" sessions (live container, agent running, no
workload). The calculator's `--active-fraction` (default 0.5)
captures that. Active sessions pay both RAM (resident pages) and
CPU (the workload). Idle sessions pay RAM only.

## What the harness measures

For each level `N` in `[1, 5, 10, 25, 50, 100, 200]`:

| Number                 | Why it matters                                |
|------------------------|-----------------------------------------------|
| burst-create p99       | First-byte latency under fan-in (operator UX) |
| per-op p99 (exec/file) | Steady-state responsiveness under load        |
| `avg_session_rss_mib`  | Per-session RAM coefficient for sizing        |
| `avg_session_cpu_pct`  | Per-session CPU coefficient for sizing        |
| degradation level      | Where the host first violated a stop condition|
| stream coverage        | SSE stays incremental under load (v0.2.9)     |

Stop conditions: `error_rate > 1%`, host `cpu > 90%` for 30 s,
host `mem_available < 5%`, or any p99 > 10× the N=1 baseline.

## The sizing model

Linear, intentionally simple:

```
ram_gib   = baseline_gib + N × per_session_rss_gib × (1 + safety)
cpu_cores = baseline_cores
            + ceil(N × active_fraction × per_session_cpu_pct / 100)
              × (1 + safety)
disk_gib  = N × 2 + audit_growth_per_day × 30 + 50
fd_limit  = next-power-of-two(1024 + N × 16)
```

- `baseline_gib` = control-plane peak RSS + 1 GiB headroom.
- `baseline_cores` = 2 (control plane + docker daemon).
- `safety` = 0.25 by default (over-provision 25%).

The model is linear above the deepest measured level. The
calculator flags `N` above the tested ceiling as extrapolated, so
you know to re-run with a higher `--max-sessions` before betting
real money on the number.

## Worked example (placeholder — replace with measured)

> _After your first ramp run, replace the table below with the
> numbers from `tests/load/results/ramp_<latest>.json`._

| Variable                 | Value (placeholder)              |
|--------------------------|----------------------------------|
| Host where ramp ran      | _e.g._ 8 vCPU / 32 GiB / runsc   |
| Last clean level         | _e.g._ 50                         |
| Degradation level        | _e.g._ 100 (host_cpu_over_90_for_30s) |
| `avg_session_rss_mib`    | _e.g._ 95                         |
| `avg_session_cpu_pct`    | _e.g._ 4                          |
| Burst-create p99 @ N=50  | _e.g._ 1.4 s                      |
| Exec p99 @ N=50          | _e.g._ 180 ms                     |

Calculator output for 100 concurrent / 50% active / 25% margin:
```
$ uv run python -m tools.sandbox_capacity_calc --concurrent-sessions 100
Resources needed (with 25% safety margin):
  CPU      : <N>  cores
  RAM      : <N>  GiB
  Disk     : <N>  GiB
  FD limit : <N>
```

## Re-running on your hardware

Each operator's container density depends on:

- **Runtime** — gVisor (`runsc`) vs `runc`. gVisor adds memory cost
  per container.
- **Storage driver** — `overlay2` vs `overlayfs` vs vfs.
- **Filesystem** — XFS+prjquota gives strict per-session disk caps;
  ext4 / network FS is advisory.
- **Workload mix** — long-running heavy execs vs short bursts of
  small commands.

The numbers in this doc are not portable across all of those. The
harness is small (~600 LOC), produces a self-contained JSON, and
re-runs in minutes — re-run on the hardware you'll deploy on, then
update this page from your `ramp_*.json`.

## Updating this doc

1. Run the ramp on the target host:
   ```bash
   uv run pytest tests/load/test_ramp.py -m load --max-sessions=200
   ```
2. Open the latest `tests/load/results/ramp_*.json` and copy:
   - `host_baseline.docker.{ncpu, mem_total, default_runtime}` →
     "Host where ramp ran".
   - `last_clean_level`, `degradation_level`, `stop_reason`.
   - `session_cost_coefficients.{avg_session_rss_mib, avg_session_cpu_pct}`.
   - `levels[k].burst_create_seconds.p99` and
     `levels[k].per_op_latency_seconds.exec.p99` for a representative `k`.
3. Re-run the calculator at `--concurrent-sessions=100` (or
   whatever your target is) and paste the output.

## What this doc deliberately doesn't do

- Prescribe a cloud instance family (AWS / GCP / Azure SKUs rot).
- Cover failure-mode characterisation — separate `tests/load/test_failure_modes.py`
  in Phase 3 of the load harness plan.
- Cover multi-host scale-out — single API instance only.

## Related

- [tests/load/README.md](../tests/load/README.md) — harness CLI, JSON schema, sampling notes.
- [tools/sandbox_capacity_calc.py](../tools/sandbox_capacity_calc.py) — the sizing model.
