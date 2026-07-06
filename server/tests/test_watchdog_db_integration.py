"""Integration tests for the watchdog DB layer against a live PostgreSQL.

Skipped unless CRYO_TEST_DSN points at a database with schema.sql applied (same
gating as test_db_integration.py). These exercise the actual SQL in
watchdog/db.py — the unit tests fake the DB, so without this the queries are
never run against Postgres.

`scripts/dev_local.sh up` provisions a suitable database. Each test uses a
unique fridge name and deletes its rows on teardown.
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from watchdog import db

DSN = os.environ.get("CRYO_TEST_DSN")

if not DSN and os.environ.get("CRYO_REQUIRE_INTEGRATION"):
    raise RuntimeError(
        "CRYO_REQUIRE_INTEGRATION is set but CRYO_TEST_DSN is unset — integration "
        "tests would skip. Set CRYO_TEST_DSN to a schema-applied test database."
    )

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not DSN, reason="set CRYO_TEST_DSN to run DB integration tests"),
]


@pytest.fixture(scope="module")
def _pool():
    db.init_pool(DSN)
    yield
    db.close_pool()


@pytest.fixture
def fridge(_pool):
    name = f"test_{uuid.uuid4().hex[:8]}"
    yield name
    with db._get_pool().connection() as conn:
        for table in ("readings", "last_seen", "maintenance", "alert_state"):
            conn.execute(f"DELETE FROM {table} WHERE fridge = %s", (name,))


def _exec(sql: str, params: tuple) -> None:
    with db._get_pool().connection() as conn:
        conn.execute(sql, params)


def test_ping_ok(_pool):
    db.ping()  # reachable DB -> no exception


def test_last_seen_absent_returns_none(fridge):
    assert db.last_seen(fridge) is None


def test_last_seen_reads_last_ts_and_received_at(fridge):
    ts = datetime(2026, 1, 1, tzinfo=timezone.utc)
    _exec("INSERT INTO last_seen (fridge, last_ts) VALUES (%s, %s)", (fridge, ts))
    seen = db.last_seen(fridge)
    assert seen is not None
    assert seen.last_ts == ts
    # received_at defaults to server now() on insert -> present and not the data ts.
    assert seen.received_at is not None


def test_is_muted_reflects_active_window(fridge):
    assert db.is_muted(fridge) is False
    # Active window (future until_ts).
    _exec("INSERT INTO maintenance (fridge, until_ts) VALUES (%s, now() + interval '1 hour')", (fridge,))
    assert db.is_muted(fridge) is True


def test_is_muted_ignores_expired_window(fridge):
    _exec("INSERT INTO maintenance (fridge, until_ts) VALUES (%s, now() - interval '1 minute')", (fridge,))
    assert db.is_muted(fridge) is False


def test_latest_reading_returns_most_recent(fridge):
    t1 = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 1, 1, 0, 1, tzinfo=timezone.utc)
    _exec("INSERT INTO readings (ts, fridge, channel, value, unit) VALUES (%s,%s,%s,%s,%s)",
          (t1, fridge, "MXC", 0.01, "K"))
    _exec("INSERT INTO readings (ts, fridge, channel, value, unit) VALUES (%s,%s,%s,%s,%s)",
          (t2, fridge, "MXC", 0.02, "K"))
    latest = db.latest_reading(fridge, "MXC")
    assert latest is not None
    assert latest.value == 0.02 and latest.ts == t2 and latest.unit == "K"


def test_latest_reading_absent_returns_none(fridge):
    assert db.latest_reading(fridge, "MXC") is None


def test_latest_reading_ignores_far_future_ts(fridge):
    # A clock-skewed far-future row must not pin the "latest" reading (defense in
    # depth alongside ingest's own far-future rejection).
    t_now = datetime.now(timezone.utc)
    t_future = t_now + timedelta(days=30)
    _exec("INSERT INTO readings (ts, fridge, channel, value, unit) VALUES (%s,%s,%s,%s,%s)",
          (t_now, fridge, "MXC", 0.01, "K"))
    _exec("INSERT INTO readings (ts, fridge, channel, value, unit) VALUES (%s,%s,%s,%s,%s)",
          (t_future, fridge, "MXC", 99.0, "K"))
    latest = db.latest_reading(fridge, "MXC")
    assert latest is not None and latest.value == 0.01


def test_alert_state_roundtrip_and_upsert(fridge):
    assert db.get_alert_state(fridge, "SILENT") is None
    since = datetime(2026, 1, 1, tzinfo=timezone.utc)
    db.upsert_alert_state(fridge, "SILENT", "ALERTING", since, since)
    row = db.get_alert_state(fridge, "SILENT")
    assert row.state == "ALERTING" and row.since == since and row.last_notified == since
    # Upsert again -> ON CONFLICT DO UPDATE path.
    db.upsert_alert_state(fridge, "SILENT", "OK", since, None)
    row = db.get_alert_state(fridge, "SILENT")
    assert row.state == "OK" and row.last_notified is None


def test_list_alert_keys_returns_persisted_rows(fridge):
    since = datetime(2026, 1, 1, tzinfo=timezone.utc)
    db.upsert_alert_state(fridge, "STALE:MXC", "ALERTING", since, None)
    assert (fridge, "STALE:MXC") in db.list_alert_keys()
