from fastapi import HTTPException


class SandboxError(HTTPException):
    code: str = "internal_error"

    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(status_code=status_code, detail={"code": code, "message": message})
        self.code = code


class SessionNotFound(SandboxError):
    def __init__(self) -> None:
        super().__init__(404, "session_not_found", "session not found")


class InvalidState(SandboxError):
    def __init__(self, message: str) -> None:
        super().__init__(409, "invalid_state", message)


class LimitExceeded(SandboxError):
    def __init__(self, message: str) -> None:
        super().__init__(429, "limit_exceeded", message)


class InvalidArgument(SandboxError):
    def __init__(self, message: str) -> None:
        super().__init__(400, "invalid_argument", message)


class Unauthorized(SandboxError):
    def __init__(self) -> None:
        super().__init__(401, "unauthorized", "missing or invalid token")
