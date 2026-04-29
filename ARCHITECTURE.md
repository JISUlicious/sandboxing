# Sandbox Service — Architecture

> **Status:** v0.1 (draft) · **Audience:** engineers building, operating, or
> reviewing the sandbox service · **Companion doc:**
> [SPECIFICATION.md](./SPECIFICATION.md)

This document describes **how** the sandbox service is built. For **what**
it does — API surface, limits, SLOs, non-goals — see
[SPECIFICATION.md](./SPECIFICATION.md).

## 1. System diagram

```
                                                  ┌──────────────────────┐
                                                  │  Audit sink (JSONL)  │
                                                  └──────────▲───────────┘
                                                             │
  ┌────────────┐    HTTPS    ┌──────────────────────────┐    │
  │   Client   │────────────▶│   Control plane (FastAPI)│────┘
  │ (agent app)│◀────────────│  - API · registry · reaper│
  └────────────┘             └────────────┬─────────────┘
                                          │ docker-py
                                          ▼
                            ┌─────────────────────────────┐
                            │  dockerd  (runtime: runsc)  │
                            └────────┬────────────────────┘
                                     │
        ┌────────────────────────────┼────────────────────────────┐
        ▼                            ▼                            ▼
  ┌───────────┐              ┌───────────┐                  ┌───────────┐
  │ Sandbox A │              │ Sandbox B │   ...            │  Squid    │
  │  /work A  │              │  /work B  │                  │  proxy    │
  └─────┬─────┘              └─────┬─────┘                  └─────┬─────┘
        │  HTTP(S)_PROXY           │                              │
        └──────────────────────────┴───────────►  egress allowlist┘
                                                       │
                                                       ▼
                                                   Internet
```

Sandboxes attach to a dedicated `sandbox-net` bridge with no route off-host
except via the Squid proxy. Sandboxes cannot reach each other (intra-bridge
isolation via iptables).

## 2. Components

### 2.1 Control plane (FastAPI)

A single Python process exposing the `/v1` API. Responsibilities:

- HTTP API and request validation (Pydantic models).
- Session registry CRUD.
- Exec dispatcher: serializes per-session exec calls, enforces timeouts,
  collects stdout/stderr.
- Idle reaper: background task that scans the registry every 30 s and
  applies SPEC-201 / SPEC-202 transitions.
- Audit emitter: writes JSONL records to the audit sink.

The control plane is the **only** component that talks to dockerd. Sandboxes
have no docker socket access.

### 2.2 Session registry

- **Storage:** SQLite on local disk for the MVP (single host).
- **Schema:**

  ```sql
  CREATE TABLE sessions (
    id              TEXT PRIMARY KEY,        -- ULID
    tenant_id       TEXT NOT NULL,
    container_id    TEXT,                    -- nullable while CREATING
    volume_name     TEXT NOT NULL,
    status          TEXT NOT NULL,           -- see §5
    limits_json     TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    last_activity_at TEXT NOT NULL,
    destroyed_at    TEXT
  );
  CREATE INDEX sessions_tenant_idx  ON sessions(tenant_id, status);
  CREATE INDEX sessions_activity_idx ON sessions(status, last_activity_at);
  ```

- **Source of truth:** the registry row is authoritative. Docker state is
  reconciled against it on control-plane boot (see §7.1).

- **ARCH-001** The registry is migration-friendly: a future scale-out
  swaps SQLite for Postgres without API changes (see §9).

### 2.3 Docker driver

A thin wrapper around `docker-py` whose sole job is to **enforce the
hardening flag-set** on every container create. This is the single
choke-point for security policy. Pseudocode:

```python
def create_sandbox(volume, limits) -> str:
    return docker.containers.create(
        image=SANDBOX_IMAGE,
        runtime="runsc",
        user="10001:10001",
        read_only=True,
        tmpfs={"/tmp": "rw,nosuid,nodev,size=64m"},
        volumes={volume: {"bind": "/workspace", "mode": "rw"}},
        cap_drop=["ALL"],
        security_opt=["no-new-privileges:true"],
        network=SANDBOX_NET,
        environment={"HTTP_PROXY": PROXY_URL, "HTTPS_PROXY": PROXY_URL},
        mem_limit=f"{limits.memory_mb}m",
        nano_cpus=int(limits.cpu * 1e9),
        pids_limit=limits.pids,
        ulimits=[{"Name": "nofile", "Soft": limits.nofile, "Hard": limits.nofile}],
        command=["sleep", "infinity"],
    ).id
```

Any code path that bypasses this wrapper is a bug.

### 2.4 Sandbox image

- **Base:** `debian:stable-slim`.
- **Tools:** `python3`, `python3-pip`, `nodejs`, `npm`, `git`, `ripgrep`,
  `build-essential`, `curl`, `ca-certificates`.
- **User:** `agent` (UID/GID 10001), home `/home/agent`. Image is built
  to run as this user; the root fs is mounted read-only at runtime.
- **Entrypoint:** `sleep infinity`. Work happens via `docker exec`.
- **Image tag is pinned** to a digest in the control plane config.

### 2.5 Egress proxy

- **Implementation:** Squid in its own container.
- **Network:** sits on `sandbox-net` so sandboxes can reach it; also
  attached to the host's egress bridge.
- **Config:** per-tenant ACL files, generated from the tenant table at
  control-plane boot and on tenant updates.
- **Enforcement:** iptables `OUTPUT` rules on `sandbox-net` drop all
  traffic except `dst=proxy:3128`. Sandboxes cannot bypass the proxy by
  using IP literals because there is no other route off-bridge.

### 2.6 Volume store

- **Type:** named Docker volumes, one per session
  (`sandbox-vol-<session_id>`).
- **Mount:** `/workspace` inside the sandbox.
- **Quota:** enforced by the storage driver (overlay2 + xfs project
  quotas) at the volume level.
- **Lifecycle:** retained across `stop`/`resume`; deleted only on
  `DELETE` (SPEC-203).

### 2.7 Audit sink

- **Format:** append-only JSONL on host disk.
- **Rotation:** daily, by `logrotate`.
- **Schema versioned** with `schema_version` field (per SPEC-305).
- **Future:** ships to a central log store; for v1 the on-disk file is
  sufficient.

## 3. Data flow

### 3.1 Create

1. Client `POST /v1/sessions` with token.
2. Control plane validates limits, allocates a ULID `session_id`, writes
   a `CREATING` row.
3. Docker driver creates a named volume and a container with the
   hardened flag-set (§2.3).
4. Control plane starts the container, updates row to `RUNNING`,
   responds `201 Created`.

### 3.2 Exec

1. Client `POST /v1/sessions/{id}/exec`.
2. Control plane authz check (token → session ownership), then takes the
   per-session lock (§6).
3. `docker exec` runs the command as `agent`, with the requested `cwd`
   and `env`. Output is buffered with hard caps (8 MiB stdout, 8 MiB
   stderr) and the wall-clock timeout from SPEC-201 / request.
4. Exit code, durations, and a redacted command line are appended to
   the audit log.
5. `last_activity_at` is updated; lock released; response returned.

### 3.3 WriteFile / ReadFile / List

- WriteFile is implemented as a `docker exec` of a small helper that
  writes to a path under `/workspace` and validates against path
  traversal (`..`, absolute paths outside `/workspace`, symlink
  escapes).
- Read and List are equivalent helpers; output streamed for large files
  with the same caps as exec output.

### 3.4 Stop / Resume

- **Stop:** `docker stop` with a 5-second SIGTERM grace, then SIGKILL.
  Volume is retained. Status → `STOPPED`.
- **Resume:** `docker start` of the same container id (re-using the
  volume mount). Status → `RUNNING`.

### 3.5 Destroy

- `docker rm -f` the container, `docker volume rm` the workspace.
- Registry row updated with `destroyed_at`; row is **not** deleted
  (kept for audit).

## 4. Isolation model — defense in depth

Each layer assumes adversarial code inside the sandbox. A break in any
single layer should not yield host compromise.

| Layer | Mechanism | Rationale |
|-------|-----------|-----------|
| **L1** | gVisor `runsc` (KVM platform) | User-space kernel intercepts syscalls; the host kernel is not directly exposed. |
| **L2** | All caps dropped, `no-new-privileges`, default seccomp, user-ns remap, non-root UID | Even within the gVisor sentry, the workload has the smallest possible privilege set. |
| **L3** | Read-only rootfs, tmpfs `/tmp`, per-session `/workspace` | Image tampering is impossible; cross-session contamination is impossible. |
| **L4** | cgroups: cpu, memory, pids; ulimits: nofile | A runaway workload cannot starve neighbors or the control plane. |
| **L5** | Dedicated `sandbox-net` bridge, no host net, mandatory egress proxy, intra-bridge drop | Sandboxes cannot scan the host LAN, reach the metadata service, or talk to each other. |
| **L6** | Bearer token + per-session ownership check on every request | A tenant cannot reach another tenant's session even if they guess the id. |
| **L7** | Audit log (post-hoc) | Detection-in-depth: even a successful breach is observable. |

**ARCH-101** A new sandbox feature must justify which existing layer
covers it; if none do, a new layer must be added before the feature
ships.

## 5. Lifecycle and state machine

```
                    ┌────────┐
            create  │CREATING│
   ───────────────▶ └───┬────┘
                        │ container started
                        ▼
                    ┌────────┐  exec / files / resume
                    │RUNNING │ ◀───────────────┐
                    └───┬────┘                 │
              idle 15m  │     stop             │
                        ▼                      │
                    ┌────────┐                 │
                    │STOPPED │ ────────────────┘
                    └───┬────┘
                        │ delete  /  age 24h
                        ▼
                  ┌──────────┐
                  │DESTROYED │   (terminal)
                  └──────────┘
```

`IDLE` is not a separate state in v1; idle sessions sit in `RUNNING`
until the reaper transitions them to `STOPPED` per SPEC-201.

## 6. Concurrency and locking

- One **per-session asyncio lock** in the control plane guards `exec`
  and lifecycle transitions for that session. This makes per-session
  operations strictly serial; the API documents this.
- Cross-session operations (different `session_id`s) run concurrently
  up to the control plane's task budget.
- The registry row's `status` column is the canonical state; lock
  acquisition rechecks status to handle races with the reaper.

## 7. Failure modes and recovery

### 7.1 Control plane crash

- Registry is on disk; on restart, the control plane runs a
  reconciliation pass: list `docker ps -a --filter label=sandbox=true`,
  compare with registry rows, and:
  - Container exists, registry says `RUNNING` → keep.
  - Container missing, registry says `RUNNING` → mark `STOPPED`.
  - Container exists, no registry row → delete container + volume
    (orphan).

### 7.2 Container OOM or crash

- Surface `503` to the in-flight `exec` caller with `error.code =
  "sandbox_unavailable"`. Volume is preserved; status → `STOPPED`. The
  client may `POST /resume` to retry.

### 7.3 Disk pressure

- The reaper escalates from idle-stop (SPEC-201) to destroying the
  oldest `STOPPED` sessions when free disk falls below a configured
  watermark (default 10 %).

### 7.4 gVisor sentry panic

- The sandbox container dies. The audit log records `runtime_panic`.
  An alert is raised. The session transitions to `STOPPED`; the
  workspace is preserved for forensics.

### 7.5 dockerd unavailable

- `/readyz` returns `503`. In-flight requests get `503`. The control
  plane keeps retrying with backoff; no in-memory queue.

## 8. Performance notes

- The sandbox image is pre-pulled at host boot (`systemd` unit) so
  cold-create stays under SPEC-201's 3-second p95.
- `docker exec` overhead measured at ~30 ms on a warm container, well
  inside the 50 ms p95 SLO.
- A pre-warm pool of idle sandboxes is **not** built in v1; the data so
  far suggests it's unnecessary. Revisit if create-p95 regresses.

## 9. Scale-out path (future, not implemented)

The single-host design is intentional for v1. The path to multi-host
preserves the public API:

1. **Registry:** swap SQLite for Postgres. The schema is unchanged.
2. **Scheduler:** introduce a placement service that picks a host for
   new sessions and records `host_id` on the session row.
3. **Sticky routing:** an L7 router in front of N control planes routes
   `{id}` to the host that owns the session.
4. **Reaper:** replace the in-process reaper with a leader-elected
   service (e.g. via Postgres advisory locks or etcd).
5. **Optional runtime upgrade:** swap `runsc` for
   `firecracker-containerd` for hardware-virtualized isolation. The
   docker-driver flag-set changes, but no other component does.

None of these change the `/v1` API surface.

## 10. Trade-offs explicitly accepted

- **gVisor compatibility cost.** Some syscalls, eBPF, and certain
  native debuggers are unsupported under `runsc`. We accept this as the
  price of strong isolation; users who need bare metal must use a
  different service.
- **Idle-but-running RAM cost.** A sandbox sits in `RUNNING` until the
  15-minute idle timer fires (SPEC-201). For the expected workload
  (interactive agent turns) this is the right default.
- **No HA on a single host.** A host failure means all in-flight
  sessions are unavailable until the host returns. The scale-out path
  (§9) addresses this; v1 does not.
- **SQLite registry locking.** SQLite handles the expected v1 load,
  but write contention will be a real ceiling around ~100
  sessions/sec. Documented; the Postgres swap is the answer.

## 11. Out of scope for this document

- Pricing and billing.
- GPU scheduling.
- Cross-region disaster recovery.
- Per-tenant SLA tiers.

## 12. Cross-references

- For the API surface, limits, and SLOs, see
  [SPECIFICATION.md](./SPECIFICATION.md).
- For the security guarantees this architecture enforces, see
  [SPECIFICATION.md §7](./SPECIFICATION.md#7-security-requirements).
