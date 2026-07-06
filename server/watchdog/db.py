"""Database access for the watchdog (Phase 2, §8).

Read-mostly: per-fridge staleness (last_seen), active maintenance mutes, and the
latest value per channel. The one write path is alert_state, persisted so a
watchdog restart neither re-spams a known alert nor forgets an active one (§8).

Mirrors ingest/db.py's pool pattern: the watchdog runs as its own process and
must not share the ingest service's pool. DSN comes from CRYO_DB_DSN — the same
database the ingest service writes to.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from psycopg_pool import ConnectionPool

import dbpool

# One pool per process; the lifecycle logic is shared with ingest (dbpool).
_db = dbpool.DbPool()


@dataclass
class LatestReading:
    value: float
    ts: datetime          # tz-aware (UTC), the data timestamp
    unit: str


@dataclass
class LastSeen:
    last_ts: datetime         # max DATA timestamp (host clock) — for display
    received_at: datetime     # server clock when data last arrived — staleness basis


@dataclass
class AlertRow:
    state: str            # 'OK' | 'ALERTING'
    since: datetime
    last_notified: datetime | None


def init_pool(dsn: str | None = None) -> ConnectionPool:
    """Open the pool. Called once at startup."""
    return _db.open(dsn, max_size=4)


def close_pool() -> None:
    _db.close()


def _get_pool() -> ConnectionPool:
    return _db.get()


def ping() -> None:
    """Prove the database is reachable. Raises if it is not — the watchdog uses
    this to decide whether to heartbeat, rather than inferring DB health from
    aggregate fridge-check failures (§8)."""
    pool = _get_pool()
    with pool.connection() as conn:
        conn.execute("SELECT 1")


def is_muted(fridge: str) -> bool:
    """True if an active maintenance window exists (DB clock: now() < until_ts)."""
    pool = _get_pool()
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM maintenance WHERE fridge = %s AND until_ts > now() LIMIT 1",
            (fridge,),
        ).fetchone()
    return row is not None


def last_seen(fridge: str) -> LastSeen | None:
    """Last-seen record for the fridge, or None if never seen. `received_at` is
    the staleness basis (server arrival time); `last_ts` is the data timestamp
    for display."""
    pool = _get_pool()
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT last_ts, received_at FROM last_seen WHERE fridge = %s",
            (fridge,),
        ).fetchone()
    return LastSeen(last_ts=row[0], received_at=row[1]) if row else None


def latest_reading(fridge: str, channel: str) -> LatestReading | None:
    """Most recent reading for (fridge, channel), or None.

    Rows with a far-future data ts are excluded: ORDER BY ts DESC would otherwise
    let one clock-skewed row pin this result — and freeze the watchdog's
    threshold input — even after the host clock is fixed. Ingest also rejects
    far-future rows; this guard survives rows that predate that check."""
    pool = _get_pool()
    with pool.connection() as conn:
        row = conn.execute(
            """
            SELECT value, ts, unit FROM readings
            WHERE fridge = %s AND channel = %s
              AND ts <= now() + interval '24 hours'
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


def list_alert_keys() -> list[tuple[str, str]]:
    """All (fridge, alert_key) pairs in alert_state — used at startup to warn
    about rows left dangling after config changes."""
    pool = _get_pool()
    with pool.connection() as conn:
        rows = conn.execute("SELECT fridge, alert_key FROM alert_state").fetchall()
    return [(row[0], row[1]) for row in rows]


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
