"""Tests for the SSE streaming endpoint and exec stdin (slice 3)."""

import base64
import json

from api.docker_client import TIMEOUT_EXIT_CODE, ExecOutput


def _create(authed) -> str:
    return authed.post("/v1/sessions", json={}).json()["session_id"]


def _parse_sse(body: str) -> list[tuple[str, dict]]:
    """Split an SSE response body into (event, data) pairs."""
    events: list[tuple[str, dict]] = []
    for block in body.strip().split("\n\n"):
        if not block.strip():
            continue
        event_name = ""
        data_str = ""
        for line in block.splitlines():
            if line.startswith("event:"):
                event_name = line[len("event:") :].strip()
            elif line.startswith("data:"):
                data_str = line[len("data:") :].strip()
        if event_name:
            events.append((event_name, json.loads(data_str) if data_str else {}))
    return events


# ----- streaming -----


def test_stream_emits_chunks_then_result(authed, fake_docker):
    sid = _create(authed)
    fake_docker.stream_exec_scripts.append(
        [
            ("stdout", b"hello "),
            ("stdout", b"world\n"),
            ("stderr", b"warn\n"),
            ("exit", 0),
        ]
    )
    r = authed.post(
        f"/v1/sessions/{sid}/exec/stream",
        json={"argv": ["echo", "hello world"]},
    )
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")

    events = _parse_sse(r.text)
    kinds = [k for k, _ in events]
    assert kinds[-1] == "result"
    # Two stdout chunks + one stderr chunk + result.
    assert kinds.count("stdout") == 2
    assert kinds.count("stderr") == 1

    # Decoded chunks reconstruct the original output.
    stdout_b64 = [d["chunk_b64"] for k, d in events if k == "stdout"]
    decoded = b"".join(base64.b64decode(c) for c in stdout_b64)
    assert decoded == b"hello world\n"

    result = next(d for k, d in events if k == "result")
    assert result["exit_code"] == 0
    assert result["stdout"] == "hello world\n"
    assert result["stderr"] == "warn\n"
    assert result["truncated"] is False


def test_stream_timeout_marked_in_result(authed, fake_docker):
    sid = _create(authed)
    fake_docker.stream_exec_scripts.append(
        [
            ("stdout", b"starting...\n"),
            ("exit", TIMEOUT_EXIT_CODE),
        ]
    )
    r = authed.post(
        f"/v1/sessions/{sid}/exec/stream",
        json={"argv": ["sleep", "999"]},
    )
    assert r.status_code == 200  # SSE: errors live in the result event
    events = _parse_sse(r.text)
    result = next(d for k, d in events if k == "result")
    assert result["error"] == "exec_timeout"
    assert result["exit_code"] == -1


def test_stream_truncation_event_emitted_once(authed, fake_docker):
    sid = _create(authed)
    # Generate enough stdout to blow past 8 MiB across multiple chunks.
    chunk = b"x" * (1024 * 1024)
    script = [("stdout", chunk)] * 10  # 10 MiB > 8 MiB cap
    script.append(("exit", 0))
    fake_docker.stream_exec_scripts.append(script)

    r = authed.post(
        f"/v1/sessions/{sid}/exec/stream",
        json={"argv": ["yes"]},
    )
    events = _parse_sse(r.text)
    kinds = [k for k, _ in events]
    # Exactly one truncated event for stdout — once announced, no repeats.
    assert kinds.count("truncated") == 1
    truncated_event = next(d for k, d in events if k == "truncated")
    assert truncated_event["stream"] == "stdout"
    result = next(d for k, d in events if k == "result")
    assert result["truncated"] is True
    assert "stdout" in result["truncated_streams"]


def test_stream_rejects_stdin_with_400(authed):
    sid = _create(authed)
    r = authed.post(
        f"/v1/sessions/{sid}/exec/stream",
        json={"argv": ["cat"], "stdin": "hello"},
    )
    # The pre-flight validate_stream_request runs synchronously, so
    # InvalidArgument propagates as a clean 400 before any SSE bytes go out.
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "invalid_argument"


# ----- stdin on synchronous /exec -----


def test_exec_with_stdin_passes_bytes(authed, fake_docker):
    sid = _create(authed)
    fake_docker.exec_responses.append(
        ExecOutput(stdout=b"echoed\n", stderr=b"", exit_code=0, duration_ms=8)
    )
    r = authed.post(
        f"/v1/sessions/{sid}/exec",
        json={"argv": ["cat"], "stdin": "echoed\n"},
    )
    assert r.status_code == 200
    assert r.json()["stdout"] == "echoed\n"
    call = fake_docker.exec_calls[0]
    assert call["stdin_bytes"] == b"echoed\n"


def test_exec_stdin_size_limit(authed):
    sid = _create(authed)
    big = "x" * (1 * 1024 * 1024 + 1)  # 1 MiB + 1 byte
    r = authed.post(
        f"/v1/sessions/{sid}/exec",
        json={"argv": ["cat"], "stdin": big},
    )
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "invalid_argument"
