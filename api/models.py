from typing import Literal

from pydantic import BaseModel, Field

SessionStatus = Literal["CREATING", "RUNNING", "IDLE", "STOPPED", "DESTROYING", "DESTROYED"]


class Limits(BaseModel):
    vcpu: int = Field(2, ge=1)
    memory_mib: int = Field(2048, ge=64)
    workspace_mib: int = Field(1024, ge=64)
    pids: int = Field(256, ge=16)
    nofile: int = Field(1024, ge=64)
    exec_timeout_s: int = Field(60, ge=1)


class CreateSessionRequest(BaseModel):
    limits: Limits | None = None


class SessionResponse(BaseModel):
    session_id: str
    status: SessionStatus
    tenant_id: str
    limits: Limits
    created_at: int
    last_activity_at: int
