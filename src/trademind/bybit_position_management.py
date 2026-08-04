"""Read-only Bybit position-management experiments.

The module applies six exit-management policies to two existing STRICT_SELL
risk plans, BASE_STRICT and WIDE20_R15. It supports an equal-start forward
experiment and a completely isolated historical backfill. Existing source
journals are only read. No order API is imported or called.
"""

from __future__ import annotations

import argparse
import html
import json
import statistics
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from trademind.bybit_risk_plan_experiments import (
    DEFAULT_FEE_BPS_PER_SIDE,
    DEFAULT_SLIPPAGE_BPS_PER_SIDE,
    _profit_factor,
    apply_risk_plan,
)
from trademind.bybit_shadow import (
    _atomic_csv,
    _atomic_json,
    _atomic_text,
    _float,
    _int,
    _iso_from_ms,
    _load_csv,
    _normalized_bars,
)

SCHEMA_VERSION = "1.14.0"
SOURCE_ID = "BYBIT_STRICT_SELL_POSITION_MANAGEMENT"
FORWARD_MODE = "FORWARD"
BACKFILL_MODE = "BACKFILL"
PLAN_ARMS = ("BASE_STRICT", "WIDE20_R15")
MANAGEMENTS = (
    "FULL_TP",
    "PART50_BE",
    "PART50_RUNNER",
    "BE_TRAIL",
    "PART_TRAIL",
    "THREE_STAGE",
)
ARMS = tuple(f"{plan}__{management}" for plan in PLAN_ARMS for management in MANAGEMENTS)
TRAIL_START_R = 1.25
TRAIL_ATR_MULTIPLE = 1.0
EPSILON = 1e-9

SIGNAL_FIELDS = (
    "schema_version",
    "management_signal_id",
    "source_decision_id",
    "updated_at",
    "signal_time",
    "start_ms",
    "end_ms",
    "source",
    "source_id",
    "symbol",
    "action",
    "plan_arm",
    "management",
    "arm",
    "quality_score",
    "entry_price",
    "initial_stop_price",
    "risk_distance",
    "atr_price",
    "horizon_bars",
    "position_size_factor",
    "estimated_cost_r",
    "be_lock_r",
    "trail_atr_multiple",
    "remaining_fraction",
    "realized_r",
    "gross_result_r",
    "net_result_r",
    "outcome",
    "completion_reason",
    "partial_1_hit",
    "partial_2_hit",
    "be_armed",
    "be_exit",
    "trail_armed",
    "trail_exit",
    "full_target_exit",
    "timeout_exit",
    "exit_count",
    "mfe_r",
    "mae_r",
    "completed",
    "event_log",
    "orders_enabled",
)

SUMMARY_FIELDS = (
    "schema_version",
    "captured_at",
    "mode",
    "arm",
    "signals",
    "completed",
    "open",
    "wins",
    "losses",
    "flat",
    "timeouts",
    "partial_1_hits",
    "partial_2_hits",
    "be_exits",
    "trail_exits",
    "full_target_exits",
    "gross_total_r",
    "gross_average_r",
    "gross_profit_factor",
    "estimated_cost_r",
    "net_total_r",
    "net_average_r",
    "net_profit_factor",
    "net_max_drawdown_r",
    "orders_enabled",
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _load_or_create_forward_start(path: Path, now: datetime) -> int:
    if path.is_file():
        payload = json.loads(path.read_text(encoding="utf-8"))
        started = _int(payload.get("started_at_ms"))
        if started <= 0 or not bool(payload.get("forward_only")):
            raise ValueError(f"Invalid position-management forward metadata: {path}")
        if bool(payload.get("orders_enabled")):
            raise ValueError("Forward position-management metadata unexpectedly enables orders")
        return started
    started = int(now.timestamp() * 1000)
    _atomic_json(
        path,
        {
            "schema_version": SCHEMA_VERSION,
            "started_at": now.isoformat(),
            "started_at_ms": started,
            "forward_only": True,
            "equal_start": True,
            "orders_enabled": False,
        },
    )
    return started


def _load_forward_cutoff(path: Path) -> int:
    if not path.is_file():
        raise ValueError(f"Forward position-management metadata not found: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    cutoff = _int(payload.get("started_at_ms"))
    if cutoff <= 0 or not bool(payload.get("forward_only")):
        raise ValueError(f"Invalid forward position-management cutoff: {path}")
    if bool(payload.get("orders_enabled")):
        raise ValueError("Forward position-management metadata unexpectedly enables orders")
    return cutoff


def _atr_price(rows: Sequence[dict[str, Any]], start_ms: int, lookback: int = 14) -> float:
    history = [row for row in rows if _int(row.get("start_ms")) <= start_ms]
    sample = history[-lookback:]
    if not sample:
        return 0.0
    values: list[float] = []
    previous_close = 0.0
    for row in sample:
        high = _float(row.get("high"))
        low = _float(row.get("low"))
        true_range = max(0.0, high - low)
        if previous_close:
            true_range = max(
                true_range,
                abs(high - previous_close),
                abs(low - previous_close),
            )
        values.append(true_range)
        previous_close = _float(row.get("close"))
    return statistics.fmean(values) if values else 0.0


def _max_drawdown(values: Sequence[float]) -> float:
    equity = peak = drawdown = 0.0
    for value in values:
        equity += value
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    return drawdown


def _close_fraction(
    *,
    fraction: float,
    exit_r: float,
    remaining: float,
    realized: float,
    events: list[str],
    label: str,
) -> tuple[float, float, int]:
    quantity = min(max(fraction, 0.0), remaining)
    if quantity <= EPSILON:
        return remaining, realized, 0
    realized += quantity * exit_r
    remaining -= quantity
    events.append(f"{label}:{quantity:.6f}@{exit_r:.6f}R")
    return max(0.0, remaining), realized, 1


def simulate_management(
    plan: dict[str, Any],
    management: str,
    bars: Sequence[dict[str, Any]],
    captured_at: datetime,
) -> dict[str, Any]:
    """Replay one SELL risk plan with a deterministic conservative M5 policy."""
    if management not in MANAGEMENTS:
        raise ValueError(f"Unknown management policy: {management}")
    if str(plan.get("action")) != "SELL":
        raise ValueError("Position-management source must be SELL")

    entry = _float(plan.get("entry_price"))
    initial_stop = _float(plan.get("stop_price"))
    risk = initial_stop - entry
    if entry <= 0 or risk <= 0:
        raise ValueError("Position-management source has invalid entry/stop")

    start_ms = _int(plan.get("start_ms"))
    horizon = max(1, _int(plan.get("horizon_bars"), 12))
    symbol = str(plan.get("symbol") or "")
    symbol_bars = [
        row
        for row in bars
        if str(row.get("symbol") or "") == symbol
        and _int(row.get("start_ms")) > start_ms
    ]
    sample = symbol_bars[:horizon]
    atr = _atr_price(bars, start_ms)
    if atr <= 0:
        atr = risk
    cost_r = max(0.0, _float(plan.get("estimated_cost_r")))
    be_lock_r = min(cost_r, 0.95)
    be_stop = entry - be_lock_r * risk

    remaining = 1.0
    realized = 0.0
    current_stop = initial_stop
    stop_mode = "ORIGINAL"
    events: list[str] = []
    exit_count = 0
    partial_1_hit = False
    partial_2_hit = False
    be_armed = False
    be_exit = False
    trail_armed = False
    trail_exit = False
    full_target_exit = False
    timeout_exit = False
    completed = False
    completion_reason = ""
    mfe_r = 0.0
    mae_r = 0.0

    for row in sample:
        high = _float(row.get("high"))
        low = _float(row.get("low"))
        favorable_r = (entry - low) / risk
        adverse_r = (high - entry) / risk
        mfe_r = max(mfe_r, favorable_r)
        mae_r = max(mae_r, adverse_r)

        if high >= current_stop - EPSILON:
            exit_r = (entry - current_stop) / risk
            remaining, realized, added = _close_fraction(
                fraction=remaining,
                exit_r=exit_r,
                remaining=remaining,
                realized=realized,
                events=events,
                label=f"{stop_mode}_STOP",
            )
            exit_count += added
            be_exit = stop_mode == "BE"
            trail_exit = stop_mode == "TRAIL"
            completed = True
            completion_reason = f"{stop_mode}_STOP"
            break

        if management == "FULL_TP":
            target_r = max(0.1, _float(plan.get("rr"), 1.5))
            if favorable_r + EPSILON >= target_r:
                remaining, realized, added = _close_fraction(
                    fraction=remaining,
                    exit_r=target_r,
                    remaining=remaining,
                    realized=realized,
                    events=events,
                    label="FULL_TARGET",
                )
                exit_count += added
                full_target_exit = True
                completed = True
                completion_reason = "FULL_TARGET"
                break

        elif management in {"PART50_BE", "PART50_RUNNER"}:
            if not partial_1_hit and favorable_r + EPSILON >= 1.0:
                remaining, realized, added = _close_fraction(
                    fraction=0.5,
                    exit_r=1.0,
                    remaining=remaining,
                    realized=realized,
                    events=events,
                    label="PARTIAL_1",
                )
                exit_count += added
                partial_1_hit = True
                be_armed = True
                current_stop = min(current_stop, be_stop)
                stop_mode = "BE"
            final_target_r = 1.5 if management == "PART50_BE" else 2.0
            if partial_1_hit and favorable_r + EPSILON >= final_target_r:
                remaining, realized, added = _close_fraction(
                    fraction=remaining,
                    exit_r=final_target_r,
                    remaining=remaining,
                    realized=realized,
                    events=events,
                    label="RUNNER_TARGET",
                )
                exit_count += added
                full_target_exit = True
                completed = True
                completion_reason = "RUNNER_TARGET"
                break

        elif management == "BE_TRAIL":
            if not be_armed and favorable_r + EPSILON >= 1.0:
                be_armed = True
                current_stop = min(current_stop, be_stop)
                stop_mode = "BE"

        elif management == "PART_TRAIL":
            if not partial_1_hit and favorable_r + EPSILON >= 1.0:
                remaining, realized, added = _close_fraction(
                    fraction=0.5,
                    exit_r=1.0,
                    remaining=remaining,
                    realized=realized,
                    events=events,
                    label="PARTIAL_1",
                )
                exit_count += added
                partial_1_hit = True
                be_armed = True
                current_stop = min(current_stop, be_stop)
                stop_mode = "BE"

        elif management == "THREE_STAGE":
            if not partial_1_hit and favorable_r + EPSILON >= 1.0:
                remaining, realized, added = _close_fraction(
                    fraction=1.0 / 3.0,
                    exit_r=1.0,
                    remaining=remaining,
                    realized=realized,
                    events=events,
                    label="PARTIAL_1",
                )
                exit_count += added
                partial_1_hit = True
                be_armed = True
                current_stop = min(current_stop, be_stop)
                stop_mode = "BE"
            if not partial_2_hit and favorable_r + EPSILON >= 1.5:
                remaining, realized, added = _close_fraction(
                    fraction=1.0 / 3.0,
                    exit_r=1.5,
                    remaining=remaining,
                    realized=realized,
                    events=events,
                    label="PARTIAL_2",
                )
                exit_count += added
                partial_2_hit = True

        trail_ready = (
            management in {"BE_TRAIL", "PART_TRAIL"}
            and favorable_r + EPSILON >= TRAIL_START_R
        ) or (
            management == "THREE_STAGE"
            and partial_2_hit
            and favorable_r + EPSILON >= 1.5
        )
        if trail_ready and remaining > EPSILON:
            trail_armed = True
            candidate_stop = low + TRAIL_ATR_MULTIPLE * atr
            if candidate_stop < current_stop:
                current_stop = candidate_stop
                stop_mode = "TRAIL"

        if remaining <= EPSILON:
            completed = True
            completion_reason = completion_reason or "TARGETS_COMPLETE"
            break

        if stop_mode != "ORIGINAL" and high >= current_stop - EPSILON:
            exit_r = (entry - current_stop) / risk
            remaining, realized, added = _close_fraction(
                fraction=remaining,
                exit_r=exit_r,
                remaining=remaining,
                realized=realized,
                events=events,
                label=f"SAME_BAR_{stop_mode}_STOP",
            )
            exit_count += added
            be_exit = stop_mode == "BE"
            trail_exit = stop_mode == "TRAIL"
            completed = True
            completion_reason = f"SAME_BAR_{stop_mode}_STOP_CONSERVATIVE"
            break

    if not completed and len(symbol_bars) >= horizon and sample:
        last_close = _float(sample[-1].get("close"))
        timeout_r = (entry - last_close) / risk
        remaining, realized, added = _close_fraction(
            fraction=remaining,
            exit_r=timeout_r,
            remaining=remaining,
            realized=realized,
            events=events,
            label="TIMEOUT",
        )
        exit_count += added
        timeout_exit = True
        completed = True
        completion_reason = "HORIZON"

    gross: float | str = round(realized, 6) if completed else ""
    net: float | str = round(realized - cost_r, 6) if completed else ""
    if completed:
        outcome = "WIN" if realized > EPSILON else "LOSS" if realized < -EPSILON else "FLAT"
    else:
        outcome = "OPEN"

    arm = f"{plan.get('plan_arm')}__{management}"
    source_id = str(plan.get("decision_id") or "")
    return {
        "schema_version": SCHEMA_VERSION,
        "management_signal_id": f"{source_id}:MANAGEMENT:{management}",
        "source_decision_id": source_id,
        "updated_at": captured_at.isoformat(),
        "signal_time": str(plan.get("signal_time") or ""),
        "start_ms": start_ms,
        "end_ms": _int(plan.get("end_ms")),
        "source": str(plan.get("source") or "BYBIT"),
        "source_id": SOURCE_ID,
        "symbol": symbol,
        "action": "SELL",
        "plan_arm": str(plan.get("plan_arm") or ""),
        "management": management,
        "arm": arm,
        "quality_score": _int(plan.get("quality_score")),
        "entry_price": entry,
        "initial_stop_price": initial_stop,
        "risk_distance": risk,
        "atr_price": atr,
        "horizon_bars": horizon,
        "position_size_factor": _float(plan.get("position_size_factor"), 1.0),
        "estimated_cost_r": cost_r,
        "be_lock_r": be_lock_r,
        "trail_atr_multiple": TRAIL_ATR_MULTIPLE,
        "remaining_fraction": round(remaining, 6),
        "realized_r": round(realized, 6),
        "gross_result_r": gross,
        "net_result_r": net,
        "outcome": outcome,
        "completion_reason": completion_reason,
        "partial_1_hit": int(partial_1_hit),
        "partial_2_hit": int(partial_2_hit),
        "be_armed": int(be_armed),
        "be_exit": int(be_exit),
        "trail_armed": int(trail_armed),
        "trail_exit": int(trail_exit),
        "full_target_exit": int(full_target_exit),
        "timeout_exit": int(timeout_exit),
        "exit_count": exit_count,
        "mfe_r": round(mfe_r, 6),
        "mae_r": round(mae_r, 6),
        "completed": int(completed),
        "event_log": " | ".join(events),
        "orders_enabled": 0,
    }


def _metrics(signals: Sequence[dict[str, Any]]) -> dict[str, Any]:
    completed = [row for row in signals if _int(row.get("completed")) == 1]
    gross = [_float(row.get("gross_result_r")) for row in completed]
    net = [_float(row.get("net_result_r")) for row in completed]
    costs = [_float(row.get("estimated_cost_r")) for row in completed]
    return {
        "signals": len(signals),
        "completed": len(completed),
        "open": len(signals) - len(completed),
        "wins": sum(value > EPSILON for value in gross),
        "losses": sum(value < -EPSILON for value in gross),
        "flat": sum(abs(value) <= EPSILON for value in gross),
        "timeouts": sum(_int(row.get("timeout_exit")) == 1 for row in completed),
        "partial_1_hits": sum(_int(row.get("partial_1_hit")) == 1 for row in completed),
        "partial_2_hits": sum(_int(row.get("partial_2_hit")) == 1 for row in completed),
        "be_exits": sum(_int(row.get("be_exit")) == 1 for row in completed),
        "trail_exits": sum(_int(row.get("trail_exit")) == 1 for row in completed),
        "full_target_exits": sum(
            _int(row.get("full_target_exit")) == 1 for row in completed
        ),
        "gross_total_r": sum(gross),
        "gross_average_r": statistics.fmean(gross) if gross else 0.0,
        "gross_profit_factor": _profit_factor(gross),
        "estimated_cost_r": sum(costs),
        "net_total_r": sum(net),
        "net_average_r": statistics.fmean(net) if net else 0.0,
        "net_profit_factor": _profit_factor(net),
        "net_max_drawdown_r": _max_drawdown(net),
        "orders_enabled": False,
    }


def _render_dashboard(status: dict[str, Any]) -> str:
    rows: list[str] = []
    cards: list[str] = []
    for arm in ARMS:
        item = status["arms"][arm]
        cards.append(
            "<article>"
            f"<h2>{html.escape(arm)}</h2>"
            f"<b>{item['net_total_r']:.3f}R</b><span> net estimate</span>"
            f"<p>N {item['completed']} | Gross {item['gross_total_r']:.3f}R</p>"
            f"<p>Part1 {item['partial_1_hits']} | BE {item['be_exits']} | Trail {item['trail_exits']}</p>"
            f"<p>Net PF {item['net_profit_factor']:.3f} | DD {item['net_max_drawdown_r']:.3f}R</p>"
            "</article>"
        )
        rows.append(
            "<tr>"
            f"<td>{html.escape(arm)}</td><td>{item['signals']}</td><td>{item['completed']}</td>"
            f"<td>{item['wins']}</td><td>{item['losses']}</td><td>{item['timeouts']}</td>"
            f"<td>{item['partial_1_hits']}</td><td>{item['be_exits']}</td><td>{item['trail_exits']}</td>"
            f"<td>{item['gross_total_r']:.4f}</td><td>{item['estimated_cost_r']:.4f}</td>"
            f"<td>{item['net_total_r']:.4f}</td><td>{item['net_average_r']:.4f}</td>"
            f"<td>{item['net_profit_factor']:.3f}</td><td>{item['net_max_drawdown_r']:.3f}</td>"
            "</tr>"
        )
    mode = html.escape(str(status["mode"]))
    return f"""<!doctype html><html lang='ru'><head><meta charset='utf-8'>
<meta name='viewport' content='width=device-width,initial-scale=1'>
<title>TradeMind v1.14 Position Management</title><style>
body{{background:#061724;color:#eaf7ff;font-family:Arial;margin:28px}}h1{{font-size:40px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(290px,1fr));gap:14px}}
article{{background:#0b2a40;border:1px solid #1c5878;border-radius:16px;padding:18px}}
article b{{font-size:32px}}article span{{color:#9bc4da}}table{{width:100%;border-collapse:collapse;margin-top:24px}}
th,td{{padding:8px;border-bottom:1px solid #17425b;text-align:left}}.ok{{color:#29e2a5}}.note{{color:#ffd479}}
</style></head><body><h1>Bybit Position Management v1.14, {mode}</h1>
<p class='ok'>Read-only. Orders OFF. Source journals untouched.</p>
<p class='note'>M5 ambiguity is conservative: existing stop first, then favorable events, then newly armed BE or trail may exit in the same bar.</p>
<p>Sources: BASE_STRICT and WIDE20_R15. Policies: full TP, partials, BE+Costs and ATR trail.</p>
<section class='grid'>{''.join(cards)}</section>
<table><thead><tr><th>Arm</th><th>Signals</th><th>N</th><th>Wins</th><th>Losses</th><th>Timeouts</th>
<th>Part1</th><th>BE exits</th><th>Trail exits</th><th>Gross R</th><th>Cost R</th><th>Net R</th>
<th>Net Avg</th><th>Net PF</th><th>Net DD</th></tr></thead><tbody>{''.join(rows)}</tbody></table>
</body></html>"""


@dataclass(frozen=True, slots=True)
class PositionManagementSummary:
    mode: str
    cutoff_ms: int
    source_candidates: int
    output_dir: Path
    arms: dict[str, dict[str, Any]]


def run_position_management(
    bars_path: Path,
    strict_decisions_path: Path,
    output_dir: Path,
    *,
    mode: str = FORWARD_MODE,
    forward_meta_path: Path | None = None,
    fee_bps_per_side: float = DEFAULT_FEE_BPS_PER_SIDE,
    slippage_bps_per_side: float = DEFAULT_SLIPPAGE_BPS_PER_SIDE,
    now: datetime | None = None,
) -> PositionManagementSummary:
    captured_at = now or _now()
    if mode not in {FORWARD_MODE, BACKFILL_MODE}:
        raise ValueError(f"Unknown position-management mode: {mode}")
    output_dir.mkdir(parents=True, exist_ok=True)
    if mode == FORWARD_MODE:
        meta_path = output_dir / "experiment_meta.json"
        cutoff_ms = _load_or_create_forward_start(meta_path, captured_at)
    else:
        if forward_meta_path is None:
            raise ValueError("Backfill requires the forward experiment metadata path")
        cutoff_ms = _load_forward_cutoff(forward_meta_path)

    bars = _normalized_bars(_load_csv(bars_path))
    if not bars:
        raise ValueError(f"No Bybit M5 bars found: {bars_path}")
    source_decisions = [
        row
        for row in _load_csv(strict_decisions_path)
        if _int(row.get("eligible")) == 1 and str(row.get("action")) == "SELL"
    ]
    if mode == FORWARD_MODE:
        source_decisions = [
            row for row in source_decisions if _int(row.get("start_ms")) >= cutoff_ms
        ]
    else:
        source_decisions = [
            row for row in source_decisions if _int(row.get("start_ms")) < cutoff_ms
        ]
    source_decisions = sorted(
        source_decisions,
        key=lambda row: (_int(row.get("start_ms")), str(row.get("decision_id"))),
    )

    by_symbol: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for bar in bars:
        by_symbol[str(bar.get("symbol") or "")].append(bar)

    arm_signals: dict[str, list[dict[str, Any]]] = {arm: [] for arm in ARMS}
    for source in source_decisions:
        symbol_bars = by_symbol.get(str(source.get("symbol") or ""), [])
        for plan_arm in PLAN_ARMS:
            plan = apply_risk_plan(
                source,
                plan_arm,
                symbol_bars,
                fee_bps_per_side=fee_bps_per_side,
                slippage_bps_per_side=slippage_bps_per_side,
            )
            for management in MANAGEMENTS:
                signal = simulate_management(plan, management, symbol_bars, captured_at)
                arm_signals[f"{plan_arm}__{management}"].append(signal)

    arm_status: dict[str, dict[str, Any]] = {}
    summary_rows: list[dict[str, Any]] = []
    historical_only = mode == BACKFILL_MODE
    for arm in ARMS:
        signals = sorted(
            arm_signals[arm],
            key=lambda row: (_int(row.get("start_ms")), str(row.get("management_signal_id"))),
        )
        arm_dir = output_dir / arm.lower()
        _atomic_csv(arm_dir / "signals.csv", SIGNAL_FIELDS, signals)
        metrics = _metrics(signals)
        arm_status[arm] = metrics
        arm_payload = {
            "schema_version": SCHEMA_VERSION,
            "state": "OK",
            "updated_at": captured_at.isoformat(),
            "mode": mode,
            "historical_only": historical_only,
            "forward_only": not historical_only,
            "arm": arm,
            "cutoff_ms": cutoff_ms,
            "cutoff": _iso_from_ms(cutoff_ms),
            **metrics,
        }
        _atomic_json(arm_dir / "status.json", arm_payload)
        summary_rows.append(
            {
                "schema_version": SCHEMA_VERSION,
                "captured_at": captured_at.isoformat(),
                "mode": mode,
                "arm": arm,
                **metrics,
            }
        )

    dashboard = output_dir / "dashboard" / "index.html"
    status = {
        "schema_version": SCHEMA_VERSION,
        "state": "OK",
        "updated_at": captured_at.isoformat(),
        "mode": mode,
        "historical_only": historical_only,
        "forward_only": not historical_only,
        "equal_start": True,
        "cutoff_ms": cutoff_ms,
        "cutoff": _iso_from_ms(cutoff_ms),
        "source_candidates": len(source_decisions),
        "source_decisions_path": str(strict_decisions_path),
        "source_plan_arms": list(PLAN_ARMS),
        "management_policies": list(MANAGEMENTS),
        "orders_enabled": False,
        "logic_changed": False,
        "source_journals_modified": False,
        "same_bar_rule": "STOP_FIRST_THEN_SAME_BAR_NEW_STOP_CONSERVATIVE",
        "cost_model": {
            "fee_bps_per_side": fee_bps_per_side,
            "slippage_bps_per_side": slippage_bps_per_side,
            "observed_entry_spread_included": True,
            "partial_fills_linear_notional_cost": True,
        },
        "arms": arm_status,
        "bars_path": str(bars_path),
        "output_dir": str(output_dir),
        "dashboard": str(dashboard),
    }
    _atomic_csv(output_dir / "comparison.csv", SUMMARY_FIELDS, summary_rows)
    _atomic_json(output_dir / "status.json", status)
    _atomic_text(dashboard, _render_dashboard(status))
    return PositionManagementSummary(
        mode=mode,
        cutoff_ms=cutoff_ms,
        source_candidates=len(source_decisions),
        output_dir=output_dir,
        arms=arm_status,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="TradeMind v1.14 Bybit position-management experiments"
    )
    parser.add_argument("--bars", type=Path, default=Path("data/bybit_v1_9/bybit_bars.csv"))
    parser.add_argument(
        "--strict-decisions",
        type=Path,
        default=Path("data/bybit_shadow_v1_11/strict_sell/decisions.csv"),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("data/bybit_position_management_v1_14"),
    )
    parser.add_argument(
        "--mode",
        choices=(FORWARD_MODE, BACKFILL_MODE),
        default=FORWARD_MODE,
    )
    parser.add_argument(
        "--forward-meta",
        type=Path,
        default=Path("data/bybit_position_management_v1_14/experiment_meta.json"),
    )
    parser.add_argument("--fee-bps-per-side", type=float, default=DEFAULT_FEE_BPS_PER_SIDE)
    parser.add_argument(
        "--slippage-bps-per-side",
        type=float,
        default=DEFAULT_SLIPPAGE_BPS_PER_SIDE,
    )
    parser.add_argument("--open-dashboard", action="store_true")
    args = parser.parse_args(argv)
    try:
        summary = run_position_management(
            args.bars.expanduser().resolve(),
            args.strict_decisions.expanduser().resolve(),
            args.output_dir.expanduser().resolve(),
            mode=args.mode,
            forward_meta_path=(
                args.forward_meta.expanduser().resolve()
                if args.mode == BACKFILL_MODE
                else None
            ),
            fee_bps_per_side=args.fee_bps_per_side,
            slippage_bps_per_side=args.slippage_bps_per_side,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"Position-management experiment failed: {exc}")
        return 1

    print(f"TradeMind v1.14 Position Management, {summary.mode}")
    print("BASE_STRICT and WIDE20_R15. Partials, BE+Costs and ATR trail.")
    print("Read-only. Source journals untouched. No orders.")
    print(f"Cutoff: {_iso_from_ms(summary.cutoff_ms)}")
    print(f"Source candidates: {summary.source_candidates}")
    for arm in ARMS:
        item = summary.arms[arm]
        print(
            f"{arm}: signals={item['signals']} completed={item['completed']} "
            f"gross_r={item['gross_total_r']:.4f} cost_r={item['estimated_cost_r']:.4f} "
            f"net_r={item['net_total_r']:.4f} net_pf={item['net_profit_factor']:.3f}"
        )
    print(f"Output: {summary.output_dir}")
    if args.open_dashboard:
        import os

        os.startfile(summary.output_dir / "dashboard" / "index.html")  # type: ignore[attr-defined]
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
