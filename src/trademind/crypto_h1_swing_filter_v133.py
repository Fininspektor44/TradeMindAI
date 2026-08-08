"""TradeMind v1.33.1 H1 swing opportunity filter.

This is a read-only learning-shadow filter. It keeps H1 direction as the hard
trend gate and only an explicit opposite M15 structure break as a veto. A
confirmed M5 close breakout may have happened within the last six closed M5
bars, provided the latest close still holds beyond the broken extremum.

Compared with v1.33.0, the shadow thresholds are intentionally less restrictive
so the system can actually collect forward candidates and outcomes:
- breakout lookback: 6 closed M5 bars;
- volume ratio: >= 1.00x median;
- first H1 target: >= 1.20R;
- H1 target distance: >= 0.40 ATR H1.
Delta direction remains mandatory. Orders and publication remain disabled.
"""

from __future__ import annotations

import statistics
from contextlib import contextmanager
from typing import Any, Iterator, Mapping, Sequence

from trademind import crypto_h1_swing_filter as base
from trademind.crypto_market_structure import MarketStructureEngine

VERSION = "1.33.1"
SETUP_FAMILY = base.SETUP_FAMILY
BREAKOUT_LOOKBACK_BARS = 6
MIN_VOLUME_RATIO = 1.00
MIN_TARGET_RR = 1.20
MIN_TARGET_ATR_H1 = 0.40

FlowBar = base.FlowBar
FlowHistory = base.FlowHistory
Opportunity = base.Opportunity
Pivot = base.Pivot


def evaluate_opportunity(
    action: str,
    m5_bars: Sequence[FlowBar],
    snapshot: Mapping[str, Any],
    *,
    minimum_volume_ratio: float = MIN_VOLUME_RATIO,
    minimum_target_rr: float = MIN_TARGET_RR,
    minimum_target_atr_h1: float = MIN_TARGET_ATR_H1,
    breakout_lookback_bars: int = BREAKOUT_LOOKBACK_BARS,
) -> Opportunity:
    """Evaluate a recent M5 breakout-and-hold continuation setup."""
    action = action.upper()
    if action not in {"BUY", "SELL"}:
        return base._reject(action, "ACTION_NOT_BUY_SELL")
    if snapshot.get("state") != "OK":
        return base._reject(action, "STRUCTURE_NOT_READY")
    if len(m5_bars) < 25:
        return base._reject(action, "M5_HISTORY_TOO_SHORT")
    if breakout_lookback_bars < 1:
        raise ValueError("breakout_lookback_bars must be positive")

    timeframes = base._mapping(snapshot.get("timeframes"))
    h1 = base._mapping(timeframes.get("H1"))
    m15 = base._mapping(timeframes.get("M15"))
    volatility = base._mapping(snapshot.get("volatility"))
    direction = "BULLISH" if action == "BUY" else "BEARISH"
    opposite = "BEARISH" if action == "BUY" else "BULLISH"

    if str(h1.get("bias") or "NEUTRAL") != direction:
        return base._reject(action, "H1_DIRECTION_NOT_ALIGNED")

    if str(m15.get("break_direction") or "NONE") == opposite:
        return base._reject(action, "M15_BREAK_VETO")

    window_start = max(1, len(m5_bars) - breakout_lookback_bars)
    breakout_bar: FlowBar | None = None
    breakout: Pivot | None = None
    opposite_pivot: Pivot | None = None
    breakout_history: Sequence[FlowBar] | None = None

    # Evaluate each recent bar against the latest pivot that was already
    # confirmed before that bar closed. Keep the most recent valid crossing.
    for index in range(window_start, len(m5_bars)):
        confirmed_history = m5_bars[:index]
        highs, lows = base._pivots(confirmed_history)
        if not highs or not lows:
            continue
        candidate_breakout = highs[-1] if action == "BUY" else lows[-1]
        candidate_opposite = lows[-1] if action == "BUY" else highs[-1]
        previous = m5_bars[index - 1]
        current = m5_bars[index]
        crossed = (
            previous.close <= candidate_breakout.price < current.close
            if action == "BUY"
            else previous.close >= candidate_breakout.price > current.close
        )
        if crossed:
            breakout_bar = current
            breakout = candidate_breakout
            opposite_pivot = candidate_opposite
            breakout_history = confirmed_history

    if breakout_bar is None or breakout is None or opposite_pivot is None or breakout_history is None:
        return base._reject(action, "M5_CLOSE_DID_NOT_BREAK_LAST_EXTREMUM")

    latest = m5_bars[-1]
    breakout_held = latest.close > breakout.price if action == "BUY" else latest.close < breakout.price
    if not breakout_held:
        return base._reject(action, "M5_BREAKOUT_NOT_HELD")

    prior_volumes = [bar.volume for bar in breakout_history[-20:] if bar.volume > 0]
    if not prior_volumes:
        return base._reject(action, "M5_VOLUME_BASELINE_MISSING")
    median_volume = statistics.median(prior_volumes)
    volume_ratio = breakout_bar.volume / median_volume if median_volume > 0 else 0.0
    if volume_ratio < minimum_volume_ratio:
        return base._reject(action, "M5_VOLUME_BELOW_THRESHOLD")
    if action == "BUY" and breakout_bar.delta_turnover <= 0:
        return base._reject(action, "M5_DELTA_NOT_BULLISH")
    if action == "SELL" and breakout_bar.delta_turnover >= 0:
        return base._reject(action, "M5_DELTA_NOT_BEARISH")

    entry = latest.close
    stop = opposite_pivot.price
    target = base._number(
        h1.get("last_swing_high") if action == "BUY" else h1.get("last_swing_low")
    )
    valid_geometry = stop < entry < target if action == "BUY" else target < entry < stop
    if not valid_geometry:
        return base._reject(action, "H1_TARGET_OR_M5_STOP_GEOMETRY_INVALID")

    risk = abs(entry - stop)
    reward = abs(target - entry)
    target_rr = reward / risk if risk > 0 else 0.0
    if target_rr < minimum_target_rr:
        return base._reject(action, "H1_TARGET_BELOW_MINIMUM_RR")

    atr_h1 = base._number(volatility.get("atr_h1"))
    target_atr_h1 = reward / atr_h1 if atr_h1 > 0 else 0.0
    if target_atr_h1 < minimum_target_atr_h1:
        return base._reject(action, "H1_TARGET_DISTANCE_TOO_SMALL")

    return Opportunity(
        eligible=True,
        action=action,
        reasons=(
            "H1_DIRECTION_ALIGNED",
            "M15_NO_OPPOSITE_BREAK",
            "M5_RECENT_EXTREMUM_CLOSE_BREAK",
            "M5_BREAKOUT_HELD",
            "M5_VOLUME_CONFIRMED",
            "M5_DELTA_CONFIRMED",
            "H1_TARGET_SPACE_CONFIRMED",
        ),
        entry=entry,
        stop=stop,
        target=target,
        breakout_level=breakout.price,
        breakout_pivot_ms=breakout.start_ms,
        volume_ratio=volume_ratio,
        current_volume=breakout_bar.volume,
        median_volume_20=median_volume,
        delta_turnover=breakout_bar.delta_turnover,
        target_rr=target_rr,
        target_atr_h1=target_atr_h1,
    )


@contextmanager
def _v133_base_version() -> Iterator[None]:
    previous_version = base.VERSION
    try:
        base.VERSION = VERSION
        yield
    finally:
        base.VERSION = previous_version


def build_candidate(
    row: Mapping[str, Any],
    snapshot: Mapping[str, Any],
    opportunity: Opportunity,
):
    with _v133_base_version():
        return base.build_candidate(row, snapshot, opportunity)


def evaluate_row(
    row: Mapping[str, Any],
    engine: MarketStructureEngine,
    flow: FlowHistory,
):
    source_id = base.source._text(row, "decision_id")
    action = base.source._text(row, "action").upper()
    symbol = base.source._text(row, "symbol").upper()
    as_of = base._decision_as_of(row)
    snapshot = engine.snapshot(symbol, as_of, action)
    m5_bars = flow.closed(symbol, int(as_of.timestamp() * 1000))
    opportunity = evaluate_opportunity(action, m5_bars, snapshot)
    audit = {
        "schema_version": VERSION,
        "source_decision_id": source_id,
        "symbol": symbol,
        "action": action,
        "as_of": as_of.isoformat(),
        "opportunity": opportunity.as_dict(),
        "snapshot_state": snapshot.get("state"),
        "bar_counts": snapshot.get("bar_counts"),
        "safety": safety_contract(),
    }
    if not opportunity.eligible:
        rejection = {
            "schema_version": VERSION,
            "source_decision_id": source_id,
            "symbol": symbol,
            "action": action,
            "as_of": as_of.isoformat(),
            "reasons": list(opportunity.reasons),
        }
        return None, audit, rejection
    return build_candidate(row, snapshot, opportunity), audit, {}


def safety_contract() -> Mapping[str, Any]:
    return {
        "read_only": True,
        "orders_enabled": False,
        "publication_enabled": False,
        "exchange_api_called": False,
        "future_bars_used": False,
        "account_sizing_calculated": False,
    }
