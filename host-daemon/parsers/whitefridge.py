"""Parser for whitefridge (BlueFors dilution refrigerator).

Same logger family as blackfridge; the shared parsing lives in bluefors_base.
This host runs a different BlueFors software build: lowercase/zero-padded
sci-notation (`2.793549e+01`), no leading space on CH lines, ~10 s sample
cadence, blank maxigauge names, and an extra `Heaters` file. None of that needs
special handling — the base parser already tolerates it. Only the CH<n> -> stage
map is bespoke here. See samples/whitefridge/notes.md.
"""
from __future__ import annotations

from .bluefors_base import BlueforsParser


class WhitefridgeParser(BlueforsParser):
    name = "whitefridge"

    # CH<n> -> canonical stage. Same channels present as blackfridge (1,2,5,6).
    # ASSUMPTION: standard BlueFors convention; fridge is warm in the samples so
    # the data can't confirm it — CONFIRM WITH BEN (notes.md).
    CHANNEL_STAGE = {"1": "50K", "2": "4K", "5": "still", "6": "MXC"}
