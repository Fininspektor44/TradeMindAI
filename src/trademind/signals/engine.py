"""Deterministic EMA, RSI and ATR signal engine."""

from __future__ import annotations

from collections.abc import Sequence

from trademind.market.models import Candle
from trademind.signals.models import SignalAction, SignalResult


class SignalEngine:
    """Scores market direction using transparent, testable indicators."""

    def __init__(
        self,
        ema_fast_period: int = 9,
        ema_slow_period: int = 21,
        rsi_period: int = 14,
        atr_period: int = 14,
        action_threshold: int = 35,
    ) -> None:
        periods = (ema_fast_period, ema_slow_period, rsi_period, atr_period)
        if any(period <= 1 for period in periods):
            raise ValueError("Indicator periods must be greater than one")
        if ema_fast_period >= ema_slow_period:
            raise ValueError("Fast EMA period must be lower than slow EMA period")
        if not 1 <= action_threshold <= 100:
            raise ValueError("Action threshold must be between 1 and 100")

        self.ema_fast_period = ema_fast_period
        self.ema_slow_period = ema_slow_period
        self.rsi_period = rsi_period
        self.atr_period = atr_period
        self.action_threshold = action_threshold

    @property
    def minimum_candles(self) -> int:
        return max(self.ema_slow_period, self.rsi_period + 1, self.atr_period + 1)

    def analyze(self, candles: Sequence[Candle]) -> SignalResult:
        if len(candles) < self.minimum_candles:
            raise ValueError(
                f"At least {self.minimum_candles} candles are required, got {len(candles)}"
            )

        symbol = candles[-1].symbol
        timeframe = candles[-1].timeframe
        if any(candle.symbol != symbol or candle.timeframe != timeframe for candle in candles):
            raise ValueError("All candles must have the same symbol and timeframe")

        closes = [candle.close for candle in candles]
        ema_fast = self._ema(closes, self.ema_fast_period)
        ema_slow = self._ema(closes, self.ema_slow_period)
        rsi = self._rsi(closes, self.rsi_period)
        atr = self._atr(candles, self.atr_period)

        latest = candles[-1]
        previous = candles[-2]
        score = 0
        reasons: list[str] = []

        if ema_fast > ema_slow:
            score += 40
            reasons.append("EMA fast is above EMA slow")
        elif ema_fast < ema_slow:
            score -= 40
            reasons.append("EMA fast is below EMA slow")
        else:
            reasons.append("EMA trend is neutral")

        if latest.close > ema_slow:
            score += 20
            reasons.append("Price is above EMA slow")
        elif latest.close < ema_slow:
            score -= 20
            reasons.append("Price is below EMA slow")

        if 52 <= rsi <= 70:
            score += 20
            reasons.append("RSI confirms bullish momentum")
        elif 30 <= rsi <= 48:
            score -= 20
            reasons.append("RSI confirms bearish momentum")
        elif rsi > 70:
            score -= 5
            reasons.append("RSI is overbought")
        elif rsi < 30:
            score += 5
            reasons.append("RSI is oversold")
        else:
            reasons.append("RSI is neutral")

        if latest.close > previous.close:
            score += 10
            reasons.append("Latest candle closed higher")
        elif latest.close < previous.close:
            score -= 10
            reasons.append("Latest candle closed lower")

        score = max(-100, min(100, score))
        confidence = min(100, abs(score))

        if score >= self.action_threshold:
            action = SignalAction.BUY
        elif score <= -self.action_threshold:
            action = SignalAction.SELL
        else:
            action = SignalAction.WAIT

        return SignalResult(
            symbol=symbol,
            timeframe=timeframe,
            action=action,
            score=score,
            confidence=confidence,
            ema_fast=ema_fast,
            ema_slow=ema_slow,
            rsi=rsi,
            atr=atr,
            reasons=tuple(reasons),
        )

    @staticmethod
    def _ema(values: Sequence[float], period: int) -> float:
        seed = sum(values[:period]) / period
        multiplier = 2.0 / (period + 1)
        result = seed
        for value in values[period:]:
            result = (value - result) * multiplier + result
        return result

    @staticmethod
    def _rsi(values: Sequence[float], period: int) -> float:
        changes = [current - previous for previous, current in zip(values, values[1:])]
        recent = changes[-period:]
        gains = sum(max(change, 0.0) for change in recent) / period
        losses = sum(max(-change, 0.0) for change in recent) / period
        if losses == 0:
            return 100.0 if gains > 0 else 50.0
        relative_strength = gains / losses
        return 100.0 - (100.0 / (1.0 + relative_strength))

    @staticmethod
    def _atr(candles: Sequence[Candle], period: int) -> float:
        true_ranges: list[float] = []
        for previous, current in zip(candles, candles[1:]):
            true_ranges.append(
                max(
                    current.high - current.low,
                    abs(current.high - previous.close),
                    abs(current.low - previous.close),
                )
            )
        recent = true_ranges[-period:]
        return sum(recent) / period
