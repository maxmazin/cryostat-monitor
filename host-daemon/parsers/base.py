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
    ts: datetime      # tz-aware; converted to UTC by the daemon
    channel: str      # canonical name, e.g. "MXC", "4K", "still", "P_still"
    value: float
    unit: str         # "K" or "mbar"


class Parser:
    def parse_new(self, raw_lines: list[str]) -> list[Reading]:
        """Convert newly-read log lines into Readings.

        MUST skip/log malformed lines, never raise on bad input — losing one
        fridge over a stray byte is unacceptable (§12).
        """
        raise NotImplementedError
