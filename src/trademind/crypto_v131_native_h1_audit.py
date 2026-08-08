"""TradeMind v1.31 native H1 strategy audit.

This diagnostic tests the documented CRYPTO_H1_SWING_M5_VOLUME_BREAKOUT rules
without using BybitShadow BUY/SELL/NONE as a prerequisite. H1 structure itself
chooses direction. The module is read-only and never places or publishes orders.

Rules kept fixed:
- H1 bias chooses BUY or SELL.
- M15 veto only on an explicit opposite structure break.
- Latest closed M5 must close through the latest confirmed M5 pivot.
- M5 volume >= 1.20x median of the prior 20 closed M5 bars.
- M5 delta must agree with direction.
- Stop is the latest confirmed opposite M5 pivot.
- Target is the latest confirmed H1 swing in trade direction.
- Minimum target space is 1.80R and 0.70 ATR H1.

Backtest convention is conservative: signal is known only after the trigger M5
bar closes; future evaluation starts with the next M5 bar; if stop and target
are both touched in one bar, stop wins. No symbol selection or parameter fitting
is performed. The final chronological 30 percent is reported separately as a
holdout diagnostic.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from trademind.crypto_h1_swing_filter import FlowBar, read_flow_bars
from trademind.crypto_market_structure import MarketStructureEngine

VERSION = "1.31-native-h1-audit.1"
SETUP_FAMILY = "CRYPTO_H1_SWING_M5_VOLUME_BREAKOUT"
MIN_VOLUME_RATIO = 1.20
MIN_TARGET_RR = 1.80
MIN_TARGET_ATR_H1 = 0.70
DEFAULT_MAX_BARS = 72
DEFAULT_COST_R = 0.04


@dataclass(frozen=True, slots=True)
class Pivot:
    index: int
    price: float
    start_ms: int


@dataclass(frozen=True, slots=True)
class Trade:
    symbol: str
    action: str
    observed_at: datetime
    entry: float
    stop: float
    target: float
    rr: float
    volume_ratio: float
    delta_turnover: float
    exit_at: datetime
    exit_price: float
    exit_reason: str
    net_r: float
    bars_observed: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "action": self.action,
            "observed_at": self.observed_at.isoformat(),
            "entry": self.entry,
            "stop": self.stop,
            "target": self.target,
            "rr": self.rr,
            "volume_ratio": self.volume_ratio,
            "delta_turnover": self.delta_turnover,
            "exit_at": self.exit_at.isoformat(),
            "exit_price": self.exit_price,
            "exit_reason": self.exit_reason,
            "net_r": self.net_r,
            "bars_observed": self.bars_observed,
        }


def _number(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if math.isfinite(parsed) else default


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _pivots(
    bars: Sequence[FlowBar], *, left: int = 2, right: int = 2
) -> tuple[list[Pivot], list[Pivot]]:
    highs: list[Pivot] = []
    lows: list[Pivot] = []
    for index in range(left, len(bars) - right):
        window = bars[index - left : index + right + 1]
        current = bars[index]
        other_highs = [bar.high for offset, bar in enumerate(window) if offset != left]
        other_lows = [bar.low for offset, bar in enumerate(window) if offset != left]
        if current.high > max(other_highs):
            highs.append(Pivot(index, current.high, current.start_ms))
        if current.low < min(other_lows):
            lows.append(Pivot(index, current.low, current.start_ms))
    return highs, lows


def _signal_geometry(
    symbol: str,
    bars: Sequence[FlowBar],
    index: int,
    engine: MarketStructureEngine,
) -> dict[str, Any] | None:
    if index < 24:
        return None
    trigger = bars[index]
    previous = bars[index - 1]
    as_of = datetime.fromtimestamp(trigger.end_ms / 1000.0, tz=timezone.utc) + timedelta(milliseconds=1)

    # Snapshot timeframes do not depend on the requested action. BUY is used only
    # to obtain a point-in-time snapshot, then H1 itself chooses the direction.
    snapshot = engine.snapshot(symbol, as_of, "BUY")
    if snapshot.get("state") != "OK":
        return None
    timeframes = _mapping(snapshot.get("timeframes"))
    h1 = _mapping(timeframes.get("H1"))
    m15 = _mapping(timeframes.get("M15"))
    volatility = _mapping(snapshot.get("volatility"))

    h1_bias = str(h1.get("bias") or "NEUTRAL").upper()
    if h1_bias == "BULLISH":
        action = "BUY"
        opposite = "BEARISH"
    elif h1_bias == "BEARISH":
        action = "SELL"
        opposite = "BULLISH"
    else:
        return None

    # Documented M15 role is veto only when structure explicitly breaks against H1.
    if str(m15.get("break_direction") or "NONE").upper() == opposite:
        return None

    history = bars[:index]
    highs, lows = _pivots(history)
    if not highs or not lows:
        return None
    breakout = highs[-1] if action == "BUY" else lows[-1]
    opposite_pivot = lows[-1] if action == "BUY" else highs[-1]

    fresh_breakout = (
        previous.close <= breakout.price < trigger.close
        if action == "BUY"
        else previous.close >= breakout.price > trigger.close
    )
    if not fresh_breakout:
        return None

    prior_volumes = [bar.volume for bar in history[-20:] if bar.volume > 0]
    if not prior_volumes:
        return None
    median_volume = statistics.median(prior_volumes)
    volume_ratio = trigger.volume / median_volume if median_volume > 0 else 0.0
    if volume_ratio < MIN_VOLUME_RATIO:
        return None
    if action == "BUY" and trigger.delta_turnover <= 0:
        return None
    if action == "SELL" and trigger.delta_turnover >= 0:
        return None

    entry = trigger.close
    stop = opposite_pivot.price
    target = _number(
        h1.get("last_swing_high") if action == "BUY" else h1.get("last_swing_low")
    )
    if action == "BUY":
        if not stop < entry < target:
            return None
    else:
        if not target < entry < stop:
            return None

    risk = abs(entry - stop)
    reward = abs(target - entry)
    if risk <= 0:
        return None
    rr = reward / risk
    if rr < MIN_TARGET_RR:
        return None
    atr_h1 = _number(volatility.get("atr_h1"))
    if atr_h1 <= 0 or reward / atr_h1 < MIN_TARGET_ATR_H1:
        return None

    return {
        "action": action,
        "observed_at": as_of,
        "entry": entry,
        "stop": stop,
        "target": target,
        "rr": rr,
        "volume_ratio": volume_ratio,
        "delta_turnover": trigger.delta_turnover,
    }


def _evaluate_trade(
    symbol: str,
    bars: Sequence[FlowBar],
    signal_index: int,
    geometry: Mapping[str, Any],
    *,
    max_bars: int,
    cost_r: float,
) -> Trade | None:
    entry = float(geometry["entry"])
    stop = float(geometry["stop"])
    target = float(geometry["target"])
    action = str(geometry["action"])
    risk = abs(entry - stop)
    if risk <= 0:
        return None

    last_index = min(len(bars) - 1, signal_index + max_bars)
    if signal_index + 1 > last_index:
        return None

    for offset, bar_index in enumerate(range(signal_index + 1, last_index + 1), start=1):
        bar = bars[bar_index]
        if action == "BUY":
            stop_hit = bar.low <= stop
            target_hit = bar.high >= target
        else:
            stop_hit = bar.high >= stop
            target_hit = bar.low <= target

        if stop_hit:
            return Trade(
                symbol=symbol,
                action=action,
                observed_at=geometry["observed_at"],
                entry=entry,
                stop=stop,
                target=target,
                rr=float(geometry["rr"]),
                volume_ratio=float(geometry["volume_ratio"]),
                delta_turnover=float(geometry["delta_turnover"]),
                exit_at=datetime.fromtimestamp(bar.end_ms / 1000.0, tz=timezone.utc),
                exit_price=stop,
                exit_reason="STOP_FIRST_CONSERVATIVE" if target_hit else "STOP",
                net_r=round(-1.0 - max(0.0, cost_r), 6),
                bars_observed=offset,
            )
        if target_hit:
            return Trade(
                symbol=symbol,
                action=action,
                observed_at=geometry["observed_at"],
                entry=entry,
                stop=stop,
                target=target,
                rr=float(geometry["rr"]),
                volume_ratio=float(geometry["volume_ratio"]),
                delta_turnover=float(geometry["delta_turnover"]),
                exit_at=datetime.fromtimestamp(bar.end_ms / 1000.0, tz=timezone.utc),
                exit_price=target,
                exit_reason="TARGET",
                net_r=round(float(geometry["rr"]) - max(0.0, cost_r), 6),
                bars_observed=offset,
            )

    final_bar = bars[last_index]
    direction = 1.0 if action == "BUY" else -1.0
    net_r = direction * (final_bar.close - entry) / risk - max(0.0, cost_r)
    return Trade(
        symbol=symbol,
        action=action,
        observed_at=geometry["observed_at"],
        entry=entry,
        stop=stop,
        target=target,
        rr=float(geometry["rr"]),
        volume_ratio=float(geometry["volume_ratio"]),
        delta_turnover=float(geometry["delta_turnover"]),
        exit_at=datetime.fromtimestamp(final_bar.end_ms / 1000.0, tz=timezone.utc),
        exit_price=final_bar.close,
        exit_reason="TIMEOUT_MARK_TO_MARKET",
        net_r=round(net_r, 6),
        bars_observed=last_index - signal_index,
    )


def _stats(rows: Sequence[Trade]) -> dict[str, Any]:
    values = [trade.net_r for trade in rows]
    wins = [value for value in values if value > 0]
    losses = [value for value in values if value < 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    pf = gross_profit / gross_loss if gross_loss > 0 else (float("inf") if gross_profit > 0 else 0.0)
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for value in values:
        equity += value
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    return {
        "trades": len(rows),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": 100.0 * len(wins) / len(rows) if rows else 0.0,
        "profit_factor": pf,
        "expectancy_r": statistics.fmean(values) if values else 0.0,
        "total_r": sum(values),
        "max_drawdown_r": max_dd,
    }


def _write_csv(path: Path, rows: Sequence[Trade]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = list(Trade.__dataclass_fields__.keys())
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            payload = row.as_dict()
            writer.writerow({key: payload[key] for key in fields})


def run(
    bars_path: Path,
    output_dir: Path,
    *,
    max_bars: int = DEFAULT_MAX_BARS,
    cost_r: float = DEFAULT_COST_R,
) -> dict[str, Any]:
    bars_by_symbol = read_flow_bars(bars_path)
    engine = MarketStructureEngine.from_csv(bars_path)
    trades: list[Trade] = []

    # One live position per symbol. After a signal, skip forward until its exit.
    for symbol, bars in sorted(bars_by_symbol.items()):
        index = 24
        while index < len(bars) - 1:
            geometry = _signal_geometry(symbol, bars, index, engine)
            if geometry is None:
                index += 1
                continue
            trade = _evaluate_trade(
                symbol,
                bars,
                index,
                geometry,
                max_bars=max_bars,
                cost_r=cost_r,
            )
            if trade is None:
                index += 1
                continue
            trades.append(trade)
            index += max(1, trade.bars_observed + 1)

    trades.sort(key=lambda row: row.observed_at)
    split = int(len(trades) * 0.70)
    train = trades[:split]
    holdout = trades[split:]
    payload = {
        "schema_version": VERSION,
        "setup_family": SETUP_FAMILY,
        "source": str(bars_path),
        "rules": {
            "direction_source": "H1_BIAS",
            "m15_veto": "OPPOSITE_BREAK_DIRECTION_ONLY",
            "minimum_volume_ratio": MIN_VOLUME_RATIO,
            "minimum_target_rr": MIN_TARGET_RR,
            "minimum_target_atr_h1": MIN_TARGET_ATR_H1,
            "max_bars": max_bars,
            "cost_r": cost_r,
            "overlap_policy": "ONE_ACTIVE_TRADE_PER_SYMBOL",
            "same_bar_stop_target": "STOP_WINS",
        },
        "all": _stats(trades),
        "train_70": _stats(train),
        "holdout_30": _stats(holdout),
        "safety": {
            "read_only": True,
            "orders_enabled": False,
            "publication_enabled": False,
            "source_files_modified": False,
            "future_bars_used_for_signal": False,
        },
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    _write_csv(output_dir / "trades.csv", trades)
    (output_dir / "status.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )
    return payload


def _fmt(value: Any) -> str:
    if isinstance(value, float):
        if math.isinf(value):
            return "INF"
        return f"{value:.4f}"
    return str(value)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="TradeMind v1.31 native H1 audit backtest")
    parser.add_argument("--bars", type=Path, default=Path("data/bybit_v1_9/bybit_bars.csv"))
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("data/crypto_v131_native_h1_audit"),
    )
    parser.add_argument("--max-bars", type=int, default=DEFAULT_MAX_BARS)
    parser.add_argument("--cost-r", type=float, default=DEFAULT_COST_R)
    args = parser.parse_args(argv)
    if args.max_bars < 1:
        raise SystemExit("--max-bars must be positive")
    payload = run(
        args.bars.expanduser().resolve(),
        args.output_dir.expanduser().resolve(),
        max_bars=args.max_bars,
        cost_r=args.cost_r,
    )
    print("===== V1.31 NATIVE H1 AUDIT =====")
    print("Direction comes from H1, not BybitShadow action.")
    for label in ("all", "train_70", "holdout_30"):
        stats = payload[label]
        print(
            f"{label.upper():10s} trades={stats['trades']} "
            f"WR={_fmt(stats['win_rate_pct'])}% "
            f"PF={_fmt(stats['profit_factor'])} "
            f"EV={_fmt(stats['expectancy_r'])}R "
            f"Total={_fmt(stats['total_r'])}R "
            f"MaxDD={_fmt(stats['max_drawdown_r'])}R"
        )
    print(f"Output: {args.output_dir.expanduser().resolve()}")
    print("READ-ONLY. No parameter fitting. Holdout is chronological final 30%.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
