"""/metrics endpoint smoke tests (slice 4)."""


def test_metrics_endpoint_serves_prometheus(client):
    r = client.get("/metrics")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    body = r.text
    # A few series we expect to be present after import.
    assert "sandbox_api_requests_total" in body
    assert "sandbox_session_create_seconds" in body
    assert "sandbox_exec_duration_seconds" in body
    assert "sandbox_resume_seconds" in body
    assert "sandbox_audit_emit_total" in body


def test_metrics_records_session_create(authed):
    authed.headers.update({"Authorization": "Bearer test-token"})
    r = authed.post("/v1/sessions", json={})
    assert r.status_code == 201

    metrics_body = authed.get("/metrics").text
    assert 'sandbox_sessions_lifecycle_total{reason="api",transition="create"}' in metrics_body
    # Histogram exposed; the count line increments by 1 per session create.
    assert "sandbox_session_create_seconds_count" in metrics_body


def test_metrics_excludes_metrics_endpoint_itself(authed):
    # Hit /metrics a few times and confirm api_requests_total doesn't grow
    # for that path (excluded to avoid feedback noise).
    for _ in range(3):
        authed.get("/metrics")
    body = authed.get("/metrics").text
    assert 'path="/metrics"' not in body


def test_metrics_uses_templated_path(authed):
    sid = authed.post("/v1/sessions", json={}).json()["session_id"]
    authed.get(f"/v1/sessions/{sid}")
    body = authed.get("/metrics").text
    # The label should be the template, not the literal session id.
    assert 'path="/v1/sessions/{session_id}"' in body
    assert sid not in body  # ULID would explode label cardinality
