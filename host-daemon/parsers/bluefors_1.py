"""Parser for fridge1 (BlueFors dilution refrigerator logger).

Format derived from real logs — see samples/fridge1/notes.md. Key points:

- One dated folder per day; new files at midnight. Lines end in CRLF.
- Per-channel temperature files `CH<n> T <date>.log` (kelvin) and resistance
  files `CH<n> R <date>.log` (ohms). Line: ` DD-MM-YY,HH:MM:SS,<sci-float>`.
  The date is day-month-year (the reverse of the folder name).
- `maxigauge <date>.log`: 6 pressure gauges (mbar), 6 fields each, mapped by
  sensor position CH1..CH6. Gauges that are off have a blank name and 0 value.
- Timestamps are naive LOCAL time; the daemon converts to UTC.

This parser ships stage TEMPERATURES (K) and gauge PRESSURES (mbar) — the
signals the watchdog acts on. Resistances, flow, and status are intentionally
not shipped yet (see notes.md "NEEDS CONFIRMATION FROM BEN").
"""
from __future__ import annotations

import logging
import re
from datetime import datetime

from .base import Parser, Reading

log = logging.getLogger("cryo.parser.bluefors_1")

# CH<n> -> canonical stage name. ASSUMPTION: standard BlueFors convention.
# CONFIRM WITH BEN before trusting thresholds (samples/fridge1/notes.md).
CHANNEL_STAGE: dict[str, str] = {
    "1": "50K",
    "2": "4K",
    "5": "still",
    "6": "MXC",
}

# maxigauge sensor position -> canonical pressure channel. Names are raw (P1..P6)
# until Ben maps them to meaning (OVC / still line / …).
GAUGE_CHANNEL: dict[str, str] = {
    "CH1": "P1", "CH2": "P2", "CH3": "P3",
    "CH4": "P4", "CH5": "P5", "CH6": "P6",
}

# "CH6 T 26-06-30.log" -> ("6", "T");  "CH1 R ..." -> ("1", "R")
_CH_FILE_RE = re.compile(r"^CH(\d+)\s+([TR])\b")
_FIELDS_PER_GAUGE = 6


def _parse_ts(date_str: str, time_str: str) -> datetime:
    # BlueFors writes the date day-month-year (e.g. 30-06-26 == 30 June 2026).
    return datetime.strptime(f"{date_str},{time_str}", "%d-%m-%y,%H:%M:%S")


class Bluefors1Parser(Parser):
    def parse_new(self, source: str, raw_lines: list[str]) -> list[Reading]:
        ch = _CH_FILE_RE.match(source)
        if ch:
            if ch.group(2) == "T":            # temperatures only (R not shipped)
                return self._parse_channel(source, raw_lines, ch.group(1))
            return []
        if source.startswith("maxigauge"):
            return self._parse_maxigauge(source, raw_lines)
        # Flowmeter / Status / Channels / Errors: not shipped.
        return []

    def _parse_channel(self, source: str, raw_lines: list[str], ch_num: str) -> list[Reading]:
        stage = CHANNEL_STAGE.get(ch_num)
        if stage is None:                     # unmapped thermometer channel
            return []
        out: list[Reading] = []
        for line in raw_lines:
            fields = line.strip().split(",")
            if len(fields) != 3:
                self._skip(source, line)
                continue
            try:
                ts = _parse_ts(fields[0], fields[1])
                value = float(fields[2])
            except ValueError:
                self._skip(source, line)
                continue
            out.append(Reading(ts=ts, channel=stage, value=value, unit="K"))
        return out

    def _parse_maxigauge(self, source: str, raw_lines: list[str]) -> list[Reading]:
        out: list[Reading] = []
        for line in raw_lines:
            fields = line.strip().split(",")
            if len(fields) < 2:
                self._skip(source, line)
                continue
            try:
                ts = _parse_ts(fields[0], fields[1])
            except ValueError:
                self._skip(source, line)
                continue
            # Gauges follow the timestamp in groups of 6:
            #   sensor(CHn), name(Pn), state, value(mbar), unit?, enabled?
            for i in range(2, len(fields) - (_FIELDS_PER_GAUGE - 1), _FIELDS_PER_GAUGE):
                group = fields[i:i + _FIELDS_PER_GAUGE]
                sensor, name = group[0], group[1].strip()
                if not name:                  # gauge off / not configured
                    continue
                channel = GAUGE_CHANNEL.get(sensor)
                if channel is None:
                    continue
                try:
                    value = float(group[3])
                except ValueError:
                    self._skip(source, line)
                    continue
                out.append(Reading(ts=ts, channel=channel, value=value, unit="mbar"))
        return out

    @staticmethod
    def _skip(source: str, line: str) -> None:
        log.warning("bluefors_1: skipping malformed line in %s: %r", source, line)
