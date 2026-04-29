# Sandbox Service — Specification

**Status:** Draft v0.1 · **Companion:** [ARCHITECTURE.md](./ARCHITECTURE.md)

## 1. Overview

The Sandbox Service provides isolated Linux execution environments for an
LLM agent application. Each sandbox is a long-lived, per-session container
that the agent uses across multiple turns to run shell commands, edit and
create files, and execute code (Python, Node, shell). Workloads are
untrusted — the service must contain malicious or buggy code without
compromising the host or other sessions.

This document defines **what** the service does. The companion
[ARCHITECTURE.md](./ARCHITECTURE.md) defines **how**.

## 2. Goals and Non-Goals

### 2.1 Goals

- **SPEC-001** Strong isolation suitable for executing untrusted code.
- **SPEC-002** Multi-turn sessions: state (filesystem, env, installed deps)
  persists across exec calls within a session.
- **SPEC-003** State persists across idle-stop / resume cycles inside a
  session's TTL window.
- **SPEC-004** Sub-second exec overhead on a warm session.
- **SPEC-005** Every exec call and file mutation is auditable.
- **SPEC-006** A single-host MVP that can be scaled out without rewriting
  the data model or API.

### 2.2 Non-goals (v1)

- **SPEC-010** GPU workloads.
- **SPEC-011** Multi-host scheduling, HA, or cross-region failover.
- **SPEC-012** Snapshot / fork / restore of session state.
- **SPEC-013** Inbound network reachability into a sandbox.
- **SPEC-014** Persistent storage beyond session destruction.
- **SPEC-015** Browser automation, GUI, or display.

## 3. Threat Model (summary)

The adversary is code running **inside** the sandbox. Adversary goals
include: escape to the host, read or write another session's data,
exfiltrate via uncontrolled network egress, exhaust shared resources,
and persist beyond session destroy. The defender is the control plane
plus the layered isolation stack defined in
[ARCHITECTURE.md §4](./ARCHITECTURE.md#4-isolation-model--defense-in-depth).
A full threat model is deferred to a future `THREAT_MODEL.md`.

## 4. Functional Requirements

- **SPEC-100** A session is created with a `tenant_id` and optional
  `limits` override (within tenant maximums).
- **SPEC-101** A new session starts with a clean `/workspace` (empty
  directory owned by the in-container user).
- **SPEC-102** No filesystem, process, or network state from any prior
  session is visible to a new session.
- **SPEC-103** Within a session, exec calls observe the cumulative effect
  of prior exec calls and file writes.
- **SPEC-104** A session may be idle-stopped (container stopped, volume
  retained) and later resumed, with `/workspace` contents preserved.
- **SPEC-105** A session may be explicitly destroyed; this stops the
  container and deletes the volume. Destruction is irreversible.
- **SPEC-106** Exec calls return stdout, stderr, exit code, and wall-clock
  duration. A streaming variant emits stdout/stderr as they arrive.
- **SPEC-107** File operations support write, read, list, and delete
  within `/workspace` only. Paths outside `/workspace` are rejected.

## 5. API Surface

Transport: HTTPS, JSON request/response, FastAPI implementation. URL
prefix `/v1`. Auth: bearer token in `Authorization` header; the token
identifies a tenant.

| Method | Path                              | Purpose                          |
| ------ | --------------------------------- | -------------------------------- |
| POST   | `/v1/sessions`                    | Create session                   |
| GET    | `/v1/sessions/{id}`               | Get status, limits, usage        |
| POST   | `/v1/sessions/{id}/exec`          | Run command, return on completion|
| POST   | `/v1/sessions/{id}/exec/stream`   | Run command, stream via SSE      |
| POST   | `/v1/sessions/{id}/files`         | Write file (path in body)        |
| GET    | `/v1/sessions/{id}/files/{path}`  | Read file                        |
| GET    | `/v1/sessions/{id}/files?dir=...` | List directory                   |
| DELETE | `/v1/sessions/{id}/files/{path}`  | Delete file                      |
| POST   | `/v1/sessions/{id}/stop`          | Idle-stop (retain volume)        |
| POST   | `/v1/sessions/{id}/resume`        | Restart container, reattach vol  |
| DELETE | `/v1/sessions/{id}`               | Destroy (delete container + vol) |
| GET    | `/healthz`                        | Liveness                         |
| GET    | `/readyz`                         | Readiness (gVisor + docker up)   |

- **SPEC-200** All session paths return `404` if the session does not
  belong to the calling tenant — the response is identical to
  "not found" to avoid existence oracles.
- **SPEC-201** `POST /v1/sessions/{id}/exec` accepts
  `{ "argv": [...], "stdin": "...", "timeout_s": int, "env": {...} }`
  and returns
  `{ "stdout", "stderr", "exit_code", "duration_ms", "truncated" }`.
  `argv` is required and must be a list — the server does no shell
  parsing; the client may pass `["bash", "-c", "..."]` for a shell.
- **SPEC-202** Streaming exec uses Server-Sent Events with `stdout`,
  `stderr`, and a final `result` event.
- **SPEC-203** Exec output is capped at 8 MiB per stream per call;
  `truncated: true` is set if the cap is hit.

## 6. Resource Limits and Defaults

| Resource              | Default per session | Tenant max     |
| --------------------- | ------------------- | -------------- |
| vCPU                  | 2                   | 4              |
| Memory                | 2 GiB               | 8 GiB          |
| `/workspace` size     | 1 GiB               | 10 GiB         |
| PIDs                  | 256                 | 1024           |
| Open files (nofile)   | 1024                | 4096           |
| Exec wall-clock       | 60 s                | 600 s          |
| Idle-stop timer       | 15 min              | n/a            |
| Hard destroy TTL      | 24 h                | n/a            |
| Concurrent sessions   | n/a                 | 50 per tenant  |

- **SPEC-300** Limits are enforced by the kernel/runtime (cgroups, gVisor
  syscall filter, per-session volume quota), not by application code.
- **SPEC-301** Exceeding a limit terminates the offending process, not
  the session, except for memory OOM which may stop the container.

## 7. Security Requirements

- **SPEC-400** Containers MUST run under the gVisor `runsc` runtime.
  Startup fails if `runsc` is not registered with the Docker daemon.
- **SPEC-401** Containers MUST be created with: `--cap-drop=ALL`,
  `--security-opt=no-new-privileges`, default seccomp profile, user
  namespace remapping, non-root UID (`10001`), read-only rootfs, tmpfs
  on `/tmp` (size 256 MiB).
- **SPEC-402** Containers MUST attach to the dedicated `sandbox_egress`
  bridge network only. Inter-container traffic on this bridge is denied
  by host iptables.
- **SPEC-403** All HTTP(S) egress flows through the egress proxy with a
  per-tenant domain allowlist. Direct egress is blocked.
- **SPEC-404** Every successful and failed `exec`, file write, and
  lifecycle transition emits an audit record (tenant, session, actor,
  command/path, exit, duration, timestamp).
- **SPEC-405** Bearer tokens are stored hashed (Argon2id) and rotated
  per-tenant on demand.

## 8. Observability and SLOs

- **SPEC-500** Logs are JSON, one event per line, on stdout.
- **SPEC-501** Per-session resource samples (cpu %, RSS, blkio, net) are
  emitted every 10 s while the container is running.
- **SPEC-502** Service-level objectives (single-host, best-effort):
  - Session create p95 < **3 s** with image pre-pulled.
  - Exec overhead (server-side, excluding command runtime) p95 < **50 ms**.
  - API availability ≥ **99.5 %** measured monthly.
- **SPEC-503** A `/metrics` endpoint exposes Prometheus-format counters
  and histograms for every API endpoint and lifecycle event.

## 9. Errors

| Condition                              | Status | Code                  |
| -------------------------------------- | ------ | --------------------- |
| Session not found / not owned          | 404    | `session_not_found`   |
| Session in wrong state for op          | 409    | `invalid_state`       |
| Limit exceeded (concurrent, size, etc) | 429    | `limit_exceeded`      |
| Exec timeout                           | 408    | `exec_timeout`        |
| Output cap exceeded                    | 200    | (returns `truncated`) |
| Path outside `/workspace`              | 400    | `invalid_path`        |
| Auth missing / invalid                 | 401    | `unauthorized`        |
| Internal (gVisor / docker error)       | 500    | `internal_error`      |

## 10. Versioning

- **SPEC-600** The HTTP API is versioned by URL prefix (`/v1`). Breaking
  changes ship as `/v2` with overlap during deprecation.
- **SPEC-601** The wire format follows semver in the response header
  `X-Sandbox-Api-Version`.

## 11. Open Questions

- Authentication for service-to-service calls beyond bearer tokens
  (mTLS? signed JWTs?). Deferred until a second consumer exists.
- Whether file write should accept tar streams for bulk upload.
  Currently single-file only; revisit if perf measurements demand it.
- Snapshot/restore for session forking. Out of scope for v1; noted for
  roadmap.
