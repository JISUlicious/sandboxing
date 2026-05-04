#!/usr/bin/env bash
# deploy/setup-host.sh — apply slice-9 security hardening on a host
# that has already followed the Docker / gVisor / iptables steps in
# docs/SETUP.md.
#
# Idempotent: re-running is safe. Each step prints OK / SKIP / FAIL.
#
# What it does, in order:
#   1. Compute SANDBOX_BIND_VOLUME_UID from /etc/subuid (dockremap +
#      10000) and persist it in /etc/sandbox/env.
#   2. Install /usr/local/bin/sandbox-quota-helper + a locked-down
#      sudoers entry. Removes the old wildcard sudoers rule if found.
#   3. Install /etc/logrotate.d/sandbox and `chattr +a` the audit log.
#   4. Enforce mode 0640 root:sandbox on /etc/sandbox/env.
#   5. Install (or replace) the hardened sandbox-api.service unit and
#      reload systemd.
#
# Usage:
#   sudo deploy/setup-host.sh            # apply all steps
#   sudo deploy/setup-host.sh --check    # report state, change nothing

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE=/etc/sandbox/env
SUDOERS_FILE=/etc/sudoers.d/sandbox-quota-helper
SUDOERS_OLD=/etc/sudoers.d/sandbox-xfs-quota
LOGROTATE_DST=/etc/logrotate.d/sandbox
HELPER_SRC="$SCRIPT_DIR/sandbox-quota-helper.sh"
HELPER_DST=/usr/local/bin/sandbox-quota-helper
LOGROTATE_SRC="$SCRIPT_DIR/sandbox.logrotate"
UNIT_SRC="$SCRIPT_DIR/sandbox-api.service"
UNIT_DST=/etc/systemd/system/sandbox-api.service
AUDIT_LOG=/var/log/sandbox/audit.log

CHECK_ONLY=0
[[ "${1:-}" == "--check" ]] && CHECK_ONLY=1

if [[ $EUID -ne 0 ]]; then
    echo "ERROR: must run as root (sudo $0)" >&2
    exit 2
fi

note()  { printf '  %s\n' "$*"; }
ok()    { printf '\e[32mOK   \e[0m %s\n' "$*"; }
skip()  { printf '\e[33mSKIP \e[0m %s\n' "$*"; }
fail()  { printf '\e[31mFAIL \e[0m %s\n' "$*" >&2; }

run() {
    if (( CHECK_ONLY )); then
        note "would run: $*"
    else
        "$@"
    fi
}

# ---------------------------------------------------------------------
# 1. Compute SANDBOX_BIND_VOLUME_UID from /etc/subuid (dockremap line).
# ---------------------------------------------------------------------
echo "1) bind-volume UID (SPEC-401)"
if ! getent passwd dockremap >/dev/null 2>&1; then
    skip "dockremap user missing — userns-remap not configured?"
    skip "follow docs/SETUP.md §3 to set up userns-remap=default"
elif [[ ! -f /etc/subuid ]]; then
    fail "/etc/subuid not found"
    exit 1
else
    DOCKREMAP_START=$(awk -F: '$1=="dockremap"{print $2}' /etc/subuid | head -n1)
    if [[ -z "$DOCKREMAP_START" ]]; then
        fail "no dockremap entry in /etc/subuid"
        exit 1
    fi
    BIND_UID=$((DOCKREMAP_START + 10000))
    note "dockremap subuid range starts at $DOCKREMAP_START → container UID 10001 → host UID $BIND_UID"

    if [[ -f "$ENV_FILE" ]] && grep -q '^SANDBOX_BIND_VOLUME_UID=' "$ENV_FILE"; then
        existing=$(awk -F= '$1=="SANDBOX_BIND_VOLUME_UID"{print $2}' "$ENV_FILE")
        if [[ "$existing" == "$BIND_UID" ]]; then
            skip "SANDBOX_BIND_VOLUME_UID already set to $BIND_UID"
        else
            note "updating SANDBOX_BIND_VOLUME_UID: $existing → $BIND_UID"
            run sed -i "s|^SANDBOX_BIND_VOLUME_UID=.*|SANDBOX_BIND_VOLUME_UID=$BIND_UID|" "$ENV_FILE"
            ok "SANDBOX_BIND_VOLUME_UID rewritten in $ENV_FILE"
        fi
    elif [[ -f "$ENV_FILE" ]]; then
        run bash -c "echo 'SANDBOX_BIND_VOLUME_UID=$BIND_UID' >> '$ENV_FILE'"
        ok "SANDBOX_BIND_VOLUME_UID=$BIND_UID appended to $ENV_FILE"
    else
        skip "$ENV_FILE missing — create it per docs/SETUP.md §6 first"
    fi
fi
echo

# ---------------------------------------------------------------------
# 2. Install sandbox-quota-helper + locked-down sudoers entry.
# ---------------------------------------------------------------------
echo "2) sandbox-quota-helper + sudoers"
if [[ ! -f "$HELPER_SRC" ]]; then
    fail "$HELPER_SRC not found"
    exit 1
fi
if cmp -s "$HELPER_SRC" "$HELPER_DST" 2>/dev/null; then
    skip "$HELPER_DST already up-to-date"
else
    run install -m 0755 -o root -g root "$HELPER_SRC" "$HELPER_DST"
    ok "installed $HELPER_DST"
fi

# Drop the old wildcard sudoers rule if present.
if [[ -f "$SUDOERS_OLD" ]]; then
    run rm -f "$SUDOERS_OLD"
    ok "removed legacy $SUDOERS_OLD (wildcard sed/tee/touch)"
fi

new_sudoers="sandbox ALL=(root) NOPASSWD: $HELPER_DST *"
if [[ -f "$SUDOERS_FILE" ]] && grep -qxF "$new_sudoers" "$SUDOERS_FILE"; then
    skip "$SUDOERS_FILE already correct"
else
    if (( CHECK_ONLY )); then
        note "would write to $SUDOERS_FILE: $new_sudoers"
    else
        printf '%s\n' "$new_sudoers" > "$SUDOERS_FILE"
        chmod 0440 "$SUDOERS_FILE"
        chown root:root "$SUDOERS_FILE"
        # Reject malformed sudoers atomically.
        if ! visudo -c -f "$SUDOERS_FILE" >/dev/null; then
            fail "$SUDOERS_FILE rejected by visudo; rolling back"
            rm -f "$SUDOERS_FILE"
            exit 1
        fi
    fi
    ok "wrote $SUDOERS_FILE"
fi
echo

# ---------------------------------------------------------------------
# 3. Logrotate + chattr +a audit log.
# ---------------------------------------------------------------------
echo "3) audit log: logrotate + chattr +a"
if [[ ! -f "$LOGROTATE_SRC" ]]; then
    fail "$LOGROTATE_SRC not found"
    exit 1
fi
if cmp -s "$LOGROTATE_SRC" "$LOGROTATE_DST" 2>/dev/null; then
    skip "$LOGROTATE_DST already up-to-date"
else
    run install -m 0644 -o root -g root "$LOGROTATE_SRC" "$LOGROTATE_DST"
    ok "installed $LOGROTATE_DST"
fi

# Ensure the audit log exists with the right ownership BEFORE chattr +a;
# afterwards, the file can't be deleted/truncated until -a is reapplied.
audit_dir=$(dirname "$AUDIT_LOG")
if [[ ! -d "$audit_dir" ]]; then
    run install -d -m 0750 -o sandbox -g sandbox "$audit_dir"
    ok "created $audit_dir"
fi
if [[ ! -f "$AUDIT_LOG" ]]; then
    run install -m 0640 -o sandbox -g sandbox /dev/null "$AUDIT_LOG"
    ok "created $AUDIT_LOG"
fi
if lsattr "$AUDIT_LOG" 2>/dev/null | awk '{print $1}' | grep -q a; then
    skip "$AUDIT_LOG already +a"
else
    if run chattr +a "$AUDIT_LOG" 2>/dev/null; then
        ok "$AUDIT_LOG is now append-only (chattr +a)"
    else
        fail "chattr +a $AUDIT_LOG failed (filesystem may not support it)"
    fi
fi
echo

# ---------------------------------------------------------------------
# 4. Enforce /etc/sandbox/env permissions.
# ---------------------------------------------------------------------
echo "4) /etc/sandbox/env permissions"
if [[ ! -f "$ENV_FILE" ]]; then
    skip "$ENV_FILE missing — create it per docs/SETUP.md §6 first"
else
    mode=$(stat -c '%a' "$ENV_FILE")
    owner=$(stat -c '%U:%G' "$ENV_FILE")
    if [[ "$mode" == "640" && "$owner" == "root:sandbox" ]]; then
        skip "$ENV_FILE already 0640 root:sandbox"
    else
        run chown root:sandbox "$ENV_FILE"
        run chmod 0640 "$ENV_FILE"
        ok "$ENV_FILE → 0640 root:sandbox (was $mode $owner)"
    fi
fi
echo

# ---------------------------------------------------------------------
# 5. Install hardened sandbox-api.service.
# ---------------------------------------------------------------------
echo "5) sandbox-api.service (hardened)"
if [[ ! -f "$UNIT_SRC" ]]; then
    fail "$UNIT_SRC not found"
    exit 1
fi
if cmp -s "$UNIT_SRC" "$UNIT_DST" 2>/dev/null; then
    skip "$UNIT_DST already up-to-date"
else
    run install -m 0644 -o root -g root "$UNIT_SRC" "$UNIT_DST"
    ok "installed $UNIT_DST"
    if (( ! CHECK_ONLY )); then
        systemctl daemon-reload
        ok "systemctl daemon-reload"
        if systemctl is-active --quiet sandbox-api; then
            note "sandbox-api is currently active; restart with:"
            note "  sudo systemctl restart sandbox-api"
        fi
    fi
fi
echo

if (( CHECK_ONLY )); then
    echo "(check mode — no changes applied)"
else
    echo "Done. Next: sudo systemctl restart sandbox-api"
fi
