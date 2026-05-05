# `deploy/` — operational artifacts

Scripts, systemd units, and config templates for installing and
running the sandbox service on a Linux host.

**For the install walkthrough, read [`../docs/DEPLOY.md`](../docs/DEPLOY.md)
(Compose) or [`../docs/SETUP.md`](../docs/SETUP.md) (systemd).**
This file is a directory index, not a tutorial.

## TL;DR install

```bash
sudo deploy/setup-host.sh --full --with-xfs-quota
sudo cp deploy/.env.compose.example /etc/sandbox/env
sudoedit /etc/sandbox/env                 # set SANDBOX_API_TOKEN + _PEPPER
sudo chmod 0640 /etc/sandbox/env
sudo docker compose --env-file /etc/sandbox/env up -d
```

The only file you **must** edit is `/etc/sandbox/env`. Everything
else is auto-generated, or has workable defaults.

## What's in here

### Bootstrap

| File | Run as | Purpose |
|---|---|---|
| `setup-host.sh` | `sudo` | Idempotent host bootstrap. `--full` installs Docker + gVisor + daemon.json + iptables + sandbox_egress; `--with-xfs-quota` adds the loopback XFS image; default mode applies the slice-9 security pieces only. `--check` for dry-run. |
| `.env.compose.example` | copy → `/etc/sandbox/env` | Operator-edited config. Two required secrets, lots of optional knobs. |

### Egress + isolation

| File | Purpose |
|---|---|
| `iptables-setup.sh` | Idempotent rules added to the `DOCKER-USER` chain (sandbox→sandbox dropped, sandbox→proxy:3128 only). Reads `SANDBOX_SUBNET` / `PROXY_IP` / `PROXY_PORT` from env. |
| `iptables.env.example` | Optional env-overrides for the systemd path (`/etc/sandbox/iptables.env`). Compose path uses `/etc/sandbox/env` instead. |
| `sandbox-iptables.service` | systemd unit that re-applies the iptables rules at boot. |

### XFS prjquota (SPEC-302)

| File | Run as | Purpose |
|---|---|---|
| `sandbox-quota-helper.sh` | root (via `setup-host.sh` install → `/usr/local/bin/`) | The privileged helper that registers project IDs and applies `bhard` limits. |
| `xfs-quota-setup-compose.sh` | root (inside container) | Compose-path entry — exec's the helper directly (control plane container is host-root). |
| `xfs-quota-teardown-compose.sh` | root (inside container) | Compose-path teardown. |
| `xfs-quota-setup.sh.example` | sandbox user (sudo wrapper) | systemd-path entry — `sudo`s into the helper. Copy from the `.example` and drop the suffix. |
| `xfs-quota-teardown.sh.example` | sandbox user (sudo wrapper) | systemd-path teardown. |

### Control plane lifecycle (systemd path)

| File | Purpose |
|---|---|
| `sandbox-api.service` | Hardened systemd unit (NoNewPrivileges, ProtectSystem=strict, tight CapabilityBoundingSet). Compose path doesn't use this; it's installed by `setup-host.sh` for operators on the systemd path. |
| `upgrade-sandbox-image.sh` | Atomic `SANDBOX_SANDBOX_IMAGE` swap + restart for the systemd path. |
| `sandbox.logrotate` | Daily rotation of `/var/log/sandbox/audit.log` with `chattr +a` preserved across rotations. |

### Backup

| File | Purpose |
|---|---|
| `backup.sh` | Stop API → snapshot registry + audit + loopback image → restart. |
| `backup.env.example` | Optional env-overrides (`/etc/sandbox/backup.env`). |
| `sandbox-backup.service` + `.timer` | systemd timer that fires `backup.sh` daily at 03:17 with jitter. |

### TLS-readiness

| File | Purpose |
|---|---|
| `tls/Caddyfile.example` | Sample reverse-proxy config terminating TLS in front of the loopback-bound API. |
| `tls/nginx.conf.example` | Same, nginx flavour. |

### Observability

| File | Purpose |
|---|---|
| `prometheus/alerts.yml` | Sample Prometheus alert rules (audit-log permissions drift, high error rate, etc.). |

## What's NOT in here (look elsewhere)

- Image build context: `sandbox/Dockerfile`, `proxy/Dockerfile`,
  `Dockerfile.control-plane` (repo root).
- Compose definition: `compose.yml` (repo root).
- API source: `api/`.
- Validation scripts (run from a client machine over SSH):
  `tools/smoke-remote.sh`, `tools/validate-slices.sh`.
- Tenant CLI: `tools/sandbox_tenants.py`.

## Compose path vs systemd path — which artifacts apply?

| Concern | Compose path uses | Systemd path uses |
|---|---|---|
| Bootstrap | `setup-host.sh --full --with-xfs-quota` | `setup-host.sh` (no `--full`; manual prereqs per docs) |
| Env file | `/etc/sandbox/env` (one file, all knobs) | `/etc/sandbox/env` + `/etc/sandbox/iptables.env` + `/etc/sandbox/backup.env` |
| Service runtime | `compose.yml` + 3 published images | `sandbox-api.service` (uvicorn from `/opt/sandbox/.venv`) |
| Quota wrappers | `xfs-quota-{setup,teardown}-compose.sh` (no sudo) | `xfs-quota-{setup,teardown}.sh` (sudo wrappers, copy from `.example`) |
| Image upgrade | `docker compose pull && up -d` | `upgrade-sandbox-image.sh` |
| Backup | `backup.sh` + `sandbox-backup.timer` | Same |
| TLS | `tls/*.example` reverse proxy | Same |
