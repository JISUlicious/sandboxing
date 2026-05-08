# Sandbox Service

Production-shaped sandbox for an LLM agent application. Long-lived,
hardened containers (gVisor + cap-drop + read-only rootfs +
userns-remap), HTTP/JSON control plane with SSE streaming, default-deny
egress through a Squid proxy, XFS project quotas on per-session
workspaces, audit log with fail-closed semantics. See
[SPECIFICATION.md](./SPECIFICATION.md) and
[ARCHITECTURE.md](./ARCHITECTURE.md) for the contract and the design.

- Compose-based production deployment: [docs/DEPLOY.md](./docs/DEPLOY.md).
- Systemd-based production deployment + dev walkthrough: [docs/SETUP.md](./docs/SETUP.md).
- End-to-end functional testing: [docs/TESTING.md](./docs/TESTING.md).
- Driving the service from Claude Code / Desktop / Cursor over MCP:
  [docs/MCP.md](./docs/MCP.md).

## Installation

### Prerequisites

- **uv** — Python ≥3.11 + venv manager. Installs its own Python; no
  system Python needed.
  ```bash
  curl -LsSf https://astral.sh/uv/install.sh | sh
  ```
- **Docker** — Docker Desktop on macOS / Windows, or Docker Engine on
  Linux. The dev path needs only the local daemon; production paths
  need the full host setup below.
- **Production hosts** also need Linux x86_64, gVisor (`runsc`)
  registered as a Docker runtime, daemon `userns-remap=default`, and
  an XFS (or ext4 + `prjquota`) volume directory (SPEC-400 / SPEC-302
  / SPEC-401). On Ubuntu/Debian the Compose path's `setup-host.sh
  --full` automates all of these. On other distros, follow
  [docs/SETUP.md](./docs/SETUP.md) §1–§5 for the manual steps.

### Path A — Local dev (no Linux host required)

For running the test suite or driving the API end-to-end against
Docker Desktop:

```bash
git clone https://github.com/JISUlicious/sandboxing && cd sandboxing
uv sync --extra dev
SANDBOX_DEV_MODE=1 SANDBOX_API_TOKEN=dev-token \
    uv run uvicorn api.server:app --reload
```

`SANDBOX_DEV_MODE=1` (SPEC-302) relaxes the production-only checks
(`runsc` required, XFS quota required) and refuses non-loopback bind.

### Path B — Production via Docker Compose (recommended)

Pulls three published images from `ghcr.io/JISUlicious/sandbox-*`:

```bash
git clone https://github.com/JISUlicious/sandboxing && cd sandboxing

# /etc/sandbox/env MUST exist before setup-host.sh runs — the script
# auto-derives SANDBOX_BIND_VOLUME_UID for THIS host's dockremap range
# and writes it back into the file. With the file missing, that step
# is silently skipped and you fall back to the example's hardcoded value.
sudo install -d -m 0755 /etc/sandbox
sudo cp deploy/.env.compose.example /etc/sandbox/env
sudoedit /etc/sandbox/env                # fill in the two required secrets (next subsection)

sudo deploy/setup-host.sh --full --with-xfs-quota
sudo docker compose --env-file /etc/sandbox/env up -d
```

`setup-host.sh --full` installs Docker, gVisor, daemon.json
(`userns-remap`), iptables, the `sandbox_egress` network, the
`sandbox` system user, and the slice-9 security hardening — all
idempotent, re-run safe. Full walkthrough including upgrades /
backup / trade-offs: [docs/DEPLOY.md](./docs/DEPLOY.md).

### Path C — Production via systemd

For deeper customisation (non-apt distros, strict-no-`SYS_ADMIN`-in-
container posture, custom systemd unit overrides):

```bash
git clone https://github.com/JISUlicious/sandboxing && cd sandboxing
# Follow docs/SETUP.md §1–§5 for distro-specific prereqs, then:
sudo deploy/setup-host.sh                # security hardening only (no --full)
sudo systemctl enable --now sandbox-api
```

[docs/SETUP.md](./docs/SETUP.md) is the reference walkthrough.

### Configuration (environment variables)

The control plane reads its config from environment variables (env
prefix `SANDBOX_`). On the Compose path these live in
`/etc/sandbox/env`; the systemd path uses the same file plus
`/etc/sandbox/{backup,iptables}.env` for tool-specific overrides.

**Required** — service will not start without these:

| Variable | Purpose | Generate |
|---|---|---|
| `SANDBOX_API_TOKEN` | Bearer token for the bootstrap `default` tenant. | `openssl rand -hex 32` |
| `SANDBOX_TOKEN_PEPPER` | HMAC pepper for hashed tenant tokens (SPEC-405). Set **once**; rotating invalidates every token. Back up `/etc/sandbox/env` after first set. | `openssl rand -hex 32` |

**Commonly overridden** — defaults usually work:

| Variable | Default | When to override |
|---|---|---|
| `SANDBOX_VERSION` | `latest` | Pin a release tag for production so an upstream `:latest` republish doesn't move under you. |
| `SANDBOX_BIND_VOLUME_UID` | (auto-filled by `setup-host.sh` from `/etc/subuid`) | Set manually if `setup-host.sh` couldn't detect dockremap; computed as `dockremap` subuid start **+ 10001** (matching the agent's container UID 10001 — NOT 10000). SPEC-401 production hardening pivot. |
| `SANDBOX_VOLUME_BASE` | `/var/lib/sandbox-volumes` | Relocate workspace bind mounts onto a dedicated big disk. Must be picked **before** the first session — see [DEPLOY.md "Customize the workspace volume path"](./docs/DEPLOY.md). |
| `SANDBOX_IMAGE_NAMESPACE` | `ghcr.io/jisulicious` | Forks publishing to their own ghcr.io path. |
| `SANDBOX_TRUST_PROXY_HEADERS` | unset | Enable when behind a TLS-terminating reverse proxy (Caddy / nginx — sample configs in `deploy/tls/`). |

The full annotated list of every knob is in
[`deploy/.env.compose.example`](./deploy/.env.compose.example).

### Building images from source

For forks, air-gapped deploys, or local development of the runtime
images:

```bash
docker build -t sandbox-runtime:latest sandbox/                            # per-session container
docker build -t sandbox-proxy:latest   proxy/                              # Squid egress proxy
docker build -f Dockerfile.control-plane -t sandbox-control-plane:latest . # FastAPI control plane
```

Tag them under your own namespace (or `ghcr.io/jisulicious/...` if
you want to override the upstream `:latest`) before `docker compose
up -d` picks them up.

### Verify

```bash
# On the host (or via SSH tunnel if remote).
TOKEN=$(sudo grep API_TOKEN /etc/sandbox/env | cut -d= -f2)   # or your dev token
curl -sS -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8000/healthz
# {"status":"ok"}
```

## Tests

```bash
uv run pytest
```

90 unit tests mock the Docker client and run without a daemon. For
end-to-end testing against a deployed Linux host, see
[docs/TESTING.md](./docs/TESTING.md) and the helper scripts:

- [`tools/smoke-remote.sh`](./tools/smoke-remote.sh) — quick smoke
  test (lifecycle / exec / files / multi-turn).
- [`tools/validate-slices.sh`](./tools/validate-slices.sh) — full
  validation of slice-6/7/8 features (resource sampler, token
  rotation, multi-tenant isolation, startup reconciliation, schema
  drift).

Two real-Docker integration tests are gated by
`pytest -m integration`; CI runs them on a Linux runner per
`.github/workflows/ci.yml`.

## API surface

- **Sessions** — `POST /v1/sessions`, `GET /v1/sessions/{id}`,
  `POST /v1/sessions/{id}/{stop,resume}`, `DELETE /v1/sessions/{id}`
- **Exec** — `POST /v1/sessions/{id}/exec` (sync) and
  `POST /v1/sessions/{id}/exec/stream` (Server-Sent Events)
- **Files** — `POST /v1/sessions/{id}/files` (write, base64 JSON body),
  `POST /v1/sessions/{id}/files/{path}` (write, raw octet-stream body),
  `GET /v1/sessions/{id}/files/{path}` (read, octet-stream),
  `GET /v1/sessions/{id}/files?dir=...` (list),
  `DELETE /v1/sessions/{id}/files/{path}?recursive=...` (delete)
- **Processes** (background, slice 11) —
  `POST /v1/sessions/{id}/processes` (start),
  `GET /v1/sessions/{id}/processes` (list),
  `GET /v1/sessions/{id}/processes/{pid}` (one),
  `GET /v1/sessions/{id}/processes/{pid}/logs` (SSE tail),
  `DELETE /v1/sessions/{id}/processes/{pid}` (stop+drop). Long-
  running commands survive across exec calls.
- **Tenants** — `POST /v1/tenants/me/tokens/rotate` (self-rotate)
  plus an admin-only management surface under `/v1/tenants/*`
  (CRUD, scoped tokens, usage). Set `SANDBOX_ADMIN_TOKEN` to
  enable. SPEC-405 + slice 12.
- **Operations** — `GET /healthz`, `GET /readyz` (reports
  `{docker, audit}`), `GET /metrics` (Prometheus exposition)
- **MCP** — `POST /mcp` (Streamable HTTP, bearer-auth) exposes
  the same surface as **15 Model Context Protocol tools**
  (lifecycle / exec / files / processes) so Claude Code /
  Desktop / Cursor can drive sandboxes directly. The `exec` tool
  streams stdout/stderr via MCP progress notifications when the
  client supplies a `progressToken`. See [docs/MCP.md](./docs/MCP.md).
- **Idempotency** — every mutating route honors an
  `Idempotency-Key: <uuid>` header for safe retries (slice 11a).

OpenAPI / Swagger UI at `/docs`, ReDoc at `/redoc`, machine-readable
schema at `/openapi.json`.

## Repo layout

```
api/         control-plane source (FastAPI, registry, docker driver,
             exec, files, audit, reaper, metrics)
sandbox/     Dockerfile for sandbox-runtime
proxy/       Dockerfile + squid.conf + allowed-domains.txt for sandbox-proxy
Dockerfile.control-plane, compose.yml, .dockerignore — Compose deployment
deploy/      iptables-setup.sh, systemd units (sandbox-api,
             sandbox-iptables, sandbox-backup, sandbox.logrotate),
             setup-host.sh, sandbox-quota-helper.sh, .env.compose.example,
             xfs-quota-{setup,teardown}{,-compose}.sh{.example,}
tools/       smoke-remote.sh (e2e smoke), validate-slices.sh (slice
             6/7/8 validation), dump_openapi.py (schema artifact),
             sandbox_tenants.py (tenant + token CLI)
docs/        DEPLOY.md (compose), SETUP.md (systemd + dev),
             TESTING.md (e2e walkthrough)
tests/       90 unit tests, all mocked at the DockerClient boundary
```
