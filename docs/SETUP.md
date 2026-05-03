# Linux Setup Guide

Two paths to a working sandbox host: **Dev** (functional, relaxed
isolation, ~10 min) and **Production** (SPEC-400 / SPEC-302 / SPEC-401
hardening, ~30 min plus slice-5 work). VM-specific differences are
flagged inline.

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

### 5 · Egress proxy + iptables — slice 5 (pending)

When slice 5 lands the repo will ship:

- `proxy/Dockerfile` and config templates for the Squid container.
- `deploy/iptables-setup.sh` — idempotent rules added to the
  **`DOCKER-USER`** chain so Docker's restart doesn't wipe them.

Until then: run with `SANDBOX_DEV_MODE=1` even in production. The rest
of the hardening (gVisor, userns-remap, non-root UID, read-only rootfs,
cap-drop, seccomp via runsc, XFS quota) is already wired and active.

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
SANDBOX_BIND_HOST=127.0.0.1
SANDBOX_BIND_PORT=8000
SANDBOX_DB_PATH=/var/lib/sandbox/sandbox.db
SANDBOX_AUDIT_LOG_PATH=/var/log/sandbox/audit.log
SANDBOX_SANDBOX_IMAGE=sandbox-runtime:latest
# Drop SANDBOX_DEV_MODE once slice 5 ships.
SANDBOX_DEV_MODE=1
```

`/etc/systemd/system/sandbox-api.service`:

```ini
[Unit]
Description=Sandbox Service control plane
After=docker.service network-online.target
Requires=docker.service

[Service]
Type=simple
User=sandbox
Group=sandbox
WorkingDirectory=/opt/sandbox
EnvironmentFile=/etc/sandbox/env
ExecStart=/opt/sandbox/.venv/bin/uvicorn api.server:app --host 127.0.0.1 --port 8000
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now sandbox-api
sudo journalctl -u sandbox-api -f
curl -H 'Authorization: Bearer '"$(sudo grep API_TOKEN /etc/sandbox/env | cut -d= -f2)" \
    http://127.0.0.1:8000/healthz
```

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
- [ ] `sandbox` user is non-root, only in the `docker` group.
- [ ] API bound to `127.0.0.1` (or behind a reverse proxy with TLS).
- [ ] `/metrics` scraping restricted to internal network.
- [ ] `sandbox-runtime:latest` pre-pulled at boot.
- [ ] Audit log directory writable by `sandbox` user, rotated daily.

---

## Common pitfalls

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
