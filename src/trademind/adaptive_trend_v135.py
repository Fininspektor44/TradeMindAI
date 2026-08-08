"""TradeMind v1.35 read-only AdaptiveTrend paper reproduction.

This module implements the core of the public 2026 AdaptiveTrend paper for the
existing Bybit USDT-perpetual research stack:

* 6-hour momentum entries;
* ATR-calibrated monotone trailing stops;
* monthly walk-forward parameter optimisation using only the preceding month;
* Sharpe-gated long/short selection;
* 70/30 long-short capital allocation.

Important reproduction note: the paper uses a 150+ asset universe and a
historical market-cap filter. TradeMind currently has a fixed Bybit universe and
no point-in-time market-cap archive, so this module deliberately omits that
stage rather than introduce look-ahead data. It is therefore a transparent
fixed-universe reproduction of the paper core, not a claim to reproduce the
paper's reported performance.

Research only. Public market-data API only. No orders and no publication.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from trademind.bybit_fixed20 import FIXED_SYMBOLS, parse_symbols
from trademind.bybit_intelligence import _http_json

VERSION = "1.35.0"
H6_MS = 6 * 60 * 60 * 1000
PERIODS_PER_YEAR = 4 * 365
DEFAULT_LOOKBACKS = (4, 8, 12, 20, 40)
DEFAULT_THRESHOLDS = (0.01, 0.02, 0.03, 0.05, 0.08)
DEFAULT_ATR_MULTIPLIERS = (2.0, 2.5, 3.0, 3.5)
DEFAULT_ATR_PERIOD = 14
DEFAULT_LONG_SHARPE = 1.30
DEFAULT_SHORT_SHARPE = 1.70
DEFAULT_LONG_ALLOCATION = 0.70


@dataclass(frozen=True, slots=True)
class H6Bar:
    symbol: str
    start_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    turnover: float

    @property
    def end_ms(self) -> int:
        return self.start_ms + H6_MS


@dataclass(frozen=True, slots=True)
class Params:
    lookback: int
    threshold: float
    atr_multiplier: float
    atr_period: int = DEFAULT_ATR_PERIOD

    def as_dict(self) -> dict[str, Any]:
        return {
            "lookback": self.lookback,
            "threshold": self.threshold,
            "atr_multiplier": self.atr_multiplier,
            "atr_period": self.atr_period,
        }


@dataclass(frozen=True, slots=True)
class Trade:
    symbol: str
    side: str
    entry_ms: int
    exit_ms: int
    entry: float
    exit: float
    net_return: float
    exit_reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "side": self.side,
            "entry_time": _iso_ms(self.entry_ms),
            "exit_time": _iso_ms(self.exit_ms),
            "entry": self.entry,
            "exit": self.exit,
            "net_return": self.net_return,
            "exit_reason": self.exit_reason,
        }


@dataclass(frozen=True, slots=True)
class SideSimulation:
    bar_returns: tuple[tuple[int, float], ...]
    trades: tuple[Trade, ...]
    sharpe: float
    total_return: float


@dataclass(frozen=True, slots=True)
class OptimizedSide:
    symbol: str
    side: str
    params: Params
    sharpe: float
    total_return: float
    trades: int


@dataclass(frozen=True, slots=True)
class BacktestRun:
    periods: int
    months: int
    trades: int
    wins: int
    losses: int
    win_rate: float
    profit_factor: float
    total_return: float
    cagr: float
    annual_volatility: float
    sharpe: float
    max_drawdown: float
    calmar: float
    output_dir: Path


def _iso_ms(value: int) -> str:
    return datetime.fromtimestamp(value / 1000.0, tz=timezone.utc).isoformat()


def _parse_date(value: str) -> int:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.astimezone(timezone.utc).timestamp() * 1000)


def _number(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def _month_start_ms(value_ms: int) -> int:
    dt = datetime.fromtimestamp(value_ms / 1000.0, tz=timezone.utc)
    return int(datetime(dt.year, dt.month, 1, tzinfo=timezone.utc).timestamp() * 1000)


def _next_month_ms(value_ms: int) -> int:
    dt = datetime.fromtimestamp(value_ms / 1000.0, tz=timezone.utc)
    year = dt.year + int(dt.month == 12)
    month = 1 if dt.month == 12 else dt.month + 1
    return int(datetime(year, month, 1, tzinfo=timezone.utc).timestamp() * 1000)


def _previous_month_ms(value_ms: int) -> int:
    dt = datetime.fromtimestamp(value_ms / 1000.0, tz=timezone.utc)
    year = dt.year - int(dt.month == 1)
    month = 12 if dt.month == 1 else dt.month - 1
    return int(datetime(year, month, 1, tzinfo=timezone.utc).timestamp() * 1000)


def fetch_h6_history(symbol: str, start_ms: int, end_ms: int) -> list[H6Bar]:
    """Fetch closed H6 USDT-perpetual candles from Bybit V5 public REST."""
    symbol = symbol.upper().strip()
    cursor_end = end_ms
    rows: dict[int, H6Bar] = {}
    while cursor_end >= start_ms:
        result = _http_json(
            "/v5/market/kline",
            {
                "category": "linear",
                "symbol": symbol,
                "interval": "360",
                "start": start_ms,
                "end": cursor_end,
                "limit": 1000,
            },
        )["result"]
        raw_rows = list(result.get("list", []))
        if not raw_rows:
            break
        oldest = cursor_end
        for raw in raw_rows:
            if not isinstance(raw, Sequence) or len(raw) < 7:
                continue
            candle_start = int(raw[0])
            oldest = min(oldest, candle_start)
            if candle_start < start_ms or candle_start + H6_MS > end_ms:
                continue
            open_price = _number(raw[1])
            high = _number(raw[2])
            low = _number(raw[3])
            close = _number(raw[4])
            volume = max(0.0, _number(raw[5]))
            turnover = max(0.0, _number(raw[6]))
            if min(open_price, high, low, close) <= 0 or high < low:
                continue
            rows[candle_start] = H6Bar(
                symbol=symbol,
                start_ms=candle_start,
                open=open_price,
                high=high,
                low=low,
                close=close,
                volume=volume,
                turnover=turnover,
            )
        if oldest <= start_ms or oldest >= cursor_end:
            break
        cursor_end = oldest - 1
        time.sleep(0.03)
    return [rows[key] for key in sorted(rows)]


def write_h6_csv(path: Path, bars: Sequence[H6Bar]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=("symbol", "start_ms", "time_utc", "open", "high", "low", "close", "volume", "turnover"),
        )
        writer.writeheader()
        for bar in bars:
            writer.writerow(
                {
                    "symbol": bar.symbol,
                    "start_ms": bar.start_ms,
                    "time_utc": _iso_ms(bar.start_ms),
                    "open": bar.open,
                    "high": bar.high,
                    "low": bar.low,
                    "close": bar.close,
                    "volume": bar.volume,
                    "turnover": bar.turnover,
                }
            )
    temporary.replace(path)


def read_h6_csv(path: Path) -> list[H6Bar]:
    rows: list[H6Bar] = []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            symbol = str(row.get("symbol") or "").upper().strip()
            start_ms = int(float(row.get("start_ms") or 0))
            values = [_number(row.get(key)) for key in ("open", "high", "low", "close")]
            if not symbol or start_ms <= 0 or min(values) <= 0:
                continue
            rows.append(
                H6Bar(
                    symbol=symbol,
                    start_ms=start_ms,
                    open=values[0],
                    high=values[1],
                    low=values[2],
                    close=values[3],
                    volume=max(0.0, _number(row.get("volume"))),
                    turnover=max(0.0, _number(row.get("turnover"))),
                )
            )
    rows.sort(key=lambda item: item.start_ms)
    return rows


def ensure_history(
    history_dir: Path,
    symbols: Sequence[str],
    start_ms: int,
    end_ms: int,
    *,
    refresh: bool = False,
) -> dict[str, list[H6Bar]]:
    history_dir.mkdir(parents=True, exist_ok=True)
    result: dict[str, list[H6Bar]] = {}
    for symbol in symbols:
        path = history_dir / f"{symbol}_H6.csv"
        if path.is_file() and not refresh:
            bars = read_h6_csv(path)
        else:
            print(f"Downloading {symbol} H6 ...")
            bars = fetch_h6_history(symbol, start_ms, end_ms)
            write_h6_csv(path, bars)
        bars = [bar for bar in bars if start_ms <= bar.start_ms < end_ms]
        if bars:
            result[symbol] = bars
            print(f"  {symbol}: {len(bars)} closed H6 bars")
        else:
            print(f"  {symbol}: no history in requested window")
    return result


def atr_series(bars: Sequence[H6Bar], period: int) -> list[float | None]:
    if period < 2:
        raise ValueError("ATR period must be >= 2")
    tr: list[float] = []
    result: list[float | None] = []
    previous_close: float | None = None
    for bar in bars:
        value = bar.high - bar.low
        if previous_close is not None:
            value = max(value, abs(bar.high - previous_close), abs(bar.low - previous_close))
        tr.append(value)
        if len(tr) >= period:
            result.append(sum(tr[-period:]) / period)
        else:
            result.append(None)
        previous_close = bar.close
    return result


def _annualized_sharpe(returns: Sequence[float], risk_free_rate: float = 0.0) -> float:
    if len(returns) < 8:
        return -math.inf
    sigma = statistics.pstdev(returns)
    if sigma <= 1e-12:
        return -math.inf
    mean = statistics.fmean(returns)
    return ((mean * PERIODS_PER_YEAR) - risk_free_rate) / (sigma * math.sqrt(PERIODS_PER_YEAR))


def _compound(returns: Sequence[float]) -> float:
    equity = 1.0
    for value in returns:
        equity *= max(1e-12, 1.0 + value)
    return equity - 1.0


def simulate_side(
    bars: Sequence[H6Bar],
    start_ms: int,
    end_ms: int,
    side: str,
    params: Params,
    *,
    cost_bps_per_side: float,
) -> SideSimulation:
    side = side.upper()
    if side not in {"LONG", "SHORT"}:
        raise ValueError("side must be LONG or SHORT")
    if cost_bps_per_side < 0:
        raise ValueError("cost cannot be negative")
    if not bars:
        return SideSimulation((), (), -math.inf, 0.0)

    atr = atr_series(bars, params.atr_period)
    direction = 1.0 if side == "LONG" else -1.0
    fee = cost_bps_per_side / 10_000.0
    in_position = False
    entry = 0.0
    entry_ms = 0
    stop = 0.0
    returns: list[tuple[int, float]] = []
    trades: list[Trade] = []

    eligible_indices = [i for i, bar in enumerate(bars) if start_ms <= bar.start_ms < end_ms]
    if not eligible_indices:
        return SideSimulation((), (), -math.inf, 0.0)
    last_index = eligible_indices[-1]

    for i in eligible_indices:
        bar = bars[i]
        period_return = 0.0
        was_open = in_position
        if was_open and i > 0:
            period_return += direction * (bar.close / bars[i - 1].close - 1.0)
            current_atr = atr[i]
            if current_atr is not None and current_atr > 0:
                if side == "LONG":
                    stop = max(stop, bar.close - params.atr_multiplier * current_atr)
                    stopped = bar.close < stop
                else:
                    stop = min(stop, bar.close + params.atr_multiplier * current_atr)
                    stopped = bar.close > stop
                if stopped:
                    period_return -= fee
                    raw_trade = direction * (bar.close / entry - 1.0) - 2.0 * fee
                    trades.append(Trade(bar.symbol, side, entry_ms, bar.start_ms, entry, bar.close, raw_trade, "TRAILING_STOP"))
                    in_position = False

        if not was_open and not in_position and i < last_index:
            if i >= params.lookback and atr[i] is not None and atr[i] > 0:
                momentum = bar.close / bars[i - params.lookback].close - 1.0
                enter = momentum > params.threshold if side == "LONG" else momentum < -params.threshold
                if enter:
                    in_position = True
                    entry = bar.close
                    entry_ms = bar.start_ms
                    stop = (
                        bar.close - params.atr_multiplier * float(atr[i])
                        if side == "LONG"
                        else bar.close + params.atr_multiplier * float(atr[i])
                    )
                    period_return -= fee

        if i == last_index and in_position:
            period_return -= fee
            raw_trade = direction * (bar.close / entry - 1.0) - 2.0 * fee
            trades.append(Trade(bar.symbol, side, entry_ms, bar.start_ms, entry, bar.close, raw_trade, "MONTH_END_REBALANCE"))
            in_position = False

        returns.append((bar.start_ms, period_return))

    raw_returns = [value for _, value in returns]
    return SideSimulation(
        bar_returns=tuple(returns),
        trades=tuple(trades),
        sharpe=_annualized_sharpe(raw_returns),
        total_return=_compound(raw_returns),
    )


def optimize_side(
    symbol: str,
    bars: Sequence[H6Bar],
    start_ms: int,
    end_ms: int,
    side: str,
    grid: Sequence[Params],
    *,
    cost_bps_per_side: float,
) -> OptimizedSide | None:
    best: tuple[float, float, Params, int] | None = None
    for params in grid:
        simulation = simulate_side(
            bars,
            start_ms,
            end_ms,
            side,
            params,
            cost_bps_per_side=cost_bps_per_side,
        )
        trades = len(simulation.trades)
        if trades < 2 or not math.isfinite(simulation.sharpe):
            continue
        score = (simulation.sharpe, simulation.total_return, params, trades)
        if best is None or (score[0], score[1]) > (best[0], best[1]):
            best = score
    if best is None:
        return None
    return OptimizedSide(symbol, side.upper(), best[2], best[0], best[1], best[3])


def _grid(
    lookbacks: Sequence[int],
    thresholds: Sequence[float],
    atr_multipliers: Sequence[float],
    atr_period: int,
) -> tuple[Params, ...]:
    return tuple(
        Params(int(lookback), float(threshold), float(multiplier), int(atr_period))
        for lookback in lookbacks
        for threshold in thresholds
        for multiplier in atr_multipliers
    )


def _resolve_conflicts(
    longs: list[OptimizedSide],
    shorts: list[OptimizedSide],
) -> tuple[list[OptimizedSide], list[OptimizedSide]]:
    long_by = {item.symbol: item for item in longs}
    short_by = {item.symbol: item for item in shorts}
    for symbol in set(long_by) & set(short_by):
        long_edge = long_by[symbol].sharpe - DEFAULT_LONG_SHARPE
        short_edge = short_by[symbol].sharpe - DEFAULT_SHORT_SHARPE
        if long_edge >= short_edge:
            short_by.pop(symbol, None)
        else:
            long_by.pop(symbol, None)
    return list(long_by.values()), list(short_by.values())


def _max_drawdown(returns: Sequence[float]) -> float:
    equity = 1.0
    peak = 1.0
    worst = 0.0
    for value in returns:
        equity *= max(1e-12, 1.0 + value)
        peak = max(peak, equity)
        worst = max(worst, 1.0 - equity / peak)
    return worst


def _profit_factor(trades: Sequence[Trade]) -> float:
    gross_win = sum(max(0.0, item.net_return) for item in trades)
    gross_loss = abs(sum(min(0.0, item.net_return) for item in trades))
    if gross_loss <= 1e-12:
        return math.inf if gross_win > 0 else 0.0
    return gross_win / gross_loss


def run_backtest(
    histories: Mapping[str, Sequence[H6Bar]],
    output_dir: Path,
    *,
    start_ms: int,
    end_ms: int,
    lookbacks: Sequence[int] = DEFAULT_LOOKBACKS,
    thresholds: Sequence[float] = DEFAULT_THRESHOLDS,
    atr_multipliers: Sequence[float] = DEFAULT_ATR_MULTIPLIERS,
    atr_period: int = DEFAULT_ATR_PERIOD,
    cost_bps_per_side: float = 8.0,
    long_sharpe_threshold: float = DEFAULT_LONG_SHARPE,
    short_sharpe_threshold: float = DEFAULT_SHORT_SHARPE,
    long_allocation: float = DEFAULT_LONG_ALLOCATION,
    risk_free_rate: float = 0.045,
) -> BacktestRun:
    if not 0.0 <= long_allocation <= 1.0:
        raise ValueError("long_allocation must be within 0..1")
    grid = _grid(lookbacks, thresholds, atr_multipliers, atr_period)
    portfolio_returns: list[tuple[int, float]] = []
    all_trades: list[Trade] = []
    month_rows: list[dict[str, Any]] = []

    month_ms = _month_start_ms(start_ms)
    if month_ms < start_ms:
        month_ms = _next_month_ms(month_ms)
    if month_ms == _month_start_ms(start_ms):
        month_ms = _next_month_ms(month_ms)

    while month_ms < end_ms:
        next_ms = min(_next_month_ms(month_ms), end_ms)
        train_start = _previous_month_ms(month_ms)
        train_end = month_ms
        long_candidates: list[OptimizedSide] = []
        short_candidates: list[OptimizedSide] = []

        for symbol, bars in histories.items():
            train_count = sum(train_start <= bar.start_ms < train_end for bar in bars)
            if train_count < max(max(lookbacks) + atr_period + 4, 80):
                continue
            long_best = optimize_side(symbol, bars, train_start, train_end, "LONG", grid, cost_bps_per_side=cost_bps_per_side)
            short_best = optimize_side(symbol, bars, train_start, train_end, "SHORT", grid, cost_bps_per_side=cost_bps_per_side)
            if long_best is not None and long_best.sharpe >= long_sharpe_threshold:
                long_candidates.append(long_best)
            if short_best is not None and short_best.sharpe >= short_sharpe_threshold:
                short_candidates.append(short_best)

        long_candidates, short_candidates = _resolve_conflicts(long_candidates, short_candidates)
        long_candidates.sort(key=lambda item: item.sharpe, reverse=True)
        short_candidates.sort(key=lambda item: item.sharpe, reverse=True)
        long_candidates = long_candidates[:15]
        short_candidates = short_candidates[:15]

        leg_returns: dict[int, float] = {}
        month_trades: list[Trade] = []
        long_weight = long_allocation / len(long_candidates) if long_candidates else 0.0
        short_weight = (1.0 - long_allocation) / len(short_candidates) if short_candidates else 0.0

        for item in long_candidates:
            simulation = simulate_side(
                histories[item.symbol], month_ms, next_ms, "LONG", item.params,
                cost_bps_per_side=cost_bps_per_side,
            )
            for timestamp, value in simulation.bar_returns:
                leg_returns[timestamp] = leg_returns.get(timestamp, 0.0) + long_weight * value
            month_trades.extend(simulation.trades)

        for item in short_candidates:
            simulation = simulate_side(
                histories[item.symbol], month_ms, next_ms, "SHORT", item.params,
                cost_bps_per_side=cost_bps_per_side,
            )
            for timestamp, value in simulation.bar_returns:
                leg_returns[timestamp] = leg_returns.get(timestamp, 0.0) + short_weight * value
            month_trades.extend(simulation.trades)

        portfolio_returns.extend(sorted(leg_returns.items()))
        all_trades.extend(month_trades)
        month_rows.append(
            {
                "month": _iso_ms(month_ms)[:7],
                "train_month": _iso_ms(train_start)[:7],
                "long_symbols": ",".join(item.symbol for item in long_candidates),
                "short_symbols": ",".join(item.symbol for item in short_candidates),
                "long_count": len(long_candidates),
                "short_count": len(short_candidates),
                "trades": len(month_trades),
                "month_return": _compound([value for _, value in sorted(leg_returns.items())]),
            }
        )
        month_ms = next_ms

    portfolio_returns.sort(key=lambda item: item[0])
    raw_returns = [value for _, value in portfolio_returns]
    total_return = _compound(raw_returns)
    periods = len(raw_returns)
    if periods >= 2:
        first_ms = portfolio_returns[0][0]
        last_ms = portfolio_returns[-1][0] + H6_MS
        years = max((last_ms - first_ms) / (365.0 * 24 * 60 * 60 * 1000), 1.0 / 365.0)
        cagr = (1.0 + total_return) ** (1.0 / years) - 1.0 if total_return > -1.0 else -1.0
        annual_vol = statistics.pstdev(raw_returns) * math.sqrt(PERIODS_PER_YEAR)
        sharpe = _annualized_sharpe(raw_returns, risk_free_rate=risk_free_rate)
    else:
        cagr = 0.0
        annual_vol = 0.0
        sharpe = 0.0
    max_dd = _max_drawdown(raw_returns)
    calmar = cagr / max_dd if max_dd > 1e-12 else 0.0
    wins = sum(item.net_return > 0 for item in all_trades)
    losses = sum(item.net_return < 0 for item in all_trades)
    completed = wins + losses
    win_rate = wins / completed if completed else 0.0
    pf = _profit_factor(all_trades)

    root = output_dir.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    with (root / "monthly_selection.csv").open("w", encoding="utf-8", newline="") as handle:
        fieldnames = ("month", "train_month", "long_symbols", "short_symbols", "long_count", "short_count", "trades", "month_return")
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(month_rows)
    with (root / "trades.csv").open("w", encoding="utf-8", newline="") as handle:
        fieldnames = ("symbol", "side", "entry_time", "exit_time", "entry", "exit", "net_return", "exit_reason")
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(item.as_dict() for item in all_trades)

    status = {
        "schema_version": VERSION,
        "state": "OK",
        "strategy": "ADAPTIVETREND_FIXED_UNIVERSE_REPRODUCTION",
        "paper": "Systematic Trend-Following with Adaptive Portfolio Construction: Enhancing Risk-Adjusted Alpha in Cryptocurrency Markets, arXiv:2602.11708",
        "reproduction_limits": [
            "Fixed Bybit universe instead of 150+ assets",
            "Historical market-cap filter omitted because point-in-time market-cap data is not stored",
            "Funding-rate history is not included in this first reproduction",
            "Parameter grid is explicit in this code because the accessible paper text states monthly grid search but not the exact grid values",
        ],
        "parameters": {
            "timeframe": "H6",
            "lookbacks": list(lookbacks),
            "entry_thresholds": list(thresholds),
            "atr_multipliers": list(atr_multipliers),
            "atr_period": atr_period,
            "long_sharpe_threshold": long_sharpe_threshold,
            "short_sharpe_threshold": short_sharpe_threshold,
            "long_allocation": long_allocation,
            "cost_bps_per_side": cost_bps_per_side,
            "risk_free_rate": risk_free_rate,
        },
        "periods": periods,
        "months": len(month_rows),
        "trades": len(all_trades),
        "wins": wins,
        "losses": losses,
        "win_rate": win_rate,
        "profit_factor": pf if math.isfinite(pf) else "INF",
        "total_return": total_return,
        "cagr": cagr,
        "annual_volatility": annual_vol,
        "sharpe": sharpe,
        "max_drawdown": max_dd,
        "calmar": calmar,
        "safety": {
            "read_only": True,
            "orders_enabled": False,
            "publication_enabled": False,
            "private_exchange_api": False,
        },
    }
    (root / "status.json").write_text(json.dumps(status, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return BacktestRun(periods, len(month_rows), len(all_trades), wins, losses, win_rate, pf, total_return, cagr, annual_vol, sharpe, max_dd, calmar, root)


def _float_list(value: str) -> tuple[float, ...]:
    return tuple(float(item.strip()) for item in value.split(",") if item.strip())


def _int_list(value: str) -> tuple[int, ...]:
    return tuple(int(item.strip()) for item in value.split(",") if item.strip())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="TradeMind v1.35 AdaptiveTrend fixed-universe reproduction")
    parser.add_argument("--symbols", default=",".join(FIXED_SYMBOLS))
    parser.add_argument("--start", default="2022-01-01T00:00:00+00:00")
    parser.add_argument("--end", default=datetime.now(timezone.utc).isoformat())
    parser.add_argument("--history-dir", type=Path, default=Path("data/adaptivetrend_v1_35/history_h6"))
    parser.add_argument("--output-dir", type=Path, default=Path("data/adaptivetrend_v1_35/backtest"))
    parser.add_argument("--refresh-history", action="store_true")
    parser.add_argument("--lookbacks", default=",".join(str(item) for item in DEFAULT_LOOKBACKS))
    parser.add_argument("--thresholds", default=",".join(str(item) for item in DEFAULT_THRESHOLDS))
    parser.add_argument("--atr-multipliers", default=",".join(str(item) for item in DEFAULT_ATR_MULTIPLIERS))
    parser.add_argument("--atr-period", type=int, default=DEFAULT_ATR_PERIOD)
    parser.add_argument("--cost-bps-per-side", type=float, default=8.0)
    parser.add_argument("--long-sharpe", type=float, default=DEFAULT_LONG_SHARPE)
    parser.add_argument("--short-sharpe", type=float, default=DEFAULT_SHORT_SHARPE)
    parser.add_argument("--long-allocation", type=float, default=DEFAULT_LONG_ALLOCATION)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        symbols = parse_symbols(args.symbols)
        start_ms = _parse_date(args.start)
        requested_end_ms = _parse_date(args.end)
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        end_ms = min(requested_end_ms, now_ms - (now_ms % H6_MS))
        if end_ms <= start_ms:
            raise ValueError("end must be after start")
        histories = ensure_history(args.history_dir, symbols, start_ms, end_ms, refresh=args.refresh_history)
        run = run_backtest(
            histories,
            args.output_dir,
            start_ms=start_ms,
            end_ms=end_ms,
            lookbacks=_int_list(args.lookbacks),
            thresholds=_float_list(args.thresholds),
            atr_multipliers=_float_list(args.atr_multipliers),
            atr_period=args.atr_period,
            cost_bps_per_side=args.cost_bps_per_side,
            long_sharpe_threshold=args.long_sharpe,
            short_sharpe_threshold=args.short_sharpe,
            long_allocation=args.long_allocation,
        )
    except (OSError, ValueError, RuntimeError, TypeError, json.JSONDecodeError) as exc:
        print(f"v1.35 AdaptiveTrend reproduction failed: {exc}")
        return 1

    pf = "INF" if math.isinf(run.profit_factor) else f"{run.profit_factor:.3f}"
    print("\nTradeMind v1.35 ADAPTIVETREND FIXED-UNIVERSE REPRODUCTION")
    print("Source: arXiv:2602.11708 (2026). H6 momentum + ATR trailing + monthly walk-forward selection + 70/30 allocation.")
    print("NOTE: fixed Bybit universe; point-in-time market-cap filter and historical funding are not yet reproduced.")
    print(f"Months: {run.months}")
    print(f"Trades: {run.trades}")
    print(f"Wins/Losses: {run.wins}/{run.losses}")
    print(f"Win rate: {100.0 * run.win_rate:.2f}%")
    print(f"Profit factor: {pf}")
    print(f"Total return: {100.0 * run.total_return:.2f}%")
    print(f"CAGR: {100.0 * run.cagr:.2f}%")
    print(f"Annual volatility: {100.0 * run.annual_volatility:.2f}%")
    print(f"Sharpe: {run.sharpe:.3f}")
    print(f"Max drawdown: {100.0 * run.max_drawdown:.2f}%")
    print(f"Calmar: {run.calmar:.3f}")
    print(f"Output: {run.output_dir}")
    print("READ-ONLY. No orders. No publication. Public Bybit market-data API only.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
