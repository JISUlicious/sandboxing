"""SQLite-backed session registry. ARCH-010, ARCH-051 step ordering."""

from __future__ import annotations

import json
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import aiosqlite

from api.models import Limits, SessionStatus

# Sentinel for slice-12 update_tenant — distinguishes "don't change"
# from explicit `None`. Don't expose; only used inside this module.
_SENTINEL: Any = object()

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

-- Slice 7 — tenants + tokens (SPEC-405). Slice 12 adds limit +
-- egress_allowlist columns to tenants and scopes / note columns to
-- tokens. CREATE includes the new columns; existing deployments get
-- them via the ALTER TABLE migration in `_apply_pending_migrations`.
CREATE TABLE IF NOT EXISTS tenants (
    id                  TEXT PRIMARY KEY,
    display_name        TEXT NOT NULL,
    created_at          INTEGER NOT NULL,
    max_concurrency     INTEGER,
    max_workspace_gib   INTEGER,
    max_exec_timeout_s  INTEGER,
    egress_allowlist_json TEXT
);
CREATE TABLE IF NOT EXISTS tokens (
    id          TEXT PRIMARY KEY,            -- ulid
    tenant_id   TEXT NOT NULL,
    hash        TEXT NOT NULL UNIQUE,        -- HMAC-SHA256(pepper, plaintext) hex
    issued_at   INTEGER NOT NULL,
    revoked_at  INTEGER,                     -- NULL while active; future ts during grace
    scopes_json TEXT,                        -- NULL = all scopes (back-compat)
    note        TEXT,
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

-- Slice 11b — background processes. One row per spawned process,
-- rows persist past EXITED so the agent can read the exit_code +
-- last_output_at after the fact (until DELETE reaps the row or the
-- session is destroyed).
CREATE TABLE IF NOT EXISTS processes (
    id              TEXT PRIMARY KEY,         -- ulid (session-scoped)
    session_id      TEXT NOT NULL,
    tenant_id       TEXT NOT NULL,
    name            TEXT,
    argv_json       TEXT NOT NULL,
    cwd             TEXT,
    restart_policy  TEXT NOT NULL,
    ospid           INTEGER,                  -- captured after spawn; NULL if spawn failed
    log_path        TEXT NOT NULL,            -- /workspace-relative path
    exit_path       TEXT NOT NULL,            -- /workspace-relative path
    state           TEXT NOT NULL,            -- RUNNING | EXITED
    exit_code       INTEGER,
    started_at      INTEGER NOT NULL,
    exited_at       INTEGER,
    last_output_at  INTEGER,
    last_polled_at  INTEGER,                  -- watcher polling throttle
    FOREIGN KEY (session_id) REFERENCES sessions(id)
);
CREATE INDEX IF NOT EXISTS idx_processes_session_state
    ON processes(session_id, state);
"""

# Slice 12 — append-only ALTER TABLE migrations for deployments that
# already have data in the old (column-less) tenants / tokens tables.
_PENDING_MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    ("tenants", "max_concurrency", "INTEGER"),
    ("tenants", "max_workspace_gib", "INTEGER"),
    ("tenants", "max_exec_timeout_s", "INTEGER"),
    ("tenants", "egress_allowlist_json", "TEXT"),
    ("tokens", "scopes_json", "TEXT"),
    ("tokens", "note", "TEXT"),
)


async def _apply_pending_migrations(db: aiosqlite.Connection) -> None:
    """Idempotent ALTER TABLE for columns the slice-12 SCHEMA adds."""
    for table, column, coltype in _PENDING_MIGRATIONS:
        cur = await db.execute(f"PRAGMA table_info({table})")
        rows = await cur.fetchall()
        if any(r[1] == column for r in rows):
            continue
        await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")
    await db.commit()


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


class ProcessRow:
    __slots__ = (
        "id",
        "session_id",
        "tenant_id",
        "name",
        "argv",
        "cwd",
        "restart_policy",
        "ospid",
        "log_path",
        "exit_path",
        "state",
        "exit_code",
        "started_at",
        "exited_at",
        "last_output_at",
        "last_polled_at",
    )

    def __init__(self, **kw: object) -> None:
        self.id: str = kw["id"]  # type: ignore[assignment]
        self.session_id: str = kw["session_id"]  # type: ignore[assignment]
        self.tenant_id: str = kw["tenant_id"]  # type: ignore[assignment]
        self.name: str | None = kw.get("name")  # type: ignore[assignment]
        self.argv: list[str] = kw["argv"]  # type: ignore[assignment]
        self.cwd: str | None = kw.get("cwd")  # type: ignore[assignment]
        self.restart_policy: str = kw["restart_policy"]  # type: ignore[assignment]
        self.ospid: int | None = kw.get("ospid")  # type: ignore[assignment]
        self.log_path: str = kw["log_path"]  # type: ignore[assignment]
        self.exit_path: str = kw["exit_path"]  # type: ignore[assignment]
        self.state: str = kw["state"]  # type: ignore[assignment]
        self.exit_code: int | None = kw.get("exit_code")  # type: ignore[assignment]
        self.started_at: int = kw["started_at"]  # type: ignore[assignment]
        self.exited_at: int | None = kw.get("exited_at")  # type: ignore[assignment]
        self.last_output_at: int | None = kw.get("last_output_at")  # type: ignore[assignment]
        self.last_polled_at: int | None = kw.get("last_polled_at")  # type: ignore[assignment]


def _row_to_process(row: aiosqlite.Row) -> ProcessRow:
    return ProcessRow(
        id=row["id"],
        session_id=row["session_id"],
        tenant_id=row["tenant_id"],
        name=row["name"],
        argv=json.loads(row["argv_json"]),
        cwd=row["cwd"],
        restart_policy=row["restart_policy"],
        ospid=int(row["ospid"]) if row["ospid"] is not None else None,
        log_path=row["log_path"],
        exit_path=row["exit_path"],
        state=row["state"],
        exit_code=int(row["exit_code"]) if row["exit_code"] is not None else None,
        started_at=int(row["started_at"]),
        exited_at=int(row["exited_at"]) if row["exited_at"] is not None else None,
        last_output_at=(int(row["last_output_at"]) if row["last_output_at"] is not None else None),
        last_polled_at=(int(row["last_polled_at"]) if row["last_polled_at"] is not None else None),
    )


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


def _row_to_tenant_dict(row: aiosqlite.Row) -> dict:
    """Slice 12 — full tenant detail for the management API."""
    keys = row.keys()
    allow_json = row["egress_allowlist_json"] if "egress_allowlist_json" in keys else None
    return {
        "id": row["id"],
        "display_name": row["display_name"],
        "created_at": int(row["created_at"]),
        "max_concurrency": (
            int(row["max_concurrency"])
            if "max_concurrency" in keys and row["max_concurrency"] is not None
            else None
        ),
        "max_workspace_gib": (
            int(row["max_workspace_gib"])
            if "max_workspace_gib" in keys and row["max_workspace_gib"] is not None
            else None
        ),
        "max_exec_timeout_s": (
            int(row["max_exec_timeout_s"])
            if "max_exec_timeout_s" in keys and row["max_exec_timeout_s"] is not None
            else None
        ),
        "egress_allowlist": json.loads(allow_json) if allow_json else None,
    }


def _row_to_token_info(row: aiosqlite.Row) -> dict:
    keys = row.keys()
    scopes_json = row["scopes_json"] if "scopes_json" in keys else None
    return {
        "id": row["id"],
        "tenant_id": row["tenant_id"],
        "issued_at": int(row["issued_at"]),
        "revoked_at": int(row["revoked_at"]) if row["revoked_at"] is not None else None,
        "scopes": json.loads(scopes_json) if scopes_json else None,
        "note": row["note"] if "note" in keys else None,
    }


class Registry:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    async def init(self) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.executescript(SCHEMA)
            await db.commit()
            # Slice 12: ALTER TABLE for older deployments where the
            # tenants / tokens tables predate the limit + scopes
            # columns. Idempotent — does nothing on fresh dbs.
            await _apply_pending_migrations(db)

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

    async def create_tenant(
        self,
        tenant_id: str,
        display_name: str,
        *,
        max_concurrency: int | None = None,
        max_workspace_gib: int | None = None,
        max_exec_timeout_s: int | None = None,
        egress_allowlist: list[str] | None = None,
    ) -> None:
        """Insert a tenant; idempotent on the primary key (no-op if it
        already exists). Slice 12 adds optional per-tenant limit
        columns. The control plane bootstraps the 'default' tenant on
        startup from settings.api_token; existing rows that pre-date
        slice 12 carry NULLs for the new columns and inherit the
        global Settings defaults at runtime."""
        ts = now_ms()
        allow_json = json.dumps(egress_allowlist) if egress_allowlist is not None else None
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT OR IGNORE INTO tenants "
                "(id, display_name, created_at, max_concurrency, "
                " max_workspace_gib, max_exec_timeout_s, egress_allowlist_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    tenant_id,
                    display_name,
                    ts,
                    max_concurrency,
                    max_workspace_gib,
                    max_exec_timeout_s,
                    allow_json,
                ),
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

    # Slice 12 — full-detail tenant API.

    async def get_tenant_full(self, tenant_id: str) -> dict | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM tenants WHERE id = ?", (tenant_id,))
            row = await cur.fetchone()
            return _row_to_tenant_dict(row) if row else None

    async def list_tenants_full(self) -> Sequence[dict]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM tenants ORDER BY created_at")
            rows = await cur.fetchall()
            return [_row_to_tenant_dict(r) for r in rows]

    async def update_tenant(
        self,
        tenant_id: str,
        *,
        display_name: str | None = None,
        max_concurrency: object = _SENTINEL,
        max_workspace_gib: object = _SENTINEL,
        max_exec_timeout_s: object = _SENTINEL,
        egress_allowlist: object = _SENTINEL,
    ) -> None:
        """Patch a tenant. Each limit field defaults to `_SENTINEL`
        (don't touch); explicit `None` clears the field back to the
        global default."""
        sets: list[str] = []
        args: list = []
        if display_name is not None:
            sets.append("display_name = ?")
            args.append(display_name)
        if max_concurrency is not _SENTINEL:
            sets.append("max_concurrency = ?")
            args.append(max_concurrency)
        if max_workspace_gib is not _SENTINEL:
            sets.append("max_workspace_gib = ?")
            args.append(max_workspace_gib)
        if max_exec_timeout_s is not _SENTINEL:
            sets.append("max_exec_timeout_s = ?")
            args.append(max_exec_timeout_s)
        if egress_allowlist is not _SENTINEL:
            sets.append("egress_allowlist_json = ?")
            args.append(json.dumps(egress_allowlist) if egress_allowlist is not None else None)
        if not sets:
            return
        args.append(tenant_id)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(f"UPDATE tenants SET {', '.join(sets)} WHERE id = ?", args)
            await db.commit()

    async def delete_tenant(self, tenant_id: str) -> None:
        """Hard-delete the tenant row. Caller is responsible for
        revoking tokens / destroying sessions FIRST — this method
        only drops the tenants row."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("DELETE FROM tenants WHERE id = ?", (tenant_id,))
            await db.commit()

    async def insert_token(
        self,
        *,
        token_id: str,
        tenant_id: str,
        hash_: str,
        scopes: list[str] | None = None,
        note: str | None = None,
    ) -> None:
        """Insert a token row. `scopes=None` ⇒ all-scopes (back-compat);
        `scopes=[]` ⇒ explicitly no scopes; `scopes=[...]` ⇒ explicit
        list."""
        ts = now_ms()
        scopes_json = json.dumps(scopes) if scopes is not None else None
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO tokens (id, tenant_id, hash, issued_at, scopes_json, note) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (token_id, tenant_id, hash_, ts, scopes_json, note),
            )
            await db.commit()

    async def lookup_token(
        self, hash_: str
    ) -> tuple[str, str, int | None, list[str] | None] | None:
        """Return (token_id, tenant_id, revoked_at, scopes) for the row
        matching `hash_`, or None. `scopes` is None when the token has
        no row in scopes_json (= all-scopes back-compat); a list
        otherwise."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT id, tenant_id, revoked_at, scopes_json FROM tokens WHERE hash = ?",
                (hash_,),
            )
            row = await cur.fetchone()
            if row is None:
                return None
            scopes_json = row["scopes_json"]
            return (
                row["id"],
                row["tenant_id"],
                int(row["revoked_at"]) if row["revoked_at"] is not None else None,
                json.loads(scopes_json) if scopes_json else None,
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

    async def revoke_all_tenant_tokens(self, tenant_id: str) -> int:
        """Slice 12: bulk-revoke for tenant deletion. Sets revoked_at
        to now on every active row; returns count revoked."""
        ts = now_ms()
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                "UPDATE tokens SET revoked_at = ? "
                "WHERE tenant_id = ? AND (revoked_at IS NULL OR revoked_at > ?)",
                (ts, tenant_id, ts),
            )
            await db.commit()
            return cur.rowcount or 0

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

    async def count_active_tokens(self, tenant_id: str) -> int:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT COUNT(*) AS n FROM tokens "
                "WHERE tenant_id = ? "
                "AND (revoked_at IS NULL OR revoked_at > ?)",
                (tenant_id, now_ms()),
            )
            row = await cur.fetchone()
            return int(row["n"]) if row else 0

    async def list_tokens_full(self, tenant_id: str) -> Sequence[dict]:
        """Slice 12 — full token detail (including scopes, note) for
        the management API."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM tokens WHERE tenant_id = ? ORDER BY issued_at",
                (tenant_id,),
            )
            rows = await cur.fetchall()
            return [_row_to_token_info(r) for r in rows]

    async def get_token_by_id(self, token_id: str) -> dict | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM tokens WHERE id = ?", (token_id,))
            row = await cur.fetchone()
            return _row_to_token_info(row) if row else None

    # ----- processes (slice 11b) -----

    async def insert_process(
        self,
        *,
        process_id: str,
        session_id: str,
        tenant_id: str,
        name: str | None,
        argv: list[str],
        cwd: str | None,
        restart_policy: str,
        ospid: int | None,
        log_path: str,
        exit_path: str,
    ) -> None:
        ts = now_ms()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO processes (id, session_id, tenant_id, name, argv_json, cwd, "
                "restart_policy, ospid, log_path, exit_path, state, started_at, last_polled_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'RUNNING', ?, ?)",
                (
                    process_id,
                    session_id,
                    tenant_id,
                    name,
                    json.dumps(argv),
                    cwd,
                    restart_policy,
                    ospid,
                    log_path,
                    exit_path,
                    ts,
                    ts,
                ),
            )
            await db.commit()

    async def get_process(
        self, *, session_id: str, process_id: str, tenant_id: str | None = None
    ) -> ProcessRow | None:
        sql = "SELECT * FROM processes WHERE id = ? AND session_id = ?"
        args: tuple = (process_id, session_id)
        if tenant_id is not None:
            sql += " AND tenant_id = ?"
            args = (process_id, session_id, tenant_id)
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(sql, args)
            row = await cur.fetchone()
            return _row_to_process(row) if row else None

    async def list_processes(
        self, *, session_id: str, tenant_id: str | None = None
    ) -> Sequence[ProcessRow]:
        sql = "SELECT * FROM processes WHERE session_id = ?"
        args: tuple = (session_id,)
        if tenant_id is not None:
            sql += " AND tenant_id = ?"
            args = (session_id, tenant_id)
        sql += " ORDER BY started_at"
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(sql, args)
            rows = await cur.fetchall()
            return [_row_to_process(r) for r in rows]

    async def count_running_processes(self, session_id: str) -> int:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT COUNT(*) AS n FROM processes WHERE session_id = ? AND state = 'RUNNING'",
                (session_id,),
            )
            row = await cur.fetchone()
            return int(row["n"]) if row else 0

    async def list_running_processes_unscoped(self, session_id: str) -> Sequence[ProcessRow]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM processes WHERE session_id = ? AND state = 'RUNNING'",
                (session_id,),
            )
            rows = await cur.fetchall()
            return [_row_to_process(r) for r in rows]

    async def mark_process_exited(
        self,
        *,
        process_id: str,
        exit_code: int | None,
        last_output_at: int | None = None,
    ) -> None:
        ts = now_ms()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE processes SET state = 'EXITED', exit_code = ?, "
                "exited_at = ?, last_polled_at = ?, "
                "last_output_at = COALESCE(?, last_output_at) "
                "WHERE id = ? AND state = 'RUNNING'",
                (exit_code, ts, ts, last_output_at, process_id),
            )
            await db.commit()

    async def touch_process_polled(self, process_id: str) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE processes SET last_polled_at = ? WHERE id = ?",
                (now_ms(), process_id),
            )
            await db.commit()

    async def delete_process(self, *, session_id: str, process_id: str) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "DELETE FROM processes WHERE session_id = ? AND id = ?",
                (session_id, process_id),
            )
            await db.commit()

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
