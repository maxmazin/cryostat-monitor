"""Shared parsing for the BlueFors logger family (§6.1).

All BlueFors fridges write the same log tree — dated daily folders, per-channel
`CH<n> T/R` files (one stage each), and a `maxigauge` pressures file, all CRLF.
Numeric formatting varies between software versions (e.g. `1.020050E+2` on one
host, `2.793549e+01` on another), but `float()` absorbs both, and a leading
space on the timestamp is stripped.

The one genuinely per-fridge thing — which `CH<n>` is which stage — lives in
each fridge's own module as `CHANNEL_STAGE`; see parsers/blackfridge.py and
parsers/whitefridge.py. This base ships stage TEMPERATURES (K) and gauge
PRESSURES (mbar); resistances, flow, status, and heaters are not shipped.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime

from .base import Parser, Reading

log = logging.getLogger("cryo.parser.bluefors")

# maxigauge sensor position -> canonical pressure channel. Mapped by POSITION
# (CH1..CH6), never by the gauge's `Pn` name: some firmwares leave the name
# blank even when the gauge is live, so the name is not a reliable key.
GAUGE_CHANNEL: dict[str, str] = {f"CH{i}": f"P{i}" for i in range(1, 7)}

# "CH6 T 26-06-30.log" -> ("6", "T");  "CH1 R ..." -> ("1", "R")
_CH_FILE_RE = re.compile(r"^CH(\d+)\s+([TR])\b")
# A maxigauge gauge group must start with a CH<n> sensor token (alignment check).
_SENSOR_RE = re.compile(r"^CH\d+$")
_FIELDS_PER_GAUGE = 6


def _parse_ts(date_str: str, time_str: str) -> datetime:
    # BlueFors writes the date day-month-year (e.g. 30-06-26 == 30 June 2026),
    # the reverse of the folder name's YY-MM-DD. Naive local time (no tz).
    return datetime.strptime(f"{date_str},{time_str}", "%d-%m-%y,%H:%M:%S")


class BlueforsParser(Parser):
    """Base parser for the BlueFors logger family.

    Subclasses set `CHANNEL_STAGE` (CH number -> canonical stage) and `name`
    (used only in skip-warnings).
    """

    CHANNEL_STAGE: dict[str, str] = {}
    name: str = "bluefors"

    def parse_new(self, source: str, raw_lines: list[str]) -> list[Reading]:
        ch = _CH_FILE_RE.match(source)
        if ch:
            if ch.group(2) == "T":            # temperatures only (R not shipped)
                return self._parse_channel(source, raw_lines, ch.group(1))
            return []
        if source.startswith("maxigauge"):
            return self._parse_maxigauge(source, raw_lines)
        # Flowmeter / Status / Channels / Heaters / Errors: not shipped.
        return []

    def _parse_channel(self, source: str, raw_lines: list[str], ch_num: str) -> list[Reading]:
        stage = self.CHANNEL_STAGE.get(ch_num)
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
            #   sensor(CHn), name(Pn), state, value(mbar), unit, enabled
            for i in range(2, len(fields) - (_FIELDS_PER_GAUGE - 1), _FIELDS_PER_GAUGE):
                group = fields[i:i + _FIELDS_PER_GAUGE]
                sensor = group[0]
                # Structural guard: every group must start with a CH<n> sensor
                # token. If it doesn't, the 6-fields-per-gauge assumption is wrong
                # for this firmware and the groups are misaligned — bail on the
                # whole line rather than emit mis-keyed pressures (a silent-wrong
                # failure worse than a skip).
                if not _SENSOR_RE.match(sensor):
                    self._skip(source, line)
                    break
                # A gauge is live iff its trailing `enabled` flag is "1". Do NOT
                # gate on the `Pn` name being blank — some firmwares blank it on
                # every line while the gauge is on (whitefridge does this), which
                # would silently drop all its pressures.
                if group[5].strip() != "1":
                    continue
                channel = GAUGE_CHANNEL.get(sensor)
                if channel is None:
                    continue
                try:
                    value = float(group[3])
                except ValueError:
                    # A live gauge with an unparseable value is a sensor fault,
                    # not a malformed line. Skip just this gauge quietly — logging
                    # a warning per gauge per poll would flood the log for the
                    # duration of the fault.
                    log.debug("%s: unparseable gauge value %r in %s",
                              self.name, group[3], source)
                    continue
                out.append(Reading(ts=ts, channel=channel, value=value, unit="mbar"))
        return out

    def _skip(self, source: str, line: str) -> None:
        log.warning("%s: skipping malformed line in %s: %r", self.name, source, line)
