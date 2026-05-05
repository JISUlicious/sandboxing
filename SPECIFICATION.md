# Sandbox Service — Specification

**Status:** Draft v0.2 · **Companion:** [ARCHITECTURE.md](./ARCHITECTURE.md)

> **Changelog**
> - v0.2 — Ambiguity audit. Clarified state-persistence semantics
>   (filesystem vs. process), output-cap behavior, destroy ordering,
>   tenant-limit handling, exec contract, the user-ns claim, and the
>   Linux/XFS production requirement.
> - v0.1 — Initial draft.

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
- **SPEC-002** Multi-turn sessions: **filesystem state** (files in
  `/workspace`, packages installed under `/workspace`) persists across
  exec calls and across stop/resume. **Process state** (env vars,
  running processes, shell history) does **not** persist between exec
  calls — each `exec` is a fresh process. Clients that need persistent
  env should write it to a file in `/workspace` (e.g., `.envrc`) and
  source it.
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
  `limits` override (within tenant maximums). If any field in `limits`
  exceeds the tenant max, the request is rejected with
  `400 limit_exceeded`.
- **SPEC-101** A new session starts with a clean `/workspace` (empty
  directory owned by the in-container user, mode `0750`).
- **SPEC-102** No filesystem, process, or network state from any prior
  session is visible to a new session.
- **SPEC-103** Within a session, exec calls observe the cumulative
  effect of prior exec calls' filesystem writes; see SPEC-002 for the
  process-state contract.
- **SPEC-104** A session may be idle-stopped (container stopped, volume
  retained) and later resumed, with `/workspace` contents preserved.
- **SPEC-105** A session may be explicitly destroyed; this stops the
  container and deletes the volume. Destruction is irreversible.
- **SPEC-106** Exec calls return stdout, stderr, exit code, and
  wall-clock duration. A streaming variant emits stdout/stderr as
  they arrive.
- **SPEC-107** File operations support write, read, list, and delete
  within `/workspace` only. Paths outside `/workspace` are rejected.
  DELETE on a file removes one file; DELETE on a directory is rejected
  unless `?recursive=true`; DELETE on a missing path returns `404`;
  the `/workspace` directory itself cannot be deleted.
- **SPEC-108** The default working directory for `exec` is `/workspace`.
  The default env is the image's plus `HOME=/workspace`, `USER=agent`,
  and the proxy variables documented in
  [ARCH-021](./ARCHITECTURE.md#23-docker-driver). Clients may set
  additional env via the `env` field, but `HTTP_PROXY`, `HTTPS_PROXY`,
  and `NO_PROXY` MUST NOT be overridden — requests that try are
  rejected with `400 invalid_argument`.

## 5. API Surface

Transport: HTTPS, JSON request/response, FastAPI implementation. URL
prefix `/v1`. Auth: bearer token in `Authorization` header; the token
identifies a tenant.

| Method | Path                                  | Purpose                                  |
| ------ | ------------------------------------- | ---------------------------------------- |
| POST   | `/v1/sessions`                        | Create session                           |
| GET    | `/v1/sessions/{id}`                   | Get status, limits, usage                |
| POST   | `/v1/sessions/{id}/exec`              | Run command, return on completion        |
| POST   | `/v1/sessions/{id}/exec/stream`       | Run command, stream via SSE              |
| POST   | `/v1/sessions/{id}/files`             | Write file (path in body)                |
| GET    | `/v1/sessions/{id}/files/{path}`      | Read file                                |
| GET    | `/v1/sessions/{id}/files?dir=...`     | List directory                           |
| DELETE | `/v1/sessions/{id}/files/{path}`      | Delete file (`?recursive=true` for dirs) |
| POST   | `/v1/sessions/{id}/stop`              | Idle-stop (retain volume)                |
| POST   | `/v1/sessions/{id}/resume`            | Restart container, reattach volume       |
| DELETE | `/v1/sessions/{id}`                   | Destroy (delete container + volume)      |
| POST   | `/v1/tenants/me/tokens/rotate`        | Rotate caller's bearer token             |
| GET    | `/healthz`                            | Liveness                                 |
| GET    | `/readyz`                             | Readiness (gVisor + docker up)           |
| GET    | `/metrics`                            | Prometheus metrics                       |

- **SPEC-200** All session paths return `404 session_not_found` if the
  session does not belong to the calling tenant — the response is
  identical to "never existed" to avoid existence oracles. **Sessions
  in `DESTROYED` state also return 404** for all operations; the audit
  log retains the row for 30 days but it is not exposed via the API.
- **SPEC-201** `POST /v1/sessions/{id}/exec` request body:

  ```json
  { "argv": ["..."], "stdin": "...", "timeout_s": 60, "env": {"K":"V"} }
  ```

  Constraints:
  - `argv` — required, list of strings, length ≥ 1. The first element
    is either an absolute path or a binary name resolvable via the
    container's `PATH`. The server does no shell parsing; clients may
    pass `["bash", "-c", "..."]` for a shell.
  - `stdin` — optional. UTF-8 string ≤ 1 MiB. For binary input, use
    the streaming endpoint with `stdin_b64` (base64-encoded, ≤ 8 MiB
    raw).
  - `timeout_s` — optional, integer. Clamped to the tenant max
    ([§6](#6-resource-limits-and-defaults)). The clamped value is
    returned as `effective_timeout_s` in the response.
  - `env` — optional, object of string → string. Keys `HTTP_PROXY`,
    `HTTPS_PROXY`, and `NO_PROXY` are forbidden and cause
    `400 invalid_argument`.

  Response body:

  ```json
  {
    "stdout": "...", "stderr": "...",
    "exit_code": 0, "duration_ms": 42,
    "effective_timeout_s": 60,
    "truncated": false, "truncated_streams": []
  }
  ```

  `duration_ms` measures the command's wall-clock runtime inside the
  container (process start to exit), excluding server-side queuing
  and resume latency.
- **SPEC-202** Streaming exec uses Server-Sent Events with `stdout`,
  `stderr`, `truncated`, and a final `result` event whose payload
  matches SPEC-201.
- **SPEC-203** Each of `stdout` and `stderr` is capped **independently
  at 8 MiB** per call. When a stream hits its cap, further bytes on
  that stream are discarded and a `truncated` SSE event is emitted (if
  streaming). The process keeps running until natural exit or
  timeout. The terminal response sets `truncated: true` and lists the
  affected streams in `truncated_streams`.

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

- **SPEC-300** Limits are enforced by the kernel/runtime (cgroups,
  gVisor syscall filter, per-session volume quota), not by application
  code.
- **SPEC-301** Memory OOM kills the offending process. If the
  OOM-killed process is PID 1 of the container, the container exits
  and the session transitions to `STOPPED` (volume retained). All
  other limits (PIDs, file size, output cap, exec timeout) terminate
  the offending process only.
- **SPEC-302** Production deployment requires a Linux host with an
  XFS-formatted volume directory (or ext4 with `prjquota`) for
  `/workspace` quota enforcement. macOS, Windows, and filesystems
  without project quotas are **not** supported in production. Local
  development may set `SANDBOX_DEV_MODE=1` to enable an advisory quota
  mode (sizes checked at write time only); dev-mode hosts MUST refuse
  to start when bound to a non-loopback interface.

## 7. Security Requirements

- **SPEC-400** Containers MUST run under the gVisor `runsc` runtime.
  Startup fails if `runsc` is not registered with the Docker daemon.
- **SPEC-401** Containers MUST be created with: `--cap-drop=ALL`,
  `--security-opt=no-new-privileges`, non-root UID `10001`, read-only
  rootfs, and a tmpfs on `/tmp` (size 256 MiB,
  `noexec,nosuid,nodev`). The Docker daemon MUST be configured with
  `userns-remap=default` so container UID 10001 maps to an
  unprivileged subuid on the host (see
  [ARCH-021](./ARCHITECTURE.md#23-docker-driver)). Seccomp filtering
  is performed by `runsc`; the Docker-level seccomp profile is set to
  `unconfined` to avoid layering an irrelevant filter on top. In
  bind-mount volume mode the operator MUST set `bind_volume_uid` to
  the dockremap-mapped UID (= `dockremap` subuid start + 10000), so
  per-session workspace directories are chown'd to that UID with
  mode `0700` rather than the world-writable `0777` fallback.
- **SPEC-402** Containers MUST attach to the dedicated `sandbox_egress`
  bridge network only. Sandbox-to-sandbox traffic on this bridge is
  denied by host iptables; sandbox-to-proxy traffic on TCP/3128 is the
  only permitted flow. All other egress is denied.
- **SPEC-403** All HTTP(S) egress flows through the egress proxy with
  a per-tenant domain allowlist. Direct egress is blocked.
- **SPEC-404** Every successful and failed `exec`, file write, and
  lifecycle transition emits an audit record (tenant, session, actor,
  command/path, exit, duration, timestamp).
- **SPEC-405** Bearer tokens are stored as `HMAC-SHA256(pepper,
  plaintext)` so the database never holds plaintext. (Implementation
  note: HMAC-SHA256 rather than Argon2id because tokens are 32 bytes
  of randomness — a slow KDF buys nothing on that input space, and
  HMAC-SHA256 lets the lookup column be indexed.) Tokens may be
  rotated by `POST /v1/tenants/me/tokens/rotate`: the endpoint
  authenticates with the current token, returns a new plaintext, and
  marks the previous token revoked-at = now + 5 min. Both the old
  and new tokens authenticate during the grace window so callers can
  switch without an outage; after the window the old token returns
  401.

## 8. Observability and SLOs

- **SPEC-500** Logs are JSON, one event per line, on stdout.
- **SPEC-501** Per-session resource samples (cpu %, RSS, blkio, net)
  are emitted every 10 s while the container is running.
- **SPEC-502** Service-level objectives (single-host, best-effort):
  - Session create p95 < **3 s** with image pre-pulled.
  - Exec overhead (server-side, excluding command runtime **and**
    resume-from-stop latency) p95 < **50 ms**.
  - API availability ≥ **99.5 %** measured monthly, excluding
    scheduled maintenance windows announced ≥ 24 h in advance.
- **SPEC-503** A `/metrics` endpoint exposes Prometheus-format
  counters and histograms for every API endpoint and lifecycle event.
- **SPEC-504** Resume-from-stop latency is reported as the
  `sandbox_resume_seconds` histogram. SLO: resume p95 < **2 s**.
  Resume latency is **not** counted toward the exec overhead SLO in
  SPEC-502.

## 9. Errors

| Condition                                  | Status | Code                |
| ------------------------------------------ | ------ | ------------------- |
| Session not found / not owned / destroyed  | 404    | `session_not_found` |
| Session in wrong state for op              | 409    | `invalid_state`     |
| Limit exceeded (concurrent, size, etc.)    | 429    | `limit_exceeded`    |
| Exec timeout                               | 408    | `exec_timeout`      |
| Path outside `/workspace`                  | 400    | `invalid_path`      |
| Forbidden env key, malformed argv, etc.    | 400    | `invalid_argument`  |
| Auth missing / invalid                     | 401    | `unauthorized`      |
| Internal (gVisor / docker error)           | 500    | `internal_error`    |

Output that exceeds the 8 MiB per-stream cap is **not** an error: the
call returns `200` with `truncated: true` and `truncated_streams`
populated (see [SPEC-203](#5-api-surface)).

## 10. Versioning

- **SPEC-600** The HTTP API is versioned by URL prefix (`/v1`).
  Breaking changes ship as `/v2` with a **≥ 90 day** deprecation
  overlap.
- **SPEC-601** The wire format follows semver in the response header
  `X-Sandbox-Api-Version`.

## 11. Open Questions

- Authentication for service-to-service calls beyond bearer tokens
  (mTLS? signed JWTs?). Deferred until a second consumer exists.
- Whether file write should accept tar streams for bulk upload.
  Currently single-file only; revisit if perf measurements demand it.
- Snapshot/restore for session forking. Out of scope for v1; noted
  for roadmap.
