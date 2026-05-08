#!/usr/bin/env bash
# deploy/setup-host.sh — bootstrap a sandbox-service host.
#
# Default mode applies the security hardening (slice 9). `--full`
# also installs the prereqs (Docker + gVisor + daemon.json
# userns-remap + iptables + sandbox_egress network) so a fresh
# Ubuntu/Debian box is ready for `docker compose up -d`.
#
# Idempotent: re-running is safe. Each step prints OK / SKIP / FAIL.
#
# Sections (security):
#   1. SANDBOX_BIND_VOLUME_UID computed from /etc/subuid → env file.
#   2. /usr/local/bin/sandbox-quota-helper + locked-down sudoers.
#   3. /etc/logrotate.d/sandbox + `chattr +a` on the audit log.
#   4. Enforce 0640 root:sandbox on /etc/sandbox/env.
#   5. Install + daemon-reload the hardened sandbox-api.service.
#
# Sections (--full only, run BEFORE the security ones):
#   F1. apt: docker.io + docker-compose-plugin.
#   F2. apt: runsc (gVisor).
#   F3. /etc/docker/daemon.json: register runsc + userns-remap=default.
#   F4. (Optional --with-xfs-quota) loopback XFS at /var/lib/sandbox-volumes.
#   F5. iptables-setup.sh.
#   F6. Pre-create the sandbox_egress network.
#
# Usage:
#   sudo deploy/setup-host.sh                       # security only
#   sudo deploy/setup-host.sh --full                # prereqs + security
#   sudo deploy/setup-host.sh --full --with-xfs-quota
#   sudo deploy/setup-host.sh --check               # dry-run, no changes

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
DAEMON_JSON=/etc/docker/daemon.json

# Several knobs are also referenced by compose.yml's variable
# substitution. When invoked via `sudo`, the operator's exported
# values would normally be stripped (secure_path reset), so for any
# unset var we pull it from /etc/sandbox/env. Putting them once in
# that file keeps this script and `docker compose --env-file
# /etc/sandbox/env up` in sync.
read_env() {
    local var=$1
    if [[ -z "${!var:-}" && -r /etc/sandbox/env ]]; then
        local v
        v=$(awk -F= -v k="$var" '$1==k{print $2; exit}' /etc/sandbox/env || true)
        if [[ -n "$v" ]]; then
            printf -v "$var" '%s' "$v"
            export "$var"
        fi
    fi
}
read_env SANDBOX_VOLUME_BASE
read_env SANDBOX_SUBNET
read_env PROXY_IP
read_env PROXY_PORT
read_env SANDBOX_FS_IMG
read_env SANDBOX_IMAGE_NAMESPACE   # informational; not used here directly

XFS_MOUNT="${SANDBOX_VOLUME_BASE:-/var/lib/sandbox-volumes}"
# Loopback image lives next to the mount; same default name backup.sh
# uses so the two scripts target one file.
XFS_IMG="${SANDBOX_FS_IMG:-/var/lib/sandbox-fs.img}"
XFS_SIZE_GB="${XFS_SIZE_GB:-50}"   # initial loopback size; override via env
SUBNET="${SANDBOX_SUBNET:-172.30.0.0/24}"

CHECK_ONLY=0
FULL=0
WITH_XFS=0
NO_USERNS_REMAP=0
REMOVE_USERNS_REMAP=0
for arg in "$@"; do
    case "$arg" in
        --check)              CHECK_ONLY=1 ;;
        --full)               FULL=1 ;;
        --with-xfs-quota)     WITH_XFS=1 ;;
        --no-userns-remap)    NO_USERNS_REMAP=1 ;;
        --remove-userns-remap) REMOVE_USERNS_REMAP=1 ;;
        -h|--help)
            sed -n '2,/^$/p' "$0" | head -n 35 | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *) echo "unknown flag: $arg" >&2; exit 2 ;;
    esac
done

# --no-userns-remap and --remove-userns-remap are mutually compatible
# but conceptually different: --no-userns-remap means "going forward,
# don't ADD userns-remap to daemon.json"; --remove-userns-remap means
# "actively REMOVE it from an existing daemon.json (and update the
# env file's SANDBOX_BIND_VOLUME_UID accordingly)". Setting both
# implies the operator definitively wants no userns-remap on this
# host — same effect as either flag alone for the F3 logic.

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
# --full prereq sections (F1–F6). Skipped unless --full was passed.
# Apt-only; Rocky/RHEL users follow docs/SETUP.md for the manual path.
# ---------------------------------------------------------------------
if (( FULL )); then
    if ! command -v apt-get >/dev/null 2>&1; then
        fail "--full only supports apt-based distros. Follow docs/SETUP.md manually on Rocky/RHEL."
        exit 1
    fi

    # ---- F1. Docker Engine + compose plugin + jq ------------------
    # jq is needed by F3's daemon.json merge logic (preserve operator
    # settings; only add what's missing). Bundling it here so F3
    # doesn't have to apt-install partway through.
    echo "F1) Docker Engine + compose plugin + jq"
    if command -v docker >/dev/null 2>&1 \
        && docker compose version >/dev/null 2>&1 \
        && command -v jq >/dev/null 2>&1; then
        skip "docker + compose + jq already installed ($(docker --version | cut -d, -f1))"
    else
        run apt-get update -qq
        run apt-get install -y --no-install-recommends \
            ca-certificates curl docker.io docker-compose-plugin jq
        run systemctl enable --now docker
        ok "Docker Engine + jq installed + enabled"
    fi
    echo

    # ---- F2. gVisor (runsc) --------------------------------------
    # Append-only on gvisor.list: never overwrite. Operators may have
    # added comments, alternative URLs, or pinned a release branch
    # other than `release`; we only add the canonical line if the
    # gVisor URL isn't already present anywhere in the file.
    GVISOR_LIST=/etc/apt/sources.list.d/gvisor.list
    GVISOR_KEYRING=/etc/apt/keyrings/gvisor-archive-keyring.gpg
    echo "F2) gVisor (runsc)"
    if command -v runsc >/dev/null 2>&1; then
        skip "runsc already installed ($(runsc --version 2>&1 | head -n1))"
    else
        run install -d -m 0755 /etc/apt/keyrings
        if (( ! CHECK_ONLY )); then
            if [[ ! -f "$GVISOR_KEYRING" ]]; then
                curl -fsSL https://gvisor.dev/archive.key \
                    | gpg --dearmor -o "$GVISOR_KEYRING"
                ok "$GVISOR_KEYRING written"
            else
                skip "$GVISOR_KEYRING already present"
            fi
            arch=$(dpkg --print-architecture)
            gvisor_line="deb [arch=$arch signed-by=$GVISOR_KEYRING] https://storage.googleapis.com/gvisor/releases release main"
            if [[ -f "$GVISOR_LIST" ]] \
                && grep -qF 'storage.googleapis.com/gvisor/releases' "$GVISOR_LIST" 2>/dev/null; then
                skip "$GVISOR_LIST already references gvisor releases"
            else
                # Append; never overwrite. If file is missing, this
                # creates it with one line.
                printf '%s\n' "$gvisor_line" >> "$GVISOR_LIST"
                ok "$GVISOR_LIST appended ($gvisor_line)"
            fi
        else
            note "would install gvisor key + apt source"
        fi
        run apt-get update -qq
        run apt-get install -y --no-install-recommends runsc
        ok "runsc installed"
    fi
    echo

    # ---- F3. /etc/docker/daemon.json (jq-merge, never overwrite) -
    # The previous implementation cat-overwrote daemon.json when the
    # `"runsc"` or `"userns-remap"` markers were missing — losing any
    # operator-set keys (log-driver, mtu, registry-mirrors, custom
    # runtimes like nvidia, etc.) silently. Now: jq-merge with `//=`
    # (assign-if-missing). Operator-set values WIN; we only add the
    # three keys we need when they're absent. Backup before write.
    if (( NO_USERNS_REMAP )); then
        echo "F3) /etc/docker/daemon.json (runsc only — userns-remap skipped per --no-userns-remap)"
        note "userns-remap=default not added to daemon.json this run."
        note "Defense-in-depth layer is skipped; gVisor + cap_drop + non-root"
        note "agent + read-only fs remain as the primary isolation. See"
        note "docs/DEPLOY.md 'Choosing the userns-remap posture' for the"
        note "trade-off."
    else
        echo "F3) /etc/docker/daemon.json (runsc + userns-remap, merge)"
    fi
    install -d -m 0755 /etc/docker

    # ----- F3-pre. Preflight `userns-remap=default` prerequisites -
    # On a host without these, `systemctl restart docker` after we
    # add userns-remap to daemon.json fails with cryptic startup
    # errors. Docker is documented to auto-create dockremap, but in
    # practice that path is fragile (AppArmor, NSS quirks, partial
    # state from a previous run). Doing it ourselves up front is
    # idempotent and removes the failure mode entirely.
    #
    # Skipped entirely when --no-userns-remap is passed (operator
    # has decided not to add the userns-remap layer; the dockremap
    # user/subuid pre-flight is then irrelevant).
    if (( ! NO_USERNS_REMAP )); then
    if ! getent passwd dockremap >/dev/null 2>&1; then
        run useradd --system --no-create-home --shell /usr/sbin/nologin dockremap
        ok "dockremap system user created"
    else
        skip "dockremap user already present"
    fi
    if ! getent group dockremap >/dev/null 2>&1; then
        run groupadd --system dockremap
        ok "dockremap system group created"
    else
        skip "dockremap group already present"
    fi
    # Subuid / subgid: ensure dockremap has a 65536-UID range, but
    # don't hardcode 100000:65536 — that range may already be in use
    # by another user on a multi-tenant host, and appending an
    # overlapping entry makes Docker reject the config at startup.
    #
    # Three-tier strategy, in order:
    #   1. If dockremap already has an entry, leave it (never overwrite).
    #   2. Else try `usermod --add-subuids/--add-subgids` (shadow 4.5+
    #      auto-finds a free range using SUB_UID_MIN/MAX in login.defs).
    #   3. Else fall back to scan-and-append: find the highest existing
    #      end-of-range and allocate the next 65536 slots.
    ensure_dockremap_subid() {
        local file=$1   # /etc/subuid or /etc/subgid
        local kind=$2   # "subuid" or "subgid"
        local flag=$3   # --add-subuids / --add-subgids
        if [[ -f "$file" ]] && grep -q '^dockremap:' "$file"; then
            skip "$file already has dockremap entry ($(grep '^dockremap:' "$file" | head -n1))"
            return 0
        fi
        if (( CHECK_ONLY )); then
            note "would allocate a free 65536 range for dockremap in $file"
            return 0
        fi
        # File may be missing on minimal images; create with safe mode.
        touch "$file" && chmod 0644 "$file"
        # Tier 2: let usermod pick a free range (preferred — respects
        # /etc/login.defs SUB_UID_MIN/MAX). Only present in shadow >=4.5.
        if usermod --help 2>&1 | grep -q -- "$flag" \
            && usermod "$flag" 100000-165535 dockremap >/dev/null 2>&1 \
            && grep -q '^dockremap:' "$file"; then
            ok "$file: dockremap range allocated via usermod $flag ($(grep '^dockremap:' "$file" | head -n1))"
            return 0
        fi
        # Tier 3: scan + append at next free slot. Find the highest
        # used end-of-range across the file; start ours one above
        # 100000 OR one above the highest in use, whichever is bigger.
        local max_end
        max_end=$(awk -F: 'NF>=3 { e = $2 + $3; if (e > m) m = e } END { print m+0 }' "$file")
        local start=$(( max_end < 100000 ? 100000 : max_end ))
        printf 'dockremap:%d:65536\n' "$start" >> "$file"
        ok "$file: appended dockremap:$start:65536 (scanned-fallback; max prior end was $max_end)"
    }
    ensure_dockremap_subid /etc/subuid subuid --add-subuids
    ensure_dockremap_subid /etc/subgid subgid --add-subgids

    # ----- F3-pre Check 3. Graphroot accessibility for dockremap UID
    # Activating userns-remap fails at restart if dockerd's data-root
    # path isn't traversable by the dockremap UID. Common cause:
    # operator set data-root to a path under /home/<user>/, where
    # /home/<user>/ is mode 0750 and excludes UID dockremap_start
    # from traversal. Walk the path and report unwalkable components
    # BEFORE we touch daemon.json, so the operator gets an actionable
    # diagnostic instead of "docker.service failed to start".
    DATA_ROOT=$(jq -r '."data-root" // "/var/lib/docker"' "$DAEMON_JSON" 2>/dev/null \
        || echo /var/lib/docker)
    SUBUID_START=$(awk -F: '$1=="dockremap"{print $2; exit}' /etc/subuid)
    unwalkable=()
    walk="$DATA_ROOT"
    while [[ "$walk" != "/" && -n "$walk" ]]; do
        if [[ -d "$walk" ]]; then
            walk_perms=$(stat -c '%a' "$walk")
            walk_other_digit=${walk_perms: -1}
            if (( (walk_other_digit & 1) == 0 )); then
                unwalkable+=("$walk (mode $walk_perms — needs o+x)")
            fi
        fi
        walk=$(dirname "$walk")
    done
    if (( ${#unwalkable[@]} > 0 )); then
        fail "userns-remap=default cannot activate: dockremap UID $SUBUID_START"
        fail "  cannot traverse to data-root '$DATA_ROOT'."
        fail ""
        fail "  Path components missing o+x:"
        for p in "${unwalkable[@]}"; do
            fail "    $p"
        done
        fail ""
        if [[ "$DATA_ROOT" == /home/* ]]; then
            fail "  Recommended fix (production posture): bind-mount /var/lib/docker"
            fail "  to your storage location so the standard data-root path is used:"
            fail "    sudo systemctl stop docker"
            fail "    sudo install -d -m 0711 -o root -g root /var/lib/docker"
            fail "    sudo rsync -aHAX $DATA_ROOT/ /var/lib/docker/   # if there's existing state"
            fail "    sudo mount --bind $DATA_ROOT /var/lib/docker"
            fail "    echo \"$DATA_ROOT /var/lib/docker none bind 0 0\" | sudo tee -a /etc/fstab"
            fail "    sudo jq 'del(.\"data-root\")' $DAEMON_JSON | sudo tee $DAEMON_JSON.new >/dev/null"
            fail "    sudo mv $DAEMON_JSON.new $DAEMON_JSON"
            fail "  Then re-run setup-host.sh."
            fail ""
            fail "  Alternative for dev/test only: chmod o+x on each path component above"
            fail "  (loosens home-dir traversal — not recommended for production)."
        else
            fail "  Recommended fix: chmod 0711 on the unwalkable path components."
            fail "  Each step adds traverse-only permission for 'others' without"
            fail "  exposing directory contents."
        fi
        fail ""
        fail "  Or: re-run setup-host.sh with --no-userns-remap to skip this layer"
        fail "  entirely. See docs/DEPLOY.md 'Choosing the userns-remap posture' for"
        fail "  the security trade-off."
        exit 1
    fi
    # Also detect + warn about stale <uid>.<gid> subdirs from
    # earlier failed attempts. These can prevent docker from
    # starting cleanly even after the perm issue is fixed.
    stale="$DATA_ROOT/$SUBUID_START.$SUBUID_START"
    if [[ -d "$stale" ]]; then
        note "stale userns subdir from previous attempt: $stale"
        note "  ownership: $(stat -c '%U:%G mode=%a' "$stale" 2>/dev/null)"
        note "  if docker still won't start: sudo rm -rf '$stale' (then retry)"
    fi

    # Warn loudly if the host has existing non-userns Docker state
    # that's about to become orphaned. Activating userns-remap=default
    # makes Docker create a separate /var/lib/docker/<dockremap-uid>.<gid>/
    # state directory and STOP using the existing /var/lib/docker/
    # contents. Old containers, images, and volumes don't disappear,
    # but they become inaccessible via the new daemon. This is
    # silent on the operator-side without this warning.
    if [[ -f "$DAEMON_JSON" ]] && grep -q '"userns-remap"' "$DAEMON_JSON" 2>/dev/null; then
        : # already userns-remapped; not a fresh activation.
    elif command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
        existing_containers=$(docker ps -aq 2>/dev/null | wc -l)
        existing_images=$(docker images -q 2>/dev/null | wc -l)
        if (( existing_containers > 0 || existing_images > 0 )); then
            note "==============================================================="
            note "WARNING: enabling userns-remap will orphan existing Docker state"
            note "  containers: $existing_containers   images: $existing_images"
            note "  Once userns-remap=default is active, Docker uses a separate"
            note "  state dir under /var/lib/docker/<dockremap-uid>.<gid>/. Your"
            note "  existing containers + images stay on disk but become"
            note "  inaccessible via the new daemon."
            note ""
            note "  If those workloads matter, abort now (Ctrl-C) and either:"
            note "    - export them first (\`docker save\` / \`docker commit\`)"
            note "    - or skip userns-remap by editing $DAEMON_JSON manually"
            note "      AFTER this script runs (the merge is idempotent)."
            note "==============================================================="
            sleep 3   # brief pause so the operator notices in interactive runs
        fi
    fi
    fi   # end of `if (( ! NO_USERNS_REMAP ))`

    # JQ_FILTER varies by --no-userns-remap. With it, we still ensure
    # the runsc + runsc-kvm runtimes are registered (they're needed
    # for the sandbox per-session containers) but DON'T add the
    # userns-remap key. With --remove-userns-remap, also strip the
    # key from any existing daemon.json.
    if (( REMOVE_USERNS_REMAP )); then
        JQ_FILTER='
            .runtimes //= {}
            | .runtimes.runsc //= {"path":"/usr/bin/runsc"}
            | .runtimes."runsc-kvm" //= {"path":"/usr/bin/runsc","runtimeArgs":["--platform=kvm"]}
            | del(.["userns-remap"])
        '
    elif (( NO_USERNS_REMAP )); then
        JQ_FILTER='
            .runtimes //= {}
            | .runtimes.runsc //= {"path":"/usr/bin/runsc"}
            | .runtimes."runsc-kvm" //= {"path":"/usr/bin/runsc","runtimeArgs":["--platform=kvm"]}
        '
    else
        JQ_FILTER='
            .runtimes //= {}
            | .runtimes.runsc //= {"path":"/usr/bin/runsc"}
            | .runtimes."runsc-kvm" //= {"path":"/usr/bin/runsc","runtimeArgs":["--platform=kvm"]}
            | .["userns-remap"] //= "default"
        '
    fi

    # Helper: try to restart docker; on failure, restore from
    # backup (if provided), retry, dump journalctl, and exit. Keeps
    # the operator's machine reachable instead of leaving Docker
    # broken with no docker.sock for the rest of the script.
    restart_docker_safely() {
        local backup_path="${1:-}"
        if systemctl restart docker; then
            return 0
        fi
        fail "systemctl restart docker FAILED with new daemon.json"
        if [[ -n "$backup_path" && -f "$backup_path" ]]; then
            fail "rolling back $DAEMON_JSON from $backup_path"
            cp -p "$backup_path" "$DAEMON_JSON"
        else
            fail "no prior daemon.json — removing the version we just wrote"
            rm -f "$DAEMON_JSON"
        fi
        if systemctl restart docker; then
            fail "rollback succeeded; docker is running with previous config"
        else
            fail "rollback ALSO failed; docker remains broken"
        fi
        fail "last 30 lines of 'journalctl -u docker':"
        journalctl -u docker --since "1 minute ago" --no-pager 2>/dev/null \
            | tail -30 >&2 || true
        exit 1
    }

    if [[ ! -f "$DAEMON_JSON" ]]; then
        if (( CHECK_ONLY )); then
            note "would write $DAEMON_JSON with runsc + userns-remap=default"
        else
            echo '{}' | jq "$JQ_FILTER" > "$DAEMON_JSON"
            restart_docker_safely ""
            ok "$DAEMON_JSON written; docker restarted"
        fi
    elif ! jq -e . "$DAEMON_JSON" >/dev/null 2>&1; then
        fail "$DAEMON_JSON exists but is not valid JSON; refusing to modify"
        fail "fix or remove it manually, then re-run setup-host.sh"
        exit 1
    else
        existing_canon=$(jq -S . "$DAEMON_JSON")
        merged=$(jq "$JQ_FILTER" "$DAEMON_JSON")
        merged_canon=$(echo "$merged" | jq -S .)
        if [[ "$existing_canon" == "$merged_canon" ]]; then
            skip "$DAEMON_JSON already has runsc + userns-remap"
        else
            if (( CHECK_ONLY )); then
                note "would merge runsc + userns-remap into $DAEMON_JSON (preserving operator settings)"
            else
                # Backup before write so we can roll back on a failed
                # docker restart (and so the operator has a manual
                # rollback path even if the script itself succeeds).
                backup="$DAEMON_JSON.bak.$(date +%s)"
                cp -p "$DAEMON_JSON" "$backup"
                printf '%s\n' "$merged" > "$DAEMON_JSON"
                restart_docker_safely "$backup"
                ok "$DAEMON_JSON merged (backup: $backup); docker restarted"
            fi
        fi
    fi
    # Ensure /etc/projects exists so compose's :rw bind has a target.
    if [[ ! -f /etc/projects ]]; then
        run touch /etc/projects
        run chmod 0644 /etc/projects
        ok "/etc/projects created (XFS prjquota registry)"
    fi
    echo

    # ---- F4. Loopback XFS volume area (optional) -----------------
    if (( WITH_XFS )); then
        echo "F4) Loopback XFS at $XFS_MOUNT (--with-xfs-quota)"
        if mountpoint -q "$XFS_MOUNT"; then
            skip "$XFS_MOUNT already mounted"
        else
            run install -d -m 0755 "$XFS_MOUNT"
            if [[ ! -f $XFS_IMG ]]; then
                run truncate -s "${XFS_SIZE_GB}G" "$XFS_IMG"
                run mkfs.xfs -q "$XFS_IMG"
                ok "created $XFS_IMG ($XFS_SIZE_GB GiB)"
            fi
            # Persist the mount via /etc/fstab so reboots restore it.
            # Two-axis check: skip if our $XFS_IMG is already in fstab
            # (idempotent re-run), but FAIL if a *different* fs is
            # already mounted at $XFS_MOUNT — appending in that case
            # would create duplicate fstab entries for the same target
            # and confuse mount(8) at boot.
            if grep -q "$XFS_IMG" /etc/fstab 2>/dev/null; then
                skip "$XFS_IMG already in /etc/fstab"
            elif awk -v m="$XFS_MOUNT" '
                    /^[[:space:]]*#/ { next }
                    NF >= 2 && $2 == m { found=1; exit }
                    END { exit !found }
                ' /etc/fstab 2>/dev/null; then
                fail "$XFS_MOUNT already has a fstab entry from a different source"
                fail "either remove the existing /etc/fstab line or pick a different SANDBOX_VOLUME_BASE"
                fail "(don't want to silently shadow your mount with our loopback XFS)"
                exit 1
            elif (( CHECK_ONLY )); then
                note "would append fstab entry for $XFS_IMG → $XFS_MOUNT"
            else
                printf '%s %s xfs loop,prjquota,defaults 0 2\n' \
                    "$XFS_IMG" "$XFS_MOUNT" >> /etc/fstab
                ok "fstab appended: $XFS_IMG → $XFS_MOUNT"
            fi
            run mount "$XFS_MOUNT"
            ok "mounted $XFS_IMG on $XFS_MOUNT"
        fi
        echo
    fi

    # ---- F5. iptables (DOCKER-USER chain rules) ------------------
    echo "F5) iptables (DOCKER-USER)"
    if iptables -L DOCKER-USER -n 2>/dev/null | grep -q sandbox; then
        skip "DOCKER-USER chain already has sandbox rules"
    else
        if [[ -x "$SCRIPT_DIR/iptables-setup.sh" ]]; then
            run "$SCRIPT_DIR/iptables-setup.sh"
            ok "iptables rules applied"
        else
            fail "$SCRIPT_DIR/iptables-setup.sh not found / not executable"
            exit 1
        fi
    fi
    echo

    # ---- F6. sandbox_egress network ------------------------------
    echo "F6) sandbox_egress network ($SUBNET)"
    if docker network inspect sandbox_egress >/dev/null 2>&1; then
        existing_subnet=$(docker network inspect sandbox_egress \
            -f '{{(index .IPAM.Config 0).Subnet}}' 2>/dev/null || echo "?")
        if [[ "$existing_subnet" == "$SUBNET" ]]; then
            skip "sandbox_egress network already exists ($SUBNET)"
        else
            fail "sandbox_egress network exists with subnet $existing_subnet; want $SUBNET"
            note "to fix: docker network rm sandbox_egress (after stopping the stack)"
            exit 1
        fi
    else
        run docker network create \
            --driver bridge \
            --subnet "$SUBNET" \
            --label sandbox.managed=true \
            sandbox_egress
        ok "sandbox_egress network created ($SUBNET)"
    fi
    echo
fi

# ---------------------------------------------------------------------
# 0. Ensure the `sandbox` system user/group exist.
# Every section below references `sandbox:sandbox` ownership (audit
# log, env-file perms, sudoers grant). Without this section, fresh
# Ubuntu hosts following docs/DEPLOY.md hit `install: invalid user
# 'sandbox'` and `set -e` aborts the run mid-script. Idempotent.
# ---------------------------------------------------------------------
echo "0) sandbox system user"
if getent passwd sandbox >/dev/null 2>&1; then
    skip "sandbox user already exists"
else
    run useradd -r -s /sbin/nologin sandbox
    ok "sandbox system user created"
fi
# Compose-path control plane reads /var/run/docker.sock; the systemd
# path's hardened unit (deploy/sandbox-api.service) runs as root but
# the operator user that invokes `docker compose` benefits from
# being in the docker group too. F1 (--full) creates the docker
# group; the systemd path's manual Docker install (SETUP.md §1) does
# the same. If neither has happened yet (e.g. --check before any
# Docker install), emit a note and continue.
if ! getent group docker >/dev/null 2>&1; then
    note "docker group not present yet — install Docker first, then re-run"
elif id -nG sandbox 2>/dev/null | tr ' ' '\n' | grep -qx docker; then
    skip "sandbox already in docker group"
else
    run usermod -aG docker sandbox
    ok "sandbox added to docker group"
fi
echo

# ---------------------------------------------------------------------
# 1. Compute SANDBOX_BIND_VOLUME_UID
# ---------------------------------------------------------------------
# WITH userns-remap=default: container UID 10001 (agent) maps via the
# dockremap subuid range to host UID dockremap_start+10001.
# WITHOUT userns-remap: container UID 10001 IS host UID 10001 directly
# (no namespace translation).
echo "1) bind-volume UID (SPEC-401)"
if (( NO_USERNS_REMAP || REMOVE_USERNS_REMAP )); then
    BIND_UID=10001
    note "userns-remap not in use → container UID 10001 == host UID 10001 (BIND_UID=$BIND_UID)"
elif ! getent passwd dockremap >/dev/null 2>&1; then
    skip "dockremap user missing — userns-remap not configured?"
    skip "follow docs/SETUP.md §3 to set up userns-remap=default"
    BIND_UID=""
elif [[ ! -f /etc/subuid ]]; then
    fail "/etc/subuid not found"
    exit 1
else
    DOCKREMAP_START=$(awk -F: '$1=="dockremap"{print $2}' /etc/subuid | head -n1)
    if [[ -z "$DOCKREMAP_START" ]]; then
        fail "no dockremap entry in /etc/subuid"
        exit 1
    fi
    # Container agent runs as UID 10001 (set in api.docker_client.hardening_flags),
    # so the matching host UID is dockremap_start + 10001. (NOT +10000 — that
    # used to be in this file and silently created an off-by-one where the
    # bind path got chowned to the host UID for container UID 10000, leaving
    # the agent unable to mkdir under /workspace.)
    BIND_UID=$((DOCKREMAP_START + 10001))
    note "dockremap subuid range starts at $DOCKREMAP_START → container UID 10001 → host UID $BIND_UID"
fi
# Common write-to-env-file path. Wrapped in a brace so we can dedent
# after the if/elif/else above.
if [[ -n "$BIND_UID" ]]; then

    if [[ -f "$ENV_FILE" ]] && grep -q '^SANDBOX_BIND_VOLUME_UID=' "$ENV_FILE"; then
        # Extract digits only — strips inline comments / trailing
        # whitespace so the comparison is value-only. `split($2, a,
        # /[^0-9]/)` puts the leading digits in a[1].
        existing=$(awk -F= '
            $1=="SANDBOX_BIND_VOLUME_UID"{ split($2, a, /[^0-9]/); print a[1]; exit }
        ' "$ENV_FILE")
        if [[ "$existing" == "$BIND_UID" ]]; then
            skip "SANDBOX_BIND_VOLUME_UID already set to $BIND_UID"
        else
            note "updating SANDBOX_BIND_VOLUME_UID: $existing → $BIND_UID"
            # Match digits ONLY in the right-hand side; leave inline
            # comments / extra whitespace intact. Pre-v0.2.4 used `.*`
            # which greedily ate trailing comments.
            run sed -i -E "s|^(SANDBOX_BIND_VOLUME_UID=)[0-9]*|\1$BIND_UID|" "$ENV_FILE"
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
    echo "Done."
    if (( FULL )); then
        # The user that invoked sudo, NOT root. We want the
        # `usermod -aG sandbox` hint to use their actual login.
        target_user="${SUDO_USER:-$USER}"
        # Note: the env-file copy + sudoedit are now a PRE-requisite
        # to running this script (see docs/DEPLOY.md Quick-start).
        # Without /etc/sandbox/env in place beforehand, sections 1
        # and 4 above will have printed SKIP — re-create the env
        # file and re-run the script to pick up auto-derivation +
        # perm enforcement.
        cat <<NEXT

Next steps:
  # Add yourself to the sandbox group so docker compose can read
  # /etc/sandbox/env without sudo on every invocation.
  sudo usermod -aG sandbox $target_user
  newgrp sandbox        # or log out + back in for the group to apply

  docker compose --env-file /etc/sandbox/env up -d
  TOKEN=\$(grep API_TOKEN /etc/sandbox/env | cut -d= -f2)
  curl -sS -H "Authorization: Bearer \$TOKEN" http://127.0.0.1:8000/healthz

(Forgot to create /etc/sandbox/env before running this script?
 sudo install -d -m 0755 /etc/sandbox
 sudo cp deploy/.env.compose.example /etc/sandbox/env
 sudoedit /etc/sandbox/env       # set SANDBOX_API_TOKEN + _PEPPER
 sudo $0 --full $([[ -f /var/lib/sandbox-fs.img ]] && echo --with-xfs-quota))
NEXT
    else
        echo "Next: sudo systemctl restart sandbox-api"
    fi
fi
