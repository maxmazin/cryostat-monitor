"""Tests for the local SQLite spool — idempotency, ack lifecycle, pruning,
corruption recovery."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from parsers.base import Reading
from spool import Spool, open_or_recover

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


# --------------------------------------------------------------------------- corruption recovery
def test_open_or_recover_quarantines_corrupt_db(tmp_path, caplog):
    # A corrupt spool.sqlite must not crash-loop the daemon: the file is
    # quarantined — renamed, not deleted, so buffered-but-unsent rows stay on
    # disk for manual recovery — and a fresh spool works. (A stale -wal sidecar
    # may already be unlinked by sqlite itself during the failed open; the
    # recovery renames whichever sidecars still exist.)
    garbage = b"definitely not a sqlite database " * 8
    path = tmp_path / "spool.sqlite"
    path.write_bytes(garbage)
    (tmp_path / "spool.sqlite-wal").write_bytes(b"stale wal junk")

    spool = open_or_recover(str(path))
    spool.append([_reading(0)])
    assert len(spool.unacked()) == 1          # fresh spool is functional
    spool.close()

    quarantined = [p for p in tmp_path.glob("spool.sqlite.corrupt-*")
                   if not p.name.endswith(("-wal", "-shm"))]
    assert len(quarantined) == 1                     # the corrupt db was renamed...
    assert quarantined[0].read_bytes() == garbage    # ...with its bytes intact
    assert not (tmp_path / "spool.sqlite-wal").exists()   # no stale sidecar left
    assert any("quarantin" in r.getMessage() for r in caplog.records)


def test_open_or_recover_reraises_transient_lock_without_quarantine(tmp_path, monkeypatch):
    # "database is locked" (AV/backup holding the file) is transient, not
    # corruption: quarantining a healthy spool would silently drop its un-acked
    # rows from the pipeline. The error must propagate so the caller retries.
    import sqlite3

    import spool as spool_module

    path = tmp_path / "spool.sqlite"
    Spool(str(path)).close()            # a real, healthy spool file

    def locked(_path):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(spool_module, "Spool", locked)
    with pytest.raises(sqlite3.OperationalError):
        open_or_recover(str(path))
    assert path.exists()                                    # untouched
    assert not list(tmp_path.glob("*.corrupt-*"))           # no quarantine


def test_open_or_recover_leaves_healthy_db_alone(tmp_path):
    path = tmp_path / "spool.sqlite"
    first = Spool(str(path))
    first.append([_reading(0)])
    first.close()

    spool = open_or_recover(str(path))
    assert len(spool.unacked()) == 1          # existing rows preserved
    spool.close()
    assert list(tmp_path.glob("*.corrupt-*")) == []
