"""Tests for the local SQLite spool — idempotency, ack lifecycle, pruning."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from parsers.base import Reading
from spool import Spool

UTC = timezone.utc


def _reading(minute: int, channel: str = "MXC", value: float = 0.01) -> Reading:
    return Reading(ts=datetime(2026, 6, 30, 0, minute, 0, tzinfo=UTC),
                   channel=channel, value=value, unit="K")


@pytest.fixture
def spool(tmp_path):
    s = Spool(str(tmp_path / "spool.sqlite"))
    yield s
    s.close()


def test_append_then_unacked_returns_rows(spool):
    spool.append([_reading(0), _reading(1)])
    pending = spool.unacked()
    assert len(pending) == 2
    assert {p["channel"] for p in pending} == {"MXC"}


def test_unacked_is_oldest_first(spool):
    spool.append([_reading(2), _reading(0), _reading(1)])
    minutes = [p["ts"] for p in spool.unacked()]
    assert minutes == sorted(minutes)


def test_append_is_idempotent_on_channel_ts(spool):
    # Re-reading the same log line (e.g. after a crash) must not duplicate rows.
    spool.append([_reading(0, value=1.0)])
    spool.append([_reading(0, value=2.0)])   # same (channel, ts)
    assert len(spool.unacked()) == 1


def test_mark_acked_removes_from_unacked(spool):
    spool.append([_reading(0), _reading(1)])
    spool.mark_acked(spool.unacked())
    assert spool.unacked() == []


def test_prune_removes_old_acked_only(spool):
    old, recent = _reading(0), _reading(5)
    spool.append([old, recent])
    spool.mark_acked(spool.unacked())                 # both acked
    # Cutoff between the two timestamps.
    cutoff = datetime(2026, 6, 30, 0, 3, 0, tzinfo=UTC)
    removed = spool.prune(cutoff)
    assert removed == 1                               # only the old acked row
    # An unacked old row must never be pruned.
    spool.append([_reading(0)])                       # re-add old as unacked
    assert spool.prune(cutoff) == 0
    assert len(spool.unacked()) == 1
