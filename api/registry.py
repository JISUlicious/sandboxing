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

-- Slice 7 — tenants + tokens (SPEC-405).
CREATE TABLE IF NOT EXISTS tenants (
    id           TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    created_at   INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS tokens (
    id          TEXT PRIMARY KEY,            -- ulid
    tenant_id   TEXT NOT NULL,
    hash        TEXT NOT NULL UNIQUE,        -- HMAC-SHA256(pepper, plaintext) hex
    issued_at   INTEGER NOT NULL,
    revoked_at  INTEGER,                     -- NULL while active; future ts during grace
    FOREIGN KEY (tenant_id) REFERENCES tenants(id)
);
CREATE INDEX IF NOT EXISTS idx_tokens_tenant ON tokens(tenant_id);
CREATE INDEX IF NOT EXISTS idx_tokens_hash ON tokens(hash);

-- Slice 11a — Idempotency-Key replay cache. (tenant_id, key) is the
-- match — the route_template column is recorded so we can return a
-- 409 if the same key is replayed against a different endpoint
-- (Stripe-style "key reused for different operation" guard).
CREATE TABLE IF NOT EXISTS idempotency_keys (
    tenant_id      TEXT NOT NULL,
    key            TEXT NOT NULL,
    route_template TEXT NOT NULL,
    status_code    INTEGER NOT NULL,
    body_json      TEXT NOT NULL,             -- empty string for 204 no-content
    content_type   TEXT NOT NULL,
    created_at     INTEGER NOT NULL,
    expires_at     INTEGER NOT NULL,
    PRIMARY KEY (tenant_id, key)
);
CREATE INDEX IF NOT EXISTS idx_idempotency_expires
    ON idempotency_keys(expires_at);
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

    async def list_non_terminal(self) -> Sequence[SessionRow]:
        """Every row whose status is not DESTROYING / DESTROYED. Used by
        startup reconciliation (ARCH-051) to find candidates whose
        underlying container may be gone after a control-plane crash."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM sessions WHERE status NOT IN ('DESTROYING', 'DESTROYED')"
            )
            rows = await cur.fetchall()
            return [_row_to_session(r) for r in rows]

    # ----- tenants + tokens (slice 7) -----

    async def create_tenant(self, tenant_id: str, display_name: str) -> None:
        """Insert a tenant; idempotent on the primary key (no-op if it
        already exists). The control plane bootstraps the 'default'
        tenant on startup from settings.api_token."""
        ts = now_ms()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT OR IGNORE INTO tenants (id, display_name, created_at) VALUES (?, ?, ?)",
                (tenant_id, display_name, ts),
            )
            await db.commit()

    async def list_tenants(self) -> Sequence[tuple[str, str, int]]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT id, display_name, created_at FROM tenants ORDER BY created_at"
            )
            rows = await cur.fetchall()
            return [(row["id"], row["display_name"], int(row["created_at"])) for row in rows]

    async def count_tenants(self) -> int:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT COUNT(*) AS n FROM tenants")
            row = await cur.fetchone()
            return int(row["n"]) if row else 0

    async def insert_token(self, *, token_id: str, tenant_id: str, hash_: str) -> None:
        ts = now_ms()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO tokens (id, tenant_id, hash, issued_at) VALUES (?, ?, ?, ?)",
                (token_id, tenant_id, hash_, ts),
            )
            await db.commit()

    async def lookup_token(self, hash_: str) -> tuple[str, str, int | None] | None:
        """Return (token_id, tenant_id, revoked_at) for the row matching
        `hash_`, or None if no such row. The caller decides whether
        revoked_at puts the token in grace, fully revoked, or active."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT id, tenant_id, revoked_at FROM tokens WHERE hash = ?",
                (hash_,),
            )
            row = await cur.fetchone()
            if row is None:
                return None
            return (
                row["id"],
                row["tenant_id"],
                int(row["revoked_at"]) if row["revoked_at"] is not None else None,
            )

    async def revoke_token(self, token_id: str, *, revoke_at_ms: int) -> None:
        """Mark a token revoked at a specific monotonic timestamp.
        Setting `revoke_at_ms` in the future creates a grace window."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE tokens SET revoked_at = ? WHERE id = ?",
                (revoke_at_ms, token_id),
            )
            await db.commit()

    async def list_active_tokens(self, tenant_id: str) -> Sequence[tuple[str, int, int | None]]:
        """All tokens for a tenant where revoked_at is NULL or > now.
        Returned shape: (token_id, issued_at, revoked_at)."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT id, issued_at, revoked_at FROM tokens "
                "WHERE tenant_id = ? "
                "AND (revoked_at IS NULL OR revoked_at > ?)",
                (tenant_id, now_ms()),
            )
            rows = await cur.fetchall()
            return [
                (
                    row["id"],
                    int(row["issued_at"]),
                    int(row["revoked_at"]) if row["revoked_at"] is not None else None,
                )
                for row in rows
            ]

    # ----- idempotency-keys (slice 11a) -----

    async def lookup_idempotency(
        self, *, tenant_id: str, key: str
    ) -> tuple[str, int, str, str] | None:
        """Return (route_template, status_code, body_json, content_type)
        for an unexpired entry matching (tenant_id, key), or None.
        Expired rows are treated as absent and the caller should
        overwrite them via `store_idempotency`."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT route_template, status_code, body_json, content_type "
                "FROM idempotency_keys "
                "WHERE tenant_id = ? AND key = ? AND expires_at > ?",
                (tenant_id, key, now_ms()),
            )
            row = await cur.fetchone()
            if row is None:
                return None
            return (
                row["route_template"],
                int(row["status_code"]),
                row["body_json"],
                row["content_type"],
            )

    async def store_idempotency(
        self,
        *,
        tenant_id: str,
        key: str,
        route_template: str,
        status_code: int,
        body_json: str,
        content_type: str,
        ttl_s: int,
    ) -> None:
        """INSERT-OR-REPLACE the cache row. Replace lets expired entries
        be overwritten without a separate delete pass."""
        ts = now_ms()
        expires = ts + ttl_s * 1000
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT OR REPLACE INTO idempotency_keys "
                "(tenant_id, key, route_template, status_code, body_json, "
                " content_type, created_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    tenant_id,
                    key,
                    route_template,
                    status_code,
                    body_json,
                    content_type,
                    ts,
                    expires,
                ),
            )
            await db.commit()

    async def purge_expired_idempotency(self) -> int:
        """Sweep expired rows; called from the reaper at its normal tick.
        Returns the number of rows removed."""
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                "DELETE FROM idempotency_keys WHERE expires_at <= ?",
                (now_ms(),),
            )
            await db.commit()
            return cur.rowcount or 0

    async def transition_orphaned(self, session_id: str) -> None:
        """Force a session to STOPPED regardless of current state, used
        by reconciliation when the underlying container has vanished.

        Bypasses the normal TRANSITIONS table because the row may be in
        CREATING (which can't normally jump straight to STOPPED) — the
        invariant the regular transition guards is "no logical state
        skips during operation"; reconciliation is by definition a
        recovery pass after that invariant has already been broken.
        """
        ts = now_ms()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE sessions SET status = 'STOPPED', last_activity_at = ? "
                "WHERE id = ? AND status NOT IN ('DESTROYING', 'DESTROYED', 'STOPPED')",
                (ts, session_id),
            )
            await db.commit()
