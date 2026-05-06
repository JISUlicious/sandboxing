from typing import Literal

from pydantic import BaseModel, Field

SessionStatus = Literal["CREATING", "RUNNING", "IDLE", "STOPPED", "DESTROYING", "DESTROYED"]


# ----- error envelope (every 4xx / 5xx body in the API) -----


class ErrorDetail(BaseModel):
    code: str = Field(
        description="Stable machine-readable error code. See SPEC §9.",
        examples=["session_not_found"],
    )
    message: str = Field(description="Human-readable explanation; safe to log.")


class ErrorResponse(BaseModel):
    detail: ErrorDetail


class Limits(BaseModel):
    vcpu: int = Field(2, ge=1)
    memory_mib: int = Field(2048, ge=64)
    workspace_mib: int = Field(1024, ge=64)
    pids: int = Field(256, ge=16)
    nofile: int = Field(1024, ge=64)
    exec_timeout_s: int = Field(60, ge=1)
    # Slice 11b — concurrent background processes per session. Tenant
    # max in Settings.tenant_max_processes (default 32).
    max_processes: int = Field(8, ge=0)


class CreateSessionRequest(BaseModel):
    limits: Limits | None = None


class SessionResponse(BaseModel):
    session_id: str
    status: SessionStatus
    tenant_id: str
    limits: Limits
    created_at: int
    last_activity_at: int


# ----- exec (SPEC-201) -----


class ExecRequest(BaseModel):
    argv: list[str] = Field(min_length=1)
    stdin: str | None = None
    timeout_s: int | None = Field(default=None, ge=1)
    env: dict[str, str] | None = None


class ExecResponse(BaseModel):
    stdout: str
    stderr: str
    exit_code: int
    duration_ms: int
    effective_timeout_s: int
    truncated: bool = False
    truncated_streams: list[str] = Field(default_factory=list)


# ----- files (SPEC-107) -----


class FileWriteRequest(BaseModel):
    path: str = Field(min_length=1)
    # Content is base64-encoded for binary safety; UTF-8 text fits too.
    content_b64: str
    mode: int = Field(default=0o640, ge=0, le=0o777)


class FileEntry(BaseModel):
    name: str
    is_dir: bool
    size: int
    mode: int


class FileListResponse(BaseModel):
    entries: list[FileEntry]


# ----- tenants + tokens (SPEC-405) -----


# ----- background processes (slice 11b) -----


ProcessState = Literal["RUNNING", "EXITED"]
RestartPolicy = Literal["never"]
# `on_failure` / `always` are reserved for a follow-up — slice 11b
# only ships `never` so the supervisor doesn't grow restart logic on
# the critical path.


class StartProcessRequest(BaseModel):
    argv: list[str] = Field(min_length=1)
    name: str | None = Field(
        default=None,
        max_length=64,
        description="Operator-friendly label for the process. Free-form.",
    )
    env: dict[str, str] | None = None
    cwd: str | None = Field(
        default=None,
        description="Working directory inside /workspace. Defaults to /workspace.",
    )
    restart_policy: RestartPolicy = "never"


class ProcessResponse(BaseModel):
    process_id: str
    name: str | None
    argv: list[str]
    state: ProcessState
    exit_code: int | None
    started_at: int
    exited_at: int | None
    last_output_at: int | None


class ProcessListResponse(BaseModel):
    entries: list[ProcessResponse]


class RotateTokenResponse(BaseModel):
    token: str = Field(
        description=(
            "The new bearer token in plaintext. Save it now; the API "
            "never returns it again. The previous token continues to "
            "authenticate for `old_token_grace_seconds` so callers "
            "have time to switch."
        ),
    )
    old_token_grace_seconds: int = Field(
        description="How long the previous token continues to authenticate.",
    )
    tenant_id: str
