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
    """A unique fridge name; rows are deleted after the test."""
    name = f"test_{uuid.uuid4().hex[:8]}"
    yield name
    with db._get_pool().connection() as conn:
        conn.execute("DELETE FROM readings WHERE fridge = %s", (name,))
        conn.execute("DELETE FROM last_seen WHERE fridge = %s", (name,))


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
