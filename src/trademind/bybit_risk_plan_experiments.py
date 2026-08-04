"""Forward-only Bybit stop/target plan experiments.

The module mirrors only new eligible STRICT_SELL decisions from v1.11 into
multiple equal-start paper arms. It changes paper risk plans only. Entry
selection, strict signal policy, source journals and trading state remain
untouched. No order API is imported or called.
"""

from __future__ import annotations

import argparse
import html
import json
import math
import statistics
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from trademind.bybit_shadow import (
    BAR_MS,
    DECISION_FIELDS,
    PAPER_FIELDS,
    _atomic_csv,
    _atomic_json,
    _atomic_text,
    _float,
    _int,
    _iso_from_ms,
    _load_csv,
    _normalized_bars,
    _update_outcome,
)

SCHEMA_VERSION = "1.13.0"
SOURCE_ID = "BYBIT_STRICT_SELL_RISK_PLANS"
ARMS = (
    "BASE_STRICT",
    "WIDE15_R15",
    "WIDE15_R20",
    "WIDE20_R15",
    "WIDE20_R20",
    "STRUCTURE_R15",
    "STRUCTURE_LIQ",
)
DEFAULT_FEE_BPS_PER_SIDE = 5.5
DEFAULT_SLIPPAGE_BPS_PER_SIDE = 1.0
MAX_COST_R_FOR_HYBRID = 0.20
STRUCTURE_BUFFER_ATR = 0.20
ATR_LOOKBACK = 14
SWING_LOOKBACK = 12
LIQUIDITY_LOOKBACK = 24

EXTRA_FIELDS = (
    "plan_arm",
    "base_risk_distance",
    "risk_distance",
    "position_size_factor",
    "estimated_round_trip_bps",
    "estimated_cost_r",
)
RISK_DECISION_FIELDS = (*DECISION_FIELDS, *EXTRA_FIELDS)
RISK_PAPER_FIELDS = (*PAPER_FIELDS, *EXTRA_FIELDS)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _load_or_create_start(path: Path, now: datetime) -> int:
    if path.is_file():
        payload = json.loads(path.read_text(encoding="utf-8"))
        return _int(payload.get("started_at_ms"))
    started = int(now.timestamp() * 1000)
    _atomic_json(
        path,
        {
            "schema_version": SCHEMA_VERSION,
            "started_at": now.isoformat(),
            "started_at_ms": started,
            "forward_only": True,
            "orders_enabled": False,
        },
    )
    return started


def _true_range_atr(rows: Sequence[dict[str, Any]], lookback: int = ATR_LOOKBACK) -> float:
    sample = list(rows[-lookback:])
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


def _cost_details(
    entry: float,
    risk_distance: float,
    spread_bps: float,
    *,
    fee_bps_per_side: float,
    slippage_bps_per_side: float,
) -> tuple[float, float]:
    total_bps = (
        2.0 * max(0.0, fee_bps_per_side)
        + 2.0 * max(0.0, slippage_bps_per_side)
        + max(0.0, spread_bps)
    )
    risk_bps = 10_000.0 * risk_distance / entry if entry > 0 else 0.0
    cost_r = total_bps / risk_bps if risk_bps > 0 else 0.0
    return total_bps, cost_r


def _history_before(
    rows: Sequence[dict[str, Any]], start_ms: int
) -> list[dict[str, Any]]:
    return [row for row in rows if _int(row.get("start_ms")) <= start_ms]


def apply_risk_plan(
    source: dict[str, Any],
    arm: str,
    m5_history: Sequence[dict[str, Any]],
    *,
    fee_bps_per_side: float = DEFAULT_FEE_BPS_PER_SIDE,
    slippage_bps_per_side: float = DEFAULT_SLIPPAGE_BPS_PER_SIDE,
) -> dict[str, Any]:
    """Clone one STRICT_SELL decision and replace only its paper SL/TP plan."""
    if arm not in ARMS:
        raise ValueError(f"Unknown risk-plan arm: {arm}")
    if _int(source.get("eligible")) != 1 or str(source.get("action")) != "SELL":
        raise ValueError("Risk-plan source must be an eligible STRICT_SELL decision")

    row = dict(source)
    entry = _float(row.get("entry_price"))
    source_stop = _float(row.get("stop_price"))
    base_risk = source_stop - entry
    if entry <= 0 or base_risk <= 0:
        raise ValueError("STRICT_SELL source has invalid entry/stop")

    start_ms = _int(row.get("start_ms"))
    history = _history_before(m5_history, start_ms)
    atr = _true_range_atr(history)
    spread_bps = max(0.0, _float(row.get("m5_spread_bps")))
    risk = base_risk
    rr = max(0.1, _float(row.get("rr"), 1.5))
    horizon = max(1, _int(row.get("horizon_bars"), 12))
    target_mode = "SOURCE"

    if arm.startswith("WIDE15"):
        risk = max(base_risk, 1.5 * atr)
        rr = 2.0 if arm.endswith("R20") else 1.5
        horizon = 18
        target_mode = f"FIXED_{rr:.1f}R"
    elif arm.startswith("WIDE20"):
        risk = max(base_risk, 2.0 * atr)
        rr = 2.0 if arm.endswith("R20") else 1.5
        horizon = 24
        target_mode = f"FIXED_{rr:.1f}R"
    elif arm.startswith("STRUCTURE"):
        recent_swing = history[-SWING_LOOKBACK:]
        recent_liquidity = history[-LIQUIDITY_LOOKBACK:]
        swing_high = max(
            (_float(item.get("high")) for item in recent_swing),
            default=entry,
        )
        structural_risk = max(0.0, swing_high - entry) + STRUCTURE_BUFFER_ATR * atr
        total_bps = (
            2.0 * max(0.0, fee_bps_per_side)
            + 2.0 * max(0.0, slippage_bps_per_side)
            + spread_bps
        )
        cost_floor = entry * (total_bps / 10_000.0) / MAX_COST_R_FOR_HYBRID
        risk = max(base_risk, 1.5 * atr, structural_risk, cost_floor)
        horizon = 24
        minimum_target = entry - 1.5 * risk
        if arm == "STRUCTURE_LIQ":
            liquidity_low = min(
                (_float(item.get("low")) for item in recent_liquidity),
                default=minimum_target,
            )
            target = min(minimum_target, liquidity_low) if liquidity_low < entry else minimum_target
            rr = max(1.5, (entry - target) / risk)
            target_mode = "LIQUIDITY_MIN_1.5R"
        else:
            rr = 1.5
            target_mode = "FIXED_1.5R"

    stop = entry + risk
    if arm == "BASE_STRICT":
        target = _float(row.get("target_price"))
    elif arm != "STRUCTURE_LIQ":
        target = entry - rr * risk

    if not target < entry < stop:
        raise ValueError(f"Invalid SELL plan for {arm}")

    total_bps, cost_r = _cost_details(
        entry,
        risk,
        spread_bps,
        fee_bps_per_side=fee_bps_per_side,
        slippage_bps_per_side=slippage_bps_per_side,
    )
    original_id = str(row.get("decision_id") or "")
    original_scenario = str(row.get("scenario") or "STRICT_SELL")
    row.update(
        {
            "schema_version": SCHEMA_VERSION,
            "source_id": SOURCE_ID,
            "decision_id": f"{original_id}:RISK:{arm}",
            "scenario": f"{original_scenario}__{arm}",
            "stop_price": stop,
            "target_price": target,
            "risk_pct": risk / entry,
            "rr": rr,
            "horizon_bars": horizon,
            "orders_enabled": 0,
            "plan_arm": arm,
            "base_risk_distance": base_risk,
            "risk_distance": risk,
            "position_size_factor": base_risk / risk,
            "estimated_round_trip_bps": total_bps,
            "estimated_cost_r": cost_r,
            "reasons": " | ".join(
                part
                for part in (
                    str(row.get("reasons") or ""),
                    (
                        f"RISK_PLAN={arm}; target={target_mode}; ATR={atr:.10g}; "
                        f"same_money_risk_size_factor={base_risk / risk:.6f}; "
                        f"estimated_cost={cost_r:.6f}R"
                    ),
                )
                if part
            ),
        }
    )
    return row


def _new_signal(decision: dict[str, Any], captured_at: datetime) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "paper_signal_id": str(decision["decision_id"]),
        "activated_at": captured_at.isoformat(),
        "updated_at": captured_at.isoformat(),
        "signal_time": decision["signal_time"],
        "start_ms": decision["start_ms"],
        "end_ms": decision["end_ms"],
        "source": decision["source"],
        "source_id": decision["source_id"],
        "symbol": decision["symbol"],
        "action": decision["action"],
        "scenario": decision["scenario"],
        "quality_score": decision["quality_score"],
        "gate_status": decision["gate_status"],
        "components": decision["components"],
        "entry_price": decision["entry_price"],
        "stop_price": decision["stop_price"],
        "target_price": decision["target_price"],
        "risk_pct": decision["risk_pct"],
        "rr": decision["rr"],
        "horizon_bars": decision["horizon_bars"],
        "outcome": "OPEN",
        "result_r": "",
        "mfe_r": "",
        "mae_r": "",
        "completed": 0,
        "completion_reason": "",
        "orders_enabled": 0,
        **{field: decision.get(field, "") for field in EXTRA_FIELDS},
    }


def _profit_factor(values: Sequence[float]) -> float:
    profit = sum(value for value in values if value > 0)
    loss = -sum(value for value in values if value < 0)
    if loss > 0:
        return profit / loss
    return 999.0 if profit > 0 else 0.0


def _max_drawdown(values: Sequence[float]) -> float:
    equity = peak = drawdown = 0.0
    for value in values:
        equity += value
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    return drawdown


def _metrics(signals: Sequence[dict[str, Any]]) -> dict[str, Any]:
    completed = [row for row in signals if _int(row.get("completed")) == 1]
    gross = [_float(row.get("result_r")) for row in completed]
    costs = [_float(row.get("estimated_cost_r")) for row in completed]
    net = [result - cost for result, cost in zip(gross, costs)]
    return {
        "signals": len(signals),
        "completed": len(completed),
        "open": len(signals) - len(completed),
        "wins": sum(value > 0 for value in gross),
        "losses": sum(value < 0 for value in gross),
        "timeouts": sum(str(row.get("outcome")) == "TIMEOUT" for row in completed),
        "gross_total_r": sum(gross),
        "gross_average_r": statistics.fmean(gross) if gross else 0.0,
        "gross_profit_factor": _profit_factor(gross),
        "estimated_cost_r": sum(costs),
        "net_total_r": sum(net),
        "net_average_r": statistics.fmean(net) if net else 0.0,
        "net_profit_factor": _profit_factor(net),
        "net_max_drawdown_r": _max_drawdown(net),
        "average_position_size_factor": statistics.fmean(
            [_float(row.get("position_size_factor"), 1.0) for row in completed]
        )
        if completed
        else 0.0,
    }


def _render_dashboard(status: dict[str, Any]) -> str:
    rows = []
    cards = []
    for arm in ARMS:
        item = status["arms"][arm]
        cards.append(
            "<article>"
            f"<h2>{html.escape(arm)}</h2>"
            f"<b>{item['net_total_r']:.3f}R</b><span> net estimate</span>"
            f"<p>N {item['completed']} | Gross {item['gross_total_r']:.3f}R</p>"
            f"<p>Costs {item['estimated_cost_r']:.3f}R | Net PF {item['net_profit_factor']:.3f}</p>"
            f"<p>Size factor {item['average_position_size_factor']:.3f}</p>"
            "</article>"
        )
        rows.append(
            "<tr>"
            f"<td>{html.escape(arm)}</td><td>{item['signals']}</td><td>{item['completed']}</td>"
            f"<td>{item['wins']}</td><td>{item['losses']}</td><td>{item['timeouts']}</td>"
            f"<td>{item['gross_total_r']:.4f}</td><td>{item['estimated_cost_r']:.4f}</td>"
            f"<td>{item['net_total_r']:.4f}</td><td>{item['net_average_r']:.4f}</td>"
            f"<td>{item['net_profit_factor']:.3f}</td><td>{item['net_max_drawdown_r']:.3f}</td>"
            f"<td>{item['average_position_size_factor']:.3f}</td>"
            "</tr>"
        )
    return f"""<!doctype html><html lang='ru'><head><meta charset='utf-8'>
<meta name='viewport' content='width=device-width,initial-scale=1'>
<title>TradeMind v1.13 Risk Plans</title><style>
body{{background:#061724;color:#eaf7ff;font-family:Arial;margin:28px}}h1{{font-size:40px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:14px}}
article{{background:#0b2a40;border:1px solid #1c5878;border-radius:16px;padding:18px}}
article b{{font-size:32px}}article span{{color:#9bc4da}}table{{width:100%;border-collapse:collapse;margin-top:24px}}
th,td{{padding:9px;border-bottom:1px solid #17425b;text-align:left}}.ok{{color:#29e2a5}}.note{{color:#ffd479}}
</style></head><body><h1>Bybit Risk Plan Experiments v1.13</h1>
<p class='ok'>Forward-only, equal-start, read-only. Orders OFF.</p>
<p class='note'>Gross and hypothetical net after fee, slippage and observed entry spread.</p>
<section class='grid'>{''.join(cards)}</section>
<table><thead><tr><th>Arm</th><th>Signals</th><th>N</th><th>Wins</th><th>Losses</th>
<th>Timeouts</th><th>Gross R</th><th>Cost R</th><th>Net R</th><th>Net Avg</th>
<th>Net PF</th><th>Net DD</th><th>Size factor</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table></body></html>"""


@dataclass(frozen=True, slots=True)
class RiskPlanSummary:
    started_at_ms: int
    source_candidates: int
    output_dir: Path
    arms: dict[str, dict[str, Any]]


def run_risk_plan_experiments(
    bars_path: Path,
    strict_decisions_path: Path,
    output_dir: Path,
    *,
    fee_bps_per_side: float = DEFAULT_FEE_BPS_PER_SIDE,
    slippage_bps_per_side: float = DEFAULT_SLIPPAGE_BPS_PER_SIDE,
    now: datetime | None = None,
) -> RiskPlanSummary:
    captured_at = now or _now()
    bars = _normalized_bars(_load_csv(bars_path))
    if not bars:
        raise ValueError(f"No Bybit M5 bars found: {bars_path}")
    source_decisions = [
        row
        for row in _load_csv(strict_decisions_path)
        if _int(row.get("eligible")) == 1 and str(row.get("action")) == "SELL"
    ]
    output_dir.mkdir(parents=True, exist_ok=True)
    started_at_ms = _load_or_create_start(output_dir / "experiment_meta.json", captured_at)
    source_decisions = [
        row for row in source_decisions if _int(row.get("start_ms")) >= started_at_ms
    ]

    by_symbol: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for bar in bars:
        by_symbol[str(bar["symbol"])].append(bar)

    arm_status: dict[str, dict[str, Any]] = {}
    for arm in ARMS:
        arm_dir = output_dir / arm.lower()
        decisions_path = arm_dir / "decisions.csv"
        signals_path = arm_dir / "signals.csv"
        previous_decisions = _load_csv(decisions_path)
        known = {str(row.get("decision_id") or "") for row in previous_decisions}
        new_decisions: list[dict[str, Any]] = []
        for source in source_decisions:
            plan = apply_risk_plan(
                source,
                arm,
                by_symbol.get(str(source.get("symbol") or ""), []),
                fee_bps_per_side=fee_bps_per_side,
                slippage_bps_per_side=slippage_bps_per_side,
            )
            if str(plan["decision_id"]) not in known:
                new_decisions.append(plan)
        decisions = sorted(
            [*previous_decisions, *new_decisions],
            key=lambda row: (_int(row.get("start_ms")), str(row.get("decision_id"))),
        )
        journal = {
            str(row.get("paper_signal_id") or ""): dict(row)
            for row in _load_csv(signals_path)
            if row.get("paper_signal_id")
        }
        for decision in new_decisions:
            journal[str(decision["decision_id"])] = _new_signal(decision, captured_at)
        updated = sorted(
            [
                _update_outcome(
                    row,
                    by_symbol.get(str(row.get("symbol") or ""), []),
                    captured_at,
                )
                for row in journal.values()
            ],
            key=lambda row: (_int(row.get("start_ms")), str(row.get("paper_signal_id"))),
        )
        _atomic_csv(decisions_path, RISK_DECISION_FIELDS, decisions)
        _atomic_csv(signals_path, RISK_PAPER_FIELDS, updated)
        metrics = {
            **_metrics(updated),
            "new_decisions": len(new_decisions),
            "orders_enabled": False,
            "forward_only": True,
            "signals_path": str(signals_path),
        }
        arm_status[arm] = metrics
        _atomic_json(
            arm_dir / "status.json",
            {
                "schema_version": SCHEMA_VERSION,
                "state": "OK",
                "updated_at": captured_at.isoformat(),
                "arm": arm,
                **metrics,
            },
        )

    dashboard = output_dir / "dashboard" / "index.html"
    status = {
        "schema_version": SCHEMA_VERSION,
        "state": "OK",
        "updated_at": captured_at.isoformat(),
        "started_at_ms": started_at_ms,
        "started_at": _iso_from_ms(started_at_ms),
        "source_id": SOURCE_ID,
        "source_decisions_path": str(strict_decisions_path),
        "forward_only": True,
        "orders_enabled": False,
        "logic_changed": False,
        "equal_start": True,
        "cost_model": {
            "fee_bps_per_side": fee_bps_per_side,
            "slippage_bps_per_side": slippage_bps_per_side,
            "observed_entry_spread_included": True,
        },
        "arms": arm_status,
        "bars_path": str(bars_path),
        "output_dir": str(output_dir),
        "dashboard": str(dashboard),
    }
    _atomic_json(output_dir / "status.json", status)
    _atomic_text(dashboard, _render_dashboard(status))
    return RiskPlanSummary(
        started_at_ms=started_at_ms,
        source_candidates=len(source_decisions),
        output_dir=output_dir,
        arms=arm_status,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="TradeMind v1.13 Bybit risk-plan experiments")
    parser.add_argument("--bars", type=Path, default=Path("data/bybit_v1_9/bybit_bars.csv"))
    parser.add_argument(
        "--strict-decisions",
        type=Path,
        default=Path("data/bybit_shadow_v1_11/strict_sell/decisions.csv"),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("data/bybit_risk_plans_v1_13"),
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
        summary = run_risk_plan_experiments(
            args.bars.expanduser().resolve(),
            args.strict_decisions.expanduser().resolve(),
            args.output_dir.expanduser().resolve(),
            fee_bps_per_side=args.fee_bps_per_side,
            slippage_bps_per_side=args.slippage_bps_per_side,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"Risk-plan experiments failed: {exc}")
        return 1
    print("TradeMind v1.13 Risk Plan Experiments")
    print("STRICT_SELL entry logic unchanged. Forward-only. Read-only. No orders.")
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
