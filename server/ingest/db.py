"""Database layer for the ingest service (Phase 0).

Thin psycopg3 wrapper. A single connection pool is opened at app startup and
reused. All writes are idempotent or capped per the spec (§3.2, §7).

Connection string comes from the CRYO_DB_DSN env var, e.g.
    postgresql://cryo@127.0.0.1:5432/cryo
"""
from __future__ import annotations

import os
from datetime import datetime

from psycopg_pool import ConnectionPool

_pool: ConnectionPool | None = None


def init_pool(dsn: str | None = None) -> ConnectionPool:
    """Open the global pool. Called once at app startup."""
    global _pool
    dsn = dsn or os.environ["CRYO_DB_DSN"]
    _pool = ConnectionPool(dsn, min_size=1, max_size=8, open=True)
    return _pool


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


def _get_pool() -> ConnectionPool:
    if _pool is None:
        raise RuntimeError("connection pool not initialized; call init_pool() first")
    return _pool


# Bulk insert is idempotent: re-sent rows after an outage are dropped by the
# primary key (fridge, channel, ts). Never rely on the host to know what was
# already accepted (§3.2).
_INSERT_SQL = """
    INSERT INTO readings (ts, fridge, channel, value, unit)
    VALUES (%s, %s, %s, %s, %s)
    ON CONFLICT (fridge, channel, ts) DO NOTHING
"""

# Keep last_seen at the max data timestamp ever received for the fridge. GREATEST
# guards against out-of-order/backfill batches lowering it.
_LAST_SEEN_SQL = """
    INSERT INTO last_seen (fridge, last_ts)
    VALUES (%s, %s)
    ON CONFLICT (fridge) DO UPDATE
        SET last_ts = GREATEST(last_seen.last_ts, EXCLUDED.last_ts)
"""


def insert_readings(
    fridge: str, rows: list[tuple[datetime, str, str, float, str]]
) -> int:
    """Bulk-insert readings and advance last_seen. Returns rows actually inserted
    (excludes ON CONFLICT duplicates). `rows` are (ts_utc, fridge, channel, value, unit).
    """
    if not rows:
        return 0
    max_ts = max(r[0] for r in rows)
    pool = _get_pool()
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.executemany(_INSERT_SQL, rows)
            # executemany rowcount is unreliable across drivers for affected
            # rows; report attempted count and let the PK enforce idempotency.
            inserted = cur.rowcount if cur.rowcount is not None and cur.rowcount >= 0 else len(rows)
            cur.execute(_LAST_SEEN_SQL, (fridge, max_ts))
    return inserted


def insert_maintenance(
    fridge: str, minutes: int, reason: str | None, set_by: str | None
) -> None:
    """Insert a maintenance window ending `minutes` from now (server clock, UTC).
    Caller is responsible for capping `minutes` (§7).
    """
    pool = _get_pool()
    with pool.connection() as conn:
        conn.execute(
            """
            INSERT INTO maintenance (fridge, until_ts, reason, set_by)
            VALUES (%s, now() + make_interval(mins => %s), %s, %s)
            """,
            (fridge, minutes, reason, set_by),
        )
