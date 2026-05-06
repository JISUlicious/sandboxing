"""File I/O endpoints (slice 2). SPEC-107.

Path validation is string-based for slice 2 (rejects traversal, absolute
paths, and `..` segments after normalization). Symlink-aware realpath
resolution inside the container is a slice 4 hardening item.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import posixpath
import time
from dataclasses import dataclass

from fastapi import HTTPException

from api import metrics
from api.audit import AuditEmitter
from api.docker_client import DockerClient
from api.errors import InvalidArgument, InvalidPath, InvalidState, SessionNotFound
from api.models import FileEntry, FileListResponse, FileWriteRequest
from api.registry import Registry

log = logging.getLogger("sandbox.files")

WORKSPACE = "/workspace"


def resolve_workspace_path(rel: str, *, allow_workspace_root: bool = False) -> str:
    """Validate `rel` and return the canonical absolute path.

    Rejects:
    - empty paths
    - absolute paths (must be relative to /workspace)
    - any `..` segment that would escape /workspace after normalization
    - paths containing NUL bytes
    """
    if not rel or "\x00" in rel:
        raise InvalidPath(
            "path is required and must not contain NUL",
            sub_code="null_or_required",
        )
    if rel.startswith("/"):
        raise InvalidPath("path must be relative to /workspace", sub_code="absolute_path")
    abs_path = posixpath.normpath(posixpath.join(WORKSPACE, rel))
    if abs_path == WORKSPACE:
        if allow_workspace_root:
            return abs_path
        raise InvalidPath("path may not refer to /workspace itself", sub_code="workspace_root")
    if not abs_path.startswith(WORKSPACE + "/"):
        raise InvalidPath("path escapes /workspace", sub_code="escaped_workspace")
    return abs_path


@dataclass
class _Session:
    id: str
    container_id: str


class FileService:
    def __init__(
        self,
        *,
        registry: Registry,
        docker: DockerClient,
        audit: AuditEmitter,
    ) -> None:
        self.registry = registry
        self.docker = docker
        self.audit = audit

    async def write(
        self, session_id: str, tenant_id: str, req: FileWriteRequest
    ) -> dict[str, object]:
        self.audit.precheck()
        session = await self._require_running(session_id, tenant_id)
        abs_path = resolve_workspace_path(req.path)
        try:
            content = base64.b64decode(req.content_b64, validate=True)
        except (ValueError, TypeError) as exc:
            raise InvalidArgument(f"content_b64 is not valid base64: {exc}") from exc

        await asyncio.to_thread(
            self.docker.put_archive_file,
            container_id=session.container_id,
            abs_path=abs_path,
            content=content,
            mode=req.mode,
        )
        await self.audit.emit(
            kind="session.file.write",
            tenant=tenant_id,
            session=session_id,
            payload={"path": abs_path, "size": len(content), "mode": req.mode},
        )
        return {"path": abs_path, "size": len(content), "mode": req.mode}

    async def read(self, session_id: str, tenant_id: str, rel_path: str) -> tuple[bytes, int]:
        session = await self._require_running(session_id, tenant_id)
        abs_path = resolve_workspace_path(rel_path)
        try:
            return await asyncio.to_thread(
                self.docker.get_archive_file,
                container_id=session.container_id,
                abs_path=abs_path,
            )
        except FileNotFoundError as exc:
            raise HTTPException(
                status_code=404,
                detail={"code": "file_not_found", "message": str(exc)},
            ) from exc
        except IsADirectoryError as exc:
            raise InvalidArgument("path is a directory; use list") from exc

    async def list_dir(self, session_id: str, tenant_id: str, rel_dir: str) -> FileListResponse:
        session = await self._require_running(session_id, tenant_id)
        if rel_dir == "" or rel_dir == ".":
            abs_path = WORKSPACE
        else:
            abs_path = resolve_workspace_path(rel_dir, allow_workspace_root=True)

        out, err, rc = await asyncio.to_thread(
            self.docker._exec_simple,
            session.container_id,
            [
                "/usr/bin/find",
                abs_path,
                "-mindepth",
                "1",
                "-maxdepth",
                "1",
                "-printf",
                "%y\\t%s\\t%m\\t%f\\n",
            ],
        )
        if rc != 0:
            stderr = err.decode("utf-8", errors="replace")
            if "No such file or directory" in stderr:
                raise HTTPException(
                    status_code=404,
                    detail={"code": "file_not_found", "message": abs_path},
                )
            raise HTTPException(
                status_code=500,
                detail={"code": "internal_error", "message": stderr.strip() or "list failed"},
            )

        entries: list[FileEntry] = []
        for line in out.decode("utf-8", errors="replace").splitlines():
            parts = line.split("\t")
            if len(parts) != 4:
                continue
            ftype, size, mode, name = parts
            entries.append(
                FileEntry(
                    name=name,
                    is_dir=(ftype == "d"),
                    size=int(size),
                    mode=int(mode, 8),
                )
            )
        return FileListResponse(entries=entries)

    async def delete(
        self,
        session_id: str,
        tenant_id: str,
        rel_path: str,
        *,
        recursive: bool,
    ) -> None:
        self.audit.precheck()
        session = await self._require_running(session_id, tenant_id)
        # SPEC-107: /workspace itself cannot be deleted.
        abs_path = resolve_workspace_path(rel_path)

        # Stat to decide between rm and rmdir behavior + 404.
        _, err, rc = await asyncio.to_thread(
            self.docker._exec_simple,
            session.container_id,
            ["/usr/bin/test", "-e", abs_path],
        )
        if rc != 0:
            raise HTTPException(
                status_code=404,
                detail={"code": "file_not_found", "message": abs_path},
            )
        _, err, rc = await asyncio.to_thread(
            self.docker._exec_simple,
            session.container_id,
            ["/usr/bin/test", "-d", abs_path],
        )
        is_dir = rc == 0
        if is_dir and not recursive:
            raise InvalidArgument("path is a directory; use ?recursive=true to delete")

        cmd = ["/bin/rm", "-rf" if recursive else "-f", "--", abs_path]
        _, err, rc = await asyncio.to_thread(self.docker._exec_simple, session.container_id, cmd)
        if rc != 0:
            raise HTTPException(
                status_code=500,
                detail={
                    "code": "internal_error",
                    "message": err.decode("utf-8", errors="replace").strip() or "rm failed",
                },
            )
        await self.audit.emit(
            kind="session.file.delete",
            tenant=tenant_id,
            session=session_id,
            payload={"path": abs_path, "recursive": recursive},
        )

    async def _require_running(self, session_id: str, tenant_id: str) -> _Session:
        """Resolve the session, transparently resuming if STOPPED / IDLE.

        Mirrors ExecService._prepare so file ops have the same multi-turn
        contract as exec (SPEC-104, ARCH §3.2). Without this, a long-lived
        agent that lets the reaper idle-stop a session would have to
        explicitly /resume before any file I/O — surfacing lifecycle state
        the agent shouldn't need to track.
        """
        session = await self.registry.get(session_id, tenant_id)
        if session is None:
            raise SessionNotFound()
        if session.status not in ("RUNNING", "IDLE", "STOPPED"):
            raise InvalidState(f"cannot operate on files in session status {session.status}")
        if session.status in ("STOPPED", "IDLE"):
            assert session.container_id is not None
            start_ns = time.monotonic_ns()
            await asyncio.to_thread(self.docker.start_container, session.container_id)
            await self.registry.transition(session_id, "RUNNING")
            metrics.resume_seconds.observe((time.monotonic_ns() - start_ns) / 1_000_000_000)
            session = await self.registry.get(session_id, tenant_id)
            assert session is not None
        assert session.container_id is not None
        return _Session(id=session.id, container_id=session.container_id)
