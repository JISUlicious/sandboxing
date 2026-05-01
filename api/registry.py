"""SQLite-backed session registry. ARCH-010, ARCH-051 step ordering."""

from __future__ import annotations

import json
import time
from collections.abc import Sequence
from pathlib import Path

import aiosqlite

from api.models import Limits, SessionStatus

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id              TEXT PRIMARY KEY,
    tenant_id       TEXT NOT NULL,
    status          TEXT NOT NULL,
    container_id    TEXT,
    volume_name     TEXT NOT NULL,
    limits_json     TEXT NOT NULL,
    created_at      INTEGER NOT NULL,
    last_activity_at INTEGER NOT NULL,
    destroyed_at    INTEGER
);
CREATE INDEX IF NOT EXISTS idx_sessions_tenant_status ON sessions(tenant_id, status);
CREATE INDEX IF NOT EXISTS idx_sessions_activity ON sessions(last_activity_at);
"""

# Allowed transitions per ARCH §5.
TRANSITIONS: dict[str, set[str]] = {
    "CREATING": {"RUNNING", "DESTROYING"},
    "RUNNING": {"IDLE", "STOPPED", "DESTROYING"},
    "IDLE": {"RUNNING", "STOPPED", "DESTROYING"},
    "STOPPED": {"RUNNING", "DESTROYING"},
    "DESTROYING": {"DESTROYED"},
    "DESTROYED": set(),
}


def now_ms() -> int:
    return int(time.time() * 1000)


class SessionRow:
    __slots__ = (
        "id",
        "tenant_id",
        "status",
        "container_id",
        "volume_name",
        "limits",
        "created_at",
        "last_activity_at",
        "destroyed_at",
    )

    def __init__(self, **kw: object) -> None:
        self.id: str = kw["id"]  # type: ignore[assignment]
        self.tenant_id: str = kw["tenant_id"]  # type: ignore[assignment]
        self.status: SessionStatus = kw["status"]  # type: ignore[assignment]
        self.container_id: str | None = kw.get("container_id")  # type: ignore[assignment]
        self.volume_name: str = kw["volume_name"]  # type: ignore[assignment]
        self.limits: Limits = kw["limits"]  # type: ignore[assignment]
        self.created_at: int = kw["created_at"]  # type: ignore[assignment]
        self.last_activity_at: int = kw["last_activity_at"]  # type: ignore[assignment]
        self.destroyed_at: int | None = kw.get("destroyed_at")  # type: ignore[assignment]


def _row_to_session(row: aiosqlite.Row) -> SessionRow:
    return SessionRow(
        id=row["id"],
        tenant_id=row["tenant_id"],
        status=row["status"],
        container_id=row["container_id"],
        volume_name=row["volume_name"],
        limits=Limits(**json.loads(row["limits_json"])),
        created_at=row["created_at"],
        last_activity_at=row["last_activity_at"],
        destroyed_at=row["destroyed_at"],
    )


class Registry:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    async def init(self) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.executescript(SCHEMA)
            await db.commit()

    async def insert(
        self,
        *,
        session_id: str,
        tenant_id: str,
        volume_name: str,
        limits: Limits,
    ) -> SessionRow:
        ts = now_ms()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO sessions "
                "(id, tenant_id, status, container_id, volume_name, "
                " limits_json, created_at, last_activity_at) "
                "VALUES (?, ?, 'CREATING', NULL, ?, ?, ?, ?)",
                (session_id, tenant_id, volume_name, limits.model_dump_json(), ts, ts),
            )
            await db.commit()
        return SessionRow(
            id=session_id,
            tenant_id=tenant_id,
            status="CREATING",
            container_id=None,
            volume_name=volume_name,
            limits=limits,
            created_at=ts,
            last_activity_at=ts,
            destroyed_at=None,
        )

    async def set_container(self, session_id: str, container_id: str) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE sessions SET container_id = ? WHERE id = ?",
                (container_id, session_id),
            )
            await db.commit()

    async def transition(self, session_id: str, new_status: SessionStatus) -> None:
        ts = now_ms()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT status FROM sessions WHERE id = ?", (session_id,))
            row = await cur.fetchone()
            if row is None:
                raise KeyError(session_id)
            current = row["status"]
            if new_status not in TRANSITIONS.get(current, set()):
                raise ValueError(f"invalid transition {current} -> {new_status} for {session_id}")
            if new_status == "DESTROYED":
                await db.execute(
                    "UPDATE sessions SET status = ?, destroyed_at = ?, "
                    "last_activity_at = ? WHERE id = ?",
                    (new_status, ts, ts, session_id),
                )
            else:
                await db.execute(
                    "UPDATE sessions SET status = ?, last_activity_at = ? WHERE id = ?",
                    (new_status, ts, session_id),
                )
            await db.commit()

    async def get_unscoped(self, session_id: str) -> SessionRow | None:
        """Tenant-agnostic lookup used by the reaper. Returns DESTROYED rows
        too — the reaper consults `status` itself."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM sessions WHERE id = ?", (session_id,))
            row = await cur.fetchone()
            return _row_to_session(row) if row else None

    async def get(self, session_id: str, tenant_id: str) -> SessionRow | None:
        """Returns None if not found, not owned, or DESTROYED (SPEC-200)."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM sessions WHERE id = ? AND tenant_id = ?",
                (session_id, tenant_id),
            )
            row = await cur.fetchone()
            if row is None:
                return None
            session = _row_to_session(row)
            if session.status == "DESTROYED":
                return None
            return session

    async def list_pending_destroy(self) -> Sequence[SessionRow]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM sessions WHERE status = 'DESTROYING'")
            rows = await cur.fetchall()
            return [_row_to_session(r) for r in rows]

    async def count_concurrent(self, tenant_id: str) -> int:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT COUNT(*) AS n FROM sessions "
                "WHERE tenant_id = ? AND status NOT IN ('DESTROYING', 'DESTROYED')",
                (tenant_id,),
            )
            row = await cur.fetchone()
            return int(row["n"]) if row else 0

    async def list_idle_running(self, idle_threshold_ms: int) -> Sequence[SessionRow]:
        """Sessions in RUNNING/IDLE that haven't seen activity since the threshold."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM sessions "
                "WHERE status IN ('RUNNING', 'IDLE') AND last_activity_at < ?",
                (idle_threshold_ms,),
            )
            rows = await cur.fetchall()
            return [_row_to_session(r) for r in rows]

    async def list_expired(self, ttl_threshold_ms: int) -> Sequence[SessionRow]:
        """Sessions older than the hard-destroy TTL, excluding terminal states."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM sessions "
                "WHERE status NOT IN ('DESTROYING', 'DESTROYED') AND created_at < ?",
                (ttl_threshold_ms,),
            )
            rows = await cur.fetchall()
            return [_row_to_session(r) for r in rows]

    async def status_counts(self) -> dict[str, int]:
        """Aggregate counts by status — used by the metrics gauge."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT status, COUNT(*) AS n FROM sessions GROUP BY status")
            rows = await cur.fetchall()
            return {row["status"]: int(row["n"]) for row in rows}
