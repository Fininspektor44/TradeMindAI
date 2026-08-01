"""Shared models and deterministic helpers for SMC OTE research."""

from __future__ import annotations

import csv
import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Sequence

from trademind.market.models import Candle

SCHEMA_VERSION = "1.5.0"
DEFAULT_SYMBOLS = (
    "XAUUSD", "XAGUSD", ".USTECHCASH", ".US500CASH", ".US30CASH",
    "WTI", "BRENT", "EURUSD", "GBPUSD", "USDJPY", "USDCHF",
    "USDCAD", "AUDUSD", "NZDUSD",
)
FIB_LEVELS = (("618", 0.618), ("705", 0.705), ("790", 0.790))
HORIZON_BARS = (("H3", 36), ("H6", 72), ("H12", 144))
VALID_ACTIONS = {"BUY", "SELL"}

SIGNAL_FIELDS = (
    "schema_version", "signal_id", "signal_time", "source_bar_time",
    "server_utc_offset_hours", "symbol", "timeframe", "session", "action",
    "variant", "fib_ratio", "score", "score_bucket", "setup_break",
    "setup_start_time", "setup_end_time", "setup_age_bars", "anchor_price",
    "impulse_extreme", "impulse_range", "impulse_atr", "fib_618", "fib_705",
    "fib_790", "entry_price", "stop_price", "target_price", "risk_price",
    "reward_price", "rr", "stop_buffer", "h1_bias", "h4_bias", "h1_aligned",
    "h4_aligned", "liquidity_sweep", "fvg_aligned", "confirmation", "rvol_20",
    "tick_rate_ratio_20", "direction_imbalance", "spread_ratio_20", "spread_cost",
    "atr", "reasons", "outcome_h3", "exit_time_h3", "exit_price_h3",
    "result_r_h3", "mfe_r_h3", "mae_r_h3", "outcome_h6", "exit_time_h6",
    "exit_price_h6", "result_r_h6", "mfe_r_h6", "mae_r_h6", "outcome_h12",
    "exit_time_h12", "exit_price_h12", "result_r_h12", "mfe_r_h12", "mae_r_h12",
)
STATE_FIELDS = (
    "captured_at", "symbol", "action", "variant", "score_filter", "horizon",
    "signals", "completed", "trading_days", "status", "win_rate",
    "profit_factor_r", "avg_r", "early_avg_r", "late_avg_r", "max_drawdown_r",
    "max_loss_streak", "reasons",
)


@dataclass(frozen=True, slots=True)
class Pivot:
    index: int
    price: float


@dataclass(slots=True)
class ActiveSetup:
    symbol: str
    action: str
    break_label: str
    start_index: int
    end_index: int
    anchor_price: float
    extreme_price: float
    atr: float
    sweep: bool
    fvg_aligned: bool
    touched_index: int | None = None
    frozen: bool = False
    emitted: set[str] = field(default_factory=set)


@dataclass(frozen=True, slots=True)
class HigherBar:
    end_time: datetime
    open: float
    high: float
    low: float
    close: float


@dataclass(frozen=True, slots=True)
class OteSummary:
    source_rows: int
    healthy_rows: int
    signals: int
    completed_h3: int
    completed_h6: int
    completed_h12: int
    states: int
    signals_path: Path
    states_path: Path
    dashboard_path: Path


def number(value: float | int | None) -> str:
    if value is None:
        return ""
    result = float(value)
    if math.isinf(result):
        return "inf" if result > 0 else "-inf"
    if math.isnan(result):
        return "nan"
    return f"{result:.12g}"


def value_float(row: dict[str, str], key: str, default: float = 0.0) -> float:
    try:
        return float(str(row.get(key, "") or default).strip())
    except (TypeError, ValueError):
        return default


def value_int(row: dict[str, str], key: str, default: int = 0) -> int:
    try:
        return int(float(str(row.get(key, "") or default).strip()))
    except (TypeError, ValueError):
        return default


def utc_time(epoch_seconds: int, server_utc_offset_hours: int) -> datetime:
    server_clock = datetime.fromtimestamp(epoch_seconds, tz=timezone.utc)
    return server_clock - timedelta(hours=server_utc_offset_hours)


def session_for_time(value: datetime) -> str:
    hour = value.astimezone(timezone.utc).hour
    if hour < 7:
        return "ASIA"
    if hour < 12:
        return "LONDON"
    if hour < 17:
        return "LONDON_NY_OVERLAP"
    if hour < 21:
        return "NEW_YORK"
    return "OFF_HOURS"


def load_volume_rows(
    path: Path,
    symbols: Iterable[str] = DEFAULT_SYMBOLS,
) -> tuple[list[dict[str, str]], int]:
    expected = {item.upper() for item in symbols}
    rows: list[dict[str, str]] = []
    source_rows = 0
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        for raw in csv.DictReader(handle):
            source_rows += 1
            row = {key: str(value or "").strip() for key, value in dict(raw).items()}
            symbol = row.get("symbol", "").upper()
            if symbol not in expected or row.get("timeframe", "").upper() != "M5":
                continue
            if row.get("tick_copy_status", "").upper() != "OK":
                continue
            if value_int(row, "time") <= 0 or value_int(row, "tick_count") <= 0:
                continue
            if value_float(row, "point") <= 0:
                continue
            prices = [value_float(row, key, math.nan) for key in ("open", "high", "low", "close")]
            if not all(math.isfinite(item) for item in prices):
                continue
            row["symbol"] = symbol
            row["timeframe"] = "M5"
            rows.append(row)
    rows.sort(key=lambda row: (row["symbol"], value_int(row, "time")))
    return rows, source_rows


def candle_from_row(row: dict[str, str], offset: int) -> Candle:
    return Candle(
        symbol=row["symbol"], timeframe="M5", time=utc_time(value_int(row, "time"), offset),
        open=value_float(row, "open"), high=value_float(row, "high"),
        low=value_float(row, "low"), close=value_float(row, "close"),
        tick_volume=value_int(row, "bar_tick_volume"),
        spread=max(0, int(round(value_float(row, "spread_mean_points")))),
    )


def atr_series(candles: Sequence[Candle], period: int = 14) -> list[float]:
    output: list[float] = []
    ranges: list[float] = []
    for index, candle in enumerate(candles):
        previous = candles[index - 1].close if index else candle.close
        ranges.append(max(candle.high - candle.low, abs(candle.high - previous), abs(candle.low - previous)))
        window = ranges[max(0, len(ranges) - period):]
        output.append(sum(window) / len(window))
    return output


def confirmed_pivots(candles: Sequence[Candle], window: int = 2) -> tuple[list[Pivot], list[Pivot]]:
    highs: list[Pivot] = []
    lows: list[Pivot] = []
    for index in range(window, len(candles) - window):
        center = candles[index]
        neighbors = list(candles[index - window:index]) + list(candles[index + 1:index + window + 1])
        if all(center.high > item.high for item in neighbors):
            highs.append(Pivot(index, center.high))
        if all(center.low < item.low for item in neighbors):
            lows.append(Pivot(index, center.low))
    return highs, lows


def latest_confirmed(pivots: Sequence[Pivot], current_index: int, window: int = 2) -> Pivot | None:
    limit = current_index - window
    return next((pivot for pivot in reversed(pivots) if pivot.index <= limit), None)


def swept_anchor(candles: Sequence[Candle], pivot: Pivot, action: str, lookback: int = 20) -> bool:
    reference = candles[max(0, pivot.index - lookback):pivot.index]
    if not reference:
        return False
    current = candles[pivot.index]
    if action == "BUY":
        level = min(item.low for item in reference)
        return current.low < level and current.close >= level
    level = max(item.high for item in reference)
    return current.high > level and current.close <= level


def fib_prices(action: str, anchor: float, extreme: float) -> dict[str, float]:
    if action not in VALID_ACTIONS:
        raise ValueError(f"Unsupported action: {action}")
    distance = abs(extreme - anchor)
    if distance <= 0:
        raise ValueError("Impulse range must be positive")
    if action == "BUY":
        return {name: extreme - distance * ratio for name, ratio in FIB_LEVELS}
    return {name: extreme + distance * ratio for name, ratio in FIB_LEVELS}


def resample(candles: Sequence[Candle], minutes: int) -> list[HigherBar]:
    if minutes <= 0:
        raise ValueError("resample minutes must be positive")
    seconds = minutes * 60
    groups: dict[int, list[Candle]] = defaultdict(list)
    for candle in candles:
        epoch = int(candle.time.timestamp())
        groups[epoch - epoch % seconds].append(candle)
    output: list[HigherBar] = []
    for bucket, rows in sorted(groups.items()):
        rows.sort(key=lambda item: item.time)
        output.append(HigherBar(
            end_time=datetime.fromtimestamp(bucket + seconds, tz=timezone.utc),
            open=rows[0].open, high=max(item.high for item in rows),
            low=min(item.low for item in rows), close=rows[-1].close,
        ))
    return output


def bias_from_bars(bars: Sequence[HigherBar]) -> str:
    if len(bars) < 6:
        return "NEUTRAL"
    recent = bars[-6:]
    fast = sum(item.close for item in recent[-3:]) / 3
    slow = sum(item.close for item in recent) / 6
    slope = recent[-1].close - recent[-3].close
    span = max(item.high for item in recent) - min(item.low for item in recent)
    tolerance = max(1e-12, span * 0.03)
    if fast > slow and slope > tolerance:
        return "BULLISH"
    if fast < slow and slope < -tolerance:
        return "BEARISH"
    return "NEUTRAL"


def higher_timeframe_biases(candles: Sequence[Candle], minutes: int) -> list[str]:
    bars = resample(candles, minutes)
    eligible: list[HigherBar] = []
    output: list[str] = []
    cursor = 0
    for candle in candles:
        while cursor < len(bars) and bars[cursor].end_time <= candle.time:
            eligible.append(bars[cursor])
            cursor += 1
        output.append(bias_from_bars(eligible))
    return output


def ratio_previous(rows: Sequence[dict[str, str]], index: int, key: str, window: int = 20) -> float:
    values = [value_float(row, key) for row in rows[max(0, index - window):index] if value_float(row, key) > 0]
    mean = sum(values) / len(values) if values else 0.0
    return value_float(rows[index], key) / mean if mean > 0 else 0.0


def aligned(action: str, bias: str) -> bool:
    return (action == "BUY" and bias == "BULLISH") or (action == "SELL" and bias == "BEARISH")
