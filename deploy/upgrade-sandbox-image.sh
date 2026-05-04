#!/usr/bin/env bash
# deploy/upgrade-sandbox-image.sh — blue/green upgrade of sandbox-runtime.
#
# Per ARCH §8 the upgrade story is "pull the new tag, point
# CreateSession at it; existing sessions keep their old image until
# destroyed." This script automates that:
#
#   1. Pull the new tag and verify it boots cleanly (smoke test).
#   2. Atomically swap SANDBOX_SANDBOX_IMAGE in /etc/sandbox/env to
#      point at the new tag.
#   3. Restart sandbox-api so the next CreateSession picks it up.
#
# Existing sessions continue running on their old image; they're
# replaced naturally as sessions get destroyed and recreated. No
# in-place upgrade.
#
# Usage:
#   sudo deploy/upgrade-sandbox-image.sh sandbox-runtime:v2026-05-04
#
# Pre-conditions:
#   - sandbox-api.service is installed and managed by systemd.
#   - The new image already exists in a registry the local Docker
#     daemon can pull from (or has been built locally with the new tag).
#   - Caller is root (we touch /etc/sandbox/env and call systemctl).

set -euo pipefail

if [[ $# -ne 1 ]]; then
    echo "usage: $0 <new-image-tag>" >&2
    echo "example: $0 sandbox-runtime:v2026-05-04" >&2
    exit 2
fi

NEW_IMAGE="$1"
ENV_FILE="${SANDBOX_ENV:-/etc/sandbox/env}"
SERVICE="${SANDBOX_SERVICE:-sandbox-api.service}"

if [[ $EUID -ne 0 ]]; then
    echo "ERROR: run as root (sudo)." >&2
    exit 1
fi

if [[ ! -f "$ENV_FILE" ]]; then
    echo "ERROR: $ENV_FILE not found" >&2
    exit 1
fi

CURRENT="$(grep -E '^SANDBOX_SANDBOX_IMAGE=' "$ENV_FILE" | cut -d= -f2- | tr -d '"' || true)"
echo "==> current image: ${CURRENT:-unset}"
echo "==> target  image: $NEW_IMAGE"

if [[ "$CURRENT" == "$NEW_IMAGE" ]]; then
    echo "==> already on $NEW_IMAGE; nothing to do."
    exit 0
fi

# 1. Pull. If the tag is a local-only build, `pull` may fail — fall
# back to `image inspect` to confirm it exists locally.
echo "==> pulling $NEW_IMAGE"
if ! docker pull "$NEW_IMAGE" 2>/dev/null; then
    if ! docker image inspect "$NEW_IMAGE" >/dev/null 2>&1; then
        echo "ERROR: $NEW_IMAGE not in any registry and not built locally." >&2
        exit 1
    fi
    echo "    (using local build; pull skipped)"
fi

# 2. Smoke test: spin a transient container with the same hardening
# subset we use in production. If it doesn't start cleanly within
# 10 s, abort the upgrade.
echo "==> smoke-testing $NEW_IMAGE"
test_name="sandbox-upgrade-smoketest-$$"
if ! docker run -d --rm --name "$test_name" \
        --read-only \
        --tmpfs /tmp:size=64m,mode=1777,noexec,nosuid,nodev \
        --user 10001:10001 \
        --cap-drop ALL \
        --security-opt no-new-privileges:true \
        --entrypoint /usr/bin/sleep \
        "$NEW_IMAGE" 5 >/dev/null; then
    echo "ERROR: smoke-test container failed to start." >&2
    exit 1
fi
sleep 1
if ! docker ps --filter "name=$test_name" --format '{{.Names}}' | grep -q "$test_name"; then
    echo "ERROR: smoke-test container died immediately." >&2
    docker logs "$test_name" 2>&1 | tail -20 || true
    exit 1
fi
docker stop "$test_name" >/dev/null 2>&1 || true
echo "    smoke test passed"

# 3. Atomic env update. Backup, edit, replace.
backup_env="${ENV_FILE}.bak.$(date -u +%Y%m%d-%H%M%S)"
echo "==> backing up $ENV_FILE → $backup_env"
cp -a "$ENV_FILE" "$backup_env"

tmp_env=$(mktemp)
trap 'rm -f "$tmp_env"' EXIT
if grep -qE '^SANDBOX_SANDBOX_IMAGE=' "$ENV_FILE"; then
    # Replace the line in place (preserves order, comments).
    sed -E "s|^SANDBOX_SANDBOX_IMAGE=.*$|SANDBOX_SANDBOX_IMAGE=${NEW_IMAGE}|" \
        "$ENV_FILE" > "$tmp_env"
else
    # Append if missing.
    cp "$ENV_FILE" "$tmp_env"
    echo "SANDBOX_SANDBOX_IMAGE=${NEW_IMAGE}" >> "$tmp_env"
fi
mv -f "$tmp_env" "$ENV_FILE"
chmod 0640 "$ENV_FILE" || true
echo "==> $ENV_FILE updated"

# 4. Restart the service so new sessions get the new image.
echo "==> restarting $SERVICE"
if systemctl restart "$SERVICE"; then
    sleep 1
    if ! systemctl is-active --quiet "$SERVICE"; then
        echo "ERROR: $SERVICE failed to come back; rolling back env" >&2
        cp -a "$backup_env" "$ENV_FILE"
        systemctl restart "$SERVICE" || true
        exit 1
    fi
fi

echo
echo "==> upgrade complete. New sessions use $NEW_IMAGE."
echo "    Existing sessions keep their old image until destroyed."
echo "    Rollback (if needed within the next few minutes):"
echo "        sudo cp -a $backup_env $ENV_FILE && sudo systemctl restart $SERVICE"
