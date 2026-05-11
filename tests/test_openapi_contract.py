"""OpenAPI contract regression tests.

Lock down the polish that `_install_openapi_polish` adds on top of
FastAPI's default schema generation. Each assertion below traces back
to a contract delta the customer audit at
`~/.claude/plans/plan-sandbox-openapi-alignment.md` flagged on
v0.1.5 — without these tests, a refactor of the openapi callable
could silently regress the spec without anyone noticing until the
next consumer audit.
"""

from __future__ import annotations


def _schema(client) -> dict:
    r = client.get("/openapi.json")
    assert r.status_code == 200
    return r.json()


# ----- info / servers -----


def test_info_version_tracks_release_tag(client):
    """`info.version` must move on every spec-affecting change so
    consumers pinned against an older version can detect drift."""
    schema = _schema(client)
    # The exact value is the current release tag; what matters here is
    # that it isn't stuck at the original 0.1.0 placeholder.
    assert schema["info"]["version"] != "0.1.0"


def test_servers_array_declared(client):
    """Without a `servers` entry, SDK generators emit clients with no
    base URL. We ship a relative `/` placeholder so generated clients
    resolve against the host the spec was fetched from."""
    schema = _schema(client)
    servers = schema.get("servers")
    assert isinstance(servers, list) and servers, "servers array missing"
    assert servers[0]["url"] == "/"


# ----- bearerAuth security scheme -----


def test_bearer_auth_security_scheme_declared(client):
    schema = _schema(client)
    schemes = schema.get("components", {}).get("securitySchemes", {})
    assert "bearerAuth" in schemes, "bearerAuth missing from securitySchemes"
    assert schemes["bearerAuth"]["type"] == "http"
    assert schemes["bearerAuth"]["scheme"] == "bearer"


def test_global_security_default_is_bearer(client):
    schema = _schema(client)
    assert schema.get("security") == [{"bearerAuth": []}]


def test_public_endpoints_opt_out_of_bearer(client):
    """Ops endpoints must NOT inherit the global bearer requirement —
    /healthz, /readyz, /metrics are reachable without auth and that
    needs to be visible in the schema."""
    schema = _schema(client)
    for path in ("/healthz", "/readyz", "/metrics"):
        op = schema["paths"][path]["get"]
        assert op.get("security") == [], f"{path} should opt out via security: []"


# ----- Idempotency-Key parameter -----


def test_idempotency_key_parameter_declared(client):
    schema = _schema(client)
    params = schema["components"].get("parameters", {})
    assert "IdempotencyKey" in params
    p = params["IdempotencyKey"]
    assert p["name"] == "Idempotency-Key"
    assert p["in"] == "header"
    assert p["required"] is False
    assert p["schema"]["type"] == "string"
    assert p["schema"]["maxLength"] == 64


def test_idempotency_key_referenced_from_mutating_routes(client):
    schema = _schema(client)
    ref = "#/components/parameters/IdempotencyKey"
    # POST /v1/sessions is the canonical mutation; if it doesn't
    # carry the ref, the openapi callable is broken for everything.
    params = schema["paths"]["/v1/sessions"]["post"]["parameters"]
    refs = [p.get("$ref") for p in params if "$ref" in p]
    assert ref in refs


def test_idempotent_replay_response_header_documented(client):
    """The middleware sets `Idempotent-Replay: true` on cache hits;
    the schema must declare that response header so SDK generators
    can surface it."""
    schema = _schema(client)
    op = schema["paths"]["/v1/sessions"]["post"]
    headers = op["responses"]["201"].get("headers") or {}
    assert "Idempotent-Replay" in headers


# ----- POST /files/{path} requestBody (octet-stream) -----


def test_files_path_post_declares_octet_stream_body(client):
    """The handler reads `await request.body()` directly so FastAPI
    can't introspect the body shape. The openapi callable injects
    application/octet-stream + format: binary so SDK generators
    stop seeing `requestBody: null` (audit issue #3)."""
    schema = _schema(client)
    op = schema["paths"]["/v1/sessions/{session_id}/files/{path}"]["post"]
    body = op.get("requestBody")
    assert body is not None, "POST /files/{path} should declare a requestBody"
    # Empty body is valid (touch-like), so required must be False.
    assert body["required"] is False
    media = body["content"]
    assert "application/octet-stream" in media
    assert media["application/octet-stream"]["schema"]["format"] == "binary"


# ----- /processes/{pid}/logs SSE response -----


def test_logs_endpoint_declares_text_event_stream(client):
    """Audit issue #1: the route returns `text/event-stream` but the
    schema used to declare `application/json`, hanging any
    SDK-generated client. This locks down the corrected declaration."""
    schema = _schema(client)
    op = schema["paths"]["/v1/sessions/{session_id}/processes/{process_id}/logs"]["get"]
    content = op["responses"]["200"]["content"]
    assert "text/event-stream" in content
    assert "application/json" not in content


# ----- Slice 13b — ETA fields on SessionResponse -----


def test_session_response_schema_includes_etas(client):
    """SessionResponse must declare both `idle_stop_at` and
    `hard_destroy_at` — consumers need them to plan around expiry
    without subscribing to the audit log."""
    schema = _schema(client)
    sr = schema["components"]["schemas"]["SessionResponse"]
    props = sr.get("properties") or {}
    assert "idle_stop_at" in props, "idle_stop_at missing from SessionResponse"
    assert "hard_destroy_at" in props, "hard_destroy_at missing from SessionResponse"

    # idle_stop_at is nullable; hard_destroy_at is required + int.
    assert sr.get("required") and "hard_destroy_at" in sr["required"]
    assert "idle_stop_at" not in (sr.get("required") or [])

    # Both fields should carry an operator-readable description.
    assert "description" in props["idle_stop_at"] and props["idle_stop_at"]["description"]
    assert "description" in props["hard_destroy_at"] and props["hard_destroy_at"]["description"]
