"""Tests for the file I/O endpoints (slice 2). SPEC-107."""

import base64

import pytest

from api.files import resolve_workspace_path


def _create(authed) -> str:
    return authed.post("/v1/sessions", json={}).json()["session_id"]


# ----- path validation (unit) -----


def test_resolve_simple_relative():
    assert resolve_workspace_path("foo.txt") == "/workspace/foo.txt"
    assert resolve_workspace_path("a/b/c") == "/workspace/a/b/c"


def test_resolve_normalizes_internal_dotdot_within_workspace():
    assert resolve_workspace_path("a/b/../c") == "/workspace/a/c"


def test_resolve_rejects_traversal():
    from api.errors import InvalidPath

    with pytest.raises(InvalidPath):
        resolve_workspace_path("../etc/passwd")
    with pytest.raises(InvalidPath):
        resolve_workspace_path("a/../../etc/passwd")


def test_resolve_rejects_absolute():
    from api.errors import InvalidPath

    with pytest.raises(InvalidPath):
        resolve_workspace_path("/etc/passwd")


def test_resolve_rejects_workspace_root_unless_allowed():
    from api.errors import InvalidPath

    with pytest.raises(InvalidPath):
        resolve_workspace_path(".")
    # `.` resolves to /workspace, which is rejected unless explicitly allowed.
    assert resolve_workspace_path(".", allow_workspace_root=True) == "/workspace"


def test_resolve_rejects_empty_and_nul():
    from api.errors import InvalidPath

    with pytest.raises(InvalidPath):
        resolve_workspace_path("")
    with pytest.raises(InvalidPath):
        resolve_workspace_path("a\x00b")


# ----- write -----


def test_write_happy_path(authed, fake_docker):
    sid = _create(authed)
    body = base64.b64encode(b"hello world").decode()
    r = authed.post(
        f"/v1/sessions/{sid}/files",
        json={"path": "greeting.txt", "content_b64": body, "mode": 0o644},
    )
    assert r.status_code == 201, r.text
    assert r.json()["path"] == "/workspace/greeting.txt"
    assert r.json()["size"] == 11

    call = fake_docker.put_archive_calls[0]
    assert call["abs_path"] == "/workspace/greeting.txt"
    assert call["content"] == b"hello world"
    assert call["mode"] == 0o644


def test_write_creates_nested_path(authed, fake_docker):
    """Issue #2 reproducer: a nested path like `sub/keep.txt` should
    succeed because put_archive_file's mkdir -p creates the parent
    directory. Previously this 500'd because /workspace was unwritable
    to the agent (issue #3); the fixture's fake_docker just records the
    call, so what we verify here is that the request actually reaches
    put_archive_file with the correct abs_path and content."""
    sid = _create(authed)
    body = base64.b64encode(b"keep").decode()
    r = authed.post(
        f"/v1/sessions/{sid}/files",
        json={"path": "sub/keep.txt", "content_b64": body, "mode": 0o644},
    )
    assert r.status_code == 201, r.text
    assert r.json()["path"] == "/workspace/sub/keep.txt"
    call = fake_docker.put_archive_calls[-1]
    assert call["abs_path"] == "/workspace/sub/keep.txt"
    assert call["content"] == b"keep"


def test_write_raw_path_in_url(authed, fake_docker):
    """POST /v1/sessions/{sid}/files/{path:path} accepts raw bytes
    via application/octet-stream and is symmetric with GET/DELETE."""
    sid = _create(authed)
    r = authed.post(
        f"/v1/sessions/{sid}/files/path/from/url.txt?mode=420",
        content=b"raw bytes \x00\xff",
        headers={"Content-Type": "application/octet-stream"},
    )
    assert r.status_code == 201, r.text
    assert r.json()["path"] == "/workspace/path/from/url.txt"
    assert r.json()["size"] == 12
    call = fake_docker.put_archive_calls[-1]
    assert call["abs_path"] == "/workspace/path/from/url.txt"
    assert call["content"] == b"raw bytes \x00\xff"
    assert call["mode"] == 0o644


def test_write_raw_rejects_traversal(authed):
    sid = _create(authed)
    r = authed.post(
        f"/v1/sessions/{sid}/files/../etc/passwd",
        content=b"x",
        headers={"Content-Type": "application/octet-stream"},
    )
    # Starlette normalises `..` segments before routing; the request
    # either fails to route (404) or hits the path-validator (400).
    # Either is correct — the workspace must NOT be escaped.
    assert r.status_code in (400, 404)


def test_write_rejects_path_traversal(authed):
    sid = _create(authed)
    r = authed.post(
        f"/v1/sessions/{sid}/files",
        json={"path": "../etc/passwd", "content_b64": ""},
    )
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "invalid_path"


def test_write_rejects_absolute_path(authed):
    sid = _create(authed)
    r = authed.post(
        f"/v1/sessions/{sid}/files",
        json={"path": "/etc/passwd", "content_b64": ""},
    )
    assert r.status_code == 400


def test_write_rejects_invalid_base64(authed):
    sid = _create(authed)
    r = authed.post(
        f"/v1/sessions/{sid}/files",
        json={"path": "f.bin", "content_b64": "!!!not base64!!!"},
    )
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "invalid_argument"


# ----- read -----


def test_read_happy_path(authed, fake_docker):
    sid = _create(authed)
    fake_docker.get_archive_responses["/workspace/data.bin"] = (
        b"\x00\x01\x02binary\xffend",
        0o640,
    )
    r = authed.get(f"/v1/sessions/{sid}/files/data.bin")
    assert r.status_code == 200
    assert r.content == b"\x00\x01\x02binary\xffend"
    assert r.headers["content-type"].startswith("application/octet-stream")
    assert r.headers["X-File-Mode"] == oct(0o640)


def test_read_missing_returns_404(authed):
    sid = _create(authed)
    r = authed.get(f"/v1/sessions/{sid}/files/nope.txt")
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "file_not_found"


# ----- list -----


def test_list_happy_path(authed, fake_docker):
    sid = _create(authed)
    # `find` output emulating a workspace with two files and one subdir.
    out = b"f\t11\t100644\tgreeting.txt\nd\t4096\t40755\tsubdir\n"
    fake_docker.simple_exec_handler = lambda argv: (
        (out, b"", 0) if "find" in argv[0] else (b"", b"", 0)
    )
    r = authed.get(f"/v1/sessions/{sid}/files")
    assert r.status_code == 200, r.text
    entries = r.json()["entries"]
    assert {e["name"] for e in entries} == {"greeting.txt", "subdir"}
    assert next(e for e in entries if e["name"] == "subdir")["is_dir"] is True
    assert next(e for e in entries if e["name"] == "greeting.txt")["size"] == 11


def test_list_missing_dir_returns_404(authed, fake_docker):
    sid = _create(authed)
    fake_docker.simple_exec_handler = lambda argv: (
        (b"", b"find: '/workspace/missing': No such file or directory\n", 1)
        if "find" in argv[0]
        else (b"", b"", 0)
    )
    r = authed.get(f"/v1/sessions/{sid}/files?dir=missing")
    assert r.status_code == 404


# ----- delete -----


def test_delete_file_happy_path(authed, fake_docker):
    sid = _create(authed)
    # test -e: ok (0); test -d: not a dir (1); rm: ok (0)
    responses: list[tuple[bytes, bytes, int]] = [
        (b"", b"", 0),  # test -e
        (b"", b"", 1),  # test -d (file, not dir)
        (b"", b"", 0),  # rm
    ]

    def handler(_argv: list[str]) -> tuple[bytes, bytes, int]:
        return responses.pop(0)

    fake_docker.simple_exec_handler = handler
    r = authed.delete(f"/v1/sessions/{sid}/files/greeting.txt")
    assert r.status_code == 204


def test_delete_missing_returns_404(authed, fake_docker):
    sid = _create(authed)
    fake_docker.simple_exec_handler = lambda argv: (b"", b"", 1)  # test -e fails
    r = authed.delete(f"/v1/sessions/{sid}/files/missing.txt")
    assert r.status_code == 404


def test_delete_directory_without_recursive_is_400(authed, fake_docker):
    sid = _create(authed)
    responses: list[tuple[bytes, bytes, int]] = [
        (b"", b"", 0),  # test -e
        (b"", b"", 0),  # test -d (it IS a dir)
    ]
    fake_docker.simple_exec_handler = lambda _argv: responses.pop(0)
    r = authed.delete(f"/v1/sessions/{sid}/files/subdir")
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "invalid_argument"


def test_delete_directory_with_recursive(authed, fake_docker):
    sid = _create(authed)
    responses: list[tuple[bytes, bytes, int]] = [
        (b"", b"", 0),  # test -e
        (b"", b"", 0),  # test -d
        (b"", b"", 0),  # rm -rf
    ]
    fake_docker.simple_exec_handler = lambda _argv: responses.pop(0)
    r = authed.delete(f"/v1/sessions/{sid}/files/subdir?recursive=true")
    assert r.status_code == 204


# Note: deleting /workspace itself via DELETE /v1/sessions/{id}/files/<path>
# can't actually be expressed in the URL — the HTTP layer collapses `.` and
# `..` segments before routing, so the path either matches the list route or
# fails to route at all. The defense-in-depth check lives in
# `resolve_workspace_path`, exercised by
# `test_resolve_rejects_workspace_root_unless_allowed` above.


# ----- transparent resume (mirrors /exec, SPEC-104) -----


def test_write_transparently_resumes_stopped_session(authed, fake_docker):
    sid = _create(authed)
    authed.post(f"/v1/sessions/{sid}/stop")
    pre_starts = len(fake_docker.started)

    body = base64.b64encode(b"hello").decode()
    r = authed.post(
        f"/v1/sessions/{sid}/files",
        json={"path": "g.txt", "content_b64": body},
    )
    assert r.status_code == 201, r.text
    # File ops should auto-resume just like exec does.
    assert len(fake_docker.started) == pre_starts + 1
    assert authed.get(f"/v1/sessions/{sid}").json()["status"] == "RUNNING"


def test_read_transparently_resumes_stopped_session(authed, fake_docker):
    sid = _create(authed)
    fake_docker.get_archive_responses["/workspace/data.bin"] = (b"hello", 0o640)
    authed.post(f"/v1/sessions/{sid}/stop")
    pre_starts = len(fake_docker.started)

    r = authed.get(f"/v1/sessions/{sid}/files/data.bin")
    assert r.status_code == 200
    assert r.content == b"hello"
    assert len(fake_docker.started) == pre_starts + 1
