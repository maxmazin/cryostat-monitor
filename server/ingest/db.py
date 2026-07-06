"""Database layer for the ingest service (Phase 0).

Thin psycopg3 wrapper. A single connection pool is opened at app startup and
reused. All writes are idempotent or capped per the spec (§3.2, §7).

Connection string comes from the CRYO_DB_DSN env var, e.g.
    postgresql://cryo@127.0.0.1:5432/cryo
"""
from __future__ import annotations

from datetime import datetime

from psycopg_pool import ConnectionPool

import dbpool

# One pool per process; the lifecycle logic is shared with the watchdog (dbpool).
_db = dbpool.DbPool()


def init_pool(dsn: str | None = None) -> ConnectionPool:
    """Open the pool. Called once at app startup."""
    return _db.open(dsn, max_size=8)


def close_pool() -> None:
    _db.close()


def _get_pool() -> ConnectionPool:
    return _db.get()


def ping() -> None:
    """Prove a database round-trip (used by /health). Raises on any failure.

    Short acquisition timeout so a down/misconfigured Postgres turns into a
    fast 503, not a hung health probe.
    """
    with _get_pool().connection(timeout=5) as conn:
        conn.execute("SELECT 1")


# Bulk insert is idempotent: re-sent rows after an outage are dropped by the
# primary key (fridge, channel, ts). Never rely on the host to know what was
# already accepted (§3.2).
_INSERT_SQL = """
    INSERT INTO readings (ts, fridge, channel, value, unit)
    VALUES (%s, %s, %s, %s, %s)
    ON CONFLICT (fridge, channel, ts) DO NOTHING
"""

# Keep last_ts at the max data timestamp ever received (GREATEST guards against
# out-of-order/backfill batches lowering it). received_at advances to the server
# clock, but only when the batch inserted genuinely NEW rows (see
# insert_readings): it is what the watchdog measures staleness against (immune
# to fridge-host clock skew), and a daemon bug replaying already-stored batches
# must not keep bumping it and mask true fridge silence — SILENT means "no NEW
# data".
_LAST_SEEN_SQL = """
    INSERT INTO last_seen (fridge, last_ts, received_at)
    VALUES (%s, %s, now())
    ON CONFLICT (fridge) DO UPDATE
        SET last_ts = GREATEST(last_seen.last_ts, EXCLUDED.last_ts),
            received_at = now()
"""


def insert_readings(
    fridge: str, rows: list[tuple[datetime, str, str, float, str]]
) -> int:
    """Bulk-insert readings and advance last_seen when new rows landed. Returns
    rows actually inserted (excludes ON CONFLICT duplicates). `rows` are
    (ts_utc, fridge, channel, value, unit).
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
            # Advance last_seen only for genuinely new data: an all-duplicate
            # replay must not refresh the staleness clock (see _LAST_SEEN_SQL
            # comment). The len(rows) fallback above errs toward advancing.
            if inserted > 0:
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
