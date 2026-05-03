# Sandbox Service

Production-shaped sandbox for an LLM agent application. Long-lived,
hardened containers (gVisor + cap-drop + read-only rootfs +
userns-remap), HTTP/JSON control plane with SSE streaming, default-deny
egress through a Squid proxy, XFS project quotas on per-session
workspaces, audit log with fail-closed semantics. See
[SPECIFICATION.md](./SPECIFICATION.md) and
[ARCHITECTURE.md](./ARCHITECTURE.md) for the contract and the design.

For the install / validation walkthroughs, see
[docs/SETUP.md](./docs/SETUP.md) and
[docs/TESTING.md](./docs/TESTING.md).

## Requirements

- Python ≥ 3.11 (the project targets 3.12; managed by [uv](https://github.com/astral-sh/uv)).
- Docker Desktop (macOS / Windows) or Docker Engine (Linux).
- For **production**: Linux x86_64 with `runsc` (gVisor) registered as
  a Docker runtime, daemon `userns-remap=default`, and an
  XFS-formatted (or ext4 + `prjquota`) volume directory. See SPEC-400
  and SPEC-302.

## Run locally (dev mode)

```bash
uv sync --extra dev
SANDBOX_DEV_MODE=1 SANDBOX_API_TOKEN=dev-token \
    uv run uvicorn api.server:app --reload
```

Then in another shell:

```bash
curl -H 'Authorization: Bearer dev-token' \
     -H 'Content-Type: application/json' \
     -d '{}' \
     http://127.0.0.1:8000/v1/sessions
```

`SANDBOX_DEV_MODE=1` (SPEC-302) relaxes the production-only checks
(runsc required, XFS quota required) so the service can run on a
developer Mac via Docker Desktop. Dev mode refuses non-loopback bind.

## Build the images

```bash
docker build -t sandbox-runtime:latest sandbox/   # the per-session container
docker build -t sandbox-proxy:latest   proxy/     # the Squid egress proxy
```

## Tests

```bash
uv run pytest
```

69 unit tests mock the Docker client and run without a daemon. For
end-to-end testing against a deployed Linux host, see
[docs/TESTING.md](./docs/TESTING.md) and
[`tools/smoke-remote.sh`](./tools/smoke-remote.sh).

## API surface

- **Sessions** — `POST /v1/sessions`, `GET /v1/sessions/{id}`,
  `POST /v1/sessions/{id}/{stop,resume}`, `DELETE /v1/sessions/{id}`
- **Exec** — `POST /v1/sessions/{id}/exec` (sync) and
  `POST /v1/sessions/{id}/exec/stream` (Server-Sent Events)
- **Files** — `POST /v1/sessions/{id}/files` (write, base64 body),
  `GET /v1/sessions/{id}/files/{path}` (read, octet-stream),
  `GET /v1/sessions/{id}/files?dir=...` (list),
  `DELETE /v1/sessions/{id}/files/{path}?recursive=...` (delete)
- **Operations** — `GET /healthz`, `GET /readyz` (reports
  `{docker, audit}`), `GET /metrics` (Prometheus exposition)

OpenAPI / Swagger UI at `/docs`, ReDoc at `/redoc`, machine-readable
schema at `/openapi.json`.

## Repo layout

```
api/         control-plane source (FastAPI, registry, docker driver,
             exec, files, audit, reaper, metrics)
sandbox/     Dockerfile for sandbox-runtime
proxy/       Dockerfile + squid.conf + allowed-domains.txt for sandbox-proxy
deploy/      iptables-setup.sh, systemd units, xfs-quota-{setup,teardown}.sh.example
tools/       smoke-remote.sh (e2e validation against a deployed host)
docs/        SETUP.md (install) and TESTING.md (e2e walkthrough)
tests/       69 unit tests, all mocked at the DockerClient boundary
```
