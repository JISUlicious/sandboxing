from fastapi import HTTPException


class SandboxError(HTTPException):
    code: str = "internal_error"

    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(status_code=status_code, detail={"code": code, "message": message})
        self.code = code


class SessionNotFound(SandboxError):
    def __init__(self) -> None:
        super().__init__(404, "session_not_found", "session not found")


class ProcessNotFound(SandboxError):
    """Process row not found in the session (or already deleted).
    Mirrors SessionNotFound — 404 + a stable structured code, instead
    of the previous 400 invalid_argument which mis-categorised "this
    resource doesn't exist" as "the request was malformed"."""

    def __init__(self, process_id: str | None = None) -> None:
        msg = f"process not found: {process_id}" if process_id else "process not found"
        super().__init__(404, "process_not_found", msg)


class InvalidState(SandboxError):
    def __init__(self, message: str) -> None:
        super().__init__(409, "invalid_state", message)


class LimitExceeded(SandboxError):
    """Operator/system limit reached at request time — concurrency
    cap, per-session process cap, etc. 429 because retrying later
    might succeed."""

    def __init__(self, message: str) -> None:
        super().__init__(429, "limit_exceeded", message)


class InvalidLimits(SandboxError):
    """Per-field limits in the *request* exceed tenant caps. 400
    because the input itself is invalid — retrying with the same
    body won't help. SPEC-100. Same `limit_exceeded` code as
    LimitExceeded so existing client error handling still matches;
    the status code is what differs."""

    def __init__(self, message: str) -> None:
        super().__init__(400, "limit_exceeded", message)


class InvalidArgument(SandboxError):
    def __init__(self, message: str) -> None:
        super().__init__(400, "invalid_argument", message)


class Unauthorized(SandboxError):
    def __init__(self) -> None:
        super().__init__(401, "unauthorized", "missing or invalid token")


class ExecTimeout(SandboxError):
    def __init__(self) -> None:
        super().__init__(408, "exec_timeout", "exec exceeded its wall-clock timeout")


class InvalidPath(SandboxError):
    """Slice 11c: optional `sub_code` distinguishes the failure mode so
    clients can render specific error messages without parsing
    `message`. Sub-codes:
      - `null_or_required` — path missing or contained NUL.
      - `absolute_path` — path started with `/`.
      - `escaped_workspace` — path used `..` to leave /workspace.
      - `workspace_root` — path resolved to /workspace itself.
    Setting `code` overall stays `invalid_path` for back-compat;
    `sub_code` lives in `detail`.
    """

    def __init__(
        self,
        message: str = "path is not within /workspace",
        *,
        sub_code: str | None = None,
    ) -> None:
        super().__init__(400, "invalid_path", message)
        if sub_code is not None:
            assert isinstance(self.detail, dict)
            self.detail["sub_code"] = sub_code


class AuditUnhealthy(SandboxError):
    """Returned when the audit log is failing and we're refusing to add
    new mutations until it's reconciled. ARCH §7 fail-closed."""

    def __init__(self) -> None:
        super().__init__(
            503,
            "audit_unhealthy",
            "audit log is unhealthy; refusing new mutations until reconciled",
        )
