"""Tests for the whitefridge (BlueFors) parser, driven by real sample lines.

whitefridge runs a different BlueFors software build than blackfridge. These
tests pin the variant's gotchas (see samples/whitefridge/notes.md):
  - lowercase, zero-padded sci-notation and no leading space on CH lines;
  - maxigauge lines with BLANK gauge names while the gauges are live — the
    pressures must still be shipped (keyed off the enabled flag, not the name).
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from parsers.whitefridge import WhitefridgeParser

REPO = Path(__file__).resolve().parents[2]
SAMPLE_DAY = REPO / "samples" / "whitefridge" / "26-06-30"


@pytest.fixture
def parser():
    return WhitefridgeParser()


# --------------------------------------------------------------------------- temperatures
def test_lowercase_zeropadded_scinotation_no_leading_space(parser):
    # whitefridge writes `2.793549e+01` with no leading space; CH6 -> MXC.
    rows = parser.parse_new("CH6 T 26-06-30.log", ["30-06-26,00:00:02,2.793549e+01\r\n"])
    assert len(rows) == 1
    assert rows[0].channel == "MXC"
    assert rows[0].unit == "K"
    assert rows[0].value == pytest.approx(27.93549)
    assert rows[0].ts == datetime(2026, 6, 30, 0, 0, 2)
    assert rows[0].ts.tzinfo is None  # naive local; daemon attaches tz


# --------------------------------------------------------------------------- pressures
def test_maxigauge_ships_pressures_despite_blank_names(parser):
    # Real whitefridge line: every gauge name is blank, but all are enabled (last
    # field 1) with real values. The previous blank-name heuristic dropped them all.
    line = ("30-06-26,00:00:02,CH1,        ,0,2.00e-02,4,1,CH2,        ,1,5.83e-02,0,1,"
            "CH3,        ,1,9.12e+00,0,1,CH4,        ,1,2.73e+02,0,1,"
            "CH5,        ,1,7.56e+02,0,1,CH6,        ,1,1.75e+00,0,1,\r\n")
    rows = parser.parse_new("maxigauge 26-06-30.log", [line])
    assert [r.channel for r in rows] == ["P1", "P2", "P3", "P4", "P5", "P6"]
    assert all(r.unit == "mbar" for r in rows)
    assert rows[0].value == pytest.approx(0.02)
    assert rows[3].value == pytest.approx(273.0)


def test_maxigauge_skips_disabled_gauge_even_when_blank(parser):
    # A blank-named gauge with enabled flag 0 is off -> skipped; the enabled one ships.
    line = ("30-06-26,00:00:02,CH1,        ,0,0.00e+00,0,0,CH2,        ,1,5.83e-02,0,1,"
            "CH3,        ,0,0.00e+00,0,0,CH4,        ,0,0.00e+00,0,0,"
            "CH5,        ,0,0.00e+00,0,0,CH6,        ,0,0.00e+00,0,0,\r\n")
    rows = parser.parse_new("maxigauge 26-06-30.log", [line])
    assert [r.channel for r in rows] == ["P2"]


def test_heaters_file_is_ignored(parser):
    # whitefridge has a Heaters file not present on blackfridge; not a reading source.
    assert parser.parse_new("Heaters 26-06-30.log", ["30-06-26,00:00:02,0,0.00e+00,0,0.00e+00\r\n"]) == []


# --------------------------------------------------------------------------- real-file smoke
@pytest.mark.skipif(not SAMPLE_DAY.exists(), reason="sample logs not present")
def test_parses_a_real_day_file(parser):
    path = SAMPLE_DAY / "CH6 T 26-06-30.log"
    lines = path.read_text(encoding="ascii", errors="replace").splitlines()
    rows = parser.parse_new(path.name, lines)
    assert len(rows) == len([ln for ln in lines if ln.strip()])
    assert rows  # non-empty day
    assert all(r.channel == "MXC" and r.unit == "K" for r in rows)
    assert all(0 < r.value < 400 for r in rows)
    assert all(r.ts.date() == datetime(2026, 6, 30).date() for r in rows)


@pytest.mark.skipif(not SAMPLE_DAY.exists(), reason="sample logs not present")
def test_parses_real_maxigauge_day_file(parser):
    path = SAMPLE_DAY / "maxigauge 26-06-30.log"
    lines = path.read_text(encoding="ascii", errors="replace").splitlines()
    rows = parser.parse_new(path.name, lines)
    # The fix's payoff: a real whitefridge maxigauge file yields pressures.
    assert rows, "expected pressures from whitefridge maxigauge despite blank names"
    assert all(r.unit == "mbar" for r in rows)
    assert {r.channel for r in rows} <= {"P1", "P2", "P3", "P4", "P5", "P6"}
