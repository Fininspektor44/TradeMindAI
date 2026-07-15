"""Deterministic observation-only market-structure engine."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from trademind.market.models import Candle
from trademind.structure.models import (
    FvgDirection,
    MarketBias,
    StructureBreak,
    StructureObservation,
)


@dataclass(frozen=True, slots=True)
class _StructureState:
    bias: MarketBias
    reference_high: float
    reference_low: float
    structure_break: StructureBreak


class MarketStructureEngine:
    """Extracts SMC-style observations without affecting signal decisions."""

    observation_version = "1.0"

    def __init__(
        self,
        internal_lookback: int = 4,
        swing_lookback: int = 30,
        liquidity_lookback: int = 20,
        fvg_min_atr: float = 0.1,
    ) -> None:
        lookbacks = (internal_lookback, swing_lookback, liquidity_lookback)
        if any(value < 2 for value in lookbacks):
            raise ValueError("Structure lookbacks must be at least two candles")
        if fvg_min_atr < 0:
            raise ValueError("FVG ATR threshold cannot be negative")
        self.internal_lookback = internal_lookback
        self.swing_lookback = swing_lookback
        self.liquidity_lookback = liquidity_lookback
        self.fvg_min_atr = fvg_min_atr

    @property
    def minimum_candles(self) -> int:
        return max(self.internal_lookback, self.swing_lookback, self.liquidity_lookback) + 1

    def analyze(self, candles: Sequence[Candle], atr: float) -> StructureObservation:
        if len(candles) < self.minimum_candles:
            raise ValueError(
                f"At least {self.minimum_candles} candles are required, got {len(candles)}"
            )
        if atr < 0:
            raise ValueError("ATR cannot be negative")

        latest = candles[-1]
        symbol = latest.symbol
        timeframe = latest.timeframe
        if any(candle.symbol != symbol or candle.timeframe != timeframe for candle in candles):
            raise ValueError("All candles must have the same symbol and timeframe")

        internal = self._structure_state(candles, self.internal_lookback)
        swing = self._structure_state(candles, self.swing_lookback)
        liquidity = candles[-(self.liquidity_lookback + 1) : -1]
        liquidity_high = max(candle.high for candle in liquidity)
        liquidity_low = min(candle.low for candle in liquidity)

        bsl_sweep = latest.high > liquidity_high and latest.close <= liquidity_high
        ssl_sweep = latest.low < liquidity_low and latest.close >= liquidity_low
        bsl_depth = max(0.0, latest.high - liquidity_high) if bsl_sweep else 0.0
        ssl_depth = max(0.0, liquidity_low - latest.low) if ssl_sweep else 0.0

        fvg_direction, fvg_size = self._fair_value_gap(candles, atr)
        event_count = sum(
            (
                internal.structure_break is not StructureBreak.NONE,
                swing.structure_break is not StructureBreak.NONE,
                bsl_sweep,
                ssl_sweep,
                fvg_direction is not FvgDirection.NONE,
            )
        )

        return StructureObservation(
            version=self.observation_version,
            internal_bias=internal.bias,
            internal_reference_high=internal.reference_high,
            internal_reference_low=internal.reference_low,
            internal_break=internal.structure_break,
            swing_bias=swing.bias,
            swing_reference_high=swing.reference_high,
            swing_reference_low=swing.reference_low,
            swing_break=swing.structure_break,
            liquidity_reference_high=liquidity_high,
            liquidity_reference_low=liquidity_low,
            bsl_sweep=bsl_sweep,
            ssl_sweep=ssl_sweep,
            bsl_sweep_depth=bsl_depth,
            ssl_sweep_depth=ssl_depth,
            bsl_sweep_depth_atr=bsl_depth / atr if bsl_sweep and atr > 0 else None,
            ssl_sweep_depth_atr=ssl_depth / atr if ssl_sweep and atr > 0 else None,
            fvg_direction=fvg_direction,
            fvg_size=fvg_size,
            fvg_size_atr=fvg_size / atr if fvg_size > 0 and atr > 0 else None,
            event_count=event_count,
        )

    @staticmethod
    def _bias(reference: Sequence[Candle]) -> MarketBias:
        change = reference[-1].close - reference[0].close
        tolerance = 1e-12
        if change > tolerance:
            return MarketBias.BULLISH
        if change < -tolerance:
            return MarketBias.BEARISH
        return MarketBias.NEUTRAL

    def _structure_state(self, candles: Sequence[Candle], lookback: int) -> _StructureState:
        reference = candles[-(lookback + 1) : -1]
        latest = candles[-1]
        reference_high = max(candle.high for candle in reference)
        reference_low = min(candle.low for candle in reference)
        bias = self._bias(reference)

        if latest.close > reference_high:
            if bias is MarketBias.BULLISH:
                structure_break = StructureBreak.BULLISH_BOS
            elif bias is MarketBias.BEARISH:
                structure_break = StructureBreak.BULLISH_CHOCH
            else:
                structure_break = StructureBreak.BULLISH_BREAK
        elif latest.close < reference_low:
            if bias is MarketBias.BEARISH:
                structure_break = StructureBreak.BEARISH_BOS
            elif bias is MarketBias.BULLISH:
                structure_break = StructureBreak.BEARISH_CHOCH
            else:
                structure_break = StructureBreak.BEARISH_BREAK
        else:
            structure_break = StructureBreak.NONE

        return _StructureState(
            bias=bias,
            reference_high=reference_high,
            reference_low=reference_low,
            structure_break=structure_break,
        )

    def _fair_value_gap(
        self,
        candles: Sequence[Candle],
        atr: float,
    ) -> tuple[FvgDirection, float]:
        first = candles[-3]
        third = candles[-1]
        bullish_size = third.low - first.high
        bearish_size = first.low - third.high
        minimum_size = atr * self.fvg_min_atr

        if bullish_size > 0 and bullish_size >= minimum_size:
            return FvgDirection.BULLISH, bullish_size
        if bearish_size > 0 and bearish_size >= minimum_size:
            return FvgDirection.BEARISH, bearish_size
        return FvgDirection.NONE, 0.0
