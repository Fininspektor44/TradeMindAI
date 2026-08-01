"""Deterministic SMC impulse, Fibonacci OTE and shadow-outcome engine."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Sequence

from trademind.market.models import Candle
from trademind.ote_models import (
    ActiveSetup,
    FIB_LEVELS,
    HORIZON_BARS,
    SIGNAL_FIELDS,
    aligned,
    atr_series,
    candle_from_row,
    confirmed_pivots,
    fib_prices,
    higher_timeframe_biases,
    latest_confirmed,
    number,
    ratio_previous,
    session_for_time,
    swept_anchor,
    value_float,
    value_int,
)
from trademind.structure import MarketStructureEngine
from trademind.structure.models import FvgDirection, StructureBreak


def break_label(values: Sequence[StructureBreak], action: str) -> str:
    prefix = "BULLISH" if action == "BUY" else "BEARISH"
    return next((value.value for value in values if value.value.startswith(prefix)), f"{prefix}_PIVOT_BREAK")


def confirmation(candles: Sequence[Candle], index: int, action: str, atr: float) -> bool:
    if index <= 0:
        return False
    current = candles[index]
    previous = candles[index - 1]
    if abs(current.close - current.open) < atr * 0.20:
        return False
    if action == "BUY":
        return current.close > current.open and current.close > previous.high
    return current.close < current.open and current.close < previous.low


def score_signal(
    *,
    action: str,
    variant: str,
    h1_bias: str,
    h4_bias: str,
    sweep: bool,
    setup_break: str,
    impulse_atr: float,
    fvg_aligned: bool,
    session: str,
    rvol: float,
    tick_ratio: float,
    imbalance: float,
    spread_ratio: float,
    confirmed: bool,
) -> tuple[int, list[str]]:
    score = 10
    reasons = ["valid SMC impulse and OTE geometry"]
    for points, condition, text in (
        (10, aligned(action, h1_bias), "H1 aligned"),
        (15, aligned(action, h4_bias), "H4 aligned"),
        (15, sweep, "liquidity sweep at impulse anchor"),
        (10, "CHOCH" in setup_break or "BOS" in setup_break, setup_break),
        (10, impulse_atr >= 1.5, "impulse >= 1.5 ATR"),
        (5, impulse_atr >= 2.5, "impulse >= 2.5 ATR"),
        (5, fvg_aligned, "FVG aligned"),
        (5, rvol >= 1.2, "RVOL elevated"),
        (5, tick_ratio >= 1.1, "tick acceleration"),
        (5, session in {"LONDON", "LONDON_NY_OVERLAP", "NEW_YORK"}, "active session"),
        (10, confirmed, "body-close confirmation"),
        (5, variant in {"TOUCH_705", "TOUCH_790"}, "deep OTE entry"),
    ):
        if condition:
            score += points
            reasons.append(text)
    pressure = (action == "BUY" and imbalance >= 0.05) or (action == "SELL" and imbalance <= -0.05)
    if pressure:
        score += 5
        reasons.append("quote pressure aligned")
    if 0 < spread_ratio <= 1.10:
        score += 5
        reasons.append("spread normal")
    elif spread_ratio >= 1.5:
        score -= 5
        reasons.append("spread expanded")
    return max(0, min(100, score)), reasons


def score_bucket(score: int) -> str:
    if score >= 80:
        return "80_PLUS"
    if score >= 70:
        return "70_79"
    if score >= 60:
        return "60_69"
    return "BELOW_60"


def _outcome(
    signal: dict[str, str],
    candles: Sequence[Candle],
    index: int,
    horizon_bars: int,
    *,
    touch_entry: bool,
) -> tuple[str, datetime | None, float | None, float | None, float, float]:
    action = signal["action"]
    entry = value_float(signal, "entry_price")
    stop = value_float(signal, "stop_price")
    target = value_float(signal, "target_price")
    risk = value_float(signal, "risk_price")
    if risk <= 0:
        return "INVALID", None, None, None, 0.0, 0.0
    if index + horizon_bars >= len(candles):
        return "PENDING", None, None, None, 0.0, 0.0
    if touch_entry:
        entry_bar = candles[index]
        stopped = entry_bar.low <= stop if action == "BUY" else entry_bar.high >= stop
        if stopped:
            return "LOSS", entry_bar.time, stop, -1.0, 0.0, 1.0

    direction = 1.0 if action == "BUY" else -1.0
    mfe = 0.0
    mae = 0.0
    end_index = index + horizon_bars
    for candle in candles[index + 1:end_index + 1]:
        if action == "BUY":
            favorable = max(0.0, candle.high - entry)
            adverse = max(0.0, entry - candle.low)
            stopped = candle.low <= stop
            targeted = candle.high >= target
        else:
            favorable = max(0.0, entry - candle.low)
            adverse = max(0.0, candle.high - entry)
            stopped = candle.high >= stop
            targeted = candle.low <= target
        mfe = max(mfe, favorable / risk)
        mae = max(mae, adverse / risk)
        if stopped:
            return "LOSS", candle.time, stop, -1.0, mfe, max(mae, 1.0)
        if targeted:
            reward_r = abs(target - entry) / risk
            return "WIN", candle.time, target, reward_r, max(mfe, reward_r), mae

    exit_candle = candles[end_index]
    result_r = direction * (exit_candle.close - entry) / risk
    return "TIMEOUT", exit_candle.time, exit_candle.close, result_r, mfe, mae


def _create_signal(
    setup: ActiveSetup,
    rows: Sequence[dict[str, str]],
    candles: Sequence[Candle],
    index: int,
    variant: str,
    fib_ratio: str,
    raw_entry: float,
    h1_bias: str,
    h4_bias: str,
    confirmed: bool,
) -> dict[str, str] | None:
    source = rows[index]
    candle = candles[index]
    point = value_float(source, "point")
    spread_cost = max(0.0, value_float(source, "spread_mean_points") * point)
    stop_buffer = max(spread_cost, setup.atr * 0.05, point * 2)
    if setup.action == "BUY":
        entry, stop, target = raw_entry + spread_cost, setup.anchor_price - stop_buffer, setup.extreme_price
        risk, reward = entry - stop, target - entry
    else:
        entry, stop, target = raw_entry - spread_cost, setup.anchor_price + stop_buffer, setup.extreme_price
        risk, reward = stop - entry, entry - target
    if risk <= 0 or reward <= 0:
        return None

    levels = fib_prices(setup.action, setup.anchor_price, setup.extreme_price)
    rvol = value_float(source, "rvol_20")
    tick_ratio = ratio_previous(rows, index, "tick_rate_per_sec")
    imbalance = value_float(source, "direction_imbalance")
    spread_ratio = ratio_previous(rows, index, "spread_mean_points")
    session = session_for_time(candle.time)
    impulse_atr = abs(setup.extreme_price - setup.anchor_price) / setup.atr
    score, reasons = score_signal(
        action=setup.action, variant=variant, h1_bias=h1_bias, h4_bias=h4_bias,
        sweep=setup.sweep, setup_break=setup.break_label, impulse_atr=impulse_atr,
        fvg_aligned=setup.fvg_aligned, session=session, rvol=rvol,
        tick_ratio=tick_ratio, imbalance=imbalance, spread_ratio=spread_ratio,
        confirmed=confirmed,
    )
    offset = int(round((datetime.fromtimestamp(value_int(source, "time"), tz=timezone.utc) - candle.time).total_seconds() / 3600))
    signal = {field: "" for field in SIGNAL_FIELDS}
    signal.update({
        "schema_version": "1.5.0",
        "signal_id": f"{setup.symbol}:M5:{int(candles[setup.start_index].time.timestamp())}:{setup.action}:{variant}:{int(candle.time.timestamp())}",
        "signal_time": candle.time.isoformat(), "source_bar_time": str(value_int(source, "time")),
        "server_utc_offset_hours": str(offset), "symbol": setup.symbol, "timeframe": "M5",
        "session": session, "action": setup.action, "variant": variant, "fib_ratio": fib_ratio,
        "score": str(score), "score_bucket": score_bucket(score), "setup_break": setup.break_label,
        "setup_start_time": candles[setup.start_index].time.isoformat(),
        "setup_end_time": candles[setup.end_index].time.isoformat(),
        "setup_age_bars": str(index - setup.end_index), "anchor_price": number(setup.anchor_price),
        "impulse_extreme": number(setup.extreme_price),
        "impulse_range": number(abs(setup.extreme_price - setup.anchor_price)),
        "impulse_atr": number(impulse_atr), "fib_618": number(levels["618"]),
        "fib_705": number(levels["705"]), "fib_790": number(levels["790"]),
        "entry_price": number(entry), "stop_price": number(stop), "target_price": number(target),
        "risk_price": number(risk), "reward_price": number(reward), "rr": number(reward / risk),
        "stop_buffer": number(stop_buffer), "h1_bias": h1_bias, "h4_bias": h4_bias,
        "h1_aligned": "1" if aligned(setup.action, h1_bias) else "0",
        "h4_aligned": "1" if aligned(setup.action, h4_bias) else "0",
        "liquidity_sweep": "1" if setup.sweep else "0",
        "fvg_aligned": "1" if setup.fvg_aligned else "0",
        "confirmation": "1" if confirmed else "0", "rvol_20": number(rvol),
        "tick_rate_ratio_20": number(tick_ratio), "direction_imbalance": number(imbalance),
        "spread_ratio_20": number(spread_ratio), "spread_cost": number(spread_cost),
        "atr": number(setup.atr), "reasons": " | ".join(reasons),
    })
    touch_entry = variant.startswith("TOUCH_") or variant == "ZONE_TOUCH"
    for horizon, bars in HORIZON_BARS:
        outcome, exit_time, exit_price, result_r, mfe_r, mae_r = _outcome(
            signal, candles, index, bars, touch_entry=touch_entry
        )
        key = horizon.lower()
        signal[f"outcome_{key}"] = outcome
        signal[f"exit_time_{key}"] = exit_time.isoformat() if exit_time else ""
        signal[f"exit_price_{key}"] = number(exit_price)
        signal[f"result_r_{key}"] = number(result_r)
        signal[f"mfe_r_{key}"] = number(mfe_r)
        signal[f"mae_r_{key}"] = number(mae_r)
    return signal


def _emit_entries(
    active: ActiveSetup,
    rows: Sequence[dict[str, str]],
    candles: Sequence[Candle],
    index: int,
    h1_bias: str,
    h4_bias: str,
) -> list[dict[str, str]]:
    candle = candles[index]
    levels = fib_prices(active.action, active.anchor_price, active.extreme_price)
    if candle.high < min(levels.values()) or candle.low > max(levels.values()):
        return []
    active.frozen = True
    active.touched_index = active.touched_index if active.touched_index is not None else index
    output: list[dict[str, str]] = []
    for level_name, _ratio in FIB_LEVELS:
        variant = f"TOUCH_{level_name}"
        if variant in active.emitted or not candle.low <= levels[level_name] <= candle.high:
            continue
        active.emitted.add(variant)
        signal = _create_signal(active, rows, candles, index, variant, level_name, levels[level_name], h1_bias, h4_bias, False)
        if signal:
            output.append(signal)
    if "ZONE_TOUCH" not in active.emitted:
        active.emitted.add("ZONE_TOUCH")
        raw_entry = max(min(levels.values()), min(max(levels.values()), candle.close))
        signal = _create_signal(active, rows, candles, index, "ZONE_TOUCH", "618_790", raw_entry, h1_bias, h4_bias, False)
        if signal:
            output.append(signal)
    return output


def build_ote_signals(
    volume_rows: Sequence[dict[str, str]],
    *,
    server_utc_offset_hours: int = 0,
    pivot_window: int = 2,
    minimum_impulse_atr: float = 0.8,
    maximum_setup_age_bars: int = 72,
) -> list[dict[str, str]]:
    by_symbol: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in volume_rows:
        by_symbol[row["symbol"].upper()].append(row)
    output: list[dict[str, str]] = []
    structure_engine = MarketStructureEngine()

    for symbol, unsorted_rows in sorted(by_symbol.items()):
        rows = sorted(unsorted_rows, key=lambda row: value_int(row, "time"))
        candles = [candle_from_row(row, server_utc_offset_hours) for row in rows]
        if len(candles) < 40:
            continue
        atrs = atr_series(candles)
        pivot_highs, pivot_lows = confirmed_pivots(candles, pivot_window)
        h1_biases = higher_timeframe_biases(candles, 60)
        h4_biases = higher_timeframe_biases(candles, 240)
        active: ActiveSetup | None = None

        for index in range(max(30, structure_engine.minimum_candles - 1), len(candles)):
            candle, atr = candles[index], atrs[index]
            if atr <= 0:
                continue
            if active is not None:
                invalid = candle.close < active.anchor_price if active.action == "BUY" else candle.close > active.anchor_price
                if invalid or index - active.end_index > maximum_setup_age_bars:
                    active = None
                else:
                    if not active.frozen and active.action == "BUY" and candle.high > active.extreme_price:
                        active.extreme_price, active.end_index = candle.high, index
                    if not active.frozen and active.action == "SELL" and candle.low < active.extreme_price:
                        active.extreme_price, active.end_index = candle.low, index
                    output.extend(_emit_entries(active, rows, candles, index, h1_biases[index], h4_biases[index]))
                    if (
                        active.touched_index is not None
                        and index <= active.touched_index + 3
                        and "CONFIRMED_ZONE" not in active.emitted
                        and confirmation(candles, index, active.action, atr)
                    ):
                        active.emitted.add("CONFIRMED_ZONE")
                        signal = _create_signal(active, rows, candles, index, "CONFIRMED_ZONE", "CONFIRMED", candle.close, h1_biases[index], h4_biases[index], True)
                        if signal:
                            output.append(signal)

            high_pivot = latest_confirmed(pivot_highs, index, pivot_window)
            low_pivot = latest_confirmed(pivot_lows, index, pivot_window)
            if high_pivot is None or low_pivot is None:
                continue
            previous = candles[index - 1]
            history = candles[max(0, index - 59):index + 1]
            structure = structure_engine.analyze(history, atr=atr)
            if low_pivot.index > high_pivot.index and previous.close <= high_pivot.price < candle.close:
                extreme = max(item.high for item in candles[low_pivot.index:index + 1])
                if (extreme - low_pivot.price) / atr >= minimum_impulse_atr:
                    active = ActiveSetup(
                        symbol, "BUY", break_label((structure.swing_break, structure.internal_break), "BUY"),
                        low_pivot.index, index, low_pivot.price, extreme, atr,
                        swept_anchor(candles, low_pivot, "BUY"), structure.fvg_direction is FvgDirection.BULLISH,
                    )
            elif high_pivot.index > low_pivot.index and previous.close >= low_pivot.price > candle.close:
                extreme = min(item.low for item in candles[high_pivot.index:index + 1])
                if (high_pivot.price - extreme) / atr >= minimum_impulse_atr:
                    active = ActiveSetup(
                        symbol, "SELL", break_label((structure.swing_break, structure.internal_break), "SELL"),
                        high_pivot.index, index, high_pivot.price, extreme, atr,
                        swept_anchor(candles, high_pivot, "SELL"), structure.fvg_direction is FvgDirection.BEARISH,
                    )

    unique = {row["signal_id"]: row for row in output}
    return sorted(unique.values(), key=lambda row: (row["signal_time"], row["symbol"], row["variant"]))
