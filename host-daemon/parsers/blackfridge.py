"""Parser for blackfridge (BlueFors dilution refrigerator).

Same logger family as whitefridge; the shared parsing lives in bluefors_base.
Only the CH<n> -> stage map is bespoke here. Format details and the per-fridge
open questions are documented in samples/blackfridge/notes.md.
"""
from __future__ import annotations

from .bluefors_base import BlueforsParser


class BlackfridgeParser(BlueforsParser):
    name = "blackfridge"

    # CH<n> -> canonical stage. ASSUMPTION: standard BlueFors convention. The
    # fridge is warm in the samples (CH1≈293 K, CH6≈102 K), so the data can't
    # confirm it — CONFIRM WITH BEN before trusting thresholds (notes.md).
    CHANNEL_STAGE = {"1": "50K", "2": "4K", "5": "still", "6": "MXC"}
