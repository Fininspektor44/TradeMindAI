"""TradeMind v1.25 deterministic crypto market-structure engine.

The engine reads local closed Bybit M5 candles only. It resamples M15 and H1
without future leakage and derives confirmed pivots, BOS/CHoCH, liquidity
sweeps, unmitigated fair-value gaps and Fibonacci OTE geometry. It never calls
an exchange, publishes a signal or sends an order.
"""

from __future__ import annotations

import bisect
import csv
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

VERSION = "1.25.0"
M5_MS = 5 * 60 * 1000


@dataclass(frozen=True, slots=True)
class Bar:
    start_ms: int
    interval_minutes: int
    open: float
    high: float
    low: float
    close: float

    @property
    def end_ms(self) -> int:
        return self.start_ms + self.interval_minutes * 60 * 1000


@dataclass(frozen=True, slots=True)
class Pivot:
    index: int
    price: float
    start_ms: int


def _number(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(str(value if value is not None else default).replace(",", "."))
    except (TypeError, ValueError):
        return default
    return parsed if math.isfinite(parsed) else default


def _integer(value: Any, default: int = 0) -> int:
    try:
        return int(float(str(value if value is not None else default).replace(",", ".")))
    except (TypeError, ValueError):
        return default


def _parse_time(value: Any) -> datetime:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("as-of time is required")
    parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("as-of time must include timezone information")
    return parsed.astimezone(timezone.utc)


def read_m5_bars(path: Path) -> dict[str, list[Bar]]:
    """Read and de-duplicate closed M5 bars from the existing Bybit journal."""

    if not path.is_file():
        raise ValueError(f"Bybit bars source not found: {path}")
    grouped: dict[str, dict[int, Bar]] = {}
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            symbol = str(row.get("symbol") or "").strip().upper()
            start_ms = _integer(row.get("start_ms"))
            open_price = _number(row.get("open"))
            high = _number(row.get("high"))
            low = _number(row.get("low"))
            close = _number(row.get("close"))
            if (
                not symbol
                or start_ms <= 0
                or min(open_price, high, low, close) <= 0
                or high < max(open_price, close)
                or low > min(open_price, close)
                or high < low
            ):
                continue
            grouped.setdefault(symbol, {})[start_ms] = Bar(
                start_ms=start_ms,
                interval_minutes=5,
                open=open_price,
                high=high,
                low=low,
                close=close,
            )
    return {
        symbol: [rows[key] for key in sorted(rows)]
        for symbol, rows in grouped.items()
        if rows
    }


def resample_bars(bars: Sequence[Bar], interval_minutes: int) -> list[Bar]:
    if interval_minutes < 5 or interval_minutes % 5:
        raise ValueError("interval_minutes must be a positive multiple of five")
    expected = interval_minutes // 5
    interval_ms = interval_minutes * 60 * 1000
    buckets: dict[int, list[Bar]] = {}
    for bar in bars:
        bucket = bar.start_ms // interval_ms * interval_ms
        buckets.setdefault(bucket, []).append(bar)

    output: list[Bar] = []
    for bucket in sorted(buckets):
        rows = sorted(buckets[bucket], key=lambda item: item.start_ms)
        expected_starts = [bucket + index * M5_MS for index in range(expected)]
        if len(rows) != expected or [item.start_ms for item in rows] != expected_starts:
            continue
        output.append(
            Bar(
                start_ms=bucket,
                interval_minutes=interval_minutes,
                open=rows[0].open,
                high=max(item.high for item in rows),
                low=min(item.low for item in rows),
                close=rows[-1].close,
            )
        )
    return output


def _atr(bars: Sequence[Bar], period: int = 14) -> float:
    if len(bars) < 2:
        return 0.0
    ranges: list[float] = []
    for previous, current in zip(bars, bars[1:]):
        ranges.append(
            max(
                current.high - current.low,
                abs(current.high - previous.close),
                abs(current.low - previous.close),
            )
        )
    selected = ranges[-period:]
    return sum(selected) / len(selected) if selected else 0.0


def _pivots(
    bars: Sequence[Bar],
    *,
    left: int = 2,
    right: int = 2,
) -> tuple[list[Pivot], list[Pivot]]:
    highs: list[Pivot] = []
    lows: list[Pivot] = []
    for index in range(left, len(bars) - right):
        window = bars[index - left : index + right + 1]
        current = bars[index]
        other_highs = [bar.high for offset, bar in enumerate(window) if offset != left]
        other_lows = [bar.low for offset, bar in enumerate(window) if offset != left]
        if current.high > max(other_highs):
            highs.append(Pivot(index=index, price=current.high, start_ms=current.start_ms))
        if current.low < min(other_lows):
            lows.append(Pivot(index=index, price=current.low, start_ms=current.start_ms))
    return highs, lows


def _bias(bars: Sequence[Bar], highs: Sequence[Pivot], lows: Sequence[Pivot]) -> str:
    if len(highs) >= 2 and len(lows) >= 2:
        higher_high = highs[-1].price > highs[-2].price
        higher_low = lows[-1].price > lows[-2].price
        lower_high = highs[-1].price < highs[-2].price
        lower_low = lows[-1].price < lows[-2].price
        if higher_high and higher_low:
            return "BULLISH"
        if lower_high and lower_low:
            return "BEARISH"
    if highs and lows and bars:
        midpoint = (highs[-1].price + lows[-1].price) / 2.0
        if bars[-1].close > midpoint:
            return "BULLISH"
        if bars[-1].close < midpoint:
            return "BEARISH"
    return "NEUTRAL"


def _latest_before(pivots: Sequence[Pivot], index: int) -> Pivot | None:
    for pivot in reversed(pivots):
        if pivot.index < index:
            return pivot
    return None


def _latest_break(
    bars: Sequence[Bar],
    highs: Sequence[Pivot],
    lows: Sequence[Pivot],
    prior_bias: str,
    *,
    lookback: int = 8,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "type": "NONE",
        "direction": "NONE",
        "level": None,
        "bar_start_ms": None,
    }
    start = max(1, len(bars) - lookback)
    for index in range(start, len(bars)):
        previous = bars[index - 1]
        current = bars[index]
        high = _latest_before(highs, index)
        low = _latest_before(lows, index)
        if high and previous.close <= high.price < current.close:
            kind = "CHOCH" if prior_bias == "BEARISH" else "BOS"
            result = {
                "type": f"BULLISH_{kind}",
                "direction": "BULLISH",
                "level": high.price,
                "bar_start_ms": current.start_ms,
            }
        if low and previous.close >= low.price > current.close:
            kind = "CHOCH" if prior_bias == "BULLISH" else "BOS"
            result = {
                "type": f"BEARISH_{kind}",
                "direction": "BEARISH",
                "level": low.price,
                "bar_start_ms": current.start_ms,
            }
    return result


def _timeframe_state(bars: Sequence[Bar]) -> dict[str, Any]:
    highs, lows = _pivots(bars)
    bias = _bias(bars, highs, lows)
    break_info = _latest_break(bars, highs, lows, bias)
    return {
        "bias": bias,
        "break": break_info["type"],
        "break_direction": break_info["direction"],
        "break_level": break_info["level"],
        "break_bar_start_ms": break_info["bar_start_ms"],
        "last_swing_high": highs[-1].price if highs else None,
        "last_swing_low": lows[-1].price if lows else None,
        "last_swing_high_start_ms": highs[-1].start_ms if highs else None,
        "last_swing_low_start_ms": lows[-1].start_ms if lows else None,
        "pivot_highs": len(highs),
        "pivot_lows": len(lows),
    }


def _latest_sweep(bars: Sequence[Bar], atr: float, *, lookback: int = 8) -> dict[str, Any]:
    highs, lows = _pivots(bars)
    result: dict[str, Any] = {
        "type": "NONE",
        "level": None,
        "depth_atr": 0.0,
        "bar_start_ms": None,
    }
    start = max(0, len(bars) - lookback)
    for index in range(start, len(bars)):
        current = bars[index]
        high = _latest_before(highs, index)
        low = _latest_before(lows, index)
        if high and current.high > high.price and current.close < high.price:
            result = {
                "type": "BSL_SWEEP",
                "level": high.price,
                "depth_atr": (current.high - high.price) / atr if atr > 0 else 0.0,
                "bar_start_ms": current.start_ms,
            }
        if low and current.low < low.price and current.close > low.price:
            result = {
                "type": "SSL_SWEEP",
                "level": low.price,
                "depth_atr": (low.price - current.low) / atr if atr > 0 else 0.0,
                "bar_start_ms": current.start_ms,
            }
    return result


def _latest_fvg(bars: Sequence[Bar], atr: float, *, lookback: int = 48) -> dict[str, Any]:
    result: dict[str, Any] = {
        "type": "NONE",
        "lower": None,
        "upper": None,
        "size_atr": 0.0,
        "bar_start_ms": None,
    }
    start = max(2, len(bars) - lookback)
    for index in range(start, len(bars)):
        first = bars[index - 2]
        third = bars[index]
        if third.low > first.high:
            lower, upper = first.high, third.low
            fully_filled = any(bar.low <= lower for bar in bars[index + 1 :])
            if not fully_filled:
                result = {
                    "type": "BULLISH_FVG",
                    "lower": lower,
                    "upper": upper,
                    "size_atr": (upper - lower) / atr if atr > 0 else 0.0,
                    "bar_start_ms": third.start_ms,
                }
        if third.high < first.low:
            lower, upper = third.high, first.low
            fully_filled = any(bar.high >= upper for bar in bars[index + 1 :])
            if not fully_filled:
                result = {
                    "type": "BEARISH_FVG",
                    "lower": lower,
                    "upper": upper,
                    "size_atr": (upper - lower) / atr if atr > 0 else 0.0,
                    "bar_start_ms": third.start_ms,
                }
    return result


def _fibonacci_geometry(bars: Sequence[Bar], action: str) -> dict[str, Any]:
    highs, lows = _pivots(bars)
    action = action.upper()
    start: Pivot | None = None
    end: Pivot | None = None

    if action == "BUY":
        for high in reversed(highs):
            low = _latest_before(lows, high.index)
            if low and high.price > low.price:
                start, end = low, high
                break
    elif action == "SELL":
        for low in reversed(lows):
            high = _latest_before(highs, low.index)
            if high and high.price > low.price:
                start, end = high, low
                break

    if not start or not end or not bars:
        return {
            "available": False,
            "retracement": None,
            "ote_low": 0.618,
            "ote_mid": 0.705,
            "ote_high": 0.790,
            "ote_hit": False,
            "level_618": None,
            "level_705": None,
            "level_790": None,
            "impulse_start": None,
            "impulse_end": None,
            "impulse_start_ms": None,
            "impulse_end_ms": None,
            "score": 0.0,
        }

    current = bars[-1].close
    distance = abs(end.price - start.price)
    if distance <= 0:
        return {
            "available": False,
            "retracement": None,
            "ote_low": 0.618,
            "ote_mid": 0.705,
            "ote_high": 0.790,
            "ote_hit": False,
            "level_618": None,
            "level_705": None,
            "level_790": None,
            "impulse_start": start.price,
            "impulse_end": end.price,
            "impulse_start_ms": start.start_ms,
            "impulse_end_ms": end.start_ms,
            "score": 0.0,
        }

    if action == "BUY":
        retracement = (end.price - current) / distance

        def level(ratio: float) -> float:
            return end.price - ratio * distance

    else:
        retracement = (current - end.price) / distance

        def level(ratio: float) -> float:
            return end.price + ratio * distance

    ote_hit = 0.618 <= retracement <= 0.790
    if ote_hit:
        score = 1.0
    elif 0.50 <= retracement <= 0.886:
        score = 0.60
    elif 0.382 <= retracement < 0.50:
        score = 0.35
    elif 0.0 <= retracement < 0.382:
        score = 0.15
    else:
        score = 0.0

    return {
        "available": True,
        "retracement": retracement,
        "ote_low": 0.618,
        "ote_mid": 0.705,
        "ote_high": 0.790,
        "ote_hit": ote_hit,
        "level_618": level(0.618),
        "level_705": level(0.705),
        "level_790": level(0.790),
        "impulse_start": start.price,
        "impulse_end": end.price,
        "impulse_start_ms": start.start_ms,
        "impulse_end_ms": end.start_ms,
        "score": score,
    }


def _aligned(action: str, value: str) -> bool:
    return value == ("BULLISH" if action.upper() == "BUY" else "BEARISH")


def _native_scores(
    action: str,
    h1: Mapping[str, Any],
    m15: Mapping[str, Any],
    sweep: Mapping[str, Any],
    fvg: Mapping[str, Any],
    fibonacci: Mapping[str, Any],
) -> tuple[dict[str, float], dict[str, tuple[str, ...]]]:
    direction = "BULLISH" if action.upper() == "BUY" else "BEARISH"
    structure = (
        0.35 * _aligned(action, str(h1.get("bias")))
        + 0.25 * _aligned(action, str(m15.get("bias")))
        + 0.20 * (str(h1.get("break_direction")) == direction)
        + 0.20 * (str(m15.get("break_direction")) == direction)
    )
    aligned_sweep = (
        action.upper() == "BUY" and sweep.get("type") == "SSL_SWEEP"
    ) or (action.upper() == "SELL" and sweep.get("type") == "BSL_SWEEP")
    aligned_fvg = (
        action.upper() == "BUY" and fvg.get("type") == "BULLISH_FVG"
    ) or (action.upper() == "SELL" and fvg.get("type") == "BEARISH_FVG")
    liquidity = 0.60 * aligned_sweep + 0.40 * aligned_fvg
    confirmation = (
        0.35 * (str(h1.get("break_direction")) == direction)
        + 0.35 * (str(m15.get("break_direction")) == direction)
        + 0.30 * aligned_fvg
    )
    scores = {
        "structure": float(structure),
        "liquidity": float(liquidity),
        "fibonacci": float(fibonacci.get("score") or 0.0),
        "confirmation": float(confirmation),
    }
    reasons = {
        "structure": (
            f"H1: {h1.get('bias', 'NEUTRAL')}; {h1.get('break', 'NONE')}",
            f"M15: {m15.get('bias', 'NEUTRAL')}; {m15.get('break', 'NONE')}",
        ),
        "liquidity": (
            f"Съём ликвидности: {sweep.get('type', 'NONE')}",
            f"FVG: {fvg.get('type', 'NONE')}",
        ),
        "fibonacci": (
            (
                f"Коррекция: {100.0 * float(fibonacci['retracement']):.1f}%"
                if fibonacci.get("retracement") is not None
                else "Коррекция Fibonacci недоступна"
            ),
            f"OTE достигнута: {bool(fibonacci.get('ote_hit'))}",
        ),
        "confirmation": (
            f"Направление структуры: {direction}",
            f"Подтверждающий FVG: {aligned_fvg}",
        ),
    }
    return scores, reasons


class MarketStructureEngine:
    """Point-in-time market structure derived from local M5 bars."""

    def __init__(self, bars_by_symbol: Mapping[str, Sequence[Bar]]) -> None:
        self._series: dict[str, dict[str, list[Bar]]] = {}
        self._ends: dict[str, dict[str, list[int]]] = {}
        self._cache: dict[tuple[str, int, str], dict[str, Any]] = {}
        for raw_symbol, raw_bars in bars_by_symbol.items():
            symbol = str(raw_symbol).upper()
            m5 = sorted(raw_bars, key=lambda item: item.start_ms)
            timeframes = {
                "M5": m5,
                "M15": resample_bars(m5, 15),
                "H1": resample_bars(m5, 60),
            }
            self._series[symbol] = timeframes
            self._ends[symbol] = {
                name: [bar.end_ms for bar in rows] for name, rows in timeframes.items()
            }

    @classmethod
    def from_csv(cls, path: Path) -> MarketStructureEngine:
        return cls(read_m5_bars(path))

    def symbols(self) -> tuple[str, ...]:
        return tuple(sorted(self._series))

    def _closed(self, symbol: str, timeframe: str, as_of_ms: int) -> list[Bar]:
        rows = self._series.get(symbol, {}).get(timeframe, [])
        ends = self._ends.get(symbol, {}).get(timeframe, [])
        count = bisect.bisect_right(ends, as_of_ms)
        return rows[:count]

    def snapshot(self, symbol: str, as_of: datetime | str, action: str) -> dict[str, Any]:
        parsed = _parse_time(as_of) if not isinstance(as_of, datetime) else as_of
        if parsed.tzinfo is None:
            raise ValueError("as-of time must include timezone information")
        parsed = parsed.astimezone(timezone.utc)
        as_of_ms = int(parsed.timestamp() * 1000)
        symbol = symbol.upper()
        action = action.upper()
        if action not in {"BUY", "SELL"}:
            raise ValueError("action must be BUY or SELL")
        cache_key = (symbol, as_of_ms, action)
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        m5 = self._closed(symbol, "M5", as_of_ms)
        m15 = self._closed(symbol, "M15", as_of_ms)
        h1 = self._closed(symbol, "H1", as_of_ms)
        if not m5:
            snapshot = {
                "schema_version": VERSION,
                "state": "NO_BARS_BEFORE_SIGNAL",
                "symbol": symbol,
                "action": action,
                "as_of": parsed.isoformat(),
                "bar_counts": {"M5": 0, "M15": 0, "H1": 0},
            }
            self._cache[cache_key] = snapshot
            return snapshot

        m5_view = m5[-240:]
        m15_view = m15[-200:]
        h1_view = h1[-160:]
        h1_state = _timeframe_state(h1_view)
        m15_state = _timeframe_state(m15_view)
        m5_state = _timeframe_state(m5_view)
        atr_m5 = _atr(m5_view)
        atr_m15 = _atr(m15_view)
        atr_h1 = _atr(h1_view)
        sweep = _latest_sweep(m5_view, atr_m5)
        fvg = _latest_fvg(m5_view, atr_m5)
        fibonacci = _fibonacci_geometry(m15_view, action)
        scores, reasons = _native_scores(
            action,
            h1_state,
            m15_state,
            sweep,
            fvg,
            fibonacci,
        )
        state = "OK" if len(m5) >= 30 and len(m15) >= 12 and len(h1) >= 6 else "DEGRADED"
        snapshot = {
            "schema_version": VERSION,
            "state": state,
            "symbol": symbol,
            "action": action,
            "as_of": parsed.isoformat(),
            "bar_counts": {"M5": len(m5), "M15": len(m15), "H1": len(h1)},
            "timeframes": {"M5": m5_state, "M15": m15_state, "H1": h1_state},
            "liquidity": {
                "ssl_sweep": sweep.get("type") == "SSL_SWEEP",
                "bsl_sweep": sweep.get("type") == "BSL_SWEEP",
                "sweep_type": sweep.get("type"),
                "sweep_level": sweep.get("level"),
                "sweep_depth_atr": sweep.get("depth_atr"),
                "sweep_bar_start_ms": sweep.get("bar_start_ms"),
            },
            "fvg": fvg,
            "fibonacci": fibonacci,
            "volatility": {"atr_m5": atr_m5, "atr_m15": atr_m15, "atr_h1": atr_h1},
            "factor_scores": scores,
            "factor_reasons": reasons,
            "safety": {
                "read_only": True,
                "orders_enabled": False,
                "publication_enabled": False,
                "exchange_api_called": False,
                "future_bars_used": False,
            },
        }
        self._cache[cache_key] = snapshot
        return snapshot


def safety_contract() -> Mapping[str, Any]:
    return {
        "read_only": True,
        "orders_enabled": False,
        "publication_enabled": False,
        "exchange_api_called": False,
        "future_bars_used": False,
    }
