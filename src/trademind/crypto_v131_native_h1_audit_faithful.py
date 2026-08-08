"""Faithful v1.31 native-H1 audit correction.

The first native-H1 audit intentionally removed the BybitShadow action gate, but
it made the M15 veto too permissive by checking only an opposite M15 break.
The documented production filter vetoes when either M15 bias OR M15 break is
explicitly opposite to H1. This module restores that exact behavior without
changing any thresholds or fitting parameters.

READ-ONLY. No orders. No publication.
"""

from __future__ import annotations

import argparse
import math
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from trademind import crypto_v131_native_h1_audit as base
from trademind.crypto_h1_swing_filter import FlowBar
from trademind.crypto_market_structure import MarketStructureEngine

VERSION = "1.31-native-h1-audit.2-faithful-m15"


def faithful_signal_geometry(
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

    # Snapshot construction is action-independent for H1/M15 structure.
    snapshot = engine.snapshot(symbol, as_of, "BUY")
    if snapshot.get("state") != "OK":
        return None

    timeframes = base._mapping(snapshot.get("timeframes"))
    h1 = base._mapping(timeframes.get("H1"))
    m15 = base._mapping(timeframes.get("M15"))
    volatility = base._mapping(snapshot.get("volatility"))

    h1_bias = str(h1.get("bias") or "NEUTRAL").upper()
    if h1_bias == "BULLISH":
        action = "BUY"
        opposite = "BEARISH"
    elif h1_bias == "BEARISH":
        action = "SELL"
        opposite = "BULLISH"
    else:
        return None

    # Exact veto logic used by crypto_h1_swing_filter.py:
    # reject if M15 bias OR M15 break is explicitly opposite.
    if str(m15.get("bias") or "NEUTRAL").upper() == opposite:
        return None
    if str(m15.get("break_direction") or "NONE").upper() == opposite:
        return None

    history = bars[:index]
    highs, lows = base._pivots(history)
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
    if volume_ratio < base.MIN_VOLUME_RATIO:
        return None

    if action == "BUY" and trigger.delta_turnover <= 0:
        return None
    if action == "SELL" and trigger.delta_turnover >= 0:
        return None

    entry = trigger.close
    stop = opposite_pivot.price
    target = base._number(
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
    if rr < base.MIN_TARGET_RR:
        return None

    atr_h1 = base._number(volatility.get("atr_h1"))
    if atr_h1 <= 0 or reward / atr_h1 < base.MIN_TARGET_ATR_H1:
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


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="TradeMind v1.31 faithful native-H1 audit")
    parser.add_argument("--bars", type=Path, default=Path("data/bybit_v1_9/bybit_bars.csv"))
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("data/crypto_v131_native_h1_audit_faithful"),
    )
    parser.add_argument("--max-bars", type=int, default=base.DEFAULT_MAX_BARS)
    parser.add_argument("--cost-r", type=float, default=base.DEFAULT_COST_R)
    args = parser.parse_args(argv)
    if args.max_bars < 1:
        raise SystemExit("--max-bars must be positive")

    # Patch only the audited signal gate. Trade evaluation/statistics stay identical.
    base._signal_geometry = faithful_signal_geometry
    payload = base.run(
        args.bars.expanduser().resolve(),
        args.output_dir.expanduser().resolve(),
        max_bars=args.max_bars,
        cost_r=args.cost_r,
    )
    payload["schema_version"] = VERSION
    payload["rules"]["m15_veto"] = "OPPOSITE_BIAS_OR_BREAK_DIRECTION"

    print("===== V1.31 FAITHFUL NATIVE H1 AUDIT =====")
    print("H1 chooses direction. M15 opposite bias OR break vetoes. Thresholds unchanged.")
    for label in ("all", "train_70", "holdout_30"):
        stats = payload[label]
        print(
            f"{label.upper():10s} trades={stats['trades']} "
            f"WR={base._fmt(stats['win_rate_pct'])}% "
            f"PF={base._fmt(stats['profit_factor'])} "
            f"EV={base._fmt(stats['expectancy_r'])}R "
            f"Total={base._fmt(stats['total_r'])}R "
            f"MaxDD={base._fmt(stats['max_drawdown_r'])}R"
        )
    print(f"Output: {args.output_dir.expanduser().resolve()}")
    print("READ-ONLY. No threshold changes. No parameter fitting.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
