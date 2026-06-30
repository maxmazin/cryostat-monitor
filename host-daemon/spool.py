"""Local SQLite spool (§6.2 steps 3-5).

Buffers readings on the fridge host so a network/server outage never loses data.
Readings are appended un-acked, POSTed in a batch, and marked acked on HTTP 2xx;
acked rows older than N days are pruned.

The table's primary key is (channel, ts), and append uses INSERT OR IGNORE, so
the spool is idempotent: if the daemon crashes and re-reads log lines it already
processed, no duplicate rows accumulate. The server is also idempotent on
(fridge, channel, ts), so duplicate *sends* are harmless too (§3.2).
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from parsers.base import Reading

SCHEMA = """
CREATE TABLE IF NOT EXISTS spool (
    ts      TEXT    NOT NULL,           -- ISO-8601 UTC
    channel TEXT    NOT NULL,
    value   REAL    NOT NULL,
    unit    TEXT    NOT NULL,
    acked   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (channel, ts)
);
CREATE INDEX IF NOT EXISTS idx_spool_acked ON spool (acked);
"""


def _iso_utc(ts: datetime) -> str:
    # The daemon converts to UTC before appending; normalize the stored form.
    return ts.astimezone(timezone.utc).isoformat()


class Spool:
    def __init__(self, path: str) -> None:
        self.conn = sqlite3.connect(path)
        self.conn.execute("PRAGMA journal_mode=WAL")   # survive crashes mid-write
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def append(self, readings: list[Reading]) -> None:
        """Append readings as un-acked rows. Idempotent on (channel, ts)."""
        if not readings:
            return
        self.conn.executemany(
            "INSERT OR IGNORE INTO spool (ts, channel, value, unit) VALUES (?, ?, ?, ?)",
            [(_iso_utc(r.ts), r.channel, r.value, r.unit) for r in readings],
        )
        self.conn.commit()

    def unacked(self, limit: int = 10000) -> list[dict]:
        """Oldest-first un-acked rows, shaped for the /ingest body."""
        cur = self.conn.execute(
            "SELECT ts, channel, value, unit FROM spool WHERE acked = 0 "
            "ORDER BY ts LIMIT ?",
            (limit,),
        )
        return [{"ts": ts, "channel": ch, "value": v, "unit": u}
                for ts, ch, v, u in cur.fetchall()]

    def mark_acked(self, rows: list[dict]) -> None:
        """Mark rows acked after a successful POST (HTTP 2xx)."""
        if not rows:
            return
        self.conn.executemany(
            "UPDATE spool SET acked = 1 WHERE channel = ? AND ts = ?",
            [(r["channel"], r["ts"]) for r in rows],
        )
        self.conn.commit()

    def prune(self, older_than: datetime) -> int:
        """Delete acked rows older than `older_than`. Returns rows removed."""
        cur = self.conn.execute(
            "DELETE FROM spool WHERE acked = 1 AND ts < ?",
            (_iso_utc(older_than),),
        )
        self.conn.commit()
        return cur.rowcount

    def close(self) -> None:
        self.conn.close()
