"""Tests for the transparent signal engine."""

from datetime import datetime, timedelta, timezone

import pytest

from trademind.market.models import Candle
from trademind.signals import SignalAction, SignalEngine


def _candles(direction: int, count: int = 60) -> list[Candle]:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    price = 100.0
    candles: list[Candle] = []
    for index in range(count):
        open_price = price
        close_price = open_price + direction * 0.5
        high = max(open_price, close_price) + 0.2
        low = min(open_price, close_price) - 0.2
        candles.append(
            Candle(
                symbol="TEST",
                timeframe="M5",
                time=start + timedelta(minutes=5 * index),
                open=open_price,
                high=high,
                low=low,
                close=close_price,
                tick_volume=100 + index,
                spread=10,
            )
        )
        price = close_price
    return candles


def test_bullish_series_returns_buy() -> None:
    result = SignalEngine().analyze(_candles(direction=1))
    assert result.action is SignalAction.BUY
    assert result.score >= 35
    assert result.ema_fast > result.ema_slow
    assert result.atr > 0


def test_bearish_series_returns_sell() -> None:
    result = SignalEngine().analyze(_candles(direction=-1))
    assert result.action is SignalAction.SELL
    assert result.score <= -35
    assert result.ema_fast < result.ema_slow


def test_rejects_insufficient_history() -> None:
    with pytest.raises(ValueError, match="At least"):
        SignalEngine().analyze(_candles(direction=1, count=10))
