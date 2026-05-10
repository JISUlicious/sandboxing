# Load harness — concurrency & capacity evaluation

A small ramp test that measures how many concurrent sandbox
sessions a host can carry, plus a calculator that turns those
measurements into a host-sizing recommendation.

The harness is **opt-in** — it's gated behind the `load` pytest
marker and only runs when `--base-url` / `--token` are supplied
(via flag or env). It is not part of CI.

## Where to run it

**Run on the same host as the control plane.** The harness samples
the host's `/proc/*` for CPU / RAM / FDs and the local Docker
daemon for per-container stats — both reflect whatever machine
pytest is on. Driving the test from a laptop against a remote API
will:
- inflate per-op latency with WAN/LAN overhead,
- record the laptop's `host_samples` instead of the deploy box's,
- skip per-container samples (the laptop's docker doesn't see
  those containers), which leaves the calculator without
  per-session coefficients.

So `--base-url` is almost always `http://127.0.0.1:8000` when the
harness is running where it should.

## Quick start

```bash
# 1) On the deploy box. Bind address = 127.0.0.1 by default.
export LOAD_BASE_URL=http://127.0.0.1:8000

# 2) Get a tenant bearer token (see "Issuing a token" below).
export LOAD_TOKEN=...

# 3) Phase 1 — snapshot host + ramp at N=[1,5,10,25,50,100,200], 60s/level.
uv run pytest tests/load/test_ramp.py -m load -v

# 4) Phase 2 — get a sizing recommendation from the result.
uv run python -m tools.sandbox_capacity_calc --concurrent-sessions 50
```

Result files land in `tests/load/results/`:

```
ramp_<run-timestamp>.json           # the ramp; one entry per level
host_baseline_<run-timestamp>.json  # host snapshot taken at run start
```

## Issuing a token

The harness needs a tenant bearer token with the standard scopes
(`session_create`, `session_destroy`, `exec`, `file_*`, `processes`).
You don't need admin scope. Two ways to get one:

**Option A — reuse the bootstrap token (simplest).**

A standard deploy stores `SANDBOX_API_TOKEN` in `/etc/sandbox/env`.
On startup the service bootstraps a `default` tenant from that
token, so it already has all scopes:

```bash
export LOAD_TOKEN=$(sudo grep -E '^SANDBOX_API_TOKEN=' /etc/sandbox/env | cut -d= -f2)
```

If you'd rather not run the harness as the prod tenant — fine.

**Option B — issue a dedicated load-test tenant.**

Use the bootstrap CLI. It talks to the SQLite registry directly, so
run it on the deploy box as the `sandbox` service user (this keeps
DB-file ownership consistent with what the service expects on its
next restart). The exact invocation matches `docs/SETUP.md`:

```bash
# Create a new tenant + first token.
sudo -u sandbox uv --directory /opt/sandbox run \
    python -m tools.sandbox_tenants create loadtest "Load testing"
# Output:
#   tenant 'loadtest' created.
#
#   Bearer token (save this — it won't be shown again):
#       sk-...
#
#   Usage from a client:
#       curl -H 'Authorization: Bearer sk-...' http://127.0.0.1:8000/v1/sessions

export LOAD_TOKEN=sk-...        # paste the printed token
```

The `--directory /opt/sandbox` is the install location used by the
deploy scripts; adjust if your repo lives elsewhere. New tokens
default to all scopes, which is what the harness needs.

**Option C — issue via the admin API (multi-tenant deployments).**

Operators with `SANDBOX_ADMIN_TOKEN` set can use the API:

```bash
ADMIN=$(sudo grep -E '^SANDBOX_ADMIN_TOKEN=' /etc/sandbox/env | cut -d= -f2)
LOAD_TOKEN=$(curl -sS -X POST http://127.0.0.1:8000/v1/tenants/loadtest/tokens \
    -H "Authorization: Bearer $ADMIN" \
    -H 'Content-Type: application/json' \
    -d '{"note": "load harness"}' | python -c 'import json,sys; print(json.load(sys.stdin)["token"])')
export LOAD_TOKEN
```

(Assumes the `loadtest` tenant exists; create it with
`POST /v1/tenants` first if not.)

**Cleaning up afterwards.** If you used Option B or C, revoke the
token after the run so it can't be reused. With the admin API:

```bash
# Optional: delete the tenant entirely (also destroys any sessions
# it left behind).
curl -X DELETE http://127.0.0.1:8000/v1/tenants/loadtest \
    -H "Authorization: Bearer $ADMIN"
```

## CLI flags

Five flags, all with environment-variable equivalents. The CLI flag
takes precedence over the env var.

| Flag              | Env                  | Default                | Purpose                       |
|-------------------|----------------------|------------------------|-------------------------------|
| `--base-url`      | `LOAD_BASE_URL`      | (required)             | Sandbox API base URL          |
| `--token`         | `LOAD_TOKEN`         | (required)             | Bearer token for the API      |
| `--max-sessions`  | `LOAD_MAX_SESSIONS`  | `100`                  | Cap the ramp ceiling          |
| `--duration-s`    | `LOAD_DURATION_S`    | `60`                   | Per-level steady-state seconds|
| `--results-dir`   | `LOAD_RESULTS_DIR`   | `tests/load/results`   | Where JSON lands              |

Example:

```bash
uv run pytest tests/load/test_ramp.py -m load \
  --base-url=http://10.0.0.42:8000 \
  --token=$LOAD_TOKEN \
  --max-sessions=50 \
  --duration-s=30
```

If the harness sees that the host has insufficient `MemAvailable`
to fit `max_sessions × default_memory_mib` at a 50% active fraction,
it lowers the ceiling automatically and records a warning in the
result file (`autocap_warning`). The intent: small hosts still get
useful data at lower N, instead of the harness refusing to start.

## What the ramp test does

For each concurrency level `N` in `[1, 5, 10, 25, 50, 100, 200]`
(capped by `--max-sessions`):

1. **Burst create** — fire `N` parallel `POST /v1/sessions` calls;
   record per-session create latency.
2. **Steady-state** — for `--duration-s` seconds, every session
   runs a mixed workload in parallel:
   - 70% `POST /exec` (varied short commands, ~20–200 ms each),
   - 20% file roundtrip (`POST /files` → `GET /files/<path>` → `DELETE`),
   - 10% process start + status (`POST /processes` then `GET`).

   5% of `exec` calls are routed to `POST /exec/stream` with an
   `Idempotency-Key` header. This is a regression for the v0.2.9 fix:
   under load, streaming responses must stay incremental and must not
   end up in the idempotency cache.
3. **Sample every 5 s** — host CPU / mem / FDs / loadavg, plus the
   control-plane container and a random subset of session containers
   when local docker is reachable.
4. **Burst destroy** — fire `N` parallel `DELETE` calls.
5. **Stop conditions** — the ramp halts before the next level if any
   of these fire during steady-state:
   - `error_rate > 1%` (lifecycle errors not counted),
   - host CPU > 90% sustained for ≥ 30 s,
   - host `MemAvailable < 5%`,
   - any per-op p99 > 10× the N=1 baseline.

Per level the JSON records:

```json
{
  "N": 25,
  "burst_create_seconds": {"p50": ..., "p95": ..., "p99": ..., "count": 25},
  "burst_destroy_seconds": {...},
  "per_op_latency_seconds": {"exec": {...}, "file": {...}, "process": {...}},
  "stream_seconds": {...},
  "host_samples": [...time series...],
  "control_plane_samples": [...],
  "session_samples": [...],
  "avg_session_rss_mib": 92.1,
  "avg_session_cpu_pct": 3.8,
  "error_count": 0,
  "error_rate": 0.0,
  "stopped_early": false
}
```

The aggregated header of the result file picks the deepest clean
level's coefficients into `session_cost_coefficients` — those are
what the calculator consumes.

## What the calculator does

`tools/sandbox_capacity_calc.py` reads the latest ramp result and
applies a simple linear model:

```
ram_gib   = baseline_gib + N * per_session_rss_gib * (1 + safety)
cpu_cores = baseline_cores
            + ceil(N * active_fraction * per_session_cpu_pct / 100)
              * (1 + safety)
disk_gib  = N * 2 + audit_growth_per_day * 30 + 50
fd_limit  = next-power-of-two(1024 + N * 16)
```

`baseline_gib` comes from the highest control-plane RSS observed in
the ramp + 1 GiB headroom. `baseline_cores` is fixed at 2 (control
plane + docker daemon).

Required input is just `--concurrent-sessions`. Override
`--active-fraction` (default 0.5), `--safety-margin` (default 0.25),
and `--audit-growth-mib-per-day` (default 50) when the operator's
workload skews differently. Without a result file the tool errors
out clearly — there is no built-in default measurement, because a
sizing recommendation that wasn't measured on the operator's
hardware is fiction.

When the requested `N` is above the deepest clean level the ramp
recorded, the recommendation is flagged as extrapolated.

## Re-running on different hardware

SSH onto the other host, install the repo, set `LOAD_TOKEN` (issued
on that host's API — see "Issuing a token"), and run:

```bash
LOAD_BASE_URL=http://127.0.0.1:8000 LOAD_TOKEN=... \
  uv run pytest tests/load/test_ramp.py -m load --max-sessions=200
```

A new `ramp_*.json` is written under `tests/load/results/` on that
host. Copy it back to your workstation if you want to feed it to the
calculator from somewhere else (`--results <path>`). Old result
files are kept (no cleanup) so you can compare runs.

## What this harness does *not* do

- **Failure-mode characterisation** — pushing the host to OOM /
  disk-full / daemon-restart is intentionally out of scope. That's
  Phase 3 in the plan; lives separately as
  `tests/load/test_failure_modes.py` if/when written.
- **Multi-host scale-out** — single API instance only.
- **Squid proxy throughput** — this hits the API directly; egress
  latency is captured incidentally inside individual ops.
- **gVisor vs runc benchmarking** — runtime is whatever the API was
  configured to use.

## Notes

- Per-session `docker stats` calls are subsampled (random 16 per
  tick) so the daemon isn't hammered when N is large. The
  per-session coefficients in the calculator are robust to that
  sampling — we average across the run.
- The harness assumes it runs **on the same host as the control
  plane** when local-docker stats are needed. If you run it from a
  laptop against a remote API, host_baseline + host_samples reflect
  the laptop, not the deploy box. Re-run on the deploy box for an
  accurate sizing answer.
- The default `--token` should be a tenant token with all scopes
  (session_create, session_destroy, exec, file_*, processes). Admin
  scope is not needed.
