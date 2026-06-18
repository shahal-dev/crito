"""In-memory activity log — the feed behind the dashboard's console panel.

A small ring buffer of recent operator/system events (device commands, execution
steps, polls). Ephemeral by design; durable history lives in the FITS archive and
the AuditEvent table. Exposed via GET /api/activity and polled by the console.
"""
from __future__ import annotations

from collections import deque
from datetime import datetime, timezone


class ActivityLog:
    def __init__(self, maxlen: int = 300):
        self._dq: deque[dict] = deque(maxlen=maxlen)

    def push(self, msg: str, kind: str = "info") -> None:
        self._dq.append({
            "ts": datetime.now(timezone.utc).isoformat(),
            "msg": msg,
            "kind": kind,   # info | cmd | exec | alert | error
        })

    def recent(self, limit: int = 100) -> list[dict]:
        items = list(self._dq)[-limit:]
        items.reverse()     # newest first
        return items
