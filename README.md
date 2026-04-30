# Sandbox Service

Production-shaped sandbox for an LLM agent application. See
[SPECIFICATION.md](./SPECIFICATION.md) and
[ARCHITECTURE.md](./ARCHITECTURE.md) for the full design.

This branch implements **Slice 1 — control-plane skeleton + session
lifecycle** (Create / Get / Stop / Resume / Destroy). Exec, file I/O,
egress proxy, audit-log fail-closed, reaper, and `/metrics` are
follow-up slices.

## Requirements

- Python ≥ 3.11 (the project targets 3.12; managed by [uv](https://github.com/astral-sh/uv)).
- Docker Desktop (macOS / Windows) or Docker Engine (Linux).
- For **production**: Linux host with `runsc` (gVisor) registered as a
  Docker runtime and an XFS-formatted volume directory. See SPEC-400
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
developer Mac. The host MUST bind to loopback in dev mode; binding
to a non-loopback interface is refused.

## Build the sandbox image

```bash
docker build -t sandbox-runtime:latest sandbox/
```

## Tests

```bash
uv run pytest
```

The tests mock the Docker client; they pass without a running daemon.
End-to-end tests against a real Docker daemon are deferred to a later
slice.

## What this slice does NOT do yet

- `exec`, `exec/stream`, file I/O endpoints (slice 2).
- Egress proxy + iptables (slice 4).
- Audit log fail-closed semantics (slice 4).
- Reaper / idle-stop (slice 5).
- `/metrics`, token rotation, `userns-remap=default` enforcement
  (slice 5+).
- runsc enforcement on production hosts (validated in CI on Linux).

The hardening flag-set in `api/docker_client.py` is the canonical list
from ARCH-021; in dev mode `runtime=runsc` is downgraded to the
default Docker runtime so the API works on macOS.
