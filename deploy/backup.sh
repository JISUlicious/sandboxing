#!/usr/bin/env bash
# deploy/backup.sh — consistent nightly backup of sandbox state.
#
# Snapshots all four pieces of on-disk state (per docs/SETUP.md):
#   - SQLite registry          (sqlite3 .backup for crash-safe copy)
#   - audit JSONL + rotations  (cp -a)
#   - loopback volume image    (sparse cp after umount)
#   - control-plane env        (cp -a — bearer tokens; chmod 0600)
#
# Output structure:
#   $BACKUP_ROOT/sandbox-YYYYmmdd-HHMMSS/
#       sandbox.db
#       audit.log*
#       sandbox-fs.img
#       env
#
# Retention: keep the most recent $BACKUP_KEEP_N (default 14) and
# delete the rest.
#
# Designed to run as root via deploy/sandbox-backup.{service,timer}.
# Pre-conditions:
#   - sandbox-api.service exists and can be safely stopped briefly.
#   - $BACKUP_ROOT exists and is writable by root.

set -euo pipefail

BACKUP_ROOT="${BACKUP_ROOT:-/var/backups/sandbox}"
BACKUP_KEEP_N="${BACKUP_KEEP_N:-14}"
SANDBOX_DB="${SANDBOX_DB:-/var/lib/sandbox/sandbox.db}"
SANDBOX_AUDIT_DIR="${SANDBOX_AUDIT_DIR:-/var/log/sandbox}"
SANDBOX_VOLUMES_MOUNT="${SANDBOX_VOLUMES_MOUNT:-/var/lib/sandbox-volumes}"
SANDBOX_FS_IMG="${SANDBOX_FS_IMG:-/var/lib/sandbox-fs.img}"
SANDBOX_ENV="${SANDBOX_ENV:-/etc/sandbox/env}"
SANDBOX_SERVICE="${SANDBOX_SERVICE:-sandbox-api.service}"

if [[ $EUID -ne 0 ]]; then
    echo "ERROR: run as root (sudo)." >&2
    exit 1
fi

ts="$(date -u +%Y%m%d-%H%M%S)"
dest="$BACKUP_ROOT/sandbox-$ts"
mkdir -p "$dest"
chmod 0700 "$dest"

log() { echo "==> [$(date -u +%H:%M:%S)] $*"; }

log "backup destination: $dest"

# Stop the service so SQLite is quiescent and the loopback file can
# be unmounted cleanly. The trap re-starts it even if a step fails.
restore_service() {
    if mountpoint -q "$SANDBOX_VOLUMES_MOUNT"; then : ; else
        log "remounting $SANDBOX_VOLUMES_MOUNT"
        mount "$SANDBOX_VOLUMES_MOUNT" || true
    fi
    if systemctl is-enabled --quiet "$SANDBOX_SERVICE"; then
        log "restarting $SANDBOX_SERVICE"
        systemctl start "$SANDBOX_SERVICE" || true
    fi
}
trap restore_service EXIT

if systemctl is-active --quiet "$SANDBOX_SERVICE"; then
    log "stopping $SANDBOX_SERVICE"
    systemctl stop "$SANDBOX_SERVICE"
fi

# Registry — sqlite3 .backup is safe even on a hot DB, but with the
# service stopped we can also `cp` directly. .backup is the more
# defensible choice.
if [[ -f "$SANDBOX_DB" ]]; then
    log "registry → $dest/sandbox.db"
    sqlite3 "$SANDBOX_DB" ".backup '$dest/sandbox.db'"
else
    log "registry not found at $SANDBOX_DB; skipping"
fi

# Audit log + rotated companions.
if [[ -d "$SANDBOX_AUDIT_DIR" ]]; then
    log "audit logs → $dest/"
    cp -a "$SANDBOX_AUDIT_DIR"/audit.log* "$dest/" 2>/dev/null || true
fi

# Loopback volume image. Unmount briefly for a consistent block-level
# snapshot; the trap re-mounts on exit.
if mountpoint -q "$SANDBOX_VOLUMES_MOUNT"; then
    log "unmounting $SANDBOX_VOLUMES_MOUNT"
    umount "$SANDBOX_VOLUMES_MOUNT"
fi
if [[ -f "$SANDBOX_FS_IMG" ]]; then
    log "loopback image → $dest/sandbox-fs.img"
    cp --sparse=always "$SANDBOX_FS_IMG" "$dest/sandbox-fs.img"
fi

# Env file — contains bearer tokens; tighten perms.
if [[ -f "$SANDBOX_ENV" ]]; then
    log "env → $dest/env (mode 0600)"
    cp -a "$SANDBOX_ENV" "$dest/env"
    chmod 0600 "$dest/env"
fi

# Retention.
log "rotation: keeping the most recent $BACKUP_KEEP_N"
mapfile -t old < <(
    find "$BACKUP_ROOT" -maxdepth 1 -mindepth 1 -type d -name 'sandbox-*' \
        -printf '%T@ %p\n' \
    | sort -rn | tail -n "+$((BACKUP_KEEP_N + 1))" | awk '{print $2}'
)
for d in "${old[@]:-}"; do
    [[ -z "$d" ]] && continue
    log "removing $d"
    rm -rf "$d"
done

log "done"
