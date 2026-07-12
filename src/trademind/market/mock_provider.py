"""Deterministic market-data provider for development on macOS and CI."""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

from trademind.market.models import Candle


_TIMEFRAME_MINUTES = {
    "M1": 1,
    "M5": 5,
    "M15": 15,
    "M30": 30,
    "H1": 60,
    "H4": 240,
    "D1": 1440,
}

_BASE_PRICES = {
    "XAUUSD": 2400.0,
    "EURUSD": 1.08,
    "GBPUSD": 1.27,
    "USDJPY": 155.0,
    "BTCUSD": 60000.0,
}


class MockMarketDataProvider:
    """Produces repeatable candles without broker access."""

    def healthcheck(self) -> bool:
        return True

    def get_candles(self, symbol: str, timeframe: str, count: int) -> list[Candle]:
        if count <= 0:
            raise ValueError("count must be greater than zero")

        timeframe = timeframe.upper()
        if timeframe not in _TIMEFRAME_MINUTES:
            raise ValueError(f"Unsupported timeframe: {timeframe}")

        symbol = symbol.upper()
        base = _BASE_PRICES.get(symbol, 100.0)
        step = max(base * 0.00025, 0.00005)
        seed = sum(ord(char) for char in symbol)
        interval = timedelta(minutes=_TIMEFRAME_MINUTES[timeframe])
        end = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        start = end - interval * (count - 1)

        candles: list[Candle] = []
        previous_close = base

        for index in range(count):
            wave = math.sin((index + seed) / 4.0) * step
            drift = index * step * 0.05
            open_price = previous_close
            close_price = base + drift + wave
            padding = step * (0.8 + abs(math.cos(index + seed)))
            high = max(open_price, close_price) + padding
            low = min(open_price, close_price) - padding

            candle = Candle(
                symbol=symbol,
                timeframe=timeframe,
                time=start + interval * index,
                open=open_price,
                high=high,
                low=low,
                close=close_price,
                tick_volume=100 + index,
                spread=20,
            )
            candles.append(candle)
            previous_close = close_price

        return candles
