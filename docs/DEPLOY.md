# Deploy via Docker Compose

Recommended for new installations. Three published images, one
script for prereqs, one compose file. Brings up a host with full
feature parity to the systemd path (XFS quota included).

For the systemd path or the dev-mode walkthrough, see
[SETUP.md](./SETUP.md). For end-to-end functional testing once the
service is running, see [TESTING.md](./TESTING.md).

## What you'll get

| Container | Image | Purpose |
|---|---|---|
| `sandbox-control-plane` | `ghcr.io/JISUlicious/sandbox-control-plane:<tag>` | FastAPI service on `127.0.0.1:8000` (host loopback only). |
| `sandbox-proxy`         | `ghcr.io/JISUlicious/sandbox-proxy:<tag>`         | Squid forward proxy at `172.30.0.2:3128` on the `sandbox_egress` bridge. |
| `sandbox-image-warmer`  | `docker:cli`                                      | One-shot pull of `sandbox-runtime:<tag>` so the first session create isn't gated on a multi-MB pull. |

Sandbox runtime containers (one per session) are created by the
control plane on the host's Docker daemon — they live on the same
`sandbox_egress` network as the proxy, not as compose siblings.

## Prerequisites

- Linux x86_64 (gVisor doesn't ship arm64).
- Root or `sudo`.
- Roughly 10 minutes for the prereq install on a fresh box.

## Quick-start (Ubuntu / Debian)

> **Before you start:** the Quick-start pulls three images from
> `ghcr.io/jisulicious/sandbox-*`. On a fresh fork those don't exist
> until you cut a `v*.*.*` tag (see "Cutting your first release"
> below) — `docker compose up -d` will fail with
> `Error Head https://ghcr.io/...: denied`. If you'd rather test
> against locally-built images first, run the three `docker build`
> commands from the Troubleshooting section's "denied" entry before
> the `up -d` step.

```bash
git clone https://github.com/JISUlicious/sandboxing
cd sandboxing

# 1. Install Docker, gVisor, daemon.json (userns-remap), iptables,
#    sandbox_egress network, slice-9 security hardening — all in one.
sudo deploy/setup-host.sh --full --with-xfs-quota

# 2. Drop in /etc/sandbox/env. Set the two secrets.
sudo cp deploy/.env.compose.example /etc/sandbox/env
sudoedit /etc/sandbox/env                    # SANDBOX_API_TOKEN + _PEPPER
sudo chown root:sandbox /etc/sandbox/env
sudo chmod 0640 /etc/sandbox/env

# 3. Add yourself to the `sandbox` group so docker compose can read
#    /etc/sandbox/env without sudo. Re-login (or `newgrp sandbox`)
#    for the group membership to take effect.
sudo usermod -aG sandbox "$USER"
newgrp sandbox    # or log out + back in

# 4. Up. The --env-file gives compose access to the same file for
#    BOTH variable substitution (image tags, bind paths) and the
#    container's runtime env. See "Customize the workspace volume
#    path" below for why this matters.
docker compose --env-file /etc/sandbox/env up -d

# 5. Smoke check.
TOKEN=$(sudo grep API_TOKEN /etc/sandbox/env | cut -d= -f2)
curl -sS -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8000/healthz
# {"status":"ok"}
```

> **Permission denied on `/etc/sandbox/env`?** This means step 3's
> group change hasn't taken effect in the current shell. Either run
> `newgrp sandbox` (or open a fresh shell), or prefix every compose
> command with `sudo`. The file is `0640 root:sandbox` so it's
> readable by group members but not world. See "Troubleshooting"
> below.

`--with-xfs-quota` creates a 50 GiB loopback file at
`/var/lib/sandbox-fs.img` and mounts it on `/var/lib/sandbox-volumes`
with `prjquota`. Override with `XFS_SIZE_GB=200 sudo deploy/setup-host.sh --full --with-xfs-quota`.
Skip the flag if you've already mounted a real XFS partition there.

## Other distros

`setup-host.sh --full` is apt-only. On Rocky/RHEL/Alma:

1. Follow [SETUP.md §1–§5](./SETUP.md) manually for Docker, gVisor,
   `daemon.json`, iptables, and the optional XFS volume area.
2. Run `sudo deploy/setup-host.sh` (no `--full`) to apply the
   slice-9 security pieces.
3. Continue from step 2 of the quick-start above.

## Trade-offs vs the systemd path

Compose has full feature parity for v1: XFS project quotas, audit
log on a host path with `chattr +a`, sandbox-to-sandbox iptables
isolation, userns-remap on the per-session containers. The
control-plane container itself runs `userns_mode: host` (= no remap)
plus `cap_drop: ALL` + `CAP_CHOWN, CAP_DAC_OVERRIDE, CAP_FOWNER,
CAP_SYS_ADMIN`. Why:

| Concern | Compose path | systemd path |
|---|---|---|
| Control plane process | container, `userns_mode: host`, host root inside | host process, `User=root` |
| Capability set | `CHOWN DAC_OVERRIDE FOWNER SYS_ADMIN` (compose) | `CHOWN DAC_OVERRIDE FOWNER` (systemd unit, slice 9-4) |
| Sandbox containers | Daemon-default `userns-remap=default` (UID 10001 → 110001) | Same |
| Audit log immutability | Same `chattr +a` on host path | Same |
| XFS quota | In-container `xfs_quota` (CAP_SYS_ADMIN against init userns) | Out-of-process `sandbox-quota-helper` via sudoers |

The compose path needs CAP_SYS_ADMIN because XFS `quotactl()` checks
the cap against the **initial** user namespace; a userns-remapped
container would be denied even with `cap_add: SYS_ADMIN`. The
systemd path side-steps this by shelling out to a root-owned helper
via sudo.

The marginal trust cost of `userns_mode: host` is small because the
control plane already mounts `/var/run/docker.sock` — that's
effectively root on host already.

If you can't accept SYS_ADMIN inside any container at all, switch to
the systemd path.

## Workspace storage — picking a backend

Per-session workspaces (the `/workspace` mount inside each sandbox)
live under `SANDBOX_VOLUME_BASE` on the host. The default
(`/var/lib/sandbox-volumes` backed by a 50 GiB loopback XFS image
on the system disk) is a sensible local-only setup — but if you're
on a VM, using attached cloud block storage, or want sessions to
share a network volume, here's how to pick.

### Decision matrix

| Backend | Quota enforced? | Survives host reboot? | Shareable across hosts? | Throughput | Setup |
|---|---|---|---|---|---|
| **A. Local XFS loopback** (default) | ✅ XFS prjquota | ✅ | ❌ | Fast (local FS) | `setup-host.sh --full --with-xfs-quota` |
| **B. Local XFS partition** | ✅ XFS prjquota | ✅ | ❌ | Fastest | Mount your XFS partition with `prjquota`, point `SANDBOX_VOLUME_BASE` at it |
| **C. Cloud block volume (EBS / PD / Azure Disk)** | ✅ if formatted XFS+prjquota | ✅ | ❌ (single-attach) | Fast (~local) | Same as B once attached + mounted |
| **D. Local NVMe / instance store** (cloud ephemeral) | ✅ if XFS+prjquota | ❌ (gone after stop/restart) | ❌ | Fastest | Same as B; accept ephemerality, document recovery |
| **E. Network mount (NFS / SMB / EFS / FSx)** | ⚠️ Advisory only | ✅ | ✅ | Slower (network RTT × small-file ops) | See "Network-mounted storage" below |

**Most operators want B or C.** They get kernel-enforced quota,
fast local IO, and the standard backup story (block-level
snapshots of the underlying disk). The default A is fine for dev
and small single-host deployments; D for cost-optimised stateless
batch workloads; E for multi-host scale-out.

### A / B / C / D — local block storage

If you've attached a dedicated block device (cloud volume, second
local disk, NVMe instance store), the steps are the same regardless
of provenance:

```bash
# 1. Format as XFS — preferred for the prjquota support.
sudo mkfs.xfs /dev/<device>          # e.g. /dev/nvme1n1, /dev/sdb1, /dev/disk/azure/scsi1/lun0

# 2. Mount with prjquota; persist in /etc/fstab.
sudo mkdir -p /data/sandbox-volumes
UUID=$(sudo blkid -s UUID -o value /dev/<device>)
echo "UUID=$UUID /data/sandbox-volumes xfs prjquota,defaults 0 2" \
    | sudo tee -a /etc/fstab
sudo mount /data/sandbox-volumes

# 3. Point SANDBOX_VOLUME_BASE at the new mount.
echo 'SANDBOX_VOLUME_BASE=/data/sandbox-volumes' | sudo tee -a /etc/sandbox/env

# 4. Run setup-host.sh WITHOUT --with-xfs-quota (your mount is the
#    XFS already; the loopback is unnecessary).
sudo deploy/setup-host.sh --full

# 5. Bring up the stack.
docker compose --env-file /etc/sandbox/env up -d
```

**Cloud-specific notes:**

- **AWS EBS** — gp3 / io2 are good defaults. Mount via the
  device-symlink form (`/dev/disk/by-id/nvme-Amazon_Elastic_Block_Store_*`)
  to survive controller-letter shuffling.
- **GCP Persistent Disk** — `/dev/disk/by-id/google-<disk-name>` is
  the stable name.
- **Azure managed disk** — `/dev/disk/azure/scsi1/lun*` is stable
  across reboots.
- **AWS local NVMe instance store / GCP local SSD / Azure
  ephemeral OS disk (D)** — these are *erased* on stop/start.
  Acceptable if your sessions are short-lived and you don't care
  about persistence; otherwise you'll lose all workspaces on the
  next instance lifecycle event. Combine with the slice-6a startup
  reconciliation: orphaned sessions get marked `STOPPED` rather
  than crashing the next exec.

### E — Network-mounted storage (NFS / SMB / EFS / FSx)

When you want sessions to share a workspace pool across hosts,
or you're using cloud-managed network storage, mount it once on
the host and point `SANDBOX_VOLUME_BASE` at a subdirectory.

**Trade-offs vs. local block:**

- ⚠️ **No XFS prjquota** — most network filesystems can't enforce
  per-directory size limits. The `Limits.workspace_mib` becomes
  advisory; nothing stops a session from filling the share.
- ⚠️ **chown semantics vary** — SMB/CIFS often rejects `chown`
  unless mounted with forced UIDs. NFSv4 with idmapping usually
  works. The control plane handles this if you set
  `SANDBOX_BIND_VOLUME_UID` correctly (or leave it unset for the
  0777 fallback).
- ⚠️ **Network RTT** on every fs op — `npm install` / `git
  clone` of large monorepos will be 5–20× slower than local disk.
- ✅ **Multi-host scale-out** — the same workspace dir can be
  mounted on multiple control-plane hosts; see slice 10 plan
  notes for the routing concerns this enables.

**SMB / CIFS quick recipe** (the case the user example covered):

```bash
# 1. Create credentials file (lock down to root).
sudo tee /etc/cifs.creds >/dev/null <<EOF
username=YOUR_SMB_USER
password=YOUR_SMB_PASSWORD
EOF
sudo chown root:root /etc/cifs.creds && sudo chmod 0600 /etc/cifs.creds

# 2. Compute the dockremap UID once — used both in fstab and env.
DOCKREMAP_UID=$(awk -F: '$1=="dockremap"{print $2 + 10001}' /etc/subuid)
# +10001 because the container's agent user is UID 10001 inside the
# container; userns-remap maps that to subuid_start + 10001 on the host.
# (NOT +10000 — that's an off-by-one that leaves /workspace owned by
# the host UID corresponding to container UID 10000, which the agent
# isn't, so mkdir / write under /workspace get EACCES.)
echo "use uid=$DOCKREMAP_UID in fstab"

# 3. Add to /etc/fstab. The forceuid/forcegid quartet makes every
#    file appear as the dockremap UID so the control plane's chown
#    is a no-op AND the agent (UID 10001 → 110001 via userns-remap)
#    is the actual owner inside the container.
#
#    ⚠️  All four flags matter. Without forceuid/forcegid the SMB
#        server's UIDs leak through and files appear as 'nobody:nogroup'
#        inside the container — /workspace will look 'rwxr-xr-x' but
#        the agent won't own it, so mkdir / writes fail. Without
#        dir_mode=0700 the SMB mount caps every dir mode at the
#        client default and the control plane's chmod is a no-op.
#        (The control plane now logs a warning + falls back to mode
#        0o777 when chown fails — but fixing the mount is the right
#        long-term answer; the 0o777 fallback is purely a safety net.)
sudo tee -a /etc/fstab >/dev/null <<EOF
//smb-server/share /mnt/shared cifs credentials=/etc/cifs.creds,uid=$DOCKREMAP_UID,gid=$DOCKREMAP_UID,forceuid,forcegid,dir_mode=0700,file_mode=0600,_netdev,vers=3.0 0 0
EOF
sudo mount -a

# 4. Verify the mount options actually took effect. Look for
#    forceuid,forcegid,dir_mode=0700 in the output — Linux silently
#    drops unknown flags so it pays to confirm.
mount | grep /mnt/shared

# 5. Create the subdirectory you want to use as the volume base.
sudo install -d -m 0755 -o root -g root /mnt/shared/data

# 6. Configure /etc/sandbox/env.
sudo tee -a /etc/sandbox/env >/dev/null <<EOF

SANDBOX_VOLUME_BASE=/mnt/shared/data
SANDBOX_BIND_VOLUME_UID=$DOCKREMAP_UID
EOF
sudo chmod 0640 /etc/sandbox/env

# 7. Setup + up. NO --with-xfs-quota.
sudo deploy/setup-host.sh --full
docker compose --env-file /etc/sandbox/env up -d
```

**NFS quick recipe:**

```ini
# /etc/fstab — NFSv4 with idmapping.
nfs-server:/share  /mnt/shared  nfs4  rw,_netdev,nfsvers=4.2,noatime  0  0
```

NFS preserves POSIX UIDs natively (no `forceuid` needed) provided
the dockremap UID exists on the NFS server side too — usually
arranged via NFSv4 ID mapping (`idmapd`) or by ensuring the same
numeric UID exists on both ends. If the NFS server has no
matching UID, files appear as `nobody`; the control plane's
chown will fail. Workaround: leave `SANDBOX_BIND_VOLUME_UID`
unset and accept the 0777 fallback.

**Cloud-managed network storage:**

- **AWS EFS** — mount via NFS; `regional` or `one-zone` storage
  classes both work. Set `nfsvers=4.1` for the AWS docs default.
- **AWS FSx for Lustre / FSx for OpenZFS** — Lustre is fastest
  for bulk IO but has its own quota story; ZFS exposes
  per-directory quotas via `zfs set quota=`.
- **Azure Files** — SMB; same recipe as above.
- **GCP Filestore** — NFSv3/v4; same recipe with `nfsvers`
  adjusted.

### Picking-by-use-case cheat sheet

- **Single-host dev, throw-away sessions** → A (default).
- **Single-host prod, persistent sessions** → B or C.
- **Cloud spot/preemptible workers, batch agents** → D.
- **Multi-host control planes, shared workspace pool** → E
  (with the advisory-quota caveat noted).
- **Hybrid (per-tenant choice)** → not supported in v1; one
  `SANDBOX_VOLUME_BASE` per host. Per-tenant routing to different
  storage is on the v1.2+ track.

### Footgun: pick the path BEFORE the first session

Whichever backend you pick, lock the path in before any session
exists. Docker volumes bake the absolute bind path into their
metadata at create time; changing `SANDBOX_VOLUME_BASE` after
sessions exist orphans them — their volumes still reference the
old path, so resume / exec on those sessions fails until you
recreate the volumes. See "Customize the workspace volume path"
below for the lossy migration recipe if you have to switch on a
running host.

## Operations

### Customize the workspace volume path

By default, per-session workspaces live under
`/var/lib/sandbox-volumes` on the host. To put them somewhere else
(e.g., a dedicated big disk you pre-mounted at
`/data/sandbox-volumes`):

```bash
# 1. Set the variable in /etc/sandbox/env BEFORE running setup-host.sh.
echo 'SANDBOX_VOLUME_BASE=/data/sandbox-volumes' | sudo tee -a /etc/sandbox/env

# 2. setup-host.sh reads /etc/sandbox/env automatically; compose
#    picks up the same value via --env-file. Single source of truth.
sudo deploy/setup-host.sh --full --with-xfs-quota
sudo docker compose --env-file /etc/sandbox/env up -d
```

Three places used to need to agree: compose's host-side bind mount,
the control plane's `SANDBOX_QUOTA_VOLUME_BASE`, and `setup-host.sh`'s
XFS mount target. They now all derive from `SANDBOX_VOLUME_BASE`,
with the legacy default preserved when unset.

**Pick the path BEFORE the first session.** Docker volumes bake the
absolute bind path into their metadata at create time. Changing
`SANDBOX_VOLUME_BASE` after sessions exist orphans them — their
docker volumes still reference the old path, so resume / exec on
those sessions fails. If you must change it on a running host:

```bash
# 1. Stop the stack but leave volumes intact.
sudo docker compose --env-file /etc/sandbox/env stop

# 2. Drain. Either destroy old sessions via the API, or move the data:
sudo systemctl stop sandbox-backup.timer
sudo rsync -aHAX /var/lib/sandbox-volumes/ /data/sandbox-volumes/
sudo umount /var/lib/sandbox-volumes
sudo sed -i 's|/var/lib/sandbox-volumes|/data/sandbox-volumes|' /etc/fstab

# 3. Recreate the docker volumes pointing at the new path. The
#    control plane recreates volumes on session create, so the
#    cleanest path is to drop old metadata for sessions you want to
#    keep, let the control plane recreate them on next /exec:
sudo sqlite3 /var/lib/sandbox/sandbox.db \
    "UPDATE sessions SET status='STOPPED' WHERE status NOT IN ('DESTROYED', 'STOPPED');"
docker volume ls -q --filter 'name=sandbox-vol-' | xargs -r docker volume rm

# 4. Update env, restart.
sudo sed -i 's|^SANDBOX_VOLUME_BASE=.*|SANDBOX_VOLUME_BASE=/data/sandbox-volumes|' /etc/sandbox/env
sudo docker compose --env-file /etc/sandbox/env up -d
```

The migration is doable but lossy for in-flight sessions. The
ARCH-051 reconciliation marks them `STOPPED`; the next exec on each
spins up a fresh container that mounts the **new** path's empty
workspace dir. If you need the old workspace contents, copy them
into the new dir at `<new-base>/<session-id>/_data/` before the
control plane creates the replacement volume.

### Multi-tenant management (slice 12)

The `/v1/tenants/*` admin surface is gated by `SANDBOX_ADMIN_TOKEN`.
Single-tenant deployments leave it unset (admin endpoints return
`503 admin_disabled`); multi-tenant managed deployments set it once:

```bash
echo "SANDBOX_ADMIN_TOKEN=$(openssl rand -hex 32)" \
    | sudo tee -a /etc/sandbox/env
sudo docker compose --env-file /etc/sandbox/env restart control-plane
```

The admin token can then create tenants and issue scoped tokens:

```bash
ADMIN=$(sudo grep ADMIN_TOKEN /etc/sandbox/env | cut -d= -f2)
api_admin() { curl -sS -H "Authorization: Bearer $ADMIN" -H 'Content-Type: application/json' "$@"; }

# Create a tenant with per-tenant limits.
api_admin -d '{"name":"acme","limits":{"max_concurrency":10,"max_workspace_gib":50}}' \
    http://127.0.0.1:8000/v1/tenants
# NOTE on units: TenantLimits.max_workspace_gib is in GiB by design
# (coarse policy cap for tenant-level governance), while per-session
# Limits.workspace_mib is in MiB (granular hard cap enforced by XFS
# prjquota). The two units are deliberate, not drift.

# Issue a scoped token for that tenant (read-only agent, e.g.).
api_admin -d '{"scopes":["session_create","exec","file_read"],"note":"acme-readonly"}' \
    http://127.0.0.1:8000/v1/tenants/acme/tokens
```

Available scopes: `session_create`, `session_destroy`, `exec`,
`file_read`, `file_write`, `file_delete`, `processes` (umbrella),
`tokens_rotate`. Tokens issued without an explicit `scopes` list
get all scopes (back-compat).

**Per-tenant egress allowlist** is a `CreateTenantRequest` field
today (`egress_allowlist`) but **not yet enforced** — Squid runtime
allowlist injection is a v1.2 follow-up. Operators can populate the
field now; the value round-trips through `GET /v1/tenants/{tid}` so
the data model is forward-compatible.

### Idempotency for retries (slice 11a)

Every mutating route under `/v1/` honors an `Idempotency-Key:
<uuid>` header. Replays return the cached response for the
`SANDBOX_IDEMPOTENCY_TTL_S` window (default 24 h). The cache key is
`(tenant_id, key)` so a key reused under different tenants is two
separate operations.

```bash
KEY=$(uuidgen)
curl -sS -H "Authorization: Bearer $TOKEN" \
     -H "Idempotency-Key: $KEY" \
     -d '{}' http://127.0.0.1:8000/v1/sessions
```

### Background processes (slice 11)

`POST /v1/sessions/{id}/processes` starts a long-running command
that survives across exec calls (dev servers, watchers, training
jobs). `GET .../processes/{pid}/logs` is an SSE tail. See the
OpenAPI schema at `/docs` for the full surface.

### Upgrades

Releases publish new images at `ghcr.io/JISUlicious/sandbox-{...}:<tag>`
and re-tag `:latest`. Pin a specific tag in `/etc/sandbox/env` for
production:

```bash
sudo sed -i 's/^SANDBOX_VERSION=.*/SANDBOX_VERSION=v0.1.0/' /etc/sandbox/env
sudo docker compose --env-file /etc/sandbox/env pull
sudo docker compose --env-file /etc/sandbox/env up -d
```

In-flight sessions keep their old `sandbox-runtime` image until the
session is destroyed (per ARCH §8, blue/green). Only sessions
created *after* the upgrade use the new runtime.

### Logs / metrics

```bash
sudo docker compose --env-file /etc/sandbox/env logs -f control-plane
sudo docker compose --env-file /etc/sandbox/env logs -f proxy
sudo lsattr /var/log/sandbox/audit.log        # confirm `+a`
curl -s http://127.0.0.1:8000/metrics | head  # Prometheus exposition
```

### Backup / restore

State lives on host paths (`/var/lib/sandbox`, `/var/log/sandbox`,
`/var/lib/sandbox-volumes`). The existing `deploy/backup.sh` +
systemd timer work unchanged. See
[SETUP.md "State management"](./SETUP.md#state-management-backup-restore-teardown).

The compose stack tolerates the brief outage `backup.sh` introduces:
`docker compose stop control-plane`, snapshot, `docker compose start
control-plane`. The reaper / sampler restart with the lifespan.

### Tear down

```bash
sudo docker compose --env-file /etc/sandbox/env down       # stops + removes containers
sudo systemctl disable --now sandbox-iptables 2>/dev/null  # if the systemd unit is active
docker network rm sandbox_egress
sudo rm -rf /var/log/sandbox /var/lib/sandbox              # destructive; back up first
sudo umount /var/lib/sandbox-volumes && sudo rm /var/lib/sandbox-fs.img
sudo sed -i '/sandbox-fs.img/d' /etc/fstab
```

## Image visibility

GHCR packages default to **private**. After the first successful
release run, flip them to public via the GitHub UI:

1. <https://github.com/JISUlicious?tab=packages>
2. For each of `sandbox-runtime`, `sandbox-proxy`,
   `sandbox-control-plane`: Package settings → "Change visibility" →
   Public.

External adopters need this to `docker pull` without a GHCR auth
token.

## Troubleshooting

### `open /etc/sandbox/env: permission denied`

Compose reads the file at parse time on the operator's host. The
file is `0640 root:sandbox`, so the calling user needs to be in
the `sandbox` group (or you `sudo` the compose command).

```bash
# Long-term fix — covers every `docker compose ...` call.
sudo usermod -aG sandbox "$USER"
newgrp sandbox    # or open a fresh shell

# One-shot — works without modifying group membership.
sudo docker compose --env-file /etc/sandbox/env up -d
```

If you accidentally cp'd the file as `root:root` (the system
default), restore the group:

```bash
sudo chown root:sandbox /etc/sandbox/env
sudo chmod 0640 /etc/sandbox/env
```

### `503 admin_disabled` on `/v1/tenants/*`

`SANDBOX_ADMIN_TOKEN` is unset. The admin surface is opt-in; set
the env var and restart the control-plane container:

```bash
echo "SANDBOX_ADMIN_TOKEN=$(openssl rand -hex 32)" \
    | sudo tee -a /etc/sandbox/env
docker compose --env-file /etc/sandbox/env restart control-plane
```

### Docker compose says "no such file" on `deploy/.env.compose.example`

The file shipped from PR #12 onwards. If you cloned an older revision
or hit the gitignore bug from before that PR, pull the latest main
and try again — `git status` will show the file as untracked vs.
already-tracked depending on which side of the fix your clone is
on.

### `Error Head https://ghcr.io/v2/.../manifests/latest: denied`

The control-plane / runtime / proxy images don't exist on ghcr.io
yet — most likely because no `v*.*.*` release has been cut, or the
packages exist but are private (GHCR's default).

**Quick fix — build locally** (you don't need a release to test):

```bash
docker build -t ghcr.io/jisulicious/sandbox-runtime:latest sandbox/
docker build -t ghcr.io/jisulicious/sandbox-proxy:latest   proxy/
docker build -f Dockerfile.control-plane \
             -t ghcr.io/jisulicious/sandbox-control-plane:latest .
docker compose --env-file /etc/sandbox/env up -d
```

Compose's default `pull_policy=missing` finds the locally-built
images and skips the pull. Forks publishing under a different
namespace can override via `SANDBOX_IMAGE_NAMESPACE` in
`/etc/sandbox/env`.

**Proper fix — cut a release tag**:

```bash
git tag v0.1.0
git push origin v0.1.0
```

The GitHub Actions workflow at `.github/workflows/release.yml`
builds + pushes the three images on every `v*.*.*` tag. After it
finishes, flip each package to **public** (one-time UI step) per
the "Image visibility" section above, then:

```bash
sudo sed -i 's/^SANDBOX_VERSION=.*/SANDBOX_VERSION=v0.1.0/' /etc/sandbox/env
docker compose --env-file /etc/sandbox/env pull
docker compose --env-file /etc/sandbox/env up -d
```

### `network sandbox_egress was found but has incorrect label`

`docker compose up` complains that a pre-existing `sandbox_egress`
network is missing its `com.docker.compose.network` label. The
network is intentionally operator-managed (created by
`setup-host.sh --full` so the subnet is pinned for the iptables
rules to target). `compose.yml` declares it `external: true` so
compose treats it as pre-existing, but older clones may not have
that declaration:

```bash
git pull
docker compose --env-file /etc/sandbox/env up -d
```

If you'd rather have compose manage the network, drop the existing
one and the iptables rules referencing it (only safe with the
stack stopped):

```bash
docker compose --env-file /etc/sandbox/env down
docker network rm sandbox_egress
sudo deploy/setup-host.sh --full           # recreates with the pinned subnet
docker compose --env-file /etc/sandbox/env up -d
```

### `ImageNotFound: ghcr.io/.../sandbox-runtime:<version>` on session create

The control plane is asking the host's docker daemon to create a
sandbox container from an image the host doesn't have. The
image-warmer service is supposed to populate the image on `up -d`,
but it can miss in three cases:

1. **First-ever `up -d` and the GHCR packages are still private** —
   the warmer's pull returns `denied`. Fix: flip the package
   visibility to public per "Image visibility" above, then re-run
   `docker compose --env-file /etc/sandbox/env up -d --force-recreate
   image-warmer`.
2. **`SANDBOX_VERSION` was bumped without recreating the warmer**
   — `compose restart control-plane` doesn't re-run the warmer,
   which is `restart: on-failure:3` (one-shot on success). Fix:
   ```
   docker compose --env-file /etc/sandbox/env up -d --force-recreate
   ```
   Force-recreate without a target re-runs the warmer along with
   any other changed services.
3. **`docker system prune` removed the runtime image after the
   warmer succeeded.** Fix: pull manually.
   ```
   VERSION=$(awk -F= '/^SANDBOX_VERSION=/{print $2; exit}' /etc/sandbox/env)
   docker pull ghcr.io/jisulicious/sandbox-runtime:${VERSION:-latest}
   ```

After fix 2 or 3, retry the session create — no service restart
needed; the docker daemon's image cache is what the control plane
queries each time.

### `dependency failed to start: container sandbox-image-warmer`

The image-warmer hit its `on-failure:3` retry cap. Logs say why:

```bash
docker compose --env-file /etc/sandbox/env logs sandbox-image-warmer
```

Most common: GHCR auth (private package, expired token, wrong
namespace). Fix the underlying issue, then:

```bash
docker compose --env-file /etc/sandbox/env up -d --force-recreate
```

### `runsc not registered` on session create

`docker info | grep runsc` should list it. If empty, run
`sudo deploy/setup-host.sh --full` to install + register gVisor and
restart the docker daemon.

### Sessions create but exec hangs

Most often a stale `sandbox_egress` network with a different subnet.
Stop the stack, drop the network, re-run setup-host.sh:

```bash
docker compose --env-file /etc/sandbox/env down
docker network rm sandbox_egress
sudo deploy/setup-host.sh --full
docker compose --env-file /etc/sandbox/env up -d
```

## What's NOT in this deployment path

- Multi-arch (`linux/arm64`) — runsc has no arm64.
- cosign image signing / SBOM attestations.
- Helm chart / k8s manifests.
- Curl-pipe-bash installer.

Re-open these if there's external demand.
