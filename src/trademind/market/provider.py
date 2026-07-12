"""Market-data provider contract."""

from __future__ import annotations

from typing import Protocol

from trademind.market.models import Candle


class MarketDataProvider(Protocol):
    """Common interface for mock, MT5, and future exchange providers."""

    def healthcheck(self) -> bool:
        """Return True when the data source is available."""

    def get_candles(self, symbol: str, timeframe: str, count: int) -> list[Candle]:
        """Return candles ordered from oldest to newest."""
