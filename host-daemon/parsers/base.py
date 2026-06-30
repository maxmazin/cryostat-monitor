"""Parser interface (§6.1).

Each fridge gets its own module implementing `Parser`. Parsers are the bulk of
the work because every logger's format / rotation differs. Canonicalize units
here: temperatures in kelvin, pressures in mbar.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass
class Reading:
    ts: datetime      # naive LOCAL time as written by the logger; the daemon
                      # attaches the configured tz and converts to UTC (§6.2).
    channel: str      # canonical name, e.g. "MXC", "4K", "still", "P_still"
    value: float
    unit: str         # "K" or "mbar"


class Parser:
    def parse_new(self, source: str, raw_lines: list[str]) -> list[Reading]:
        """Convert newly-read log lines into Readings.

        `source` is the basename of the file the lines came from (e.g.
        "CH6 T 26-06-30.log", "maxigauge 26-06-30.log"). Loggers like BlueFors
        split readings across many files, so the parser needs it to know how to
        interpret each line.

        MUST skip/log malformed lines, never raise on bad input — losing one
        fridge over a stray byte is unacceptable (§12).
        """
        raise NotImplementedError
