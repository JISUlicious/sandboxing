# MCP Server

The sandbox service exposes its lifecycle / exec / file API as
[Model Context Protocol](https://modelcontextprotocol.io/) tools at
`/mcp` on the same FastAPI app, transport **Streamable HTTP**. Any
MCP-aware client (Claude Code, Claude Desktop, Cursor, in-house
agents) can connect with a single config line and drive sandboxes
without a per-client adapter.

## Endpoint

| | |
|---|---|
| URL | `POST /mcp` (and `GET /mcp` for transport-level events) |
| Transport | Streamable HTTP, stateless (`stateless_http=True, json_response=True`) |
| Auth | `Authorization: Bearer <SANDBOX_API_TOKEN>` — same tokens as the HTTP API |
| Tenant scoping | The bearer token resolves to a `tenant_id` (SPEC-405); all tool calls are scoped to that tenant. Cross-tenant access returns `session_not_found`. |

The endpoint inherits the existing port-binding posture: behind
`docker compose --env-file /etc/sandbox/env up -d` it's reachable on
`http://127.0.0.1:8000/mcp` of the host. Put a TLS-terminating
reverse proxy (Caddy / nginx — see `deploy/tls/*.example`) in front
for remote access.

## Tool catalogue (v1)

All 10 tools are thin wrappers over the existing service layer.
Schemas come straight from the same Pydantic types the HTTP API
uses, so MCP and HTTP behave identically.

### Lifecycle

| Tool | Inputs | Returns | Notes |
|---|---|---|---|
| `session_create` | `limits: Limits \| None` | `SessionResponse` | RUNNING on success. Tenant cap (default 50) bounds concurrency. |
| `session_get` | `session_id: str` | `SessionResponse` | |
| `session_stop` | `session_id: str` | `SessionResponse` | Container stopped, `/workspace` retained. |
| `session_resume` | `session_id: str` | `SessionResponse` | Reverse of stop. |
| `session_destroy` | `session_id: str` | `{ok: true}` | Permanent. Idempotent. |

### Exec

| Tool | Inputs | Returns | Notes |
|---|---|---|---|
| `exec` | `session_id: str, req: ExecRequest` | `ExecResponse` | Synchronous. STOPPED sessions auto-resume on first exec. Streaming exec is not exposed via MCP in v1. |

### Files

| Tool | Inputs | Returns | Notes |
|---|---|---|---|
| `file_write` | `session_id: str, req: FileWriteRequest` | `{path, size, mode}` | `content_b64` is base64. Creates parent dirs. |
| `file_read` | `session_id: str, path: str` | `{content_b64, mode}` | |
| `file_list` | `session_id: str, subdir: str = ""` | `FileListResponse` | |
| `file_delete` | `session_id: str, path: str, recursive: bool = False` | `{ok: true}` | |

`Limits`, `ExecRequest`, `ExecResponse`, `FileWriteRequest`, and
`FileListResponse` are the same Pydantic models the HTTP API uses;
their schemas appear in the MCP tool definitions verbatim and in
`/openapi.json`.

## Client setup

### Claude Code

```bash
claude mcp add --transport http sandbox \
    https://your-host/mcp \
    --header "Authorization: Bearer $SANDBOX_API_TOKEN"
claude mcp list           # confirms registration
# In any Claude Code session: "create a sandbox session and run
# `echo hello`" → Claude calls session_create then exec.
```

For local dev pointing at a loopback-bound API, swap the URL for
`http://127.0.0.1:8000/mcp`.

### Claude Desktop

Recent Claude Desktop versions support remote HTTP MCP servers
directly. Edit `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "sandbox": {
      "transport": {
        "type": "http",
        "url": "https://your-host/mcp",
        "headers": {
          "Authorization": "Bearer YOUR_SANDBOX_API_TOKEN"
        }
      }
    }
  }
}
```

If your Claude Desktop build doesn't yet support remote HTTP MCP
natively, run [`mcp-remote`](https://www.npmjs.com/package/mcp-remote)
as a stdio bridge:

```json
{
  "mcpServers": {
    "sandbox": {
      "command": "npx",
      "args": [
        "mcp-remote",
        "https://your-host/mcp",
        "--header",
        "Authorization: Bearer YOUR_SANDBOX_API_TOKEN"
      ]
    }
  }
}
```

### Cursor / generic MCP clients

Same pattern: HTTP transport, URL `https://your-host/mcp`, header
`Authorization: Bearer <token>`.

## Concurrency

The MCP endpoint shares the FastAPI app's event loop, thread pool,
and per-session asyncio locks with the HTTP API. Two parallel
`exec` calls on the same session interleave the same way two
parallel `POST /v1/sessions/<id>/exec` calls would. See
SPECIFICATION.md §6 and ARCHITECTURE.md for the full picture.

`stateless_http=True` means each tool call is independent at the
transport layer — there is no MCP-side session affinity. Ordering
comes from whatever the client issues; the SDK guarantees nothing
across calls beyond JSON-RPC request/response correlation.

## Security stance (v1)

- **Bearer token only.** OAuth 2.1 + PKCE is what the MCP spec
  requires for *publicly reachable* MCP servers. Our threat model
  is "trusted internal team behind a reverse proxy", which the spec
  permits with a static bearer. If you expose `/mcp` directly to
  the public internet, add OAuth — re-open the plan.
- **No DNS-rebinding protection.** The SDK's built-in check rejects
  unknown `Host` headers (default empty allowlist), which is too
  strict for the reverse-proxy deployment shape. The bearer token
  already neutralises browser-based DNS-rebinding (the attacker's
  page would need a valid token, which CORS prevents leaking).
  Operators with stricter posture can re-enable it via a settings
  flag — not in v1.
- **Tenant isolation** is the same code path the HTTP API uses
  (token → hash → tenant_id, ownership enforced at every
  service-layer call). Cross-tenant access returns
  `session_not_found`.

## Troubleshooting

- **HTTP 401 on every call** — check the bearer token matches the
  active value in `/etc/sandbox/env`. After token rotation the
  previous token authenticates for `SANDBOX_TOKEN_GRACE_SECONDS`
  (default 5 minutes); past that, 401.
- **`tools/list` returns nothing** — the SDK is up but tools
  weren't registered. Confirm the server log shows
  `StreamableHTTP session manager started`.
- **Tool call returns `session_not_found` on a session you just
  created** — usually means the second client used a different
  bearer (different tenant). Confirm with
  `claude mcp list` / Claude Desktop config.
- **Tool call hangs forever** — sandboxes auto-resume on first
  exec, which can take a few seconds. The default request timeout
  on most MCP clients is generous; if you set a custom timeout
  shorter than `exec_timeout_s + resume time`, the tool will look
  hung. Don't.
- **HTTP 421 Misdirected Request** — the SDK's DNS-rebinding check
  rejected the request. Should not happen in v1 (we disable it).
  If it does, file an issue.
