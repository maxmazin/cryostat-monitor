"""Tests for the daemon's LogTailer and poll cycle.

The tailer/spool tests cover the Phase 1 acceptance criteria at unit level:
after a daemon restart or a network outage, the gap is backfilled with zero
duplicate rows.
"""
from __future__ import annotations

from datetime import timezone
from zoneinfo import ZoneInfo

import pytest

from daemon import ConfigError, LogTailer, load_parser, run_cycle, to_utc, validate_config
from parsers.blackfridge import BlackfridgeParser
from parsers.whitefridge import WhitefridgeParser
from spool import Spool

UTC = ZoneInfo("UTC")


def _append_bytes(path, text: str) -> None:
    with open(path, "ab") as fh:
        fh.write(text.encode("ascii"))


def _ch6_line(minute: int) -> str:
    # A real-format BlueFors CH6 (MXC) temperature line, CRLF-terminated.
    return f" 30-06-26,00:{minute:02d}:32,1.020050E+2\r\n"


# --------------------------------------------------------------------------- LogTailer
def test_reads_complete_lines_once(tmp_path):
    f = tmp_path / "CH6 T 26-06-30.log"
    _append_bytes(f, _ch6_line(0) + _ch6_line(1))
    tailer = LogTailer([str(tmp_path / "CH6 T *.log")], str(tmp_path / "state.json"))

    out = tailer.read_new_lines()
    assert len(out) == 1
    source, lines = out[0]
    assert source == "CH6 T 26-06-30.log"
    assert len(lines) == 2
    # Nothing new on the next poll.
    assert tailer.read_new_lines() == []


def test_picks_up_appended_lines(tmp_path):
    f = tmp_path / "CH6 T 26-06-30.log"
    _append_bytes(f, _ch6_line(0))
    state = str(tmp_path / "state.json")
    tailer = LogTailer([str(tmp_path / "CH6 T *.log")], state)
    tailer.read_new_lines()
    _append_bytes(f, _ch6_line(1) + _ch6_line(2))
    out = tailer.read_new_lines()
    assert [src for src, _ in out] == ["CH6 T 26-06-30.log"]
    assert len(out[0][1]) == 2          # only the two new lines


def test_holds_back_partial_final_line(tmp_path):
    f = tmp_path / "CH6 T 26-06-30.log"
    state = str(tmp_path / "state.json")
    tailer = LogTailer([str(tmp_path / "CH6 T *.log")], state)
    # A complete line followed by a partial (no trailing newline yet).
    _append_bytes(f, _ch6_line(0) + " 30-06-26,00:01:32,1.0")
    out = tailer.read_new_lines()
    assert len(out[0][1]) == 1          # only the complete line
    # The rest of the partial line arrives.
    _append_bytes(f, "20050E+2\r\n")
    out = tailer.read_new_lines()
    assert out[0][1] == [" 30-06-26,00:01:32,1.020050E+2"]


def test_resumes_after_restart_and_reads_gap(tmp_path):
    # Acceptance (a): the daemon is down while the logger keeps writing; on
    # restart it reads exactly the gap, no re-reads.
    f = tmp_path / "CH6 T 26-06-30.log"
    state = str(tmp_path / "state.json")
    _append_bytes(f, _ch6_line(0) + _ch6_line(1))
    t1 = LogTailer([str(tmp_path / "CH6 T *.log")], state)             # tailer #1
    t1.read_new_lines()
    t1.commit()                                                        # readings spooled
    # ...daemon down; logger appends two more lines...
    _append_bytes(f, _ch6_line(2) + _ch6_line(3))
    tailer2 = LogTailer([str(tmp_path / "CH6 T *.log")], state)         # restart
    out = tailer2.read_new_lines()
    assert len(out[0][1]) == 2          # only the gap, not the first two again


def test_offsets_not_persisted_until_commit(tmp_path):
    # Durability: a crash after reading but before the spool commit must NOT
    # advance the persisted offset — on restart the lines are re-read (and later
    # dedup'd by the spool) rather than lost.
    f = tmp_path / "CH6 T 26-06-30.log"
    state = str(tmp_path / "state.json")
    _append_bytes(f, _ch6_line(0) + _ch6_line(1))
    t1 = LogTailer([str(tmp_path / "CH6 T *.log")], state)
    t1.read_new_lines()                 # read, but crash before commit() (no commit)
    tailer2 = LogTailer([str(tmp_path / "CH6 T *.log")], state)  # restart
    out = tailer2.read_new_lines()
    assert len(out[0][1]) == 2          # re-reads the same lines (no offset advanced)


def test_new_file_read_from_start(tmp_path):
    # Midnight rotation: a new dated file appears and is read from offset 0.
    state = str(tmp_path / "state.json")
    f1 = tmp_path / "CH6 T 26-06-29.log"
    _append_bytes(f1, _ch6_line(0))
    tailer = LogTailer([str(tmp_path / "CH6 T *.log")], state)
    tailer.read_new_lines()
    f2 = tmp_path / "CH6 T 26-06-30.log"
    _append_bytes(f2, _ch6_line(0) + _ch6_line(1))
    out = dict(tailer.read_new_lines())
    assert len(out["CH6 T 26-06-30.log"]) == 2


def test_resets_offset_on_truncation(tmp_path):
    f = tmp_path / "CH6 T 26-06-30.log"
    state = str(tmp_path / "state.json")
    _append_bytes(f, _ch6_line(0) + _ch6_line(1) + _ch6_line(2))
    tailer = LogTailer([str(tmp_path / "CH6 T *.log")], state)
    tailer.read_new_lines()
    # File shrinks (e.g. replaced/rewound) -> read from the start again.
    f.write_bytes(_ch6_line(9).encode("ascii"))
    out = tailer.read_new_lines()
    assert len(out[0][1]) == 1


def test_resets_offset_on_inode_change(tmp_path):
    f = tmp_path / "CH6 T 26-06-30.log"
    state = str(tmp_path / "state.json")
    _append_bytes(f, _ch6_line(0) + _ch6_line(1))
    tailer = LogTailer([str(tmp_path / "CH6 T *.log")], state)
    tailer.read_new_lines()
    # Replace the file with a different inode (in-place rotation).
    replacement = tmp_path / "replacement.tmp"
    _append_bytes(replacement, _ch6_line(5))
    import os
    os.replace(replacement, f)
    out = tailer.read_new_lines()
    assert len(out[0][1]) == 1          # new inode -> read from 0


def _age(path, days: float) -> None:
    import os
    import time
    t = time.time() - days * 86400
    os.utime(path, (t, t))


def test_skips_years_of_backlog_by_default(tmp_path):
    # A years-old dated file is skipped as backlog; the current day's file ships.
    state = str(tmp_path / "state.json")
    old = tmp_path / "CH6 T 20-01-01.log"
    new = tmp_path / "CH6 T 26-06-30.log"
    _append_bytes(old, _ch6_line(0))
    _append_bytes(new, _ch6_line(1))
    _age(old, 400)
    tailer = LogTailer([str(tmp_path / "CH6 T *.log")], state)   # backfill off (default)
    out = dict(tailer.read_new_lines())
    assert "CH6 T 26-06-30.log" in out
    assert "CH6 T 20-01-01.log" not in out
    assert str(old) not in tailer.state          # old backlog never enters state


def test_backfill_true_reads_old_files(tmp_path):
    state = str(tmp_path / "state.json")
    old = tmp_path / "CH6 T 20-01-01.log"
    _append_bytes(old, _ch6_line(0))
    _age(old, 400)
    tailer = LogTailer([str(tmp_path / "CH6 T *.log")], state, backfill=True)
    out = dict(tailer.read_new_lines())
    assert "CH6 T 20-01-01.log" in out           # full-history mode reads it


def test_prunes_state_for_files_aged_out_of_window(tmp_path):
    state = str(tmp_path / "state.json")
    f = tmp_path / "CH6 T 26-06-30.log"
    _append_bytes(f, _ch6_line(0))
    tailer = LogTailer([str(tmp_path / "CH6 T *.log")], state)
    tailer.read_new_lines()
    assert str(f) in tailer.state
    _age(f, 10)                                   # older than the 2-day window
    tailer.read_new_lines()
    assert str(f) not in tailer.state             # offsets.json stays bounded


# --------------------------------------------------------------------------- helpers
def test_to_utc_localizes_naive_timestamp():
    from datetime import datetime
    naive = datetime(2026, 6, 30, 12, 0, 0)
    got = to_utc(naive, ZoneInfo("America/Los_Angeles"))
    assert got.tzinfo == timezone.utc
    assert got == datetime(2026, 6, 30, 19, 0, 0, tzinfo=timezone.utc)  # PDT = UTC-7


def test_dry_run_parses_without_persisting(tmp_path, capsys):
    # --dry-run parses the current logs and reports what it WOULD post, without
    # creating a spool or persisting offsets (so it's safe to run repeatedly on a
    # live host before going live).
    from daemon import run_dry
    f = tmp_path / "CH6 T 26-06-30.log"
    _append_bytes(f, _ch6_line(0) + _ch6_line(1))
    cfg = {
        "fridge": "blackfridge",
        "parser": "blackfridge",
        "timezone": "UTC",
        "log_globs": [str(tmp_path / "CH6 T *.log")],
    }
    readings = run_dry(cfg)
    assert len(readings) == 2
    assert all(r.channel == "MXC" and r.ts.tzinfo is not None for r in readings)
    assert not (tmp_path / "offsets.json").exists()   # nothing persisted
    out = capsys.readouterr().out
    assert "DRY RUN" in out and "MXC" in out


def test_failed_append_re_reads_next_cycle(tmp_path):
    # Robustness: a spool failure mid-cycle must not skip the batch. After the
    # in-memory offset rollback, the next cycle re-reads the same lines.
    f, tailer, spool = _setup(tmp_path)
    parser = BlackfridgeParser()
    _append_bytes(f, _ch6_line(0) + _ch6_line(1))

    class BoomSpool:
        def append(self, readings):
            raise RuntimeError("disk full")

    with pytest.raises(RuntimeError):
        run_cycle(tailer, parser, BoomSpool(), UTC, lambda rows: True)

    sent: list[dict] = []
    ok = lambda rows: (sent.extend(rows), True)[1]
    run_cycle(tailer, parser, spool, UTC, ok)   # same tailer: must re-read
    assert len(sent) == 2


# --------------------------------------------------------------------------- config validation
def _valid_cfg(**over):
    cfg = {"fridge": "whitefridge", "parser": "whitefridge", "timezone": "UTC",
           "server_url": "https://x/ingest", "token": "t", "log_globs": ["x"]}
    cfg.update(over)
    return cfg


def test_validate_config_accepts_valid():
    validate_config(_valid_cfg(), require_network=True)   # no raise


def test_validate_config_requires_network_keys_only_when_asked():
    cfg = _valid_cfg()
    del cfg["server_url"], cfg["token"]
    validate_config(cfg, require_network=False)            # ok for --dry-run
    with pytest.raises(ConfigError):
        validate_config(cfg, require_network=True)


def test_validate_config_timezone_optional_defaults_to_lab_zone():
    cfg = _valid_cfg()
    del cfg["timezone"]
    validate_config(cfg, require_network=True)   # no raise: falls back to the default


def test_validate_config_requires_log_globs():
    cfg = _valid_cfg()
    del cfg["log_globs"]
    with pytest.raises(ConfigError):
        validate_config(cfg, require_network=True)


def test_validate_config_rejects_bad_timezone():
    with pytest.raises(ConfigError):
        validate_config(_valid_cfg(timezone="Not/AZone"), require_network=True)


def test_load_parser_returns_instance():
    assert isinstance(load_parser("blackfridge"), BlackfridgeParser)


def test_load_parser_picks_module_class_not_imported_base():
    # whitefridge.py imports BlueforsParser; the loader must return the
    # fridge-specific subclass, not the imported base.
    assert isinstance(load_parser("whitefridge"), WhitefridgeParser)


# --------------------------------------------------------------------------- full cycle / backfill
def _setup(tmp_path):
    f = tmp_path / "CH6 T 26-06-30.log"
    tailer = LogTailer([str(tmp_path / "CH6 T *.log")], str(tmp_path / "state.json"))
    spool = Spool(str(tmp_path / "spool.sqlite"))
    return f, tailer, spool


def _keys(rows):
    return [(r["channel"], r["ts"]) for r in rows]


def test_cycle_backfills_after_network_outage(tmp_path):
    # Acceptance (b): POST fails for a while; rows accumulate un-acked and flush
    # on recovery with zero duplicates.
    f, tailer, spool = _setup(tmp_path)
    parser = BlackfridgeParser()
    sent: list[dict] = []

    fail = lambda rows: False
    ok = lambda rows: (sent.extend(rows), True)[1]

    _append_bytes(f, _ch6_line(0) + _ch6_line(1))
    run_cycle(tailer, parser, spool, UTC, fail)
    assert len(spool.unacked()) == 2          # nothing left

    _append_bytes(f, _ch6_line(2))
    run_cycle(tailer, parser, spool, UTC, fail)
    assert len(spool.unacked()) == 3

    run_cycle(tailer, parser, spool, UTC, ok)  # network back
    assert spool.unacked() == []
    assert len(sent) == 3
    assert len(set(_keys(sent))) == 3          # zero duplicates


def test_cycle_zero_duplicates_across_restart(tmp_path):
    # Acceptance (a): persisted spool + offsets mean a restart backfills the gap
    # exactly once.
    f, tailer, spool = _setup(tmp_path)
    parser = BlackfridgeParser()
    sent: list[dict] = []
    ok = lambda rows: (sent.extend(rows), True)[1]

    _append_bytes(f, _ch6_line(0) + _ch6_line(1))
    run_cycle(tailer, parser, spool, UTC, ok)
    spool.close()

    # ...daemon down; logger writes more...
    _append_bytes(f, _ch6_line(2) + _ch6_line(3))

    # Restart: reopen spool and tailer from the same persisted state.
    tailer2 = LogTailer([str(tmp_path / "CH6 T *.log")], str(tmp_path / "state.json"))
    spool2 = Spool(str(tmp_path / "spool.sqlite"))
    run_cycle(tailer2, parser, spool2, UTC, ok)

    assert len(sent) == 4
    assert len(set(_keys(sent))) == 4          # all distinct, nothing re-sent
