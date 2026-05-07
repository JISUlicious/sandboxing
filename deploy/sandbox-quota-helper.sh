#!/usr/bin/env bash
# /usr/local/bin/sandbox-quota-helper — privileged XFS project quota
# orchestration for sandbox sessions (SPEC-302).
#
# Runs as root via a single sudoers entry — collapses the previous
# multiple `sudo xfs_quota / sudo sed / sudo tee / sudo touch` grants
# (which accepted arbitrary path args) into one wrapper. The control
# plane shells out to this script via the slim `xfs-quota-{setup,
# teardown}.sh` wrappers.
#
# Sudoers: /etc/sudoers.d/sandbox-quota-helper
#     sandbox ALL=(root) NOPASSWD: /usr/local/bin/sandbox-quota-helper *
#
# Invocation:
#   sudo sandbox-quota-helper setup     # env: SESSION_ID, VOLUME_PATH,
#                                       #      VOLUME_BASE, WORKSPACE_MIB
#   sudo sandbox-quota-helper teardown  # env: SESSION_ID, VOLUME_PATH,
#                                       #      VOLUME_BASE
#
# Both subcommands validate that VOLUME_PATH is a child of VOLUME_BASE
# so a compromised caller can't aim the script at /etc, /home, etc.

set -euo pipefail

PROJECTS_FILE=/etc/projects

usage() {
    cat >&2 <<'EOF'
usage:
  sandbox-quota-helper setup
  sandbox-quota-helper teardown
required env: SESSION_ID, VOLUME_PATH, VOLUME_BASE
setup also needs: WORKSPACE_MIB
EOF
    exit 2
}

# ---------- input validation ----------
require_inputs() {
    : "${SESSION_ID:?SESSION_ID required}"
    : "${VOLUME_PATH:?VOLUME_PATH required}"
    : "${VOLUME_BASE:?VOLUME_BASE required}"
    # Reject path traversal — VOLUME_PATH must be strictly inside VOLUME_BASE.
    # The trailing-slash form is required so /var/lib/sandbox-volumes-evil
    # doesn't accidentally pass.
    case "$VOLUME_PATH" in
        "$VOLUME_BASE"/*) : ;;
        *)
            echo "ERROR: VOLUME_PATH ($VOLUME_PATH) is not under VOLUME_BASE ($VOLUME_BASE)" >&2
            exit 2
            ;;
    esac
    # Disallow shell metacharacters / .. segments in SESSION_ID — defense
    # in depth against an attacker-controlled label sneaking through.
    case "$SESSION_ID" in
        *[^A-Za-z0-9_-]*|*..*|"")
            echo "ERROR: SESSION_ID has unexpected characters: $SESSION_ID" >&2
            exit 2
            ;;
    esac
}

project_id_for() {
    # Stable, deterministic project ID derived from the ULID. Hash to a
    # 31-bit positive int (XFS allows uint32, but tooling sometimes
    # treats the value as int).
    printf '%s' "$1" | cksum | awk '{print $1 % 2147483647}'
}

rewrite_projects_file() {
    # Rewrite /etc/projects, dropping any stale lines for the given
    # project_id or volume_path, then appending the fresh mapping.
    # Preserves the inode (truncate + write) so bind-mounted single
    # files (Compose path: /etc/projects mounted into control-plane)
    # don't EBUSY on `sed -i`'s rename trick.
    local pid=$1
    local vpath=$2
    local filtered
    filtered=$(grep -vE "^${pid}:|:${vpath}$" "$PROJECTS_FILE" 2>/dev/null || true)
    {
        # Print existing entries (if any) followed by the fresh line.
        # The `[[ -n ... ]]` guards an empty filtered string from
        # injecting an empty line at the top.
        if [[ -n "$filtered" ]]; then
            printf '%s\n' "$filtered"
        fi
        printf '%s:%s\n' "$pid" "$vpath"
    } > "$PROJECTS_FILE"
}

remove_from_projects_file() {
    # Drop any line matching `^<pid>:`. Same inode-preserving truncate
    # + write pattern as rewrite_projects_file.
    local pid=$1
    local filtered
    filtered=$(grep -vE "^${pid}:" "$PROJECTS_FILE" 2>/dev/null || true)
    if [[ -n "$filtered" ]]; then
        printf '%s\n' "$filtered" > "$PROJECTS_FILE"
    else
        : > "$PROJECTS_FILE"
    fi
}

cmd_setup() {
    require_inputs
    : "${WORKSPACE_MIB:?WORKSPACE_MIB required}"

    PROJECT_ID="$(project_id_for "$SESSION_ID")"

    # Ensure /etc/projects exists, then idempotently rewrite the line
    # for this project / path. NOTE: do NOT use `sed -i` — it works
    # by writing a temp file and `rename(2)`-ing it over the original,
    # which fails with EBUSY when /etc/projects is a bind-mounted
    # single file (the Compose path bind-mounts it into the control-
    # plane container). Truncate + write preserves the inode the bind
    # mount is pinned to.
    [[ -f "$PROJECTS_FILE" ]] || : > "$PROJECTS_FILE"
    rewrite_projects_file "$PROJECT_ID" "$VOLUME_PATH"

    # Apply project ID + limit in a single xfs_quota invocation. Two
    # separate `xfs_quota -c` calls have been observed to lose userspace
    # state between them on loopback-mounted XFS.
    xfs_quota -x \
        -c "project -s -p ${VOLUME_PATH} ${PROJECT_ID}" \
        -c "limit -p bhard=${WORKSPACE_MIB}m ${PROJECT_ID}" \
        "$VOLUME_BASE"

    # Verify the limit actually took effect.
    expected_blocks=$((WORKSPACE_MIB * 1024))
    applied_hard=$(
        xfs_quota -x -c "report -p" "$VOLUME_BASE" \
        | grep -E "^#${PROJECT_ID}([[:space:]]|\$)" \
        | awk '{print $4}'
    )
    if [[ -z "${applied_hard:-}" ]] || (( applied_hard < expected_blocks )); then
        echo "ERROR: project ${PROJECT_ID} hard limit is '${applied_hard:-unset}', " \
             "expected ${expected_blocks} blocks (= ${WORKSPACE_MIB} MiB)" >&2
        exit 1
    fi

    # Persist project ID so teardown can find it. Owned by root; teardown
    # runs as root via the same helper, so permissions don't matter.
    echo "$PROJECT_ID" > "$VOLUME_PATH/.project_id"
}

cmd_teardown() {
    require_inputs

    if [[ ! -f "$VOLUME_PATH/.project_id" ]]; then
        # Already torn down (or never set up). Best-effort.
        exit 0
    fi
    PROJECT_ID="$(cat "$VOLUME_PATH/.project_id" 2>/dev/null || true)"
    if [[ -z "$PROJECT_ID" ]]; then
        exit 0
    fi

    # Clear the limit and the project mapping. Failures tolerated so a
    # partially-cleaned project doesn't block destroy.
    xfs_quota -x \
        -c "limit -p bhard=0 ${PROJECT_ID}" \
        -c "project -C -p ${VOLUME_PATH} ${PROJECT_ID}" \
        "$VOLUME_BASE" \
        || true

    # Same bind-mount-friendly rewrite as cmd_setup uses; sed -i
    # would EBUSY on the bind-mounted /etc/projects.
    remove_from_projects_file "$PROJECT_ID" 2>/dev/null || true
    rm -f "$VOLUME_PATH/.project_id"
}

case "${1:-}" in
    setup)    cmd_setup ;;
    teardown) cmd_teardown ;;
    --help|-h|"") usage ;;
    *)        echo "unknown subcommand: $1" >&2; usage ;;
esac
