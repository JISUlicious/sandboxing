#!/usr/bin/env bash
# tools/validate-slices.sh — automated validation of slice 6/7/8 features
# against a remote Linux host.
#
# Sibling of tools/smoke-remote.sh: reuses the same .env, SSH-tunnel
# pattern, and ephemeral uvicorn lifecycle, but focused on the new
# surface that landed in slices 6 and 7 (and the local part of 8).
#
# What it covers:
#   - Slice 6a: startup reconciliation (kill uvicorn mid-state, drop
#     the container directly, restart, verify the row was orphaned to
#     STOPPED).
#   - Slice 6b: per-session resource sampler (waits 12 s for one tick,
#     verifies sandbox_resource_samples_total advanced and at least
#     one session.sample audit line landed).
#   - Slice 7: token rotation grace + two-tenant isolation via the
#     CLI and the rotate endpoint.
#   - Slice 8b: local OpenAPI schema-drift check (no remote needed).
#
# What it DOES NOT cover:
#   - Slice 6c (systemd backup timer) — touches /etc/systemd; manual.
#     See docs/SETUP.md "Backup" section.
#   - Slice 6d (image upgrade) — mutates production env; manual.
#     See docs/SETUP.md "§8 Sandbox image upgrades".
#   - Slice 8a CI workflow — runs in GitHub Actions, not on the box.
#   - Slice 8d Prometheus alerts — no Prometheus to inject into here.
#   - Slice 8e TLS-readiness — exercised only behind a real TLS proxy.
#
# Same caveats as smoke-remote.sh: ephemeral state under /tmp on the
# remote, never touches /var/lib/sandbox or /etc/sandbox/env.

set -euo pipefail

# ----- locate repo + config -----

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
ENV_FILE="${ENV_FILE:-$REPO_ROOT/.env}"

if [[ ! -f "$ENV_FILE" ]]; then
    echo "ERROR: $ENV_FILE not found." >&2
    echo "       Copy .env.example to .env first." >&2
    exit 1
fi

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

SSH_KEY="${SSH_KEY/#\~/$HOME}"

# ----- ephemeral identifiers (shared across the two uvicorn launches
# this script does — DB / audit persist across the kill+relaunch so
# the reconciliation test can observe its own session row). -----

RUN_ID="$$"
TOKEN="$(openssl rand -hex 32)"
PEPPER="$(openssl rand -hex 32)"
REMOTE_PORT="$SANDBOX_PORT"
REMOTE_DB="/tmp/sandbox-validate-${RUN_ID}.db"
REMOTE_AUDIT="/tmp/sandbox-validate-${RUN_ID}.log"
REMOTE_LOG="/tmp/sandbox-validate-${RUN_ID}.uvicorn.log"
REMOTE_PID_FILE="/tmp/sandbox-validate-${RUN_ID}.pid"

BASE="http://127.0.0.1:${TUNNEL_LOCAL_PORT}"
TUNNEL_PID=""

ssh_opts=(
    -i "$SSH_KEY"
    -o BatchMode=yes
    -o StrictHostKeyChecking=accept-new
    -o ServerAliveInterval=30
)

# ----- helpers -----

ssh_remote() { ssh "${ssh_opts[@]}" "$SSH_USER@$SSH_HOST" "$@"; }

api() {
    curl -sS -H "Authorization: Bearer $TOKEN" \
        -H "Content-Type: application/json" "$@"
}
api_as() {
    local tok="$1" ; shift
    curl -sS -H "Authorization: Bearer $tok" \
        -H "Content-Type: application/json" "$@"
}
api_status() {
    curl -sS -o /dev/null -w "%{http_code}" \
        -H "Authorization: Bearer $TOKEN" \
        -H "Content-Type: application/json" "$@"
}

ok()   { printf "  OK   %s\n" "$*"; }
fail() { printf "  FAIL %s\n" "$*" >&2; exit 1; }
step() { printf "\n==> %s\n" "$*"; }

# ----- launch / stop helpers (callable twice for the reconcile test) -----

start_remote_uvicorn() {
    ssh_remote bash -s <<EOF
set -e
cd $REMOTE_REPO_PATH
nohup env \\
    SANDBOX_DEV_MODE=1 \\
    SANDBOX_API_TOKEN='$TOKEN' \\
    SANDBOX_TOKEN_PEPPER='$PEPPER' \\
    SANDBOX_DB_PATH='$REMOTE_DB' \\
    SANDBOX_AUDIT_LOG_PATH='$REMOTE_AUDIT' \\
    SANDBOX_BIND_HOST=127.0.0.1 \\
    SANDBOX_BIND_PORT=$REMOTE_PORT \\
    SANDBOX_RESOURCE_SAMPLE_INTERVAL_S=2 \\
    .venv/bin/uvicorn api.server:app \\
        --host 127.0.0.1 --port $REMOTE_PORT \\
    >> $REMOTE_LOG 2>&1 &
echo \$! > $REMOTE_PID_FILE
disown
EOF
    sleep 1
    ssh_remote "kill -0 \$(cat $REMOTE_PID_FILE 2>/dev/null) 2>/dev/null" \
        || fail "remote uvicorn did not stay alive"
}

stop_remote_uvicorn() {
    ssh_remote bash -s <<EOF 2>/dev/null
if [[ -f $REMOTE_PID_FILE ]]; then
    kill \$(cat $REMOTE_PID_FILE) 2>/dev/null || true
    # Wait up to 5 s for it to actually exit so the next launch
    # doesn't race the previous process holding the SQLite file.
    for _ in {1..50}; do
        kill -0 \$(cat $REMOTE_PID_FILE) 2>/dev/null || break
        sleep 0.1
    done
fi
EOF
}

start_tunnel() {
    ssh "${ssh_opts[@]}" -o ExitOnForwardFailure=yes -N \
        -L "$TUNNEL_LOCAL_PORT:127.0.0.1:$REMOTE_PORT" \
        "$SSH_USER@$SSH_HOST" &
    TUNNEL_PID=$!
    sleep 1
    if ! kill -0 "$TUNNEL_PID" 2>/dev/null; then
        TUNNEL_PID=""
        fail "SSH tunnel died (port $TUNNEL_LOCAL_PORT busy?)"
    fi
}

stop_tunnel() {
    if [[ -n "$TUNNEL_PID" ]]; then
        kill "$TUNNEL_PID" 2>/dev/null || true
        wait "$TUNNEL_PID" 2>/dev/null || true
        TUNNEL_PID=""
    fi
}

wait_for_healthz() {
    for _ in {1..40}; do
        curl -sf -o /dev/null "$BASE/healthz" && return
        sleep 0.5
    done
    ssh_remote "tail -40 $REMOTE_LOG 2>/dev/null" >&2 || true
    fail "API never became reachable"
}

# ----- cleanup -----

cleanup() {
    local rc=$?
    set +e
    step "cleanup"
    if (( rc != 0 )); then
        echo "  remote uvicorn log tail:"
        ssh_remote "tail -40 $REMOTE_LOG 2>/dev/null" 2>/dev/null || true
    fi
    stop_tunnel
    stop_remote_uvicorn
    ssh_remote bash -s <<EOF 2>/dev/null
rm -f $REMOTE_PID_FILE $REMOTE_DB $REMOTE_AUDIT $REMOTE_LOG
EOF
    exit "$rc"
}
trap cleanup EXIT INT TERM

# ----- preflight -----

step "preflight"
if ! ssh_remote "test -x $REMOTE_REPO_PATH/.venv/bin/uvicorn"; then
    fail "$REMOTE_REPO_PATH/.venv/bin/uvicorn not found on the remote."
fi
if command -v lsof >/dev/null \
   && lsof -nP -iTCP:"$TUNNEL_LOCAL_PORT" -sTCP:LISTEN >/dev/null 2>&1; then
    fail "local port $TUNNEL_LOCAL_PORT is already in use; set TUNNEL_LOCAL_PORT in .env"
fi
ok "preflight"

# ----- launch (1) -----

step "launching uvicorn (1/2) on $SSH_USER@$SSH_HOST"
start_remote_uvicorn
start_tunnel
wait_for_healthz
ok "uvicorn up"

# ===== Slice 6b — resource sampler =====

step "slice 6b — resource sampler"
SID_S=$(api -d '{}' $BASE/v1/sessions | jq -r .session_id)
[[ -n "$SID_S" && "$SID_S" != "null" ]] || fail "create returned no session_id"

# resource_sample_interval_s=2 (set in launch env), so within ~3 s we
# should have at least one tick.
sleep 3

samples_total=$(curl -sS "$BASE/metrics" \
    | awk '/^sandbox_resource_samples_total\{result="ok"\}/ {print $2; exit}')
[[ -n "${samples_total:-}" ]] || fail "metric sandbox_resource_samples_total{result=\"ok\"} not present"
# Compare numerically (Prometheus emits floats: "1.0").
if (( $(awk -v v="$samples_total" 'BEGIN{print (v >= 1)}') )); then
    ok "metrics: sandbox_resource_samples_total{result=\"ok\"} = $samples_total"
else
    fail "expected ≥1 ok sample; saw $samples_total"
fi

audit_samples=$(ssh_remote "grep -c '\"kind\":\"session.sample\"' $REMOTE_AUDIT 2>/dev/null || true")
audit_samples="${audit_samples:-0}"
if (( audit_samples >= 1 )); then
    ok "audit: $audit_samples session.sample line(s) written"
else
    fail "no session.sample audit lines written"
fi

api -X DELETE "$BASE/v1/sessions/$SID_S" >/dev/null

# ===== Slice 7 — multi-tenant + rotation =====

step "slice 7 — token rotation grace"
new_token=$(api -X POST "$BASE/v1/tenants/me/tokens/rotate" | jq -r .token)
[[ -n "$new_token" && ${#new_token} -eq 64 ]] || fail "rotate did not return a 64-char token"
ok "rotate returned new token (length 64)"

# Old token still works in grace.
code=$(api_status -X POST -d '{}' "$BASE/v1/sessions")
[[ "$code" == "201" ]] || fail "old token rejected during grace (got $code)"
ok "old token still authenticates during grace"

# New token also works.
code_new=$(curl -sS -o /dev/null -w "%{http_code}" \
    -H "Authorization: Bearer $new_token" -H 'Content-Type: application/json' \
    -X POST -d '{}' "$BASE/v1/sessions")
[[ "$code_new" == "201" ]] || fail "new token rejected (got $code_new)"
ok "new token authenticates"

step "slice 7 — multi-tenant isolation via CLI"
# Create a second tenant via the CLI on the remote.
new_tenant_token=$(ssh_remote "
cd $REMOTE_REPO_PATH
SANDBOX_API_TOKEN='$TOKEN' \\
SANDBOX_TOKEN_PEPPER='$PEPPER' \\
SANDBOX_DB_PATH='$REMOTE_DB' \\
SANDBOX_AUDIT_LOG_PATH='$REMOTE_AUDIT' \\
.venv/bin/python -m tools.sandbox_tenants create alice 'Alice' \\
    | awk '/^    [0-9a-f]{64}\$/ {print \$1}' | head -1
")
[[ -n "$new_tenant_token" && ${#new_tenant_token} -eq 64 ]] \
    || fail "CLI did not produce a 64-char token (got '$new_tenant_token')"
ok "CLI created tenant 'alice' with token"

# Default tenant creates a session.
SID_DEF=$(api -d '{}' "$BASE/v1/sessions" | jq -r .session_id)
# Alice cannot see it (404 — existence-oracle parity).
code=$(curl -sS -o /dev/null -w "%{http_code}" \
    -H "Authorization: Bearer $new_tenant_token" \
    "$BASE/v1/sessions/$SID_DEF")
[[ "$code" == "404" ]] || fail "expected 404 cross-tenant; got $code"
ok "tenant isolation enforced"

# Alice creates her own and can see it.
SID_ALICE=$(curl -sS -H "Authorization: Bearer $new_tenant_token" \
    -H 'Content-Type: application/json' -d '{}' \
    "$BASE/v1/sessions" | jq -r .session_id)
code=$(curl -sS -o /dev/null -w "%{http_code}" \
    -H "Authorization: Bearer $new_tenant_token" \
    "$BASE/v1/sessions/$SID_ALICE")
[[ "$code" == "200" ]] || fail "alice can't see her own session ($code)"
ok "alice sees her own session"

# Cleanup tenant sessions.
api -X DELETE "$BASE/v1/sessions/$SID_DEF" >/dev/null
curl -sS -X DELETE -H "Authorization: Bearer $new_tenant_token" \
    "$BASE/v1/sessions/$SID_ALICE" >/dev/null

# ===== Slice 6a — startup reconciliation =====

step "slice 6a — startup reconciliation"
# Create a session, then drop its container directly to simulate a crash.
SID_R=$(api -d '{}' "$BASE/v1/sessions" | jq -r .session_id)
ssh_remote "docker rm -f sandbox-$SID_R" >/dev/null 2>&1 || true
ok "created and orphaned session $SID_R (container removed out-of-band)"

# Stop the tunnel + uvicorn, relaunch against the SAME ephemeral DB.
stop_tunnel
stop_remote_uvicorn
step "launching uvicorn (2/2) — reconciliation pass"
start_remote_uvicorn
start_tunnel
wait_for_healthz

# Look for the reconcile log line in the remote uvicorn log.
recon_seen=$(ssh_remote "grep -c 'reconcile_on_startup: done' $REMOTE_LOG" || echo 0)
recon_seen="${recon_seen:-0}"
if (( recon_seen >= 1 )); then
    ok "reconcile_on_startup ran ($recon_seen sweep(s) logged)"
else
    fail "no reconcile_on_startup log line found"
fi

# The orphaned session should now be STOPPED.
status=$(api "$BASE/v1/sessions/$SID_R" | jq -r .status)
[[ "$status" == "STOPPED" ]] \
    || fail "expected STOPPED after reconcile; got '$status'"
ok "orphaned session reconciled to STOPPED"

# Audit log should have a session.reconciled record.
recon_audit=$(ssh_remote "grep -c '\"kind\":\"session.reconciled\"' $REMOTE_AUDIT 2>/dev/null || true")
recon_audit="${recon_audit:-0}"
if (( recon_audit >= 1 )); then
    ok "audit: $recon_audit session.reconciled record(s)"
else
    fail "no session.reconciled audit record"
fi

# Cleanup the orphan row.
api -X DELETE "$BASE/v1/sessions/$SID_R" >/dev/null

# ===== Slice 8b — local schema drift check =====

step "slice 8b — OpenAPI schema artifact (local)"
if (cd "$REPO_ROOT" && uv run python -m tools.dump_openapi --check); then
    ok "docs/openapi.json matches the running app schema"
else
    fail "schema drift detected; run 'uv run python -m tools.dump_openapi' and commit"
fi

step "all validations passed"
