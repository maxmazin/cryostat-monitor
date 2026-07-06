"""Tests for the daemon's LogTailer and poll cycle.

The tailer/spool tests cover the Phase 1 acceptance criteria at unit level:
after a daemon restart or a network outage, the gap is backfilled with zero
duplicate rows.
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

import daemon
from daemon import (ConfigError, LogTailer, load_parser, post_batch, run_cycle,
                    to_utc, validate_config)
from parsers.base import Parser, Reading
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


# --------------------------------------------------------------------------- non-finite values
def test_run_cycle_drops_non_finite_readings(tmp_path, caplog):
    # inf/nan pass float() in the parsers but poison the pipeline downstream
    # (NaN -> SQL NULL silently dropped; inf -> invalid JSON the server rejects
    # forever). run_cycle must filter them out before the spool, loudly.
    f, tailer, spool = _setup(tmp_path)
    _append_bytes(f, _ch6_line(0))

    class NonFiniteParser(Parser):
        def parse_new(self, source, lines):
            base = datetime(2026, 6, 30, 0, 0, 0)
            return [
                Reading(ts=base, channel="MXC", value=float("inf"), unit="K"),
                Reading(ts=base.replace(minute=1), channel="MXC",
                        value=float("nan"), unit="K"),
                Reading(ts=base.replace(minute=2), channel="MXC", value=1.5, unit="K"),
            ]

    with caplog.at_level(logging.WARNING):
        run_cycle(tailer, NonFiniteParser(), spool, UTC, lambda rows: False)
    assert [p["value"] for p in spool.unacked()] == [1.5]   # only the finite one
    warned = [r for r in caplog.records if "non-finite" in r.getMessage()]
    assert len(warned) == 2                                 # one per bad reading


# --------------------------------------------------------------------------- post_batch
class _FakeResp:
    def __init__(self, status_code, body=None, text="", headers=None):
        self.status_code = status_code
        self._body = body
        self.text = text
        self.headers = headers or {}

    def json(self):
        if self._body is None:
            raise ValueError("not JSON")
        return self._body


def _patch_post(monkeypatch, resp):
    seen = {}

    def fake_post(url, **kwargs):
        seen["url"] = url
        seen.update(kwargs)
        return resp

    monkeypatch.setattr(daemon.requests, "post", fake_post)
    return seen


_ROWS = [{"ts": "2026-06-30T00:00:00+00:00", "channel": "MXC", "value": 0.01, "unit": "K"}]


def test_post_batch_acks_ingest_contract_response(monkeypatch):
    _patch_post(monkeypatch, _FakeResp(200, body={"received": 1}))
    assert post_batch("https://x/ingest", "t", "f", _ROWS) is True


def test_post_batch_redirect_is_not_followed_and_not_acked(monkeypatch, caplog):
    # 301/302/303 would turn the POST into a GET; a landing page's 200 would then
    # ack (and later prune) rows that never reached the server.
    seen = _patch_post(monkeypatch, _FakeResp(302, headers={"Location": "https://portal/login"}))
    with caplog.at_level(logging.ERROR):
        assert post_batch("https://x/ingest", "t", "f", _ROWS) is False
    assert seen["allow_redirects"] is False
    assert any("server_url" in r.getMessage() for r in caplog.records)


def test_post_batch_2xx_html_body_is_not_acked(monkeypatch, caplog):
    # A captive portal / wrong endpoint returning 200 with HTML must not ack.
    _patch_post(monkeypatch, _FakeResp(200, body=None, text="<html>welcome</html>"))
    with caplog.at_level(logging.ERROR):
        assert post_batch("https://x/ingest", "t", "f", _ROWS) is False
    assert any("ingest" in r.getMessage() for r in caplog.records)


def test_post_batch_2xx_json_without_received_is_not_acked(monkeypatch):
    _patch_post(monkeypatch, _FakeResp(200, body={"status": "ok"}))
    assert post_batch("https://x/ingest", "t", "f", _ROWS) is False


def test_post_batch_401_logs_token_error(monkeypatch, caplog):
    _patch_post(monkeypatch, _FakeResp(401, body={}, text="unauthorized"))
    with caplog.at_level(logging.ERROR):
        assert post_batch("https://x/ingest", "t", "f", _ROWS) is False
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert any("token" in r.getMessage() for r in errors)


# --------------------------------------------------------------------------- per-cycle read budget
def test_read_budget_drains_large_file_across_cycles(tmp_path, monkeypatch):
    # A file larger than the per-cycle budget is consumed over several cycles
    # with no lost or duplicated lines (backfill=true stays bounded in memory).
    monkeypatch.setattr(daemon, "READ_BUDGET_BYTES", 150)
    monkeypatch.setattr(daemon, "FILE_CHUNK_BYTES", 100)
    f = tmp_path / "CH6 T 26-06-30.log"
    _append_bytes(f, "".join(_ch6_line(m) for m in range(10)))   # 320 bytes
    tailer = LogTailer([str(tmp_path / "CH6 T *.log")], str(tmp_path / "state.json"))

    got: list[str] = []
    first = tailer.read_new_lines()
    for _, lines in first:
        got.extend(lines)
    assert 0 < len(got) < 10            # the budget actually truncated the read
    for _ in range(10):                 # subsequent cycles drain the rest
        for _, lines in tailer.read_new_lines():
            got.extend(lines)
    assert got == [f" 30-06-26,00:{m:02d}:32,1.020050E+2" for m in range(10)]


def test_budget_exhaustion_defers_last_run_advance(tmp_path, monkeypatch):
    # If the budget ran out before every candidate file was visited, advancing
    # last_run would move the backlog cutoff past never-read files and strand
    # them. The anchor must hold until a cycle visits everything.
    monkeypatch.setattr(daemon, "READ_BUDGET_BYTES", 30)   # < one 32-byte line
    monkeypatch.setattr(daemon, "FILE_CHUNK_BYTES", 30)
    a = tmp_path / "CH1 T 26-06-30.log"
    b = tmp_path / "CH6 T 26-06-30.log"
    _append_bytes(a, _ch6_line(0))
    _append_bytes(b, _ch6_line(1))
    tailer = LogTailer([str(tmp_path / "CH* T *.log")], str(tmp_path / "state.json"))

    tailer.read_new_lines()             # budget dies inside file a; b never visited
    tailer.commit()
    assert tailer.last_run is None      # anchor must not advance past b

    monkeypatch.setattr(daemon, "READ_BUDGET_BYTES", 10_000)
    monkeypatch.setattr(daemon, "FILE_CHUNK_BYTES", 10_000)
    tailer.read_new_lines()             # now everything is visited
    tailer.commit()
    assert tailer.last_run is not None


def test_read_error_defers_last_run_advance(tmp_path):
    # A transient OSError on one file during a catch-up must hold last_run back
    # like an unvisited file: advancing it would age the unread file past the
    # cutoff and strand it permanently.
    a = tmp_path / "CH1 T 26-06-30.log"
    b = tmp_path / "CH6 T 26-06-30.log"
    _append_bytes(a, _ch6_line(0))
    _append_bytes(b, _ch6_line(1))
    tailer = LogTailer([str(tmp_path / "CH* T *.log")], str(tmp_path / "state.json"))

    real_read = tailer._read_file

    def flaky(path, cutoff, max_bytes):
        if "CH1" in path:
            raise OSError("AV lock")
        return real_read(path, cutoff, max_bytes)

    tailer._read_file = flaky
    tailer.read_new_lines()
    tailer.commit()
    assert tailer.last_run is None      # anchor must not advance past file a

    tailer._read_file = real_read
    tailer.read_new_lines()
    tailer.commit()
    assert tailer.last_run is not None


def test_chunk_truncated_file_defers_last_run_advance(tmp_path, monkeypatch):
    # A file cut short by the per-file chunk cap (budget NOT exhausted) is still
    # an incomplete drain: last_run must hold until its remainder ships,
    # otherwise the prefilter/prune can age the leftover out of the window.
    monkeypatch.setattr(daemon, "READ_BUDGET_BYTES", 1_000)
    monkeypatch.setattr(daemon, "FILE_CHUNK_BYTES", 100)
    f = tmp_path / "CH6 T 26-06-30.log"
    _append_bytes(f, "".join(_ch6_line(m) for m in range(10)))   # 320 bytes
    tailer = LogTailer([str(tmp_path / "CH6 T *.log")], str(tmp_path / "state.json"))

    tailer.read_new_lines()             # chunk cap truncates the only file
    tailer.commit()
    assert tailer.last_run is None

    monkeypatch.setattr(daemon, "FILE_CHUNK_BYTES", 10_000)
    tailer.read_new_lines()             # remainder drains
    tailer.commit()
    assert tailer.last_run is not None


def test_prune_keeps_entry_with_unshipped_bytes(tmp_path):
    # An entry whose offset is behind the file's size still has data to ship
    # (a gap file mid-drain that fell out of the candidate list); pruning it
    # would orphan the remainder.
    f = tmp_path / "CH6 T 26-06-27.log"
    _append_bytes(f, _ch6_line(0))
    tailer = LogTailer([str(tmp_path / "CH6 T *.log")], str(tmp_path / "state.json"))
    other = str(tmp_path / "CH1 T 26-06-30.log")
    sig = tailer._signature(os.stat(f))

    tailer.state[str(f)] = {"sig": sig, "offset": 10}       # mid-drain
    tailer._prune_state([other], time.time() + 60)
    assert str(f) in tailer.state                           # kept

    tailer.state[str(f)]["offset"] = os.stat(f).st_size     # fully shipped
    tailer._prune_state([other], time.time() + 60)
    assert str(f) not in tailer.state                       # now prunable


# --------------------------------------------------------------------------- last_run-anchored cutoff
def test_outage_longer_than_window_still_reads_gap(tmp_path):
    # A host powered off Thu->Mon: gap files have mtimes older than the 2-day
    # window but newer than the persisted last_run — they must ship on restart,
    # not be silently skipped as "historical backlog".
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"files": {}, "last_run": time.time() - 5 * 86400}))
    f = tmp_path / "CH6 T 26-06-27.log"
    _append_bytes(f, _ch6_line(0))
    _age(f, 3)                          # written mid-outage
    tailer = LogTailer([str(tmp_path / "CH6 T *.log")], str(state))
    out = dict(tailer.read_new_lines())
    assert "CH6 T 26-06-27.log" in out


def test_commit_persists_last_run_across_restart(tmp_path):
    f = tmp_path / "CH6 T 26-06-30.log"
    state = str(tmp_path / "state.json")
    _append_bytes(f, _ch6_line(0))
    t1 = LogTailer([str(tmp_path / "CH6 T *.log")], state)
    assert t1.last_run is None          # true first run
    t1.read_new_lines()
    t1.commit()
    assert t1.last_run is not None
    t2 = LogTailer([str(tmp_path / "CH6 T *.log")], state)
    assert t2.last_run == pytest.approx(t1.last_run)


@pytest.mark.parametrize("legacy_form", ["ino", "ctime"])
def test_legacy_flat_offsets_load_and_resume_without_reread(tmp_path, legacy_form):
    # The deployed whitefridge host has the pre-restructure state: a flat
    # {path: {sig: "ino:X"/"ctime:Y", offset: N}} map. It must load cleanly and
    # keep its offsets — re-reading everything would re-ship days of data.
    f = tmp_path / "CH6 T 26-06-30.log"
    _append_bytes(f, _ch6_line(0) + _ch6_line(1))
    st = os.stat(f)
    sig = f"ino:{st.st_ino}" if legacy_form == "ino" else f"ctime:{st.st_ctime_ns}"
    state = tmp_path / "state.json"
    state.write_text(json.dumps({str(f): {"sig": sig, "offset": st.st_size}}))

    tailer = LogTailer([str(tmp_path / "CH6 T *.log")], str(state))
    assert tailer.last_run is None                # flat format has no last_run
    assert tailer.read_new_lines() == []          # offset honored: no re-read
    _append_bytes(f, _ch6_line(2))
    out = tailer.read_new_lines()
    assert len(out[0][1]) == 1                    # only the newly appended line


# --------------------------------------------------------------------------- prune guards
def test_empty_glob_does_not_wipe_offset_state(tmp_path, caplog):
    # A transient share hiccup can make the glob come back empty; wiping every
    # offset then would re-read all files when the share recovers.
    f = tmp_path / "CH6 T 26-06-30.log"
    _append_bytes(f, _ch6_line(0))
    tailer = LogTailer([str(tmp_path / "CH6 T *.log")], str(tmp_path / "state.json"))
    tailer.read_new_lines()
    assert str(f) in tailer.state
    os.remove(f)                                  # glob now matches nothing
    with caplog.at_level(logging.WARNING):
        tailer.read_new_lines()
    assert str(f) in tailer.state                 # prune skipped, state intact
    assert any("matched no files" in r.getMessage() for r in caplog.records)


def test_stat_error_during_prune_keeps_entry(tmp_path, monkeypatch):
    f = tmp_path / "CH6 T 26-06-30.log"
    _append_bytes(f, _ch6_line(0))
    tailer = LogTailer([str(tmp_path / "CH6 T *.log")], str(tmp_path / "state.json"))
    tailer.read_new_lines()
    assert str(f) in tailer.state

    real_stat = os.stat

    def flaky_stat(path, *args, **kwargs):
        if str(path) == str(f):
            raise OSError("share hiccup")
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(daemon.os, "stat", flaky_stat)
    # Aggressive cutoff: if stat worked, the entry would be pruned. The OSError
    # must keep it instead of deleting a live file's offset.
    tailer._prune_state([str(f)], cutoff=time.time() + 1000)
    assert str(f) in tailer.state


# --------------------------------------------------------------------------- unreadable-file warnings
def test_unreadable_file_warns_first_and_then_hourly(tmp_path, caplog):
    f = tmp_path / "CH6 T 26-06-30.log"
    _append_bytes(f, _ch6_line(0))
    tailer = LogTailer([str(tmp_path / "CH6 T *.log")], str(tmp_path / "state.json"))
    real_read = tailer._read_file
    mode = {"fail": True}

    def maybe_boom(path, cutoff, max_bytes):
        if mode["fail"]:
            raise OSError("locked by AV")
        return real_read(path, cutoff, max_bytes)

    tailer._read_file = maybe_boom
    with caplog.at_level(logging.WARNING):
        for _ in range(61):
            tailer.read_new_lines()
    warned = [r for r in caplog.records if "cannot read" in r.getMessage()]
    assert len(warned) == 2                       # 1st and 61st failure only

    mode["fail"] = False
    tailer.read_new_lines()                       # success resets the counter
    mode["fail"] = True
    with caplog.at_level(logging.WARNING):
        tailer.read_new_lines()
    warned = [r for r in caplog.records if "cannot read" in r.getMessage()]
    assert len(warned) == 3                       # fresh failure warns again


# --------------------------------------------------------------------------- signature stability
def test_same_file_signature_logic():
    same = LogTailer._same_file
    assert same({"ino": 7, "ctime": 1}, {"ino": 7, "ctime": 2})       # ino wins
    assert not same({"ino": 7, "ctime": 1}, {"ino": 8, "ctime": 1})   # replaced
    assert same({"ino": 0, "ctime": 5}, {"ino": 7, "ctime": 5})       # ctime fallback
    assert not same({"ino": 0, "ctime": 5}, {"ino": 0, "ctime": 6})


def test_ino_availability_flap_does_not_reset_offset(tmp_path, monkeypatch):
    # Windows shares / AV interference can make st_ino flap between real and 0
    # across polls. That must NOT look like a replaced file (offset reset would
    # re-read and re-ship the whole file every flip). Simulate Windows stat
    # semantics: st_ctime is the CREATION time, stable across appends (unlike
    # POSIX, where it shifts on every write — which is why the real ctime can't
    # be used here).
    f = tmp_path / "CH6 T 26-06-30.log"
    _append_bytes(f, _ch6_line(0) + _ch6_line(1))
    real_stat = os.stat
    creation_ctime = real_stat(f).st_ctime_ns
    mode = {"ino_available": True}

    def windows_stat(path, *args, **kwargs):
        st = real_stat(path, *args, **kwargs)
        if str(path) == str(f):
            return SimpleNamespace(st_ino=st.st_ino if mode["ino_available"] else 0,
                                   st_ctime_ns=creation_ctime,
                                   st_size=st.st_size, st_mtime=st.st_mtime)
        return st

    monkeypatch.setattr(daemon.os, "stat", windows_stat)
    tailer = LogTailer([str(tmp_path / "CH6 T *.log")], str(tmp_path / "state.json"))
    assert len(tailer.read_new_lines()[0][1]) == 2

    _append_bytes(f, _ch6_line(2))
    mode["ino_available"] = False                    # ino flaps to 0
    out = tailer.read_new_lines()
    assert len(out[0][1]) == 1          # only the new line: offset survived

    _append_bytes(f, _ch6_line(3))
    mode["ino_available"] = True                     # ino comes back
    out = tailer.read_new_lines()
    assert len(out[0][1]) == 1          # offset survived the flap back too


# --------------------------------------------------------------------------- dated-folder prefilter
def test_old_dated_folders_are_skipped_without_stat(tmp_path, monkeypatch):
    old_dir = tmp_path / "20-01-01"
    old_dir.mkdir()
    old = old_dir / "CH6 T 20-01-01.log"
    _append_bytes(old, _ch6_line(0))
    today = time.strftime("%y-%m-%d")
    new_dir = tmp_path / today
    new_dir.mkdir()
    new = new_dir / f"CH6 T {today}.log"
    _append_bytes(new, _ch6_line(1))

    statted: list[str] = []
    real_stat = os.stat

    def recording_stat(path, *args, **kwargs):
        statted.append(str(path))
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(daemon.os, "stat", recording_stat)
    tailer = LogTailer([str(tmp_path / "*" / "CH6 T *.log")], str(tmp_path / "state.json"))
    out = dict(tailer.read_new_lines())
    assert f"CH6 T {today}.log" in out
    assert "CH6 T 20-01-01.log" not in out
    assert str(old) not in statted      # dismissed by folder name, never statted


def test_unparseable_folder_name_is_still_processed(tmp_path):
    # Conservative: only a folder that PARSES as an old date is skipped.
    weird = tmp_path / "misc"
    weird.mkdir()
    f = weird / "CH6 T 26-06-30.log"
    _append_bytes(f, _ch6_line(0))
    tailer = LogTailer([str(tmp_path / "*" / "CH6 T *.log")], str(tmp_path / "state.json"))
    out = dict(tailer.read_new_lines())
    assert "CH6 T 26-06-30.log" in out


def test_backfill_reads_old_dated_folders(tmp_path):
    old_dir = tmp_path / "20-01-01"
    old_dir.mkdir()
    old = old_dir / "CH6 T 20-01-01.log"
    _append_bytes(old, _ch6_line(0))
    _age(old, 400)
    tailer = LogTailer([str(tmp_path / "*" / "CH6 T *.log")],
                       str(tmp_path / "state.json"), backfill=True)
    out = dict(tailer.read_new_lines())
    assert "CH6 T 20-01-01.log" in out  # prefilter is bypassed in backfill mode


# --------------------------------------------------------------------------- state-file robustness
def test_load_state_retries_transient_permission_error(tmp_path, monkeypatch):
    # Windows AV can hold offsets.json briefly at service start.
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"files": {}, "last_run": 123.0}))
    real_open = open
    attempts = {"n": 0}

    def flaky_open(file, *args, **kwargs):
        if str(file) == str(state):
            attempts["n"] += 1
            if attempts["n"] <= 2:
                raise PermissionError("held by AV")
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr("builtins.open", flaky_open)
    monkeypatch.setattr(daemon.time, "sleep", lambda s: None)
    tailer = LogTailer([str(tmp_path / "CH6 T *.log")], str(state))
    assert tailer.last_run == 123.0     # loaded on the 3rd attempt
    assert attempts["n"] == 3


def test_load_state_raises_after_persistent_permission_error(tmp_path, monkeypatch):
    state = tmp_path / "state.json"
    state.write_text("{}")
    real_open = open
    attempts = {"n": 0}

    def always_denied(file, *args, **kwargs):
        if str(file) == str(state):
            attempts["n"] += 1
            raise PermissionError("held forever")
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr("builtins.open", always_denied)
    monkeypatch.setattr(daemon.time, "sleep", lambda s: None)
    with pytest.raises(PermissionError):
        LogTailer([str(tmp_path / "CH6 T *.log")], str(state))
    assert attempts["n"] == 3           # bounded retries, then loud failure
