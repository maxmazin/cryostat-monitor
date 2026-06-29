"""Parser for bluefors_1 (BlueFors dilution refrigerator logger).

STUB — fill in once representative log samples land (§11 Q5). BlueFors writes
one dated directory per day with per-channel CH*.log files; confirm the exact
filename pattern, column layout, timestamp format, timezone, and units before
trusting this.
"""
from __future__ import annotations

from .base import Parser, Reading

# Map BlueFors channel file IDs to canonical stage names.
CHANNEL_MAP: dict[str, str] = {
    # "CH1": "50K",
    # "CH2": "4K",
    # "CH5": "still",
    # "CH6": "MXC",
}


class Bluefors1Parser(Parser):
    def parse_new(self, raw_lines: list[str]) -> list[Reading]:
        readings: list[Reading] = []
        for line in raw_lines:
            try:
                # TODO: parse a BlueFors log line into (ts, channel, value).
                # Skip malformed lines — never raise (§6.1, §12).
                continue
            except Exception:
                # TODO: log.warning("skipping malformed line: %r", line)
                continue
        return readings
