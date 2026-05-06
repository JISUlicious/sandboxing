"""MCP server endpoint tests (slice M-3).

Drives the mounted /mcp endpoint via the existing TestClient with
raw JSON-RPC requests. The SDK is configured `json_response=True`
so each tool call returns one JSON object — no SSE — which the
sync TestClient handles cleanly. See api/mcp_server.py for the
auth bridge and tool catalogue under test.
"""

from __future__ import annotations

import base64

import pytest

EXPECTED_TOOLS = {
    "session_create",
    "session_get",
    "session_stop",
    "session_resume",
    "session_destroy",
    "exec",
    "file_write",
    "file_read",
    "file_list",
    "file_delete",
    # Slice 11c — background-process MCP tools.
    "process_start",
    "process_list",
    "process_get",
    "process_logs",
    "process_stop",
}

MCP_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json,text/event-stream",
}


def _rpc(client, method, params=None, *, request_id=1, with_auth=True):
    headers = dict(MCP_HEADERS)
    if with_auth:
        headers["Authorization"] = client.headers.get("Authorization", "Bearer test-token")
    body = {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": method,
        "params": params or {},
    }
    return client.post("/mcp", json=body, headers=headers)


def _initialize(client):
    return _rpc(
        client,
        "initialize",
        {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "tests", "version": "0"},
        },
    )


def _call_tool(client, name, arguments=None):
    r = _rpc(client, "tools/call", {"name": name, "arguments": arguments or {}})
    assert r.status_code == 200, r.text
    body = r.json()
    assert "error" not in body, body["error"]
    return body["result"]


# ---------------------------------------------------------------------
# Transport / catalogue
# ---------------------------------------------------------------------


def test_initialize_handshake(authed):
    r = _initialize(authed)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["jsonrpc"] == "2.0"
    assert body["result"]["serverInfo"]["name"] == "sandbox"
    # The SDK negotiates a protocolVersion string back; just assert it's there.
    assert body["result"].get("protocolVersion")


def test_tools_list_returns_full_catalogue(authed):
    r = _rpc(authed, "tools/list")
    assert r.status_code == 200, r.text
    names = {t["name"] for t in r.json()["result"]["tools"]}
    assert names == EXPECTED_TOOLS

    # Each tool must have a non-empty description — that's what the
    # LLM reads to decide when to call it.
    for tool in r.json()["result"]["tools"]:
        assert tool.get("description"), f"tool {tool['name']} missing description"


# ---------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------


def test_mcp_rejects_missing_bearer(client):
    r = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        headers=MCP_HEADERS,
    )
    assert r.status_code == 401
    assert r.json()["detail"]["code"] == "unauthorized"


def test_mcp_rejects_invalid_bearer(client):
    r = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        headers={**MCP_HEADERS, "Authorization": "Bearer not-a-real-token"},
    )
    assert r.status_code == 401
    assert r.json()["detail"]["code"] == "unauthorized"


def test_unknown_path_falls_through_to_404(authed):
    """The MCP sub-app is mounted at root as a catch-all; the auth
    middleware must NOT enforce on non-/mcp paths so a typo'd URL
    surfaces a clean 404 instead of a confusing 401."""
    r = authed.get("/this-does-not-exist")
    assert r.status_code == 404


# ---------------------------------------------------------------------
# Lifecycle round-trip via MCP
# ---------------------------------------------------------------------


def test_session_lifecycle_via_mcp(authed):
    create = _call_tool(authed, "session_create", {})
    payload = create["structuredContent"]
    sid = payload["session_id"]
    assert payload["status"] == "RUNNING"
    assert payload["tenant_id"] == "default"

    got = _call_tool(authed, "session_get", {"session_id": sid})
    assert got["structuredContent"]["session_id"] == sid

    stopped = _call_tool(authed, "session_stop", {"session_id": sid})
    assert stopped["structuredContent"]["status"] == "STOPPED"

    resumed = _call_tool(authed, "session_resume", {"session_id": sid})
    assert resumed["structuredContent"]["status"] == "RUNNING"

    destroyed = _call_tool(authed, "session_destroy", {"session_id": sid})
    assert destroyed["structuredContent"]["ok"] is True


def test_exec_via_mcp(authed):
    sid = _call_tool(authed, "session_create", {})["structuredContent"]["session_id"]
    res = _call_tool(
        authed,
        "exec",
        {"session_id": sid, "req": {"argv": ["echo", "hello"]}},
    )
    payload = res["structuredContent"]
    assert payload["exit_code"] == 0
    # The fake docker driver returns empty stdout by default; we're
    # exercising the wiring + schema, not the runtime behaviour.
    assert "stdout" in payload


def test_file_roundtrip_via_mcp(authed):
    sid = _call_tool(authed, "session_create", {})["structuredContent"]["session_id"]
    content = b"sandbox mcp roundtrip"
    b64 = base64.b64encode(content).decode()

    write = _call_tool(
        authed,
        "file_write",
        {"session_id": sid, "req": {"path": "note.txt", "content_b64": b64, "mode": 0o640}},
    )
    assert write["structuredContent"]["path"].endswith("note.txt")

    # The fake docker fixture doesn't store a real fs; assert wiring
    # by ensuring the call shape is accepted and the response decodes.
    listed = _call_tool(authed, "file_list", {"session_id": sid})
    assert "entries" in listed["structuredContent"]


# ---------------------------------------------------------------------
# Cross-tenant isolation
# ---------------------------------------------------------------------


@pytest.fixture
async def alice_token(client, settings, service):
    """Issue a second tenant + token; return a Bearer string usable
    via the same TestClient (different Authorization header)."""
    from api.auth import TokenAuthenticator, generate_token_plaintext

    authn = TokenAuthenticator(settings=settings, registry=service.registry)
    await service.registry.create_tenant("alice", "Alice's team")
    plaintext = generate_token_plaintext()
    await authn.issue_initial_token("alice", plaintext)
    return plaintext


async def test_cross_tenant_isolation_via_mcp(authed, client, alice_token):
    # Create a session as the default tenant.
    sid = _call_tool(authed, "session_create", {})["structuredContent"]["session_id"]

    # Switch the client to alice's bearer and try to fetch.
    client.headers["Authorization"] = f"Bearer {alice_token}"
    r = _rpc(client, "tools/call", {"name": "session_get", "arguments": {"session_id": sid}})
    assert r.status_code == 200
    body = r.json()
    # The MCP SDK reports tool errors inside `result.isError`/content,
    # not as a transport-level error. session_not_found is mapped to
    # a RuntimeError surfacing with the error code.
    assert body["result"].get("isError") is True
    text = body["result"]["content"][0]["text"]
    assert "session_not_found" in text


# ---------------------------------------------------------------------
# Slice 13 — exec streaming via MCP progress notifications
# ---------------------------------------------------------------------


def test_exec_without_progress_token_uses_sync_path(authed):
    """No progressToken → sync path → one JSON response object,
    no SSE event framing."""
    sid = _call_tool(authed, "session_create", {})["structuredContent"]["session_id"]
    r = _rpc(
        authed,
        "tools/call",
        {
            "name": "exec",
            "arguments": {"session_id": sid, "req": {"argv": ["echo", "hi"]}},
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert "exit_code" in body["result"]["structuredContent"]


def test_exec_with_progress_token_uses_streaming_path(authed, fake_docker):
    """With a progressToken the tool consumes ExecService.run_stream
    chunk-by-chunk (the streaming path) instead of the sync .run path.
    Both chunks aggregate into the final stdout — that only happens
    via run_stream; the sync path would have returned empty stdout
    from FakeDockerClient.exec_in_container's default.

    Note: `json_response=True` collapses progress notifications into
    the final JSON tool result rather than emitting them as separate
    SSE events to a sync TestClient. Operators who need over-the-wire
    progress notifications drop `json_response=True` from build_mcp;
    the internal streaming path is what matters for the behavioural
    difference and is what this test verifies.
    """
    fake_docker.stream_exec_scripts.append(
        [
            ("stdout", b"first chunk\n"),
            ("stdout", b"second chunk\n"),
            ("exit", 0),
        ]
    )

    sid = _call_tool(authed, "session_create", {})["structuredContent"]["session_id"]
    body = {
        "jsonrpc": "2.0",
        "id": 99,
        "method": "tools/call",
        "params": {
            "name": "exec",
            "arguments": {"session_id": sid, "req": {"argv": ["echo", "hi"]}},
            "_meta": {"progressToken": "test-progress-1"},
        },
    }
    r = authed.post("/mcp", json=body, headers=MCP_HEADERS)
    assert r.status_code == 200, r.text
    payload = r.json()["result"]["structuredContent"]
    assert payload["stdout"] == "first chunk\nsecond chunk\n"
    assert payload["exit_code"] == 0
