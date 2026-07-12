"""Signal result models."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class SignalAction(StrEnum):
    BUY = "BUY"
    SELL = "SELL"
    WAIT = "WAIT"


@dataclass(frozen=True, slots=True)
class SignalResult:
    symbol: str
    timeframe: str
    action: SignalAction
    score: int
    confidence: int
    ema_fast: float
    ema_slow: float
    rsi: float
    atr: float
    reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        if not -100 <= self.score <= 100:
            raise ValueError("Signal score must be between -100 and 100")
        if not 0 <= self.confidence <= 100:
            raise ValueError("Signal confidence must be between 0 and 100")
        if self.atr < 0:
            raise ValueError("ATR cannot be negative")
