"""Database access for the watchdog (Phase 2, §8).

Read-mostly: per-fridge staleness (last_seen), active maintenance mutes, and the
latest value per channel. The one write path is alert_state, persisted so a
watchdog restart neither re-spams a known alert nor forgets an active one (§8).

Mirrors ingest/db.py's pool pattern: the watchdog runs as its own process and
must not share the ingest service's pool. DSN comes from CRYO_DB_DSN — the same
database the ingest service writes to.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime

from psycopg_pool import ConnectionPool

_pool: ConnectionPool | None = None


@dataclass
class LatestReading:
    value: float
    ts: datetime          # tz-aware (UTC), the data timestamp
    unit: str


@dataclass
class AlertRow:
    state: str            # 'OK' | 'ALERTING'
    since: datetime
    last_notified: datetime | None


def init_pool(dsn: str | None = None) -> ConnectionPool:
    """Open the global pool. Called once at startup."""
    global _pool
    dsn = dsn or os.environ["CRYO_DB_DSN"]
    _pool = ConnectionPool(dsn, min_size=1, max_size=4, open=True)
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


def is_muted(fridge: str) -> bool:
    """True if an active maintenance window exists (DB clock: now() < until_ts)."""
    pool = _get_pool()
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM maintenance WHERE fridge = %s AND until_ts > now() LIMIT 1",
            (fridge,),
        ).fetchone()
    return row is not None


def last_seen(fridge: str) -> datetime | None:
    """Max data timestamp received for the fridge, or None if never seen."""
    pool = _get_pool()
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT last_ts FROM last_seen WHERE fridge = %s",
            (fridge,),
        ).fetchone()
    return row[0] if row else None


def latest_reading(fridge: str, channel: str) -> LatestReading | None:
    """Most recent reading for (fridge, channel), or None."""
    pool = _get_pool()
    with pool.connection() as conn:
        row = conn.execute(
            """
            SELECT value, ts, unit FROM readings
            WHERE fridge = %s AND channel = %s
            ORDER BY ts DESC
            LIMIT 1
            """,
            (fridge, channel),
        ).fetchone()
    if row is None:
        return None
    return LatestReading(value=row[0], ts=row[1], unit=row[2])


def get_alert_state(fridge: str, key: str) -> AlertRow | None:
    """Current persisted alert state for (fridge, alert_key), or None if absent
    (which the state machine treats as OK)."""
    pool = _get_pool()
    with pool.connection() as conn:
        row = conn.execute(
            """
            SELECT state, since, last_notified FROM alert_state
            WHERE fridge = %s AND alert_key = %s
            """,
            (fridge, key),
        ).fetchone()
    if row is None:
        return None
    return AlertRow(state=row[0], since=row[1], last_notified=row[2])


def upsert_alert_state(
    fridge: str,
    key: str,
    state: str,
    since: datetime,
    last_notified: datetime | None,
) -> None:
    """Persist the alert state for (fridge, alert_key)."""
    pool = _get_pool()
    with pool.connection() as conn:
        conn.execute(
            """
            INSERT INTO alert_state (fridge, alert_key, state, since, last_notified)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (fridge, alert_key) DO UPDATE
                SET state = EXCLUDED.state,
                    since = EXCLUDED.since,
                    last_notified = EXCLUDED.last_notified
            """,
            (fridge, key, state, since, last_notified),
        )
