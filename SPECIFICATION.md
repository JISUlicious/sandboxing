# Sandbox Service — Specification

> **Status:** v0.1 (draft) · **Audience:** engineers building or integrating
> with the sandbox service · **Companion doc:** [ARCHITECTURE.md](./ARCHITECTURE.md)

## 1. Overview

The Sandbox Service runs untrusted shell commands and file edits on behalf of
an LLM agent across multi-turn sessions. Each session is a long-lived,
isolated execution environment that preserves filesystem state between
turns and is destroyed when the agent finishes its task. This document
describes **what** the service does. The companion document
[ARCHITECTURE.md](./ARCHITECTURE.md) describes **how**.

## 2. Goals and non-goals

### 2.1 Goals

- **SPEC-001** Strong isolation between the sandbox and the host, and
  between sandboxes belonging to different tenants.
- **SPEC-002** Multi-turn sessions: filesystem and process-spawned state
  persist across `exec` calls within a session.
- **SPEC-003** Idle-stop with state retention: sessions can be paused to
  free memory/CPU and resumed later with `/workspace` intact.
- **SPEC-004** Sub-second exec latency on warm sessions (see SLOs in §8).
- **SPEC-005** Auditable: every command, file write, and lifecycle
  transition is recorded with tenant/session attribution.
- **SPEC-006** A clear, documented path from single-host MVP to multi-host
  scale-out without an API break.

### 2.2 Non-goals (v1)

- **SPEC-010** GPU workloads.
- **SPEC-011** Multi-host scheduling, HA, or cross-region failover.
- **SPEC-012** Snapshot/restore of running process state (only on-disk
  state in `/workspace` is preserved).
- **SPEC-013** Inbound network connections to the sandbox.
- **SPEC-014** Persistent storage beyond a session's lifetime.
- **SPEC-015** Interactive PTY sessions (deferred to v2).

## 3. Threat model (brief)

The primary adversary is **code running inside the sandbox**, assumed to
be hostile. Secondary adversaries are tenants attempting to read or
influence sessions belonging to other tenants. The host operator is
trusted. A full treatment is deferred to a future `THREAT_MODEL.md`.

| Adversary             | Capability                            | Mitigation summary           |
|-----------------------|---------------------------------------|------------------------------|
| Code in sandbox       | Arbitrary syscalls, network, fs in `/workspace` | Layered isolation (§7) |
| Other tenant          | API access with own bearer token      | Authz + session ownership   |
| Host operator         | Full host access                      | Out of scope (trusted)      |

## 4. Functional requirements

- **SPEC-101** A session is created in a clean state: empty `/workspace`,
  no leftover processes, no inherited environment from prior sessions.
- **SPEC-102** Within a session, `exec` calls share filesystem state and
  may observe processes spawned by prior `exec` calls (e.g. background
  servers started with `&`).
- **SPEC-103** Session state in `/workspace` survives an idle-stop /
  resume cycle byte-for-byte.
- **SPEC-104** Destroying a session deletes the container, the
  `/workspace` volume, and all in-memory state. The session id may not be
  reused.
- **SPEC-105** All API operations are scoped to the calling tenant; a
  tenant cannot reference another tenant's session id.

## 5. API surface

### 5.1 Conventions

- Transport: HTTPS, JSON request/response bodies, UTF-8.
- Versioning: URL prefix `/v1`. The API follows semver (§10).
- Auth: `Authorization: Bearer <tenant-token>` on every request except
  `/healthz` and `/readyz`.
- All timestamps are RFC 3339 with timezone.
- All durations are integer milliseconds unless suffixed (`_s`, `_ms`).

### 5.2 Endpoints

| Method | Path                              | Purpose                                  |
|--------|-----------------------------------|------------------------------------------|
| POST   | `/v1/sessions`                    | Create a new session                     |
| GET    | `/v1/sessions/{id}`               | Get session status, last activity, usage |
| POST   | `/v1/sessions/{id}/exec`          | Run a command, return full output        |
| POST   | `/v1/sessions/{id}/exec/stream`   | Run a command, stream output via SSE     |
| POST   | `/v1/sessions/{id}/files`         | Write a file (create or overwrite)       |
| GET    | `/v1/sessions/{id}/files/{path}`  | Read a file                              |
| GET    | `/v1/sessions/{id}/files?dir=...` | List a directory                         |
| POST   | `/v1/sessions/{id}/stop`          | Idle-stop the session                    |
| POST   | `/v1/sessions/{id}/resume`        | Resume a stopped session                 |
| DELETE | `/v1/sessions/{id}`               | Destroy session and volume               |
| GET    | `/healthz`                        | Liveness                                 |
| GET    | `/readyz`                         | Readiness (docker + runsc available)     |

### 5.3 Representative shapes

**Create session** — `POST /v1/sessions`

```json
// request
{ "image": "sandbox:debian-py-node", "limits": { "cpu": 2, "memory_mb": 2048 } }
// response
{ "session_id": "s_01HZ...", "status": "RUNNING", "limits": { ... },
  "created_at": "2026-04-29T10:00:00Z" }
```

**Exec** — `POST /v1/sessions/{id}/exec`

```json
// request
{ "command": ["bash", "-lc", "pytest -q"], "timeout_ms": 60000,
  "cwd": "/workspace", "env": { "FOO": "bar" } }
// response
{ "stdout": "...", "stderr": "...", "exit_code": 0, "duration_ms": 1342,
  "timed_out": false }
```

The streaming variant emits SSE events of types `stdout`, `stderr`,
`exit`, terminating the stream on `exit`.

### 5.4 Authn / authz

- One bearer token per tenant, issued out-of-band.
- Per-tenant rate limits: 10 sessions concurrent, 60 API requests/sec
  (defaults; configurable per tenant).
- A tenant token may only operate on session ids it created.

## 6. Resource limits and defaults

Per session, applied at container create time and enforced by cgroups
(see [ARCHITECTURE.md §4](./ARCHITECTURE.md#4-isolation-model--defense-in-depth)):

| Resource          | Default   | Hard maximum |
|-------------------|-----------|--------------|
| vCPU              | 2         | 4            |
| Memory            | 2 GiB     | 8 GiB        |
| `/workspace` size | 1 GiB     | 16 GiB       |
| PIDs              | 256       | 1024         |
| Open files        | 1024      | 4096         |
| Exec wall-clock   | 60 s      | 600 s        |

Lifecycle defaults:

- **SPEC-201** Idle-stop after **15 minutes** with no API activity.
- **SPEC-202** Hard-destroy after **24 hours** of total session age,
  regardless of activity.
- **SPEC-203** A `stop` keeps the volume; only `DELETE` removes it.

## 7. Security requirements

- **SPEC-301** The service refuses to start if the host does not have the
  `runsc` runtime registered with Docker.
- **SPEC-302** Every sandbox container is created with: `--runtime=runsc`,
  all Linux capabilities dropped, `--security-opt=no-new-privileges`,
  the default seccomp profile, user-namespace remapping, a non-root UID
  (`agent`, 10001), a read-only root filesystem, and a writable tmpfs at
  `/tmp`.
- **SPEC-303** Sandboxes have **no host network access**. Outbound HTTP(S)
  traffic must traverse a service-managed proxy with a per-tenant domain
  allowlist. All other egress is dropped.
- **SPEC-304** Sandboxes cannot reach each other on the network.
- **SPEC-305** Every `exec` call and file write produces an audit record
  containing `tenant_id`, `session_id`, redacted command line, exit code,
  duration, and timestamp.
- **SPEC-306** Bearer tokens are validated on every request; a missing or
  invalid token returns `401`.

## 8. Observability and SLOs

### 8.1 Telemetry

- Structured JSON logs at `INFO` for lifecycle events, `WARN`/`ERROR` for
  failures.
- Per-session resource samples (CPU%, RSS, IO bytes) at 10-second
  intervals while running.
- Audit log (see SPEC-305) emitted as append-only JSONL.

### 8.2 SLOs (single-host, best-effort)

| Metric                          | Target          |
|---------------------------------|-----------------|
| `POST /sessions` p95 (warm img) | < 3 s           |
| `exec` overhead p95             | < 50 ms         |
| API availability                | 99.5 % monthly  |

## 9. Errors and status codes

| Code | Condition                                                |
|------|----------------------------------------------------------|
| 400  | Malformed body, invalid limits, path traversal in files  |
| 401  | Missing or invalid bearer token                          |
| 403  | Token valid but session belongs to another tenant        |
| 404  | Session id unknown or already destroyed                  |
| 409  | Lifecycle conflict (e.g. `exec` on a `STOPPED` session)  |
| 413  | File write exceeds workspace quota                       |
| 422  | Validation error (e.g. `cpu` above hard maximum)         |
| 429  | Rate limit exceeded                                      |
| 500  | Internal error (control plane bug)                       |
| 503  | Docker or runsc unavailable; service draining            |
| 504  | `exec` exceeded `timeout_ms`                             |

Error bodies follow:

```json
{ "error": { "code": "session_not_found", "message": "..." } }
```

## 10. Versioning and compatibility

- The API is mounted at `/v1`. Breaking changes require `/v2`.
- Within `/v1`, additive fields are non-breaking; removed or
  type-changed fields are breaking.
- The audit-log JSON schema is versioned independently
  (`schema_version` field).

## 11. Open questions

- **OQ-1** Should `exec` accept a stdin payload in v1, or defer to v2
  with the streaming endpoint?
- **OQ-2** What is the right granularity for the per-tenant proxy
  allowlist (apex domain vs FQDN)?
- **OQ-3** Should idle-stop be opt-out per session for callers that
  rely on background processes? Default is currently opt-out via a
  flag at create time, but the flag is not yet specified above.

## 12. Cross-references

- For component layout, data flow, and isolation layering, see
  [ARCHITECTURE.md](./ARCHITECTURE.md).
- For the scale-out path, see
  [ARCHITECTURE.md §9](./ARCHITECTURE.md#9-scale-out-path-future-not-implemented).
