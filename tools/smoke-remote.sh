#!/usr/bin/env bash
# tools/smoke-remote.sh — end-to-end smoke test against a remote Linux host.
#
# What it does:
#   1. Reads .env.
#   2. Generates a fresh API token (per-run; never persisted).
#   3. SSHes to $SSH_HOST and launches an ephemeral uvicorn (DEV_MODE=1,
#      DB and audit log under /tmp, never touches /var/lib/sandbox state).
#   4. Opens an SSH tunnel from the client.
#   5. Runs an asserted curl flow: create, get, exec, file roundtrip,
#      multi-turn persistence, forbidden-env negative test, destroy.
#   6. Tears everything down on exit (success, failure, or Ctrl-C):
#      remote uvicorn killed, ephemeral files removed, tunnel closed.
#
# Requires on the client: bash 4+, ssh, curl, jq, openssl, base64.
# Requires on the remote: bash, the repo cloned at $REMOTE_REPO_PATH
# with `uv sync` already run there.

set -euo pipefail

# ----- locate repo + config -----

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
ENV_FILE="${ENV_FILE:-$REPO_ROOT/.env}"

if [[ ! -f "$ENV_FILE" ]]; then
    echo "ERROR: $ENV_FILE not found." >&2
    echo "       Copy .env.example to .env and fill in your values." >&2
    exit 1
fi

# Auto-export every var defined while sourcing.
set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

: "${SANDBOX_HOST:?SANDBOX_HOST not set in $ENV_FILE}"
: "${SSH_HOST:=$SANDBOX_HOST}"
: "${SSH_USER:?SSH_USER not set in $ENV_FILE}"
: "${SSH_KEY:=$HOME/.ssh/id_ed25519}"
: "${SANDBOX_PORT:=8000}"
: "${TUNNEL_LOCAL_PORT:=8000}"
: "${REMOTE_REPO_PATH:=/opt/sandbox}"

# Expand a leading ~/ in SSH_KEY (POSIX shells don't auto-expand inside vars).
SSH_KEY="${SSH_KEY/#\~/$HOME}"

# ----- ephemeral identifiers (per-run) -----

RUN_ID="$$"
TOKEN="$(openssl rand -hex 32)"
REMOTE_PORT="$SANDBOX_PORT"
REMOTE_DB="/tmp/sandbox-smoke-${RUN_ID}.db"
REMOTE_AUDIT="/tmp/sandbox-smoke-${RUN_ID}.log"
REMOTE_LOG="/tmp/sandbox-smoke-${RUN_ID}.uvicorn.log"
REMOTE_PID_FILE="/tmp/sandbox-smoke-${RUN_ID}.pid"

BASE="http://127.0.0.1:${TUNNEL_LOCAL_PORT}"
TUNNEL_PID=""
SID=""

ssh_opts=(
    -i "$SSH_KEY"
    -o BatchMode=yes
    -o StrictHostKeyChecking=accept-new
    -o ServerAliveInterval=30
)

# ----- helpers -----

ssh_remote() { ssh "${ssh_opts[@]}" "$SSH_USER@$SSH_HOST" "$@"; }

api() {
    curl -sS \
        -H "Authorization: Bearer $TOKEN" \
        -H "Content-Type: application/json" \
        "$@"
}

api_status() {
    curl -sS -o /dev/null -w "%{http_code}" \
        -H "Authorization: Bearer $TOKEN" \
        -H "Content-Type: application/json" \
        "$@"
}

ok()   { printf "  OK   %s\n" "$*"; }
fail() { printf "  FAIL %s\n" "$*" >&2; exit 1; }

# ----- cleanup (trap-driven; runs on success / failure / Ctrl-C) -----

cleanup() {
    local rc=$?
    set +e
    echo "==> cleanup"

    # Dump remote log FIRST while it still exists — this is the
    # diagnostic we need most when uvicorn failed to start.
    if (( rc != 0 )); then
        echo "==> exit $rc; remote uvicorn log tail (best effort):"
        ssh_remote "tail -40 $REMOTE_LOG 2>/dev/null || echo '(log not found at $REMOTE_LOG)'" || true
    fi

    # Best-effort delete of any session created during the run.
    if [[ -n "$SID" ]]; then
        api -X DELETE "$BASE/v1/sessions/$SID" >/dev/null 2>&1
    fi
    # Close the local tunnel.
    if [[ -n "$TUNNEL_PID" ]]; then
        kill "$TUNNEL_PID" 2>/dev/null
        wait "$TUNNEL_PID" 2>/dev/null
    fi
    # Kill the remote uvicorn and remove ephemeral files.
    ssh_remote bash -s <<EOF 2>/dev/null
if [[ -f $REMOTE_PID_FILE ]]; then
    kill \$(cat $REMOTE_PID_FILE) 2>/dev/null
fi
rm -f $REMOTE_PID_FILE $REMOTE_DB $REMOTE_AUDIT $REMOTE_LOG
EOF
    exit "$rc"
}
trap cleanup EXIT INT TERM

# ----- 1. start uvicorn on remote (ephemeral) -----

echo "==> launching uvicorn on $SSH_USER@$SSH_HOST (ephemeral state)"
echo "    REMOTE_REPO_PATH=$REMOTE_REPO_PATH  REMOTE_PORT=$REMOTE_PORT"

# Pre-flight: make sure the venv exists where we expect.
if ! ssh_remote "test -x $REMOTE_REPO_PATH/.venv/bin/uvicorn"; then
    echo "ERROR: $REMOTE_REPO_PATH/.venv/bin/uvicorn not found on the remote." >&2
    echo "       SSH in and run 'cd $REMOTE_REPO_PATH && uv sync' first." >&2
    exit 1
fi

ssh_remote bash -s <<EOF
set -e
cd $REMOTE_REPO_PATH
nohup env \\
    SANDBOX_DEV_MODE=1 \\
    SANDBOX_API_TOKEN='$TOKEN' \\
    SANDBOX_DB_PATH='$REMOTE_DB' \\
    SANDBOX_AUDIT_LOG_PATH='$REMOTE_AUDIT' \\
    SANDBOX_BIND_HOST=127.0.0.1 \\
    SANDBOX_BIND_PORT=$REMOTE_PORT \\
    .venv/bin/uvicorn api.server:app \\
        --host 127.0.0.1 --port $REMOTE_PORT \\
    > $REMOTE_LOG 2>&1 &
echo \$! > $REMOTE_PID_FILE
disown
EOF

# Verify uvicorn is actually still running 1 second later. Catches
# fast crashes (missing module, port already in use, etc.) instead of
# blocking until /healthz times out.
sleep 1
if ! ssh_remote "kill -0 \$(cat $REMOTE_PID_FILE 2>/dev/null) 2>/dev/null"; then
    echo "ERROR: remote uvicorn died immediately." >&2
    exit 1
fi

# ----- 2. open SSH tunnel -----

echo "==> tunneling 127.0.0.1:$TUNNEL_LOCAL_PORT -> remote 127.0.0.1:$REMOTE_PORT"

# Refuse to start if the local port is already bound — almost always a
# leftover tunnel or another uvicorn from a previous session. Without
# this check, ssh -L silently fails to bind, exits, and curl hits
# whatever else owns the port.
if command -v lsof >/dev/null && lsof -nP -iTCP:"$TUNNEL_LOCAL_PORT" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "ERROR: local port $TUNNEL_LOCAL_PORT is already in use." >&2
    echo "       Investigate with: lsof -nP -iTCP:$TUNNEL_LOCAL_PORT -sTCP:LISTEN" >&2
    echo "       Or set TUNNEL_LOCAL_PORT to a free port in .env." >&2
    exit 1
fi

ssh "${ssh_opts[@]}" \
    -o ExitOnForwardFailure=yes \
    -N \
    -L "$TUNNEL_LOCAL_PORT:127.0.0.1:$REMOTE_PORT" \
    "$SSH_USER@$SSH_HOST" &
TUNNEL_PID=$!

# Verify the ssh process is still alive — if it bound failed, ExitOn-
# ForwardFailure=yes makes it exit, but the `&` swallows that.
sleep 1
if ! kill -0 "$TUNNEL_PID" 2>/dev/null; then
    echo "ERROR: SSH tunnel died immediately. Most common cause: port" >&2
    echo "       $TUNNEL_LOCAL_PORT was already bound at the moment ssh" >&2
    echo "       tried to forward, or sshd on the remote denies forwarding." >&2
    TUNNEL_PID=""
    exit 1
fi

# ----- 3. wait for /healthz -----

echo "==> waiting for /healthz"
for _ in {1..40}; do
    if curl -sf -o /dev/null "$BASE/healthz"; then break; fi
    sleep 0.5
done
if ! curl -sf -o /dev/null "$BASE/healthz"; then
    echo "ERROR: API never became reachable. See remote uvicorn log on cleanup." >&2
    exit 1
fi
ok "healthz"

# ----- 4. tests -----

echo "==> tests"

# 4.1 create
SID=$(api -d '{}' "$BASE/v1/sessions" | jq -r .session_id)
[[ -n "$SID" && "$SID" != "null" ]] || fail "create returned no session_id"
ok "create $SID"

# 4.2 get
STATUS=$(api "$BASE/v1/sessions/$SID" | jq -r .status)
[[ "$STATUS" == "RUNNING" ]] || fail "get: expected RUNNING, got '$STATUS'"
ok "get RUNNING"

# 4.3 exec
OUT=$(api -d '{"argv":["echo","hello"]}' "$BASE/v1/sessions/$SID/exec" | jq -r .stdout)
[[ "$OUT" == "hello" ]] || fail "exec: expected 'hello', got '$OUT'"
ok "exec echo"

# 4.4 file write + read roundtrip — random binary, byte-for-byte compared.
# Don't capture curl output via $(...) — bash command substitution
# strips trailing newlines and would corrupt binary content.
LOCAL_PAYLOAD=$(mktemp -t sandbox-smoke.XXXXXX)
LOCAL_ROUND=$(mktemp -t sandbox-smoke.XXXXXX)
head -c 1024 /dev/urandom > "$LOCAL_PAYLOAD"
B64=$(base64 < "$LOCAL_PAYLOAD" | tr -d '\n')
api -d "{\"path\":\"note.bin\",\"content_b64\":\"$B64\"}" \
    "$BASE/v1/sessions/$SID/files" >/dev/null
curl -sSf -H "Authorization: Bearer $TOKEN" \
    "$BASE/v1/sessions/$SID/files/note.bin" \
    --output "$LOCAL_ROUND"
if cmp -s "$LOCAL_PAYLOAD" "$LOCAL_ROUND"; then
    ok "file roundtrip (1 KiB random binary)"
else
    rm -f "$LOCAL_PAYLOAD" "$LOCAL_ROUND"
    fail "file roundtrip: bytes differ"
fi
rm -f "$LOCAL_PAYLOAD" "$LOCAL_ROUND"

# 4.5 multi-turn (filesystem persists across stop+auto-resume)
api -d '{"argv":["bash","-c","echo persist > /workspace/m"]}' \
    "$BASE/v1/sessions/$SID/exec" >/dev/null
api -X POST "$BASE/v1/sessions/$SID/stop" >/dev/null
M=$(api -d '{"argv":["cat","/workspace/m"]}' \
    "$BASE/v1/sessions/$SID/exec" | jq -r .stdout)
[[ "$M" == "persist" ]] || fail "multi-turn: got '$M'"
ok "multi-turn (auto-resume + filesystem)"

# 4.6 forbidden env key (negative)
CODE=$(api_status -X POST \
    -d '{"argv":["true"],"env":{"HTTP_PROXY":"x"}}' \
    "$BASE/v1/sessions/$SID/exec")
[[ "$CODE" == "400" ]] || fail "forbidden env: expected 400, got $CODE"
ok "forbidden env key rejected (400)"

# 4.7 destroy
CODE=$(api_status -X DELETE "$BASE/v1/sessions/$SID")
[[ "$CODE" == "204" ]] || fail "destroy: expected 204, got $CODE"
ok "destroy"
SID=""  # already gone — cleanup shouldn't double-delete

echo "==> all checks passed"
