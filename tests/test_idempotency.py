"""Idempotency-Key middleware tests (slice 11a).

Drives `POST /v1/sessions` because it's the simplest mutating
endpoint that returns a 2xx body — the same machinery applies to
every other POST/DELETE under `/v1/`.
"""

from __future__ import annotations

import json
import uuid


def _idem_headers(client, key):
    return {"Authorization": "Bearer test-token", "Idempotency-Key": key}


def test_replay_returns_same_body(authed):
    key = str(uuid.uuid4())
    r1 = authed.post("/v1/sessions", json={}, headers=_idem_headers(authed, key))
    assert r1.status_code == 201, r1.text
    sid_first = r1.json()["session_id"]

    r2 = authed.post("/v1/sessions", json={}, headers=_idem_headers(authed, key))
    assert r2.status_code == 201
    # Replay must return the cached body — the second call must NOT
    # have created a second session.
    assert r2.json()["session_id"] == sid_first
    # The replay carries the marker header so clients can tell.
    assert r2.headers.get("idempotent-replay") == "true"


def test_get_requests_are_not_cached(authed):
    """GET /healthz with an Idempotency-Key must not poison the cache."""
    key = str(uuid.uuid4())
    r1 = authed.get("/healthz", headers={"Idempotency-Key": key})
    assert r1.status_code == 200

    # Subsequent POST under the same key starts fresh.
    r2 = authed.post("/v1/sessions", json={}, headers=_idem_headers(authed, key))
    assert r2.status_code == 201
    assert r2.headers.get("idempotent-replay") is None


def test_route_mismatch_returns_409(authed, settings, service):
    """Reusing a key against a different endpoint surfaces a 409
    so the caller realises the key was already consumed."""
    import asyncio

    key = str(uuid.uuid4())
    r1 = authed.post("/v1/sessions", json={}, headers=_idem_headers(authed, key))
    assert r1.status_code == 201
    sid = r1.json()["session_id"]

    # DELETE /v1/sessions/{sid} reuses the same key against a
    # different route_template.
    r2 = authed.delete(f"/v1/sessions/{sid}", headers=_idem_headers(authed, key))
    assert r2.status_code == 409
    assert r2.json()["detail"]["code"] == "idempotency_route_mismatch"

    # Cleanup: actually destroy the session under a fresh key so the
    # test fixture can tear down without leaking state.
    authed.delete(
        f"/v1/sessions/{sid}",
        headers=_idem_headers(authed, str(uuid.uuid4())),
    )

    # Sanity-check the cached row is what we expect.
    cached = asyncio.run(service.registry.lookup_idempotency(tenant_id="default", key=key))
    assert cached is not None
    route_template, status, body, _ = cached
    assert route_template == "/v1/sessions"
    assert status == 201
    assert json.loads(body)["session_id"] == sid


def test_no_idempotency_header_passes_through(authed):
    """Without the header, every call creates a new session."""
    r1 = authed.post("/v1/sessions", json={})
    r2 = authed.post("/v1/sessions", json={})
    assert r1.json()["session_id"] != r2.json()["session_id"]


def test_invalid_bearer_skips_middleware(client):
    """A bad bearer falls through to the route's auth dependency,
    which returns 401. The middleware must NOT short-circuit with
    its own response."""
    r = client.post(
        "/v1/sessions",
        json={},
        headers={
            "Authorization": "Bearer not-a-real-token",
            "Idempotency-Key": "abc",
        },
    )
    assert r.status_code == 401
    # Body shape is the standard ErrorResponse, not the
    # idempotency middleware's shape.
    assert r.json()["detail"]["code"] == "unauthorized"


def test_streaming_response_not_cached(authed, fake_docker, service):
    """v0.2.9 regression: a streaming response (SSE on /exec/stream)
    with an Idempotency-Key must NOT be drained into bytes and cached.
    Pre-fix: middleware called `_read_body_bytes()` on the streaming
    response, computed Content-Length, and rebuilt as a static
    Response — clients saw all SSE frames bunch at the end of execution
    instead of arriving incrementally. Customer (adk-cc) reported this
    as plan-sandbox-issues-from-e2e.md #14.

    The fix lets the StreamingResponse pass through untouched when
    Content-Type starts with 'text/event-stream'; cache row is NOT
    written, so a retry with the same key re-executes (acceptable for
    streaming endpoints — the alternative, cached frozen snapshot
    replay, doesn't preserve real-time timing anyway)."""
    import asyncio

    sid = authed.post("/v1/sessions", json={}).json()["session_id"]
    stream_key = str(uuid.uuid4())
    fake_docker.stream_exec_scripts.append(
        [
            ("stdout", b"line-1\n"),
            ("stdout", b"line-2\n"),
            ("stdout", b"line-3\n"),
            ("exit", 0),
        ]
    )
    r = authed.post(
        f"/v1/sessions/{sid}/exec/stream",
        json={"argv": ["/bin/echo", "hi"]},
        headers={
            "Authorization": "Bearer test-token",
            "Idempotency-Key": stream_key,
        },
    )
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    # Body must contain the SSE-framed events (not a cached single blob).
    assert "event: stdout" in r.text
    assert "event: result" in r.text

    # The load-bearing assertion: the streaming response was NOT cached.
    cached = asyncio.run(
        service.registry.lookup_idempotency(tenant_id="default", key=stream_key)
    )
    assert cached is None, (
        "IdempotencyMiddleware must not drain+cache SSE responses; "
        f"got {cached!r}"
    )


def test_concurrent_replays_dedupe(authed, settings, service):
    """Two concurrent POSTs with the same key result in ONE session."""
    import threading

    key = str(uuid.uuid4())
    results: list[int] = []

    def go():
        r = authed.post("/v1/sessions", json={}, headers=_idem_headers(authed, key))
        results.append(r.json()["session_id"])

    threads = [threading.Thread(target=go) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # All four requests resolved to the same session id.
    assert len(set(results)) == 1, f"expected one session, got {set(results)}"
