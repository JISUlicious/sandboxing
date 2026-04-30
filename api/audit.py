"""Append-only JSONL audit emitter. ARCH-060.

Slice 1 emits best-effort. Fail-closed semantics (ARCH §7) come in
slice 4 once the egress proxy and full audit pipeline land.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any


class AuditEmitter:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()

    async def emit(
        self,
        *,
        kind: str,
        tenant: str,
        session: str | None = None,
        actor: str | None = None,
        payload: Any = None,
        result: str = "ok",
        duration_ms: int | None = None,
    ) -> None:
        record = {
            "ts": int(time.time() * 1000),
            "kind": kind,
            "tenant": tenant,
            "session": session,
            "actor": actor,
            "payload": payload,
            "result": result,
            "duration_ms": duration_ms,
        }
        line = json.dumps(record, separators=(",", ":")) + "\n"
        async with self._lock:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line)
