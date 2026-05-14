# End-to-End Testing Guide

Drive the deployed sandbox from a client machine and verify each
feature works: lifecycle, exec, streaming exec, file I/O, multi-turn
state, and the negative cases the spec promises will fail. Production
posture verification (gVisor actually intercepting, hardening flags
applied, etc.) lives in [`SETUP.md`'s "Validation" section](./SETUP.md).
This doc covers *functional* correctness.

## 1 · Connect to the deployed instance

The control plane should be bound to `127.0.0.1` on the Linux host (per
SPEC-302). Tunnel from your client:

```bash
# In a dedicated shell — leave it open while testing.
ssh -L 8000:127.0.0.1:8000 <user>@<linux-host>
```

### Get a tenant token

You need a **tenant bearer token** to drive the API. Don't reuse
`SANDBOX_API_TOKEN` from `/etc/sandbox/env` for testing — that value is
a *bootstrap* secret. It's used to seed the registry on first start and
is **ignored on every subsequent start**, so any rotation (or pepper
change) makes the env-file value diverge from what the live service
authenticates against. See [SETUP.md → Token lifecycle](./SETUP.md#token-lifecycle).

Ask whoever operates the host to mint a tenant token for you. As the
**operator** (the one with `sudo` on the host), the simplest issuance
path is the bootstrap CLI:

```bash
sudo -u sandbox uv --directory /opt/sandbox run \
    python -m tools.sandbox_tenants create tester "Functional testing"
# Prints a single bearer token — copy it now; the API never reprints it.
```

Or, if `SANDBOX_ADMIN_TOKEN` is set, the admin API:

```bash
ADMIN=$(sudo grep -E '^SANDBOX_ADMIN_TOKEN=' /etc/sandbox/env | cut -d= -f2)
curl -sS -X POST http://127.0.0.1:8000/v1/tenants \
    -H "Authorization: Bearer $ADMIN" -H 'Content-Type: application/json' \
    -d '{"name":"tester","display_name":"Functional testing"}'
TOKEN=$(curl -sS -X POST http://127.0.0.1:8000/v1/tenants/tester/tokens \
    -H "Authorization: Bearer $ADMIN" -H 'Content-Type: application/json' \
    -d '{"note":"TESTING.md walkthrough"}' | jq -r .token)
```

The **client** (whoever runs curl) only ever sees the tenant token.
`SANDBOX_ADMIN_TOKEN` stays on the host and is never exported to the
client shell.

Once you have a token, you (the client) can rotate it yourself with
`POST /v1/tenants/me/tokens/rotate` (no admin needed) — useful for
periodic refresh without going back to the operator.

### Set up the testing shell

```bash
export BASE=http://127.0.0.1:8000
export TOKEN=<the tenant token from above>
alias api='curl -sS -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json"'
# Smoke check — should print {"status":"ok"}.
api $BASE/healthz
# Auth smoke — should return a session_id, not null:
api -d '{}' $BASE/v1/sessions | jq -r .session_id
```

If `session_id` is `null`, run with `-i` to see the status code; a 401
means your `TOKEN` doesn't authenticate (most often: someone handed you
the env-file value rather than a freshly minted tenant token).

If you bound to a LAN interface and want to skip the tunnel, replace
`BASE` with `http://<linux-ip>:8000`. (Note the SPEC-302 caveat — only
do this on a trusted network with a strong token.)

`jq` is used throughout; install with `brew install jq` if you don't
have it.

---

## 2 · Sanity probes

```bash
api $BASE/healthz                 # {"status":"ok"}
api $BASE/readyz                  # {"docker": true}
curl -s $BASE/metrics | head -3   # # HELP / # TYPE Prometheus header
```

`docker: false` means the daemon isn't reachable from the control plane
— check `sudo systemctl status docker` on the Linux host.

---

## 3 · Lifecycle round-trip

```bash
# Create a session.
SID=$(api -d '{}' $BASE/v1/sessions | jq -r .session_id)
echo "session: $SID"

# Inspect it. Every response includes `idle_stop_at` (epoch ms when
# the reaper would idle-stop absent further activity; null for
# already-STOPPED rows) and `hard_destroy_at` (epoch ms when the
# session would be hard-destroyed: container + workspace volume
# both removed, irreversible). Both shift forward with every
# mutating op (exec, file write, process start, etc.).
api $BASE/v1/sessions/$SID | jq '{status, limits, idle_stop_at, hard_destroy_at}'

# Stop / resume (filesystem survives both). idle_stop_at becomes null
# when STOPPED and reappears on resume; hard_destroy_at stays set.
api -X POST $BASE/v1/sessions/$SID/stop   | jq '{status, idle_stop_at}'   # "STOPPED", null
api -X POST $BASE/v1/sessions/$SID/resume | jq '{status, idle_stop_at}'   # "RUNNING", <epoch>

# Destroy at the end.
api -X DELETE $BASE/v1/sessions/$SID -o /dev/null -w "%{http_code}\n"  # 204

# A destroyed session looks like "never existed" (SPEC-200).
api $BASE/v1/sessions/$SID -o /dev/null -w "%{http_code}\n"            # 404
```

---

## 4 · Code execution

Create a fresh session for the rest of the doc:

```bash
SID=$(api -d '{}' $BASE/v1/sessions | jq -r .session_id)
```

### 4.1 Basic argv

```bash
api -d '{"argv":["echo","hello world"]}' \
    $BASE/v1/sessions/$SID/exec | jq '{stdout, exit_code, duration_ms}'
# stdout = "hello world\n", exit_code = 0
```

### 4.2 Environment override (SPEC-108)

```bash
api -d '{"argv":["bash","-c","echo $FOO-$BAR"], "env":{"FOO":"a","BAR":"b"}}' \
    $BASE/v1/sessions/$SID/exec | jq -r .stdout
# a-b
```

### 4.3 Inline stdin (SPEC-201)

```bash
api -d '{"argv":["wc","-c"], "stdin":"hello\n"}' \
    $BASE/v1/sessions/$SID/exec | jq -r .stdout
# 6  (5 letters + newline)
```

### 4.4 Wall-clock timeout

```bash
# Default exec timeout is 60s; set 2s for the test.
api -d '{"argv":["sleep","30"], "timeout_s":2}' \
    $BASE/v1/sessions/$SID/exec -o /dev/null -w "HTTP %{http_code}\n"
# HTTP 408 — the `timeout` utility kills sleep with exit 124, mapped
# to exec_timeout.
```

### 4.5 Tenant-max clamp

```bash
api -d '{"argv":["true"], "timeout_s":99999}' \
    $BASE/v1/sessions/$SID/exec | jq .effective_timeout_s
# 600 — clamped to SPEC §6 tenant max.
```

### 4.6 Output cap (SPEC-203)

```bash
# Generate ~10 MiB of stdout — well past the 8 MiB per-stream cap.
api -d '{"argv":["bash","-c","yes hello | head -c 10485760"]}' \
    $BASE/v1/sessions/$SID/exec \
    | jq '{truncated, truncated_streams, stdout_len: (.stdout | length)}'
# truncated=true, truncated_streams=["stdout"], stdout_len ~ 8388608.
```

---

## 5 · Streaming execution (SSE)

`curl -N` keeps the connection unbuffered so you see chunks as they
arrive:

```bash
curl -N -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
    -d '{"argv":["bash","-c","for i in 1 2 3; do echo line $i; sleep 1; done"]}' \
    $BASE/v1/sessions/$SID/exec/stream
```

You should see ~3 `event: stdout` chunks (one per second), then a final
`event: result`. The `chunk_b64` payloads decode to `line 1\n`, etc.

Decode in-flight:

```bash
curl -N -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
    -d '{"argv":["bash","-c","for i in 1 2 3; do echo $i; sleep 1; done"]}' \
    $BASE/v1/sessions/$SID/exec/stream \
  | awk -F': ' '
      /^event:/ { ev = $2 }
      /^data:/  { print ev ": " $2 }
    '
```

For `event: stdout` lines the data is `{"chunk_b64":"..."}`; for
`event: result` it's the full ExecResponse.

---

## 6 · File I/O

### 6.1 Write a file

```bash
# Plain text.
echo -n "hello, sandbox" | base64 | tr -d '\n' > /tmp/payload.b64
api -d '{
  "path": "greeting.txt",
  "content_b64": "'"$(cat /tmp/payload.b64)"'",
  "mode": 420
}' $BASE/v1/sessions/$SID/files | jq .
# {"path":"/workspace/greeting.txt","size":14,"mode":420}
```

(`420` = `0o644`; the API takes decimal.)

The same write also has a path-in-URL shape that mirrors `GET` / `DELETE`,
useful when you have raw bytes in hand and don't want to base64-encode
them into JSON:

```bash
curl -s -H "Authorization: Bearer $TOKEN" \
    -H 'Content-Type: application/octet-stream' \
    --data-binary @/tmp/payload.bin \
    "$BASE/v1/sessions/$SID/files/sub/dir/file.bin?mode=420"
# {"path":"/workspace/sub/dir/file.bin","size":...,"mode":420}
```

Parent directories are created on the fly (`mkdir -p`) for both shapes.

### 6.2 Read it back (binary-safe, raw bytes out)

```bash
curl -s -H "Authorization: Bearer $TOKEN" \
    $BASE/v1/sessions/$SID/files/greeting.txt
# hello, sandbox
```

The response is `application/octet-stream`; the file's mode is in the
`X-File-Mode` header.

### 6.3 List the workspace

```bash
api $BASE/v1/sessions/$SID/files | jq .
# entries: [{name:"greeting.txt", is_dir:false, size:14, mode:420}, ...]
```

`?dir=subdir` to list a subdirectory; absent means `/workspace`.

### 6.4 Write and read a binary file

```bash
# Random 2 KiB binary blob.
head -c 2048 /dev/urandom > /tmp/blob.bin
B64=$(base64 -i /tmp/blob.bin | tr -d '\n')
api -d '{"path":"blob.bin","content_b64":"'"$B64"'"}' \
    $BASE/v1/sessions/$SID/files | jq .

curl -s -H "Authorization: Bearer $TOKEN" \
    $BASE/v1/sessions/$SID/files/blob.bin > /tmp/blob.out
diff -q /tmp/blob.bin /tmp/blob.out && echo "binary roundtrip OK"
```

### 6.5 Delete

```bash
api -X DELETE $BASE/v1/sessions/$SID/files/greeting.txt \
    -o /dev/null -w "HTTP %{http_code}\n"     # 204

# Directory delete requires ?recursive=true (SPEC-107).
api -d '{"argv":["mkdir","-p","subdir"], "stdin":null}' \
    $BASE/v1/sessions/$SID/exec > /dev/null
api -X DELETE $BASE/v1/sessions/$SID/files/subdir \
    -o /dev/null -w "HTTP %{http_code}\n"     # 400 — needs recursive
api -X DELETE "$BASE/v1/sessions/$SID/files/subdir?recursive=true" \
    -o /dev/null -w "HTTP %{http_code}\n"     # 204
```

---

## 7 · Multi-turn state

The contract: filesystem state in `/workspace` persists across exec
calls (SPEC-002) and across stop/resume (SPEC-104). Process state does
**not** persist between exec calls.

### 7.1 Filesystem persists across execs

```bash
api -d '{"argv":["bash","-c","echo turn1 > /workspace/notes && pwd"]}' \
    $BASE/v1/sessions/$SID/exec > /dev/null

api -d '{"argv":["cat","/workspace/notes"]}' \
    $BASE/v1/sessions/$SID/exec | jq -r .stdout
# turn1
```

### 7.2 Env vars do NOT persist (SPEC-002 process state)

```bash
api -d '{"argv":["bash","-c","export FOO=bar; echo set"]}' \
    $BASE/v1/sessions/$SID/exec > /dev/null

api -d '{"argv":["bash","-c","echo FOO=${FOO:-MISSING}"]}' \
    $BASE/v1/sessions/$SID/exec | jq -r .stdout
# FOO=MISSING — each exec is a fresh process.
```

To persist env, write a file:

```bash
api -d '{"argv":["bash","-c","echo FOO=bar > /workspace/.envrc"]}' \
    $BASE/v1/sessions/$SID/exec > /dev/null

api -d '{"argv":["bash","-c","source /workspace/.envrc && echo FOO=$FOO"]}' \
    $BASE/v1/sessions/$SID/exec | jq -r .stdout
# FOO=bar
```

### 7.3 State survives stop / resume (SPEC-104)

```bash
api -d '{"argv":["bash","-c","date > /workspace/stamp"]}' \
    $BASE/v1/sessions/$SID/exec > /dev/null

# Idle-stop the container; volume retained.
api -X POST $BASE/v1/sessions/$SID/stop > /dev/null

# Implicit transparent resume on the next exec (SPEC-104, ARCH §3.2).
api -d '{"argv":["cat","/workspace/stamp"]}' \
    $BASE/v1/sessions/$SID/exec | jq -r .stdout
# original timestamp — file survived the container restart.
```

---

## 8 · Negative cases (must fail correctly)

```bash
# 8.1 Missing auth — 401.
curl -sS -o /dev/null -w "%{http_code}\n" -X POST -H 'Content-Type: application/json' \
    -d '{}' $BASE/v1/sessions
# 401

# 8.2 Forbidden env key — 400 (SPEC-201).
api -d '{"argv":["true"], "env":{"HTTP_PROXY":"evil"}}' \
    $BASE/v1/sessions/$SID/exec -o /dev/null -w "%{http_code}\n"
# 400

# 8.3 Path traversal — 400 (SPEC-107).
api -d '{"path":"../etc/passwd","content_b64":""}' \
    $BASE/v1/sessions/$SID/files -o /dev/null -w "%{http_code}\n"
# 400

# 8.4 Empty argv — 422 (pydantic).
api -d '{"argv":[]}' $BASE/v1/sessions/$SID/exec \
    -o /dev/null -w "%{http_code}\n"
# 422

# 8.5 Stdin on /exec/stream — 400 (slice 3 limitation).
api -d '{"argv":["cat"],"stdin":"hi"}' $BASE/v1/sessions/$SID/exec/stream \
    -o /dev/null -w "%{http_code}\n"
# 400

# 8.6 Read non-existent file — 404.
api $BASE/v1/sessions/$SID/files/nope.txt -o /dev/null -w "%{http_code}\n"
# 404
```

---

## 9 · /metrics after the run

```bash
curl -s $BASE/metrics | grep -E '^sandbox_(api_requests|exec_duration|session_create|sessions_lifecycle|audit_emit)' \
    | grep -v '_created'
```

You should see counters incremented by the activity above:
- `sandbox_api_requests_total{...}` per endpoint.
- `sandbox_session_create_seconds_count` ≥ number of sessions you made.
- `sandbox_exec_duration_seconds_count{result="ok"}` matching successful execs.
- `sandbox_audit_emit_total{kind="session.exec"}` matching exec calls.

---

## 10 · Cleanup

```bash
api -X DELETE $BASE/v1/sessions/$SID -o /dev/null -w "%{http_code}\n"  # 204
```

If you accumulated test sessions and want to wipe them all, see
[`SETUP.md`'s "Manually clean a single session"](./SETUP.md) — the
reaper will hard-destroy them automatically at the 24 h TTL.

---

## What this guide does **not** cover

- Production posture (gVisor actually intercepting, hardening flags
  applied, egress allowlist actually blocking, sandbox-to-sandbox
  iptables drop, XFS quota cap actually firing) — see `SETUP.md`
  "Validation" section.
- Adversarial isolation (host escape attempts, syscall fuzzing).
- Load testing.
