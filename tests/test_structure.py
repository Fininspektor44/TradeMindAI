"""Tests for deterministic market-structure observations."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from trademind.market.models import Candle
from trademind.structure import MarketStructureEngine
from trademind.structure.models import FvgDirection, MarketBias, StructureBreak


def _candle(index: int, open_: float, high: float, low: float, close: float) -> Candle:
    time = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=5 * index)
    return Candle("XAUUSD", "M5", time, open_, high, low, close)


def test_bullish_bos_is_not_mislabeled_as_sweep() -> None:
    candles: list[Candle] = []
    for index in range(30):
        base = 100.0 + index * 0.1
        candles.append(_candle(index, base, base + 0.5, base - 0.5, base + 0.1))

    reference_high = max(candle.high for candle in candles[-20:])
    latest = _candle(
        30,
        reference_high - 0.2,
        reference_high + 0.4,
        reference_high - 0.3,
        reference_high + 0.2,
    )
    observation = MarketStructureEngine().analyze(candles + [latest], atr=1.0)

    assert observation.internal_bias is MarketBias.BULLISH
    assert observation.internal_break is StructureBreak.BULLISH_BOS
    assert observation.bsl_sweep is False


def test_bearish_context_can_produce_bullish_choch_and_fvg() -> None:
    candles: list[Candle] = []
    for index in range(31):
        base = 110.0 - index * 0.2
        candles.append(_candle(index, base, base + 0.3, base - 0.3, base - 0.1))

    prior = candles[:-1]
    reference_high = max(candle.high for candle in prior[-30:])
    first = prior[-2]
    latest_low = max(reference_high + 0.1, first.high + 0.2)
    latest = _candle(30, latest_low, latest_low + 0.3, latest_low, latest_low + 0.2)
    observation = MarketStructureEngine().analyze(prior + [latest], atr=1.0)

    assert observation.swing_bias is MarketBias.BEARISH
    assert observation.swing_break is StructureBreak.BULLISH_CHOCH
    assert observation.fvg_direction is FvgDirection.BULLISH
    assert observation.fvg_size_atr == pytest.approx(observation.fvg_size)


def test_liquidity_sweeps_require_close_back_inside_reference() -> None:
    candles = [_candle(index, 100.0, 101.0, 99.0, 100.0) for index in range(30)]
    latest = _candle(30, 100.0, 101.5, 98.5, 100.0)
    observation = MarketStructureEngine().analyze(candles + [latest], atr=2.0)

    assert observation.bsl_sweep is True
    assert observation.ssl_sweep is True
    assert observation.bsl_sweep_depth_atr == pytest.approx(0.25)
    assert observation.ssl_sweep_depth_atr == pytest.approx(0.25)
