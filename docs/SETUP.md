# Linux Setup Guide

Two paths to a working sandbox host: **Dev** (functional, relaxed
isolation, ~10 min) and **Production** (SPEC-400 / SPEC-302 / SPEC-401
hardening, ~30 min plus slice-5 work). VM-specific differences are
flagged inline.

 > **If you just want a working production install, use the Compose
> path: [DEPLOY.md](./DEPLOY.md).** It pulls prebuilt images from
> `ghcr.io/JISUlicious/sandbox-*` and brings up the full stack with
> XFS-quota feature parity. This document is the reference for
> *what every step does* (and the path for non-apt distros, custom
> systemd integrations, or anyone who can't grant `userns_mode:
> host` to a container).
>
> Once the service is up, see [`MCP.md`](./MCP.md) for connecting
> Claude Code / Desktop / Cursor to the `/mcp` endpoint.

## Prerequisites

- Linux x86_64. (`runsc` does not ship arm64 binaries; on Apple Silicon,
  use a remote x86_64 host or stay in dev mode.)
- Root or `sudo`.
- Network egress to apt/dnf, GitHub, PyPI.

---

## Dev setup

Goal: real Docker, no gVisor, no iptables, no XFS quotas. Same posture
as `SANDBOX_DEV_MODE=1` on macOS but talking to a real daemon. Good for
running the test suite and exercising the API end-to-end.

### 1 · Install Docker Engine

**Ubuntu / Debian** (use Docker's official repo, *not* the distro
`docker.io` package):

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
# Replace `ubuntu` with `debian` on a Debian box.
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
    -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo $VERSION_CODENAME) stable" \
    | sudo tee /etc/apt/sources.list.d/docker.list
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io
sudo usermod -aG docker $USER     # re-login for the group to apply
```

**Rocky / RHEL / Alma:**

```bash
sudo dnf install -y dnf-plugins-core
sudo dnf config-manager --add-repo \
    https://download.docker.com/linux/centos/docker-ce.repo
sudo dnf install -y docker-ce docker-ce-cli containerd.io
sudo systemctl enable --now docker
sudo usermod -aG docker $USER
```

Verify: `docker run --rm hello-world`.

### 2 · Install uv (Python 3.12 + venv manager)

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
exec $SHELL                       # reload PATH
```

uv installs its own Python; no system Python needed.

### 3 · Clone, build, run

```bash
git clone <your-fork-url> sandboxing
cd sandboxing
uv sync --extra dev
docker build -t sandbox-runtime:latest sandbox/
SANDBOX_DEV_MODE=1 SANDBOX_API_TOKEN=dev-token \
    uv run uvicorn api.server:app --host 127.0.0.1 --port 8000
```

In another shell:

```bash
curl -H 'Authorization: Bearer dev-token' \
     -H 'Content-Type: application/json' -d '{}' \
     http://127.0.0.1:8000/v1/sessions
uv run pytest -q
```

### VM notes (dev)

- Docker works on any VM. KVM matters only for gVisor (production).
- If UFW or firewalld is active, port 8000 stays loopback-bound — no
  inbound rules needed. Open it only if you want remote access.

---

## Production setup

Goal: gVisor required (SPEC-400), userns-remap (SPEC-401), XFS / prjquota
(SPEC-302), control plane as a systemd service. Egress proxy + iptables
land with slice 5; until then keep `SANDBOX_DEV_MODE=1` even on the
production box — the rest of the hardening is independent.

### 1 · Verify KVM (recommended for gVisor performance)

```bash
# Ubuntu/Debian:
sudo apt-get install -y cpu-checker && kvm-ok
# Rocky/RHEL:
ls /dev/kvm    # exists if KVM is available
```

**On a VM:** KVM inside a VM = nested virtualization. Enable on the
hypervisor:

- **VMware:** VM settings → CPU → "Expose hardware-assisted
  virtualization to guest OS".
- **Proxmox:** VM → Hardware → Processor → type=`host` and "Enable
  nested virtualization".
- **KVM/qemu host:** `modprobe kvm-intel nested=1` or
  `kvm-amd nested=1`.
- **Cloud:** only specific instance families (AWS `*.metal`, GCP regions
  with nested-virt enabled, Azure Dv3+).

If KVM is unavailable, gVisor falls back to the **ptrace** platform —
works, ~2–3× slower on syscall-heavy workloads. Same isolation guarantee.

### 2 · Install gVisor (runsc)

**Ubuntu / Debian:**

```bash
sudo curl -fsSL https://gvisor.dev/archive.key | sudo gpg --dearmor \
    -o /usr/share/keyrings/gvisor-archive-keyring.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/gvisor-archive-keyring.gpg] \
https://storage.googleapis.com/gvisor/releases release main" \
    | sudo tee /etc/apt/sources.list.d/gvisor.list
sudo apt-get update
sudo apt-get install -y runsc
```

**Rocky / RHEL:**

```bash
sudo tee /etc/yum.repos.d/gvisor.repo <<'EOF'
[gvisor]
name=gVisor x86_64
baseurl=https://yum.dl.google.com/repo/x86_64/
enabled=1
repo_gpgcheck=1
gpgcheck=1
gpgkey=https://yum.dl.google.com/yum-key.gpg
EOF
sudo dnf install -y runsc
```

### 3 · Register runsc with Docker

Create / edit `/etc/docker/daemon.json`:

```json
{
  "runtimes": {
    "runsc": { "path": "/usr/bin/runsc" },
    "runsc-kvm": {
      "path": "/usr/bin/runsc",
      "runtimeArgs": ["--platform=kvm"]
    }
  },
  "userns-remap": "default"
}
```

```bash
sudo systemctl restart docker
docker info | grep -iE 'runsc|userns'
# Expect: Runtimes: io.containerd.runc.v2 runc runsc runsc-kvm
#         Userns: default
```

If you don't have KVM, drop the `runsc-kvm` entry and tell the control
plane to use `runsc` (already the default in `api/docker_client.py`).

### 4 · Volume directory with quotas

**Recommended: dedicated XFS partition.**

```bash
# Adjust /dev/sdb1 to your actual block device.
sudo mkfs.xfs /dev/sdb1
sudo mkdir -p /var/lib/sandbox-volumes
UUID=$(sudo blkid -s UUID -o value /dev/sdb1)
echo "UUID=$UUID /var/lib/sandbox-volumes xfs prjquota,defaults 0 2" \
    | sudo tee -a /etc/fstab
sudo mount /var/lib/sandbox-volumes
```

**No spare disk — ext4 + prjquota:**

```bash
sudo tune2fs -O quota -Q prjquota /dev/<root-or-data-fs>
# Remount or reboot for prjquota to activate.
```

**No quota at all** (small deployments): keep advisory mode and stay in
`SANDBOX_DEV_MODE=1`. SPEC-302 documents this as not-production.

### 5 · Egress proxy + iptables

The repo ships:

- `proxy/Dockerfile` + `proxy/squid.conf` + `proxy/allowed-domains.txt`
  for the Squid container.
- `deploy/iptables-setup.sh` + `deploy/sandbox-iptables.service` —
  idempotent rules added to the **`DOCKER-USER`** chain so Docker's
  restart doesn't wipe them.

Pre-create the bridge with the pinned subnet, then run the proxy on
the fixed IP:

```bash
docker network create --subnet=172.30.0.0/24 \
    --label sandbox.managed=true sandbox_egress

docker build -t sandbox-proxy:latest /opt/sandbox/proxy/
docker run -d --name proxy --restart=unless-stopped \
    --network sandbox_egress --ip=172.30.0.2 \
    -v /opt/sandbox/proxy/allowed-domains.txt:/etc/squid/allowed-domains.txt:ro \
    sandbox-proxy:latest

sudo /opt/sandbox/deploy/iptables-setup.sh
sudo cp /opt/sandbox/deploy/sandbox-iptables.service \
       /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now sandbox-iptables
```

Edit `proxy/allowed-domains.txt` for your workload. `pip` / `npm` /
`git` defaults are present; **the leading-dot form (`*.example.com`)
matches subdomains only**, the no-dot form covers both the bare
domain and subdomains. Squid 6 refuses to start if you list both for
the same domain.

**Set the proxy URL on the control plane** so sandboxes use the
proxy's IP rather than the hostname (Docker's embedded DNS isn't
always reachable from inside gVisor's netstack):

```ini
# in /etc/sandbox/env
SANDBOX_EGRESS_PROXY_URL=http://172.30.0.2:3128
```

### 6 · Control plane as a systemd service

```bash
sudo useradd -r -s /sbin/nologin sandbox
sudo usermod -aG docker sandbox
sudo mkdir -p /opt/sandbox /var/lib/sandbox /var/log/sandbox /etc/sandbox
# Deploy the repo to /opt/sandbox, then as the sandbox user:
sudo -u sandbox bash -c 'cd /opt/sandbox && uv sync'
sudo chown -R sandbox:sandbox /var/lib/sandbox /var/log/sandbox
```

`/etc/sandbox/env`:

```ini
SANDBOX_API_TOKEN=<32-byte random; openssl rand -hex 32>
SANDBOX_TOKEN_PEPPER=<32-byte random; openssl rand -hex 32>
SANDBOX_BIND_HOST=127.0.0.1
SANDBOX_BIND_PORT=8000
SANDBOX_DB_PATH=/var/lib/sandbox/sandbox.db
SANDBOX_AUDIT_LOG_PATH=/var/log/sandbox/audit.log
SANDBOX_SANDBOX_IMAGE=sandbox-runtime:latest
SANDBOX_EGRESS_PROXY_URL=http://172.30.0.2:3128
SANDBOX_QUOTA_SETUP_CMD=/opt/sandbox/deploy/xfs-quota-setup.sh
SANDBOX_QUOTA_TEARDOWN_CMD=/opt/sandbox/deploy/xfs-quota-teardown.sh
SANDBOX_QUOTA_VOLUME_BASE=/var/lib/sandbox-volumes
# SPEC-401 — host UID that maps to container UID 10001 under
# userns-remap=default. setup-host.sh fills this in automatically;
# compute manually with:
#   awk -F: '$1=="dockremap"{print $2 + 10000}' /etc/subuid
SANDBOX_BIND_VOLUME_UID=110001
# Slice 12 — optional admin token for the tenant-management API.
# Single-tenant deployments don't need this; admin endpoints return
# 503 admin_disabled when unset. Generate with `openssl rand -hex 32`.
# SANDBOX_ADMIN_TOKEN=<32-byte random; openssl rand -hex 32>
# SANDBOX_DEV_MODE intentionally absent — production posture.
```

**Multi-tenant tokens** (slice 7). On first start the service
bootstraps a `default` tenant from `SANDBOX_API_TOKEN` so existing
single-token deployments keep working. To add more tenants:

```bash
sudo -u sandbox uv --directory /opt/sandbox run \
    python -m tools.sandbox_tenants create alice "Alice's team"
# Prints the bearer token — save it; the API never returns it again.

sudo -u sandbox uv --directory /opt/sandbox run \
    python -m tools.sandbox_tenants list
```

Token rotation goes through the API:

```bash
curl -X POST -H "Authorization: Bearer $OLD_TOKEN" \
    http://127.0.0.1:8000/v1/tenants/me/tokens/rotate
# {"token":"<new>","old_token_grace_seconds":300,"tenant_id":"alice"}
```

Both old and new tokens authenticate during the 5-minute grace
window. After that the old token returns 401.

**Tenant management API (slice 12).** Setting `SANDBOX_ADMIN_TOKEN`
unlocks the admin surface under `/v1/tenants/*`: create / list /
update / delete tenants, issue scoped tokens, read per-tenant usage.
Endpoints return `503 admin_disabled` when the env var is unset.
Issuing a scoped token from the admin token:

```bash
ADMIN=$(sudo grep ADMIN_TOKEN /etc/sandbox/env | cut -d= -f2)
curl -sS -X POST -H "Authorization: Bearer $ADMIN" \
    -H 'Content-Type: application/json' \
    -d '{"name":"acme","limits":{"max_concurrency":10}}' \
    http://127.0.0.1:8000/v1/tenants

curl -sS -X POST -H "Authorization: Bearer $ADMIN" \
    -H 'Content-Type: application/json' \
    -d '{"scopes":["exec","file_read"]}' \
    http://127.0.0.1:8000/v1/tenants/acme/tokens
# Returns the new token plaintext + token_id; save the plaintext.
```

Available scopes: `session_create`, `session_destroy`, `exec`,
`file_read`, `file_write`, `file_delete`, `processes` (umbrella),
`tokens_rotate`. Tokens issued without an explicit `scopes` list
get all scopes (back-compat for single-token deployments).

**Background processes (slice 11).** `POST /v1/sessions/{id}/processes`
starts a long-running command that survives across exec calls;
`GET /processes/{pid}/logs` is an SSE tail of merged stdout+stderr.
Useful for `npm run dev`, training jobs, browser drivers — see
SPECIFICATION.md and the OpenAPI schema at `/docs`.

**Idempotency** (slice 11a). Mutating endpoints honor an
`Idempotency-Key: <uuid>` header; replays return the cached
response for 24 h (configurable via `SANDBOX_IDEMPOTENCY_TTL_S`).
Cache scope is per-tenant; the same key under two tenants is two
separate operations.

**About `SANDBOX_TOKEN_PEPPER`:** it's HMAC'd with each bearer token
to produce the hash stored in the `tokens` table — so the database
never contains plaintext. **Don't rotate the pepper:** it would
invalidate every existing token. Generate it once with
`openssl rand -hex 32` and back up `/etc/sandbox/env` (or the entire
secret-store of your choice).

Wire the quota scripts (copy from `.example`, drop the suffix). The
heavy lifting now lives in `deploy/sandbox-quota-helper.sh`, installed
to `/usr/local/bin` with a single, locked-down sudoers entry — see
slice 9 below. The `setup-host.sh` script handles all of that for you.

```bash
sudo cp /opt/sandbox/deploy/xfs-quota-setup.sh.example \
       /opt/sandbox/deploy/xfs-quota-setup.sh
sudo cp /opt/sandbox/deploy/xfs-quota-teardown.sh.example \
       /opt/sandbox/deploy/xfs-quota-teardown.sh
sudo chmod +x /opt/sandbox/deploy/xfs-quota-{setup,teardown}.sh
```

The Docker volume layout differs from the default in production: when
`quota_volume_base` is set, the control plane creates each session's
volume as a bind mount onto `$volume_base/<session_id>`. That's what
makes the XFS project quota actually apply — Docker's default
`/var/lib/docker/volumes` location is usually on a non-prjquota
filesystem. With `SANDBOX_BIND_VOLUME_UID` set (slice 9), the bind dir
is chown'd to the dockremap-mapped UID and chmod 0700; without it, the
control plane falls back to mode 0777 and logs a startup warning.

Apply the hardened systemd unit + sudoers helper + logrotate + audit
log immutability with the slice-9 bootstrap:

```bash
sudo /opt/sandbox/deploy/setup-host.sh
sudo systemctl enable --now sandbox-api
sudo journalctl -u sandbox-api -f
curl -H 'Authorization: Bearer '"$(sudo grep API_TOKEN /etc/sandbox/env | cut -d= -f2)" \
    http://127.0.0.1:8000/healthz
```

`setup-host.sh` is idempotent (re-run safe). It:

1. Computes `SANDBOX_BIND_VOLUME_UID` from `/etc/subuid` (`dockremap`
   start + 10000) and writes it into `/etc/sandbox/env`.
2. Installs `/usr/local/bin/sandbox-quota-helper` and writes a
   single-line sudoers grant to `/etc/sudoers.d/sandbox-quota-helper`.
   Removes the legacy wildcard `sandbox-xfs-quota` entry if found.
3. Installs `/etc/logrotate.d/sandbox` and `chattr +a` the audit log.
4. Enforces `0640 root:sandbox` on `/etc/sandbox/env`.
5. Installs the hardened `sandbox-api.service` (the unit lives in
   `deploy/sandbox-api.service` — runs as root with a tight
   `CapabilityBoundingSet`, `NoNewPrivileges`, `ProtectSystem=strict`,
   etc.).

Use `sudo deploy/setup-host.sh --check` to dry-run without changes.

### 7 · Pre-pull the sandbox image at boot (ARCH-033)

```bash
sudo tee /etc/systemd/system/sandbox-image-warm.service <<'EOF'
[Unit]
Description=Pre-pull sandbox-runtime:latest
After=docker.service
Requires=docker.service

[Service]
Type=oneshot
ExecStart=/usr/bin/docker pull sandbox-runtime:latest
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl enable --now sandbox-image-warm
```

### 8 · Sandbox image upgrades

Build a new sandbox image (different tag), then run the upgrade
script — it pulls the tag, smoke-tests it, atomically swaps
`SANDBOX_SANDBOX_IMAGE` in `/etc/sandbox/env`, and restarts the API.

```bash
docker build -t sandbox-runtime:v2026-05-04 sandbox/
sudo /opt/sandbox/deploy/upgrade-sandbox-image.sh sandbox-runtime:v2026-05-04
```

Existing sessions keep their old image (per ARCH §8 blue/green —
"old sessions keep their image until destroyed"). Only sessions
created *after* the swap use the new image. Rollback prints at the
end of the script: copy the `.bak` env file back and restart.

### VM notes (production)

- **No nested KVM:** drop `runsc-kvm` from `daemon.json`; gVisor uses
  ptrace, ~2–3× slower on syscalls.
- **Cloud VMs (AWS / GCP / Azure):** rules added directly to `INPUT` /
  `OUTPUT` may be overridden by cloud security-group integrations.
  Adding to the **`DOCKER-USER`** chain (where slice 5 will land its
  rules) is generally safe across clouds. Verify with
  `sudo iptables -L DOCKER-USER -nv` after a reboot.
- **Cloud block storage:** attach a separate volume for
  `/var/lib/sandbox-volumes`; mkfs + mount per step 4.

### Hardening checklist

- [ ] `docker info | grep runsc` shows runsc registered.
- [ ] `docker info | grep -i userns` shows userns-remap=default.
- [ ] `/var/lib/sandbox-volumes` is XFS or ext4 + prjquota.
- [ ] `SANDBOX_BIND_VOLUME_UID` is set in `/etc/sandbox/env`
      (= dockremap subuid start + 10000); session bind dirs are mode
      `0700` not `0777`.
- [ ] API bound to `127.0.0.1` (or behind a reverse proxy with TLS).
- [ ] `/metrics` scraping restricted to internal network.
- [ ] `sandbox-runtime:latest` pre-pulled at boot.
- [ ] `lsattr /var/log/sandbox/audit.log` shows the `a` attribute
      (immutable-append).
- [ ] `/etc/logrotate.d/sandbox` is installed; daily rotation keeps
      the `+a` bit.
- [ ] `/etc/sandbox/env` mode `0640`, owned `root:sandbox`.
- [ ] `/etc/sudoers.d/sandbox-quota-helper` is the ONLY sudoers grant
      for the `sandbox` user (no wildcard `sed`/`tee`/`touch`).
- [ ] `systemctl show sandbox-api.service | grep NoNewPrivileges` is
      `yes`; `ProtectSystem=strict`; capability set is just
      `CAP_CHOWN CAP_DAC_OVERRIDE CAP_FOWNER`.

---

## Validation: smoke tests for production posture

Two helper scripts run from a client machine over SSH; both use the
same `.env` (see `.env.example`):

- **`tools/smoke-remote.sh`** — quick smoke test of the slice-1-to-5
  surface (create / exec / files / multi-turn / forbidden-env /
  destroy). Use this after every deploy to confirm nothing's
  obviously broken.
- **`tools/validate-slices.sh`** — full validation of the slice-6/7/8
  surface: resource sampler emits, token rotation grace, multi-tenant
  isolation via the CLI, startup reconciliation (kills uvicorn
  mid-state, drops a container, restarts, asserts the row was
  orphaned to STOPPED), and OpenAPI schema-drift check. Use this
  after upgrading the deployment to a new feature slice.

Both scripts use ephemeral state under `/tmp` on the remote and
never touch `/var/lib/sandbox` or `/etc/sandbox/env`.

The hand-written checks below cover production-only signal that the
automation can't observe (gVisor identifying itself, real iptables
DROP, real XFS quota cap). Run after a fresh production deploy.

```bash
TOKEN=$(sudo grep API_TOKEN /etc/sandbox/env | cut -d= -f2)
SID=$(curl -s -H "Authorization: Bearer $TOKEN" \
        -H 'Content-Type: application/json' \
        -d '{}' http://127.0.0.1:8000/v1/sessions | jq -r .session_id)
CID=$(docker ps -q --filter "label=sandbox.session_id=$SID")

# --- Hardening flags actually applied (ARCH-021) ---
docker inspect "$CID" --format '{{.HostConfig.Runtime}}'        # runsc
docker inspect "$CID" --format '{{.HostConfig.UsernsMode}}'     # empty (daemon default)
docker inspect "$CID" --format '{{.HostConfig.ReadonlyRootfs}}' # true
docker inspect "$CID" --format '{{.HostConfig.CapDrop}}'        # [ALL]

# --- gVisor actually intercepting (not just registered) ---
curl -s -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
    -d '{"argv":["cat","/proc/version"]}' \
    http://127.0.0.1:8000/v1/sessions/$SID/exec | jq -r .stdout
# Expect a line mentioning "gVisor" or "Sentry". Plain "Linux ... x86_64"
# means the daemon used runc — runsc isn't actually being applied.

# --- Non-root + read-only rootfs ---
curl -s -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
    -d '{"argv":["id"]}' http://127.0.0.1:8000/v1/sessions/$SID/exec | jq -r .stdout
# uid=10001(agent) gid=10001(agent)

curl -s -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
    -d '{"argv":["bash","-c","touch /escape 2>&1; echo EXIT:$?"]}' \
    http://127.0.0.1:8000/v1/sessions/$SID/exec | jq -r .stdout
# "Read-only file system" + "EXIT:1"

# --- userns-remap mapping container UID 10001 to a host subuid ---
ps -eo uid,pid,cmd | grep "$CID" | head -3
# Process owner should NOT be uid=10001 on the host. With
# userns-remap=default it'll be in the dockremap subuid range
# (typically starting at 165536 or similar).

# --- /metrics serves Prometheus ---
curl -s http://127.0.0.1:8000/metrics | grep -c '^sandbox_'
# Expect 30+ sandbox_* series.

# --- Egress: blocked domain (SPEC-403) ---
curl -s -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
    -d '{"argv":["curl","-sS","-o","/dev/null","-w","HTTP=%{http_code}\n","-m","10","https://example.com"]}' \
    http://127.0.0.1:8000/v1/sessions/$SID/exec | jq '{stdout, exit_code}'
# Expect: stdout="HTTP=000", exit_code=56 (curl reports CONNECT-tunnel-failed
# when Squid responds 403 to the proxy CONNECT — HTTPS tunnel never forms,
# so %{http_code} stays 000). That's the success signal here.

# --- Egress: allowed domain (SPEC-403) ---
curl -s -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
    -d '{"argv":["curl","-sS","-o","/dev/null","-w","HTTP=%{http_code}\n","-m","10","https://pypi.org"]}' \
    http://127.0.0.1:8000/v1/sessions/$SID/exec | jq '{stdout, exit_code}'
# Expect: stdout="HTTP=200", exit_code=0.

# --- Sandbox-to-sandbox blocked at iptables (SPEC-402) ---
SID2=$(curl -s -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
        -d '{}' http://127.0.0.1:8000/v1/sessions | jq -r .session_id)
SID2_IP=$(docker inspect "sandbox-$SID2" \
    -f '{{ (index .NetworkSettings.Networks "sandbox_egress").IPAddress }}')
curl -s -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
    -d "{\"argv\":[\"timeout\",\"3\",\"bash\",\"-c\",\"echo > /dev/tcp/$SID2_IP/22 2>&1; echo EXIT:\$?\"]}" \
    http://127.0.0.1:8000/v1/sessions/$SID/exec | jq -r .stdout
# Expect: "EXIT:1" (or 124) — connection dropped by DOCKER-USER rule.

# --- Workspace quota cap (SPEC-302) ---
curl -s -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
    -d '{"argv":["bash","-c","dd if=/dev/zero of=/workspace/big bs=1M count=2048 2>&1; echo DD_EXIT:${PIPESTATUS[0]}"]}' \
    http://127.0.0.1:8000/v1/sessions/$SID/exec | jq -r .stdout
# Expect: "dd: error writing '/workspace/big': No space left on device"
# at ~1024 MiB written, with DD_EXIT:1.

# --- Cleanup ---
curl -s -X DELETE -H "Authorization: Bearer $TOKEN" \
    http://127.0.0.1:8000/v1/sessions/$SID
curl -s -X DELETE -H "Authorization: Bearer $TOKEN" \
    http://127.0.0.1:8000/v1/sessions/$SID2
```

If any of those don't match, check the corresponding pitfall below.

---

## State management: backup, restore, teardown

The service has four pieces of on-disk state:

| Path | What it is | Replaceable? |
|---|---|---|
| `/var/lib/sandbox-fs.img` | XFS loopback file (mounted at `/var/lib/sandbox-volumes`) | No — losing it loses every session's `/workspace` |
| `/var/lib/sandbox-volumes/<vol>/_data/` | Per-session workspace contents | Per-session; survives container stop/restart |
| `/var/lib/sandbox/sandbox.db` | SQLite registry — source of truth for session→container/volume mapping | No — losing it orphans containers and volumes |
| `/var/log/sandbox/audit.log*` | Append-only JSONL audit | Yes — historical only; service runs without it |

### Backup

**Automated nightly backup (recommended).** The repo ships a
self-contained backup script + systemd timer:

```bash
# Install the timer + service.
sudo cp /opt/sandbox/deploy/sandbox-backup.{service,timer} /etc/systemd/system/
sudo cp /opt/sandbox/deploy/backup.env.example /etc/sandbox/backup.env  # optional overrides
sudo systemctl daemon-reload
sudo systemctl enable --now sandbox-backup.timer

# Verify the timer is scheduled.
systemctl list-timers sandbox-backup.timer
# Next: Sun 2026-05-04 03:17:00 KST

# Force a one-shot run to populate the first backup.
sudo systemctl start sandbox-backup.service
journalctl -u sandbox-backup.service -n 30 --no-pager
ls -la /var/backups/sandbox/
```

The timer fires daily at 03:17 (with a 5-minute jitter). Each run
stops the API briefly (~5–10 s), takes a consistent snapshot of the
registry, audit log, loopback image, and env file, then restarts the
API. Old backups beyond `BACKUP_KEEP_N` (default 14) are removed.

Override defaults via `/etc/sandbox/backup.env` (sample at
`deploy/backup.env.example`).

**Manual on-demand snapshot.** Same flow without the timer:

```bash
sudo systemctl stop sandbox-api      # ~5 s grace; in-flight calls drain

BACKUP=/path/to/backup-$(date +%Y%m%d-%H%M%S)
sudo mkdir -p "$BACKUP"

# Registry — use sqlite3 .backup for a consistent copy. Plain `cp` of
# a live SQLite file is safe only if the service is stopped.
sudo sqlite3 /var/lib/sandbox/sandbox.db ".backup '$BACKUP/sandbox.db'"

# Audit log (rotation-friendly: copy all rotated files too).
sudo cp -a /var/log/sandbox/audit.log* "$BACKUP/" 2>/dev/null || true

# Volume area — block-level copy of the loopback file is fastest and
# captures every session volume in one shot. Unmount first for a
# clean image.
sudo umount /var/lib/sandbox-volumes
sudo cp --sparse=always /var/lib/sandbox-fs.img "$BACKUP/sandbox-fs.img"
sudo mount /var/lib/sandbox-volumes

sudo systemctl start sandbox-api
```

Per-session backup (e.g., to ship a single agent's workspace
elsewhere) — the volume mount path is exposed by Docker:

```bash
VOL=$(docker volume inspect sandbox-vol-<session-id> -f '{{.Mountpoint}}')
sudo tar -C "$VOL" -czf /path/to/session.tgz .
```

### Restore drill

Walk through this **once** after enabling backups, and re-run quarterly,
to make sure the procedure is correct on your specific host. The
recovery time objective on a tested drill is ~2 minutes for the
service to be back up; cleanup of orphaned rows is automatic via the
slice-6a startup reconciliation.

```bash
# 0. Sanity: pick the backup you'll restore (latest is usually fine).
BACKUP=$(ls -td /var/backups/sandbox/sandbox-* | head -1)
echo "restoring from: $BACKUP"

# 1. Stop the service AND prevent the timer from firing during the drill.
sudo systemctl stop sandbox-api
sudo systemctl stop sandbox-backup.timer

# 2. Wipe production state. (DANGEROUS — only on the box you're drilling.)
sudo umount /var/lib/sandbox-volumes
sudo rm -f /var/lib/sandbox-fs.img
sudo rm -f /var/lib/sandbox/sandbox.db
sudo rm -rf /var/log/sandbox/audit.log*

# 3. Restore from backup.
sudo cp --sparse=always "$BACKUP/sandbox-fs.img" /var/lib/sandbox-fs.img
sudo mount /var/lib/sandbox-volumes
sudo cp "$BACKUP/sandbox.db" /var/lib/sandbox/sandbox.db
sudo cp -a "$BACKUP"/audit.log* /var/log/sandbox/ 2>/dev/null || true
sudo chown -R "$USER":"$USER" /var/lib/sandbox /var/log/sandbox

# 4. Start the service. Lifespan's startup reconciliation will mark
#    every non-terminal row STOPPED (their containers are gone now).
sudo systemctl start sandbox-api
journalctl -u sandbox-api -n 20 --no-pager | grep -i reconcile
# Expect: "reconcile_on_startup: done (finished_destroy=N orphaned=M)"

# 5. Verify the registry returned with prior session metadata.
TOKEN=$(sudo grep API_TOKEN /etc/sandbox/env | cut -d= -f2)
curl -sS http://127.0.0.1:8000/healthz
# Browse a known session id from before the drill:
curl -sS -H "Authorization: Bearer $TOKEN" \
    http://127.0.0.1:8000/v1/sessions/<old-session-id>
# Expect: 200 OK with status="STOPPED" (volume preserved; container gone).

# 6. Re-enable the backup timer.
sudo systemctl start sandbox-backup.timer
```

Sessions that existed before the drill come back as `STOPPED`. The
agent can resume them — slice 6a's reconciliation marked the rows
`STOPPED`, and the existing transparent-resume code path on
`/exec` (or `/files`) will create a fresh container that re-attaches
to the preserved `/workspace` volume.

### Restore

```bash
sudo systemctl stop sandbox-api

# Loopback file: copy back, then mount.
sudo umount /var/lib/sandbox-volumes 2>/dev/null || true
sudo cp --sparse=always "$BACKUP/sandbox-fs.img" /var/lib/sandbox-fs.img
sudo mount /var/lib/sandbox-volumes

# Registry + audit.
sudo cp "$BACKUP/sandbox.db" /var/lib/sandbox/sandbox.db
sudo cp "$BACKUP"/audit.log* /var/log/sandbox/ 2>/dev/null || true
sudo chown -R sandbox:sandbox /var/lib/sandbox /var/log/sandbox

# Containers from the previous lifetime are gone, but the registry
# still references their container_ids. The control plane does NOT
# yet auto-reconcile on startup (ARCH-051's reconcile step is
# deferred). Run the cleanup pass below before serving traffic.
sudo systemctl start sandbox-api
```

**Post-restore cleanup.** Volumes survived; container_ids didn't. Mark
every non-terminal row STOPPED so the next exec / resume call returns a
clean error rather than NotFound from docker-py:

```bash
sudo sqlite3 /var/lib/sandbox/sandbox.db <<'SQL'
UPDATE sessions
   SET status = 'STOPPED'
 WHERE status IN ('CREATING', 'RUNNING', 'IDLE');
SQL
```

For each session you want to keep, the workspace volume is intact at
`/var/lib/sandbox-volumes/sandbox-vol-<session-id>/_data/`. Easiest
recovery flow: create a fresh session and `tar`-restore the workspace
into it, then destroy the orphaned row. Sessions you don't care about
will be hard-destroyed by the reaper at the 24 h TTL.

### Resize the loopback volume area

The loopback file can grow online (XFS shrink is unsupported):

```bash
# Bump the file size (sparse — no actual writes yet).
sudo truncate -s 50G /var/lib/sandbox-fs.img
# OR: sudo fallocate -l 50G /var/lib/sandbox-fs.img to fully reserve.

# Re-read the loop device size.
LOOPDEV=$(losetup -j /var/lib/sandbox-fs.img | cut -d: -f1)
sudo losetup -c "$LOOPDEV"

# Grow the XFS to fill the new device size.
sudo xfs_growfs /var/lib/sandbox-volumes
df -h /var/lib/sandbox-volumes
```

### Manually clean a single session (when reaper is broken)

```bash
SID=01KQP6XG1GFD4GR9PF712X2KEW
docker rm -f "sandbox-$SID" 2>/dev/null || true
docker volume rm "sandbox-vol-$SID" 2>/dev/null || true
sudo sqlite3 /var/lib/sandbox/sandbox.db \
    "UPDATE sessions SET status='DESTROYED', destroyed_at=$(date +%s%3N) WHERE id='$SID';"
```

### Remove the entire installation

In order:

```bash
sudo systemctl disable --now sandbox-api sandbox-image-warm
sudo rm -f /etc/systemd/system/sandbox-api.service \
           /etc/systemd/system/sandbox-image-warm.service
sudo systemctl daemon-reload

# Remove all sandbox containers + volumes.
docker rm -f $(docker ps -aq --filter 'label=sandbox.managed=true' \
                                --filter 'label=sandbox.session_id') 2>/dev/null
docker volume rm $(docker volume ls -q --filter 'label=sandbox.managed=true' \
                                       --filter 'name=sandbox-vol-') 2>/dev/null

# Remove the network and the runtime image.
docker network rm sandbox_egress 2>/dev/null || true
docker image rm sandbox-runtime:latest 2>/dev/null || true

# Tear down the loopback volume area.
sudo umount /var/lib/sandbox-volumes
sudo sed -i '/sandbox-fs.img/d' /etc/fstab
sudo rm -rf /var/lib/sandbox-fs.img /var/lib/sandbox-volumes

# Wipe registry / audit / config / installation root.
sudo rm -rf /var/lib/sandbox /var/log/sandbox /etc/sandbox /opt/sandbox
sudo userdel sandbox 2>/dev/null || true

# Optional: uninstall gVisor + Docker if no other services use them.
sudo apt-get remove -y runsc                  # or: sudo dnf remove -y runsc
# Leave Docker alone unless you're sure nothing else uses it.
```

---

## Common pitfalls

- **`mkfs.xfs: command not found`:** `xfsprogs` isn't installed.
  - Ubuntu/Debian: `sudo apt-get install -y xfsprogs`
  - Rocky/RHEL: `sudo dnf install -y xfsprogs`
- **`docker images` shows nothing after a build that "succeeded":**
  the build failed silently — re-run with `; echo "exit=$?"` appended,
  or check that you're talking to the right daemon
  (`docker context ls`, `docker version`).
- **`/proc/version` doesn't mention gVisor inside a session:** runsc
  is registered but not actually being used. Causes:
  1. `daemon.json` not picked up — `sudo systemctl restart docker`.
  2. The control plane is running with `SANDBOX_DEV_MODE=1`, which
     strips `runtime=runsc` from the hardening flags. Drop it from
     `/etc/sandbox/env` and restart `sandbox-api`.
  3. Manual `docker run` without `--runtime=runsc` won't show gVisor;
     verify by going through the API.
- **`--runtime=runsc` errors "no such file":** runsc not on Docker's
  PATH. Use the absolute path in `daemon.json`.
- **gVisor + KVM fails with "no /dev/kvm":** nested virtualization not
  enabled on the hypervisor (step 1).
- **userns-remap and bind mounts:** host UID-owned bind mounts surface
  as nobody:nogroup inside the container. We don't bind arbitrary host
  paths, but worth knowing if you experiment.
- **`DOCKER-USER` chain doesn't exist** on a fresh install: it appears
  after Docker's first start and after the first `iptables` lookup that
  references it. Restart Docker if `iptables -L DOCKER-USER` errors.
- **Loopback volume "device or resource busy" on umount:** something is
  still using the mount. `sudo lsof +D /var/lib/sandbox-volumes` to
  find it; usually a leftover session container — `docker rm -f` it
  first.
- **`curl ... exit 5 "could not resolve proxy"` from inside a sandbox:**
  Docker's embedded DNS (127.0.0.11) isn't reachable from gVisor's
  netstack on some `runsc` versions. Set
  `SANDBOX_EGRESS_PROXY_URL=http://172.30.0.2:3128` in `/etc/sandbox/env`
  to bypass DNS. Existing sessions keep the old env (it's baked at
  container-create time) — destroy and recreate them.
- **`dd if=/dev/zero of=/workspace/big ...` writes past the workspace
  cap:** The XFS project quota's userspace registration is missing.
  Check `sudo xfs_quota -x -c "report -p" /var/lib/sandbox-volumes` —
  if your session's project ID has `Hard=0`, the script's `limit`
  call silently no-oped. The hardened `xfs-quota-setup.sh` writes to
  `/etc/projects` and runs `project -s` + `limit` in one xfs_quota
  invocation to avoid this; older deployments need the new version.
  Confirm the Docker volume is bind-mounted to your XFS dir:
  `docker volume inspect sandbox-vol-<SID> -f '{{.Options}}'` should
  show `o:bind type:none`.
- **`dd | tail -3; echo EXIT:$?` shows `EXIT:0` even when dd fails:**
  bash reports the exit of `tail`, not `dd`. Use
  `${PIPESTATUS[0]}` instead: `dd … 2>&1; echo DD_EXIT:${PIPESTATUS[0]}`.
- **`iptables -L DOCKER-USER` shows duplicated `sandbox-egress` rules:**
  you're on a pre-fix `iptables-setup.sh` — `git pull`, then re-run.
  The current cleanup loop deletes existing tagged rules before
  appending; old versions had a quoting bug that left stale rules
  in place.
