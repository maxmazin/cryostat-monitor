"""Local SQLite spool (§6.2 step 3-5).

Buffers readings on the fridge host so a network/server outage never loses
data. Readings are appended un-acked, POSTed in a batch, and marked acked on
HTTP 2xx. Acked rows older than N days are pruned. Duplicate sends are safe
because the server dedups on (fridge, channel, ts).

Skeleton: API shape is defined; SQLite wiring is marked TODO.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime

from parsers.base import Reading

SCHEMA = """
CREATE TABLE IF NOT EXISTS spool (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      TEXT NOT NULL,      -- ISO-8601 UTC
    channel TEXT NOT NULL,
    value   REAL NOT NULL,
    unit    TEXT NOT NULL,
    acked   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_spool_acked ON spool (acked);
"""


class Spool:
    def __init__(self, path: str) -> None:
        self.conn = sqlite3.connect(path)
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def append(self, readings: list[Reading]) -> None:
        """Append newly-parsed readings as un-acked rows."""
        # TODO: executemany INSERT; ts stored as UTC ISO string.
        ...

    def unacked(self) -> list[tuple[int, Reading]]:
        """Return all un-acked rows as (rowid, Reading) for batching."""
        # TODO: SELECT ... WHERE acked = 0
        return []

    def mark_acked(self, ids: list[int]) -> None:
        """Mark rows acked after a successful POST (HTTP 2xx)."""
        # TODO: UPDATE spool SET acked = 1 WHERE id IN (...)
        ...

    def prune(self, older_than: datetime) -> int:
        """Delete acked rows older than `older_than`. Returns rows removed."""
        # TODO: DELETE FROM spool WHERE acked = 1 AND ts < ?
        return 0
