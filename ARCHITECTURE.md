# Sandbox Service — Architecture

**Status:** Draft v0.2 · **Companion:** [SPECIFICATION.md](./SPECIFICATION.md)

> **Changelog**
> - v0.2 — Ambiguity audit. Tightened user-ns model, destroy ordering
>   (added `DESTROYING` state), network policy, audit failure-mode,
>   state machine, seccomp/tmpfs flags, dev-mode quota fallback.
> - v0.1 — Initial draft.

This document describes **how** the sandbox service is built and why.
For **what** it does, see [SPECIFICATION.md](./SPECIFICATION.md).

## 1. System Diagram

```
                       +---------------------------+
   Agent client  --->  |  Control Plane (FastAPI)  |
   (HTTPS, bearer)     |  - HTTP API               |
                       |  - Session registry       |
                       |  - Docker driver          |
                       |  - Idle reaper            |
                       |  - Audit emitter          |
                       +-------------+-------------+
                                     |
                          docker.sock (root, local)
                                     |
                          +----------v----------+
                          |  Docker Engine      |
                          |  runtime: runsc     |
                          +----------+----------+
                                     |
       +-----------------+-----------+-----------+-----------------+
       |                 |                       |                 |
  +----v-----+     +-----v----+            +-----v----+      +-----v----+
  | Sandbox  |     | Sandbox  |    ...     | Sandbox  |      | Squid    |
  | session  |     | session  |            | session  |      | egress   |
  | A        |     | B        |            | N        |      | proxy    |
  | /workspc |     | /workspc |            | /workspc |      |          |
  +----+-----+     +----+-----+            +----+-----+      +-----+----+
       |                |                       |                  |
       +----------------+----+------------------+                  |
                             |                                     |
                       sandbox_egress bridge                       |
                       (sandbox->sandbox denied;                   |
                        sandbox->proxy:3128 only)                  |
                             |                                     |
                             +-------------------------------------+
                                                |
                                       allowlisted internet egress

  Host disk: per-session named volumes, audit JSONL, SQLite registry.
```

## 2. Components

### 2.1 Control Plane (FastAPI)

- **ARCH-001** Single Python process, FastAPI + Uvicorn, deployed via
  systemd or as a container itself (with a Docker socket mount).
  Stateless except for SQLite + audit log on disk.

  **Security note:** mounting `/var/run/docker.sock` into a container
  is effectively root on the host. When running the control plane as
  a container, it MUST run on a dedicated, locked-down host (the same
  host it manages) and MUST NOT run alongside untrusted workloads.
  Long term: front the socket with a Docker API proxy (e.g.,
  `tecnativa/docker-socket-proxy`) that exposes only the verbs the
  control plane needs.
- **ARCH-002** Modules:
  - `api/server.py` — HTTP routes, auth, request validation.
  - `api/sessions.py` — lifecycle state machine, registry CRUD, locks.
  - `api/docker_client.py` — the **only** module that talks to Docker;
    all hardening flags live here so policy is enforced in one place.
  - `api/exec.py` — exec dispatch, stream multiplexing, output caps.
  - `api/files.py` — `/workspace` path validation (resolve + jail
    check), upload/download via `docker cp` or a small in-container
    helper.
  - `api/audit.py` — append-only JSONL emitter.
  - `api/reaper.py` — periodic background task for idle-stop / hard
    TTL.

### 2.2 Session Registry (SQLite for MVP)

- **ARCH-010** Source of truth for session state. Schema:

```sql
CREATE TABLE sessions (
  id              TEXT PRIMARY KEY,         -- ulid
  tenant_id       TEXT NOT NULL,
  status          TEXT NOT NULL,
  -- status ∈ {CREATING, RUNNING, IDLE, STOPPED, DESTROYING, DESTROYED}
  container_id    TEXT,
  volume_name     TEXT NOT NULL,
  limits_json     TEXT NOT NULL,
  -- All *_at columns are unix-epoch milliseconds (UTC).
  created_at      INTEGER NOT NULL,
  last_activity_at INTEGER NOT NULL,
  destroyed_at    INTEGER
);
CREATE INDEX idx_sessions_tenant_status ON sessions(tenant_id, status);
CREATE INDEX idx_sessions_activity ON sessions(last_activity_at);
```

- **ARCH-011** Migrate to Postgres when moving to multi-host (see §9).
  Schema is portable.

### 2.3 Docker Driver

- **ARCH-020** Wraps `docker-py`. Every `create_container` call applies
  the full hardening flag-set; no caller bypasses it.
- **ARCH-021** Hardening flags (canonical list — also enforced by
  tests):

```
runtime              = "runsc"
read_only            = True
tmpfs                = {"/tmp": "size=256m,mode=1777,noexec,nosuid,nodev"}
volumes              = {volume_name: {"bind": "/workspace", "mode": "rw"}}
user                 = "10001:10001"
working_dir          = "/workspace"
cap_drop             = ["ALL"]
security_opt         = ["no-new-privileges:true",
                        "seccomp=unconfined"]   # runsc filters syscalls
userns_mode          = ""                       # daemon-level userns-remap=default
pids_limit           = limits.pids
mem_limit            = limits.memory_bytes
nano_cpus            = limits.cpu_nanos         # = limits.vcpu * 1_000_000_000
network              = "sandbox_egress"
environment          = {"HTTPS_PROXY": "http://proxy:3128",
                        "HTTP_PROXY":  "http://proxy:3128",
                        "NO_PROXY":    "",
                        "HOME":        "/workspace",
                        "USER":        "agent"}
ulimits              = [{"name": "nofile", "soft": 1024, "hard": 1024}]
entrypoint           = ["/usr/bin/sleep", "infinity"]
labels               = {"sandbox.session_id": session_id,
                        "sandbox.tenant_id":  tenant_id}
# pid, ipc, uts namespaces use Docker defaults (per-container).
# seccomp=unconfined is intentional: gVisor performs syscall filtering
# in userspace, and stacking runc's default seccomp on top adds an
# irrelevant filter. Do not re-enable without coordinating with the
# isolation table in §4.
# userns_mode is empty so the container inherits the daemon's
# userns-remap=default mapping; container UID 10001 maps to a host
# subuid in the dockremap range. gVisor adds a separate user-ns inside
# its sentry.
```

### 2.4 Sandbox Image

- **ARCH-030** Base: `debian:stable-slim`.
- **ARCH-031** Packages: `python3`, `python3-pip`, `nodejs`, `npm`,
  `git`, `ripgrep`, `build-essential`, `ca-certificates`, `curl`,
  `procps`, `less`. The Dockerfile pins each package via
  `apt-get install <pkg>=<version>` so image rebuilds are reproducible
  unless a version is explicitly bumped.
- **ARCH-032** Non-root user `agent` with UID/GID `10001`. `/workspace`
  is its `$HOME` and working directory.
- **ARCH-033** Image is pre-pulled at host boot via systemd unit so
  first session create hits a warm image and meets the
  [SPEC-502](./SPECIFICATION.md#8-observability-and-slos) latency
  SLO.

### 2.5 Egress Proxy

- **ARCH-040** Squid container on the `sandbox_egress` bridge.
  Sandboxes reach it as `proxy:3128`. Squid config holds the
  per-tenant allowlist.
- **ARCH-041** Host iptables rules:
  - **Sandbox containers**: outbound permitted only to the proxy
    container's IP on TCP/3128; all other outbound (including to
    other sandboxes on the bridge) is dropped. Inbound is dropped
    from any source.
  - **Proxy container**: outbound permitted to allowlisted
    destinations on TCP/80 and TCP/443. Inbound permitted only from
    the sandbox subnet on TCP/3128.
- **ARCH-042** Proxy logs include method, host, and HTTP status. URL
  path and query string are **not** logged in v1 to avoid capturing
  tokens or PII; revisit if forensic need outweighs that risk. Logs
  feed the audit sink.

### 2.6 Volume Store

- **ARCH-050** One Docker named volume per session
  (`sandbox-vol-{session_id}`), mounted at `/workspace`. Volume size
  enforcement:
  - **Production:** XFS project quota on `/var/lib/sandbox-volumes`
    (or ext4 with `prjquota`). The driver assigns a unique project ID
    per volume and applies the configured size limit at the FS layer.
  - **Dev mode** (`SANDBOX_DEV_MODE=1`, see
    [SPEC-302](./SPECIFICATION.md#6-resource-limits-and-defaults)):
    no FS-level quota; the driver tracks volume size with a periodic
    `du` and rejects writes after the limit is reached. Dev mode is
    not safe against malicious workloads — local iteration only.
- **ARCH-051** Volumes survive idle-stop and resume. Destroy is a
  multi-step operation; the registry is the source of truth:
  1. Mark row `DESTROYING` (committed before any docker call).
  2. `docker rm -f` the container.
  3. `docker volume rm` the volume.
  4. Mark row `DESTROYED`, set `destroyed_at`.

  On reconcile (control-plane restart), any row in `DESTROYING` is
  resumed from the failed step idempotently — both `docker rm` and
  `docker volume rm` are safe to retry against a missing target.

### 2.7 Audit Sink

- **ARCH-060** Append-only JSONL at `/var/log/sandbox/audit.log`.
  Rotated daily, retained 30 days. One line per exec call, file
  mutation, and lifecycle transition. Schema fields: `ts`, `tenant`,
  `session`, `kind`, `actor` (token id), `payload`, `result`,
  `duration_ms`. Timestamps are unix-epoch milliseconds (UTC) to match
  ARCH-010.
- **ARCH-061** v1 audit is host-local; shipping to an external SIEM is
  expected but out of scope here.

## 3. Data Flow

### 3.1 Create Session

1. Client `POST /v1/sessions` with bearer token.
2. Server authenticates, checks tenant concurrency limit
   ([SPEC-300](./SPECIFICATION.md#6-resource-limits-and-defaults)).
3. Insert registry row in status `CREATING`.
4. Create the volume, then create the container with hardening flags.
5. Start container; transition to `RUNNING`; emit audit.
6. Return `{session_id, status, limits}`.

### 3.2 Exec

1. Acquire per-session lock (registry row advisory lock).
2. Update `last_activity_at`.
3. If state is `STOPPED` / `IDLE`, transparently `resume` first.
   Resume latency is reported on the `sandbox_resume_seconds`
   histogram and is **not** counted toward the
   [SPEC-502](./SPECIFICATION.md#8-observability-and-slos) exec
   overhead SLO; see
   [SPEC-504](./SPECIFICATION.md#8-observability-and-slos).
4. `docker exec` with the requested argv, env, timeout, working
   directory `/workspace`. Stream or buffer stdout/stderr; enforce
   the 8 MiB per-stream cap
   ([SPEC-203](./SPECIFICATION.md#5-api-surface)).
5. Emit audit (success or failure, including timeouts).
6. Release lock; return result.

### 3.3 Write File

1. Resolve session ownership; if state is `STOPPED` / `IDLE`,
   transparently `resume` first (same contract as
   [§3.2 step 3](#32-exec)). Long-lived agents shouldn't have to track
   lifecycle state to do file I/O.
2. Validate path: must resolve under `/workspace` after symlink
   resolution; reject otherwise with `invalid_path`.
3. Stream content via `docker cp -` or via an in-container `cat >file`
   over `docker exec` (MVP uses the latter; `cp` upgrade tracked).
4. Emit audit (path, size, mode); update `last_activity_at`.

The same step 1 transparent-resume applies to file read / list /
delete.

### 3.4 Stop / Resume

- **Stop**: `docker stop` the container with a 5 s grace; transition
  to `STOPPED`; volume retained.
- **Resume**: `docker start` the existing container; transition to
  `RUNNING`. PIDs from before stop are gone — only filesystem state
  persists. This is a deliberate, documented contract that matches
  [SPEC-002](./SPECIFICATION.md#21-goals).

### 3.5 Destroy

See [ARCH-051](#26-volume-store) for the step-by-step ordering. Each
step emits its own audit record; the row is retained for audit
joinability and purged after 30 days.

## 4. Isolation Model — Defense in Depth

Each layer is independently meaningful; an escape requires breaking
multiple layers.

| # | Layer                       | Mechanism                                       | Stops                                                |
|---|-----------------------------|-------------------------------------------------|------------------------------------------------------|
| 1 | User-space kernel           | gVisor `runsc` (KVM platform when available)    | Direct host kernel exploit via syscalls              |
| 2 | Capability + syscall filter | `cap_drop=ALL`, no-new-privileges, runsc filter | Privilege escalation, suspicious syscalls            |
| 3 | Identity                    | Non-root UID 10001, daemon userns-remap         | UID 0 abuse if a layer breaks                        |
| 4 | Filesystem                  | Read-only rootfs, tmpfs `/tmp`, per-session vol | Persistence, cross-session FS access, host FS writes |
| 5 | Resource                    | cgroup cpu/mem/pids, ulimit nofile, vol quota   | Resource exhaustion, fork bombs, disk fill           |
| 6 | Network                     | Dedicated bridge, no host net, allowlisted prox | Lateral movement, exfiltration, C2                   |
| 7 | Authorization               | Tenant-scoped tokens, ownership checks          | Tenant A reading tenant B's session                  |
| 8 | Audit                       | Append-only JSONL of every exec/file/lifecycle  | Post-hoc detection and forensics                     |

## 5. Lifecycle State Machine

```
                +-----------+
                | CREATING  |
                +-----+-----+
                      | start ok
                      v
                +-----+-----+   stop          +---------+
        +------>| RUNNING   +---------------->| STOPPED |
        |       +-+--+------+                 +----+----+
        | resume   |  | idle (>15 min)             |
        |          |  v                            |
        |        +-+----+      exec               |
        +--------+ IDLE +--------------+          |
                 +--+---+              |          |
                    |                  v          |
                    |             (resume + run)  | resume
                    | reaper                      |
                    v                             v
                +---+------+   destroy   +--------+----------+
                | STOPPED  +------------>|     DESTROYING    |
                +----------+             +---------+---------+
                                                   |
                                                   | rm + volume rm
                                                   v
                                          +--------+----------+
                                          |     DESTROYED     |
                                          +-------------------+
```

Transitions:
- `IDLE → RUNNING` on `exec` (resumes container if needed).
- `IDLE → STOPPED` on the next reaper sweep.
- Any non-terminal state → `DESTROYING` on `DELETE /v1/sessions/{id}`
  or hard-TTL expiry.
- `DESTROYING → DESTROYED` after `docker rm` + `docker volume rm`
  succeed and the registry row is committed.

## 6. Concurrency and Locking

- **ARCH-200** All exec and lifecycle operations on a session take a
  per-session lock. SQLite implementation: `BEGIN IMMEDIATE` plus an
  in-process `asyncio.Lock` keyed by session id.
- **ARCH-201** The registry row is the source of truth; `docker ps` is
  used only for reconciliation, never as primary state.

## 7. Failure Modes and Recovery

| Failure                          | Detection                         | Recovery                                                                                                                                                                                                                                  |
|----------------------------------|-----------------------------------|-------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| Control plane crash              | systemd restart                   | On boot, reconcile registry vs. `docker ps`; orphans → `STOPPED`; rows in `DESTROYING` resume from the failed step.                                                                                                                       |
| Container OOM                    | exec exits 137 / engine event     | Surface as exec failure; volume retained; user can resume.                                                                                                                                                                                |
| gVisor sentry panic              | container dies abnormally         | Audit + alert; mark session `STOPPED`; allow resume.                                                                                                                                                                                      |
| Disk pressure on volume store    | host monitor                      | Reaper escalates: stop oldest IDLE → destroy oldest STOPPED beyond TTL.                                                                                                                                                                   |
| Docker daemon down               | `/readyz` fails                   | Service returns 503; reads still work; no new sessions.                                                                                                                                                                                   |
| Audit log fsync failure          | log writer error                  | **New** lifecycle and exec calls are rejected (fail-closed). **In-flight** exec calls run to completion; their audit records are buffered in memory and flushed on recovery, or written to `audit.fallback.jsonl` after a 5 s buffer timeout. Operators must reconcile the fallback file before clearing the alert. |

## 8. Performance Notes

- `docker exec` overhead is ~30 ms warm. Acceptable for v1.
- gVisor `runsc` adds ~10–30 % overhead on syscall-heavy workloads
  (e.g., compilation). LLM-driven shell + edit traffic is far below
  this ceiling.
- A pre-warm pool of idle containers is **not** in v1; revisit if
  create p95 misses
  [SPEC-502](./SPECIFICATION.md#8-observability-and-slos).
- Image is pre-pulled at host boot. Layer cache survives restarts.
- **Image upgrades** roll via blue/green: pull the new tag, point
  `CreateSession` at it; existing sessions keep their image (and its
  layer cache) until they are destroyed. There is no in-place upgrade
  of a running session.

## 9. Scale-Out Path (Future)

These are explicitly **not** built in v1, but the v1 design avoids
choices that would block them:

1. **Postgres registry.** SQLite schema is already portable; a few
   advisory-lock helpers will need a Postgres equivalent.
2. **Multi-host scheduler with session affinity.** A session is pinned
   to its host because its volume lives there. Add a stateless router
   in front that looks up `session → host` from the registry. Volumes
   stay local; sessions never migrate. **Failure mode:** if a host
   dies, all sessions on it are lost (volumes are local). Mitigation
   requires networked storage (item 5) or session-level
   snapshot/restore (out of scope for v1).
3. **Leader-elected reaper.** Replace the in-process reaper with a
   leader-elected job (e.g., via Postgres advisory locks) so only one
   host reaps a given session.
4. **Stronger isolation upgrade.** Swap `runsc` for
   `firecracker-containerd` per-host without API changes; the docker
   driver is the only module affected.
5. **Networked storage.** Optional, only if session migration becomes
   a requirement. Default plan keeps volumes local.

## 10. Trade-offs Explicitly Accepted

- **gVisor compatibility cost.** Some syscalls, eBPF, certain native
  debuggers, and a few exotic compilers fail or run slowly. Acceptable
  for the workload class declared in
  [SPEC-001/SPEC-002](./SPECIFICATION.md#21-goals).
- **Idle RAM cost.** Long-lived containers hold memory until
  idle-stop. At the
  [SPEC-300](./SPECIFICATION.md#6-resource-limits-and-defaults)
  defaults this is bounded by the per-tenant concurrency cap.
- **Single-host = no HA.** Documented; control-plane crash is a
  systemd-restart event and registry survives on disk.
- **SQLite write contention** at very high session churn. Acceptable
  up to a few hundred sessions per host; beyond that, use Postgres.

## 11. Out of Scope for This Document

Pricing, billing, GPU support, cross-region DR, multi-tenant network
policy beyond per-tenant proxy allowlists, and snapshot/fork
semantics.
