"""Market-structure observation models."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class MarketBias(StrEnum):
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    NEUTRAL = "NEUTRAL"


class StructureBreak(StrEnum):
    NONE = "NONE"
    BULLISH_BOS = "BULLISH_BOS"
    BEARISH_BOS = "BEARISH_BOS"
    BULLISH_CHOCH = "BULLISH_CHOCH"
    BEARISH_CHOCH = "BEARISH_CHOCH"
    BULLISH_BREAK = "BULLISH_BREAK"
    BEARISH_BREAK = "BEARISH_BREAK"


class FvgDirection(StrEnum):
    NONE = "NONE"
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"


@dataclass(frozen=True, slots=True)
class StructureObservation:
    version: str
    internal_bias: MarketBias
    internal_reference_high: float
    internal_reference_low: float
    internal_break: StructureBreak
    swing_bias: MarketBias
    swing_reference_high: float
    swing_reference_low: float
    swing_break: StructureBreak
    liquidity_reference_high: float
    liquidity_reference_low: float
    bsl_sweep: bool
    ssl_sweep: bool
    bsl_sweep_depth: float
    ssl_sweep_depth: float
    bsl_sweep_depth_atr: float | None
    ssl_sweep_depth_atr: float | None
    fvg_direction: FvgDirection
    fvg_size: float
    fvg_size_atr: float | None
    event_count: int
