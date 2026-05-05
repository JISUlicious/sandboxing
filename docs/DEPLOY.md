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

```bash
git clone https://github.com/JISUlicious/sandboxing
cd sandboxing

# 1. Install Docker, gVisor, daemon.json (userns-remap), iptables,
#    sandbox_egress network, slice-9 security hardening — all in one.
sudo deploy/setup-host.sh --full --with-xfs-quota

# 2. Drop in /etc/sandbox/env. Set the two secrets.
sudo cp deploy/.env.compose.example /etc/sandbox/env
sudoedit /etc/sandbox/env                    # SANDBOX_API_TOKEN + _PEPPER
sudo chown root:root /etc/sandbox/env
sudo chmod 0640 /etc/sandbox/env

# 3. Up. The --env-file gives compose access to the same file for
#    BOTH variable substitution (image tags, bind paths) and the
#    container's runtime env. See "Customize the workspace volume
#    path" below for why this matters.
sudo docker compose --env-file /etc/sandbox/env up -d

# 4. Smoke check.
TOKEN=$(sudo grep API_TOKEN /etc/sandbox/env | cut -d= -f2)
curl -sS -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8000/healthz
# {"status":"ok"}
```

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

## What's NOT in this deployment path

- Multi-arch (`linux/arm64`) — runsc has no arm64.
- cosign image signing / SBOM attestations.
- Helm chart / k8s manifests.
- Curl-pipe-bash installer.

Re-open these if there's external demand.
