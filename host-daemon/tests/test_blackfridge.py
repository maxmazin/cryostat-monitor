"""Tests for the blackfridge (BlueFors) parser, driven by real sample lines.

Covers the format gotchas from samples/blackfridge/notes.md: CRLF endings,
leading spaces, day-month-year dates, T-vs-R files, maxigauge multi-gauge lines,
off (disabled) gauges, and malformed-line resilience.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from parsers.blackfridge import BlackfridgeParser

REPO = Path(__file__).resolve().parents[2]
SAMPLE_DAY = REPO / "samples" / "blackfridge" / "26-06-30"


@pytest.fixture
def parser():
    return BlackfridgeParser()


# --------------------------------------------------------------------------- temperatures
def test_temperature_line_maps_channel_and_value(parser):
    # CH6 -> MXC; CRLF + leading space; 1.020050E+2 == 102.005 K.
    rows = parser.parse_new("CH6 T 26-06-30.log", [" 30-06-26,00:00:32,1.020050E+2\r\n"])
    assert len(rows) == 1
    r = rows[0]
    assert r.channel == "MXC"
    assert r.unit == "K"
    assert r.value == pytest.approx(102.005)


def test_date_is_day_month_year_not_folder_order(parser):
    # The line date 30-06-26 is 30 June 2026 — NOT 26 June (YY-MM-DD folder order).
    rows = parser.parse_new("CH1 T 26-06-30.log", [" 30-06-26,00:00:32,2.934560E+2\r\n"])
    assert rows[0].ts == datetime(2026, 6, 30, 0, 0, 32)
    assert rows[0].channel == "50K"  # CH1
    assert rows[0].value == pytest.approx(293.456)


def test_returns_naive_local_timestamp(parser):
    # The daemon attaches the tz; the parser must not invent one.
    rows = parser.parse_new("CH2 T 26-06-30.log", [" 30-06-26,12:00:00,4.2E+0\r\n"])
    assert rows[0].ts.tzinfo is None


def test_resistance_files_are_not_shipped(parser):
    # CH<n> R files carry ohms; we ship temperatures only for now.
    rows = parser.parse_new("CH1 R 26-06-30.log", [" 30-06-26,00:00:32,1.082670E+2\r\n"])
    assert rows == []


def test_unmapped_thermometer_channel_is_skipped(parser):
    # CH3/CH4 aren't in CHANNEL_STAGE for this fridge.
    rows = parser.parse_new("CH3 T 26-06-30.log", [" 30-06-26,00:00:32,1.0E+0\r\n"])
    assert rows == []


# --------------------------------------------------------------------------- pressures
def test_maxigauge_parses_six_gauges(parser):
    # Real 26-06-20 sample line: every gauge on (state 1, status 0).
    line = ("20-06-26,00:00:38,CH1,P1  ,1, 2.42E-6,0,1,CH2,P2  ,1, 1.17E-2,0,1,"
            "CH3,P3  ,1, 7.58E+2,0,1,CH4,P4  ,1, 7.61E+2,0,1,CH5,P5  ,1, 5.74E+0,0,1,"
            "CH6,P6,1, 3.85E+0,0,1,\r\n")
    rows = parser.parse_new("maxigauge 26-06-20.log", [line])
    assert [r.channel for r in rows] == ["P1", "P2", "P3", "P4", "P5", "P6"]
    assert all(r.unit == "mbar" for r in rows)
    assert rows[0].value == pytest.approx(2.42e-6)  # P1
    assert rows[5].value == pytest.approx(3.85)     # P6
    assert rows[0].ts == datetime(2026, 6, 20, 0, 0, 38)


def test_maxigauge_skips_disabled_gauges(parser):
    # Degraded line: gauges off -> state field 0 (and 0 value). Skip all.
    line = ("30-06-26,14:17:04,CH1,,0, 0.00E+0,0,0,CH2,,0, 0.00E+0,0,0,"
            "CH3,,0, 0.00E+0,0,0,CH4,,0, 0.00E+0,0,0,CH5,,0, 0.00E+0,0,0,"
            "CH6,,0, 0.00E+0,0,0,\r\n")
    assert parser.parse_new("maxigauge 26-06-30.log", [line]) == []


def test_maxigauge_drops_state_off_gauge_keeps_state_on(parser):
    # Real 26-06-24 sample line: CH1 turned OFF (state 0, Pfeiffer status 4) and
    # its value froze at the placeholder 2.00E-2 — but the trailing 6th field is
    # STILL 1. Gating on the trailing field (the old bug) shipped the frozen
    # placeholder as a live pressure; gating on the state field drops P1 while
    # keeping the state-1 gauges on the same line.
    line = ("24-06-26,00:00:58,CH1,P1  ,0, 2.00E-2,4,1,CH2,P2  ,1, 1.32E-1,0,1,"
            "CH3,P3  ,1,-6.00E+0,0,1,CH4,P4  ,1, 3.50E+2,0,1,CH5,P5  ,1, 7.32E+2,0,1,"
            "CH6,P6,1, 2.40E+0,0,1,\r\n")
    rows = parser.parse_new("maxigauge 26-06-24.log", [line])
    assert [r.channel for r in rows] == ["P2", "P3", "P4", "P5", "P6"]
    assert not any(r.value == pytest.approx(0.02) for r in rows)  # placeholder gone


def test_maxigauge_misalignment_discards_whole_line(parser):
    # An extra field before CH2 shifts every later group off the 6-per-gauge grid.
    # CH1 parses fine on its own, but once alignment is lost the whole line is
    # suspect — none of it (not even the valid CH1 prefix) may be shipped.
    line = ("30-06-26,00:00:20,CH1,P1  ,1, 2.00E-2,4,1,EXTRA,CH2,P2  ,1, 7.04E-1,0,1,"
            "CH3,P3  ,1,-6.00E+0,0,1,\r\n")
    assert parser.parse_new("maxigauge 26-06-30.log", [line]) == []


# --------------------------------------------------------------------------- robustness
@pytest.mark.parametrize("line", [
    "",
    "   ",
    "garbage with no commas",
    " 30-06-26,00:00:32",            # too few fields
    " not-a-date,00:00:32,1.0E+0",   # bad date
    " 30-06-26,00:00:32,not-a-float",
])
def test_malformed_lines_are_skipped_not_raised(parser, line):
    assert parser.parse_new("CH6 T 26-06-30.log", [line]) == []


@pytest.mark.parametrize("source", [
    "Flowmeter 26-06-30.log",
    "Status_26-06-30.log",
    "Channels 26-06-30.log",
    "Errors 26-06-30.log",
])
def test_non_reading_files_are_ignored(parser, source):
    assert parser.parse_new(source, ["30-06-26,00:00:15,0.005062\r\n"]) == []


# --------------------------------------------------------------------------- real-file smoke
@pytest.mark.skipif(not SAMPLE_DAY.exists(), reason="sample logs not present")
def test_parses_a_real_day_file(parser):
    path = SAMPLE_DAY / "CH6 T 26-06-30.log"
    lines = path.read_text(encoding="ascii", errors="replace").splitlines()
    rows = parser.parse_new(path.name, lines)
    # Every non-blank line yields one MXC reading in a physically plausible range.
    assert len(rows) == len([ln for ln in lines if ln.strip()])
    assert rows  # non-empty day
    assert all(r.channel == "MXC" and r.unit == "K" for r in rows)
    assert all(0 < r.value < 400 for r in rows)        # kelvin, fridge warm-ish
    assert all(r.ts.date() == datetime(2026, 6, 30).date() for r in rows)
