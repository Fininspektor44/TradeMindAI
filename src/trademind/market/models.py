"""Core market-data models."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class Candle:
    symbol: str
    timeframe: str
    time: datetime
    open: float
    high: float
    low: float
    close: float
    tick_volume: int = 0
    spread: int = 0

    def __post_init__(self) -> None:
        if self.high < self.low:
            raise ValueError("Candle high cannot be below candle low")
        if not self.low <= self.open <= self.high:
            raise ValueError("Candle open must be inside the high-low range")
        if not self.low <= self.close <= self.high:
            raise ValueError("Candle close must be inside the high-low range")
        if self.tick_volume < 0:
            raise ValueError("Tick volume cannot be negative")
        if self.spread < 0:
            raise ValueError("Spread cannot be negative")
