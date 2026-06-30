"""Integration tests for the DB layer against a live PostgreSQL.

Skipped unless CRYO_TEST_DSN points at a database with schema.sql applied:
    CRYO_TEST_DSN=postgresql://cryo@127.0.0.1:54329/cryo pytest -m integration

`scripts/dev_local.sh up` provisions exactly such a database. Each test uses a
unique fridge name and deletes its rows on teardown, so it is safe to run
against the dev database. The connecting role must have DELETE for cleanup
(dev_local grants it; the production app role intentionally does not).
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from ingest import db

DSN = os.environ.get("CRYO_TEST_DSN")

# Locally a missing DSN skips these (no DB needed for unit work). In CI we set
# CRYO_REQUIRE_INTEGRATION so a missing/typo'd DSN is a hard error instead of a
# silent skip-to-green that would leave the DB layer untested.
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
    """Open the connection pool once for the whole module."""
    db.init_pool(DSN)
    yield
    db.close_pool()


@pytest.fixture
def fridge(_pool):
    """A unique fridge name; all its rows are deleted after the test."""
    name = f"test_{uuid.uuid4().hex[:8]}"
    yield name
    # All three deletes share one transaction (the `with` block): they commit
    # together or roll back together, so a failure can't leave rows in only some
    # tables. Covers every table the integration tests write to.
    with db._get_pool().connection() as conn:
        for table in ("readings", "last_seen", "maintenance"):
            conn.execute(f"DELETE FROM {table} WHERE fridge = %s", (name,))


def _latest_seen(fridge: str) -> datetime:
    with db._get_pool().connection() as conn:
        row = conn.execute("SELECT last_ts FROM last_seen WHERE fridge = %s", (fridge,)).fetchone()
    return row[0]


def test_insert_is_idempotent(fridge):
    ts = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows = [(ts, fridge, "MXC", 0.0102, "K")]
    assert db.insert_readings(fridge, rows) == 1   # first insert lands
    assert db.insert_readings(fridge, rows) == 0   # ON CONFLICT DO NOTHING


def test_last_seen_advances_to_batch_max(fridge):
    t1 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    t2 = t1 + timedelta(seconds=30)
    db.insert_readings(fridge, [(t1, fridge, "MXC", 0.01, "K"), (t2, fridge, "4K", 3.9, "K")])
    assert _latest_seen(fridge) == t2


def test_last_seen_never_goes_backward(fridge):
    t_new = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    t_old = datetime(2026, 1, 1, 6, 0, tzinfo=timezone.utc)
    db.insert_readings(fridge, [(t_new, fridge, "MXC", 0.01, "K")])
    # A late backfill batch with older timestamps must not lower last_seen.
    db.insert_readings(fridge, [(t_old, fridge, "MXC", 0.02, "K")])
    assert _latest_seen(fridge) == t_new


def test_insert_maintenance_creates_window(fridge):
    # Exercises the make_interval(mins => %s) SQL path end-to-end.
    db.insert_maintenance(fridge, 60, "regen", "ben")
    with db._get_pool().connection() as conn:
        row = conn.execute(
            "SELECT reason, set_by, EXTRACT(EPOCH FROM (until_ts - now())) / 60 "
            "FROM maintenance WHERE fridge = %s",
            (fridge,),
        ).fetchone()
    reason, set_by, minutes_out = row
    assert reason == "regen"
    assert set_by == "ben"
    assert 59 <= minutes_out <= 61   # until_ts is ~60 minutes from now
