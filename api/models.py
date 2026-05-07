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
    workspace_mib: int = Field(
        1024,
        ge=64,
        description=(
            "Per-session hard cap on /workspace usage, in MiB. "
            "Enforced by XFS prjquota when SANDBOX_VOLUME_BASE is on "
            "an XFS or ext4+prjquota filesystem; advisory on network "
            "storage. Pair with the coarser tenant-level "
            "TenantLimits.max_workspace_gib (in GiB) — the two units "
            "are deliberate."
        ),
    )
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
    # Slice 11c — cross-cutting contract sharpening. Lets clients size
    # buffers without hardcoding the SPEC-203 8 MiB cap.
    effective_truncation_cap_bytes: int = Field(
        default=8 * 1024 * 1024,
        description="Per-stream byte cap used for stdout/stderr truncation.",
    )
    # Time spent auto-resuming a STOPPED session before this exec ran;
    # 0 when the session was already RUNNING. Lets clients tell whether
    # a slow exec was queue / process work vs. resume.
    resume_latency_ms: int = Field(default=0, ge=0)


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
# Internal-only restart policy. The DB column persists this value;
# user-facing requests don't carry it because the v1 supervisor only
# implements `never`. Exposing a Literal-of-one as a request field
# looked like a closed enum but rejected every other value, which
# misled the e2e consumer team. `on_failure` / `always` are reserved
# for a follow-up that grows restart logic on the supervisor.
RestartPolicy = Literal["never"]


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


class ProcessResponse(BaseModel):
    process_id: str
    name: str | None
    argv: list[str] = Field(
        description=(
            "WARNING: argv is visible to any token holding the "
            "`processes` scope (via process_list / process_get). "
            "Pass credentials via `env`, never as positional args. "
            "Operators handling sensitive workloads should issue "
            "tokens without `processes` scope."
        ),
    )
    state: ProcessState
    exit_code: int | None
    started_at: int
    exited_at: int | None
    last_output_at: int | None


class ProcessListResponse(BaseModel):
    entries: list[ProcessResponse]


# ----- tenant management + scoped tokens (slice 12) -----


# The closed set of scopes a token can carry. NULL / empty in storage
# means "all scopes" (back-compat for tokens issued before slice 12).
Scope = Literal[
    "session_create",
    "session_destroy",
    "exec",
    "file_read",
    "file_write",
    "file_delete",
    "processes",
    "tokens_rotate",
]


ALL_SCOPES: tuple[str, ...] = (
    "session_create",
    "session_destroy",
    "exec",
    "file_read",
    "file_write",
    "file_delete",
    "processes",
    "tokens_rotate",
)


class TenantLimits(BaseModel):
    """Per-tenant overrides for the global Settings.tenant_max_*
    defaults. Each field None ⇒ inherit the global default."""

    max_concurrency: int | None = Field(default=None, ge=1)
    max_workspace_gib: int | None = Field(
        default=None,
        ge=1,
        description=(
            "Tenant-level coarse cap on workspace usage, in GiB. "
            "Used for policy / governance at the tenant tier. "
            "Per-session caps live on Limits.workspace_mib (in MiB) "
            "for finer granularity — the two units are deliberate."
        ),
    )
    max_exec_timeout_s: int | None = Field(default=None, ge=1)


class CreateTenantRequest(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    display_name: str | None = Field(default=None, max_length=256)
    limits: TenantLimits | None = None
    # Per-tenant Squid allowlist. Slice-12 ACCEPTS the field but does
    # NOT enforce it yet — Squid's runtime allowlist injection is a
    # follow-up. Stored verbatim for now so operators can pre-populate.
    egress_allowlist: list[str] | None = None


class UpdateTenantRequest(BaseModel):
    display_name: str | None = Field(default=None, max_length=256)
    limits: TenantLimits | None = None
    egress_allowlist: list[str] | None = None


class TenantResponse(BaseModel):
    tenant_id: str
    display_name: str
    created_at: int
    limits: TenantLimits
    egress_allowlist: list[str] | None
    active_token_count: int


class TenantListResponse(BaseModel):
    entries: list[TenantResponse]


class IssueTokenRequest(BaseModel):
    scopes: list[Scope] | None = Field(
        default=None,
        description=(
            "Optional list of scopes this token may use. None / omitted "
            "= grant all scopes (back-compat default). Empty list = "
            "explicitly no scopes (token can only call routes that "
            "require no scope, useful for read-only health probes)."
        ),
    )
    note: str | None = Field(
        default=None,
        max_length=128,
        description="Operator-friendly note attached to the token.",
    )


class IssueTokenResponse(BaseModel):
    token_id: str
    token: str = Field(
        description=(
            "Plaintext bearer token. Saved nowhere by the service; the operator must record it now."
        ),
    )
    tenant_id: str
    scopes: list[Scope] | None
    issued_at: int


class TokenInfo(BaseModel):
    token_id: str
    tenant_id: str
    scopes: list[Scope] | None
    issued_at: int
    revoked_at: int | None
    note: str | None


class TokenListResponse(BaseModel):
    entries: list[TokenInfo]


class TenantUsageResponse(BaseModel):
    tenant_id: str
    concurrent_sessions: int
    max_concurrency: int
    workspace_bytes: int | None  # None if quota tracking unavailable
    active_token_count: int


class DeleteTenantResponse(BaseModel):
    tenant_id: str
    sessions_destroyed: int
    tokens_revoked: int


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
