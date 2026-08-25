"""Deterministic volatility calculations with no directional authority."""

from __future__ import annotations

from collections.abc import Sequence

from trademind.market.models import Candle


def true_ranges(candles: Sequence[Candle]) -> list[float]:
    """Return true range for each candle, including the first candle's range."""
    output: list[float] = []
    for index, candle in enumerate(candles):
        previous_close = candles[index - 1].close if index else candle.close
        output.append(
            max(
                candle.high - candle.low,
                abs(candle.high - previous_close),
                abs(candle.low - previous_close),
            )
        )
    return output


def average_true_range(candles: Sequence[Candle], period: int = 14) -> float:
    """Return the deterministic trailing simple average true range.

    The value is volatility-only. It cannot emit, infer, or alter a market
    direction. Mature windows preserve the calculation previously embedded in
    the retired legacy signal container.
    """
    if period <= 1:
        raise ValueError("ATR period must be greater than one")
    if len(candles) < period + 1:
        raise ValueError(f"At least {period + 1} candles are required, got {len(candles)}")
    return sum(true_ranges(candles)[-period:]) / period


def average_true_range_series(
    candles: Sequence[Candle], period: int = 14
) -> list[float]:
    """Return past-only trailing ATR values for every candle."""
    if period <= 1:
        raise ValueError("ATR period must be greater than one")
    ranges = true_ranges(candles)
    return [
        sum(ranges[max(0, index - period + 1) : index + 1])
        / len(ranges[max(0, index - period + 1) : index + 1])
        for index in range(len(ranges))
    ]
