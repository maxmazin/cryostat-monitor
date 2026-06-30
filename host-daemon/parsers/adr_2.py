"""Parser for adr_2 (adiabatic demagnetization refrigerator logger).

STUB — fill in once representative log samples land (§11 Q5). ADRs differ from
the dil fridges: no still/MXC, expect magnet/regen and stage channels (e.g.
GGG, FAA). Regen cycles warm stages by design — that is handled by maintenance
mutes, not by the parser.
"""
from __future__ import annotations

from .base import Parser, Reading

CHANNEL_MAP: dict[str, str] = {
    # confirm per fridge
}


class Adr2Parser(Parser):
    def parse_new(self, source: str, raw_lines: list[str]) -> list[Reading]:
        readings: list[Reading] = []
        for line in raw_lines:
            try:
                # TODO: parse an ADR log line; skip malformed lines, never raise.
                continue
            except Exception:
                continue
        return readings
