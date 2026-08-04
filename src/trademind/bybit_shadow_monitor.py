"""TradeMind v1.11.1 analytics monitor for equal-start Bybit shadow experiments.

The monitor never changes CONTROL, BUY_ONLY or STRICT_SELL decisions. It reads
completed paper signals, applies a configurable hypothetical execution-cost
model, measures concentration and timing, tracks 50/100/200 milestones, and
emits descriptive alerts. No order API is imported or called.
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import math
import statistics
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from trademind.bybit_shadow import (
    BAR_MS,
    _atomic_csv,
    _atomic_json,
    _atomic_text,
    _float,
    _int,
    _load_csv,
)
from trademind.bybit_shadow_experiments import ARMS

SCHEMA_VERSION = "1.11.1"
MILESTONES = (50, 100, 200)
DEFAULT_FEE_BPS_PER_SIDE = 5.5
DEFAULT_SLIPPAGE_BPS_PER_SIDE = 1.0
RECENT_WINDOW = 20

SUMMARY_FIELDS = (
    "schema_version",
    "captured_at",
    "arm",
    "signals",
    "completed",
    "open",
    "gross_wins",
    "gross_losses",
    "net_wins",
    "net_losses",
    "gross_total_r",
    "gross_average_r",
    "gross_profit_factor",
    "net_total_r",
    "net_average_r",
    "net_profit_factor",
    "estimated_cost_r",
    "net_max_drawdown_r",
    "peak_concurrent",
    "peak_same_direction",
    "largest_entry_cluster",
    "next_milestone",
    "milestone_progress_pct",
    "alert",
    "orders_enabled",
)

BREAKDOWN_FIELDS = (
    "schema_version",
    "captured_at",
    "arm",
    "dimension",
    "key",
    "completed",
    "gross_total_r",
    "net_total_r",
    "net_average_r",
    "net_profit_factor",
    "estimated_cost_r",
)

SIGNAL_COST_FIELDS = (
    "schema_version",
    "captured_at",
    "arm",
    "paper_signal_id",
    "signal_time",
    "start_ms",
    "symbol",
    "action",
    "quality_score",
    "outcome",
    "gross_result_r",
    "estimated_cost_r",
    "net_result_r",
    "risk_pct",
    "entry_spread_bps",
    "holding_horizon_bars",
    "utc_hour",
    "orders_enabled",
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


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


def _decision_lookup(rows: Sequence[dict[str, str]]) -> dict[str, dict[str, str]]:
    return {
        str(row.get("decision_id") or ""): row
        for row in rows
        if str(row.get("decision_id") or "")
    }


def estimated_cost_r(
    signal: dict[str, Any],
    decision: dict[str, Any] | None,
    *,
    fee_bps_per_side: float,
    slippage_bps_per_side: float,
) -> tuple[float, float, float]:
    """Return estimated cost in R, risk percentage and observed entry spread.

    The model is deliberately configurable and hypothetical. It charges fees
    and slippage on entry and exit, plus the observed entry spread from the
    matching decision when available.
    """
    entry = _float(signal.get("entry_price"))
    stop = _float(signal.get("stop_price"))
    risk_pct = abs(entry - stop) / entry if entry else _float(signal.get("risk_pct"))
    if risk_pct <= 0:
        risk_pct = _float(signal.get("risk_pct"))
    spread_bps = max(0.0, _float((decision or {}).get("m5_spread_bps")))
    total_cost_bps = 2.0 * max(0.0, fee_bps_per_side) + 2.0 * max(
        0.0, slippage_bps_per_side
    ) + spread_bps
    risk_bps = risk_pct * 10_000.0
    cost_r = total_cost_bps / risk_bps if risk_bps > 0 else 0.0
    return cost_r, risk_pct, spread_bps


def _signal_rows(
    arm: str,
    signals: Sequence[dict[str, str]],
    decisions: Sequence[dict[str, str]],
    *,
    captured_at: datetime,
    fee_bps_per_side: float,
    slippage_bps_per_side: float,
) -> list[dict[str, Any]]:
    lookup = _decision_lookup(decisions)
    output: list[dict[str, Any]] = []
    for signal in signals:
        if _int(signal.get("completed")) != 1:
            continue
        signal_id = str(signal.get("paper_signal_id") or "")
        decision = lookup.get(signal_id)
        cost_r, risk_pct, spread_bps = estimated_cost_r(
            signal,
            decision,
            fee_bps_per_side=fee_bps_per_side,
            slippage_bps_per_side=slippage_bps_per_side,
        )
        gross = _float(signal.get("result_r"))
        start_ms = _int(signal.get("start_ms"))
        hour = datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc).hour if start_ms else -1
        output.append(
            {
                "schema_version": SCHEMA_VERSION,
                "captured_at": captured_at.isoformat(),
                "arm": arm,
                "paper_signal_id": signal_id,
                "signal_time": str(signal.get("signal_time") or ""),
                "start_ms": start_ms,
                "symbol": str(signal.get("symbol") or ""),
                "action": str(signal.get("action") or ""),
                "quality_score": _int(signal.get("quality_score")),
                "outcome": str(signal.get("outcome") or ""),
                "gross_result_r": gross,
                "estimated_cost_r": cost_r,
                "net_result_r": gross - cost_r,
                "risk_pct": risk_pct,
                "entry_spread_bps": spread_bps,
                "holding_horizon_bars": _int(signal.get("horizon_bars"), 12),
                "utc_hour": hour,
                "orders_enabled": False,
            }
        )
    return sorted(output, key=lambda row: (_int(row["start_ms"]), str(row["paper_signal_id"])))


def _concurrency(rows: Sequence[dict[str, Any]]) -> dict[str, int]:
    events: list[tuple[int, int, str]] = []
    clusters: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        start = _int(row.get("start_ms"))
        horizon = max(1, _int(row.get("holding_horizon_bars"), 12))
        end = start + horizon * BAR_MS
        action = str(row.get("action") or "")
        events.append((start, 1, action))
        events.append((end, -1, action))
        clusters[start // BAR_MS].append(row)
    current = buys = sells = 0
    peak = peak_same = 0
    for _, delta, action in sorted(events, key=lambda item: (item[0], item[1])):
        current += delta
        if action == "BUY":
            buys += delta
        elif action == "SELL":
            sells += delta
        peak = max(peak, current)
        peak_same = max(peak_same, buys, sells)
    largest_cluster = max((len(group) for group in clusters.values()), default=0)
    return {
        "peak_concurrent": peak,
        "peak_same_direction": peak_same,
        "largest_entry_cluster": largest_cluster,
    }


def _milestone(completed: int) -> dict[str, Any]:
    next_target = next((target for target in MILESTONES if completed < target), None)
    if next_target is None:
        return {
            "reached": list(MILESTONES),
            "next": None,
            "remaining": 0,
            "progress_pct": 100.0,
        }
    previous = max((target for target in MILESTONES if target <= completed), default=0)
    span = next_target - previous
    progress = 100.0 * (completed - previous) / span if span else 100.0
    return {
        "reached": [target for target in MILESTONES if completed >= target],
        "next": next_target,
        "remaining": next_target - completed,
        "progress_pct": progress,
    }


def _recent_metrics(rows: Sequence[dict[str, Any]], size: int = RECENT_WINDOW) -> dict[str, float]:
    sample = list(rows[-size:])
    values = [_float(row.get("net_result_r")) for row in sample]
    return {
        "n": float(len(values)),
        "average_r": statistics.fmean(values) if values else 0.0,
        "profit_factor": _profit_factor(values),
        "total_r": sum(values),
    }


def _alert(arm: str, completed: int, net_total: float, recent: dict[str, float]) -> str:
    if completed < 30:
        return "WATCH_INSUFFICIENT_SAMPLE"
    recent_avg = recent["average_r"]
    recent_pf = recent["profit_factor"]
    if arm == "STRICT_SELL":
        if recent_avg <= 0 or recent_pf < 1.0:
            return "EDGE_DEGRADING"
        if completed >= 50 and net_total > 0 and recent_avg > 0 and recent_pf >= 1.15:
            return "EDGE_HOLDING"
        return "WATCH_EDGE_NOT_PROVEN"
    if arm == "BUY_ONLY":
        if recent_avg > 0.10 and recent_pf >= 1.15:
            return "RECOVERY_SIGNAL"
        if net_total < 0 and recent_avg <= 0:
            return "WEAK"
        return "WATCH"
    if recent_avg <= 0 and net_total <= 0:
        return "BASELINE_WEAK"
    return "BASELINE_WATCH"


def _summary(
    arm: str,
    all_signals: Sequence[dict[str, str]],
    completed_rows: Sequence[dict[str, Any]],
    captured_at: datetime,
) -> dict[str, Any]:
    gross = [_float(row.get("gross_result_r")) for row in completed_rows]
    net = [_float(row.get("net_result_r")) for row in completed_rows]
    costs = [_float(row.get("estimated_cost_r")) for row in completed_rows]
    milestone = _milestone(len(completed_rows))
    recent = _recent_metrics(completed_rows)
    concentration = _concurrency(completed_rows)
    net_total = sum(net)
    return {
        "schema_version": SCHEMA_VERSION,
        "captured_at": captured_at.isoformat(),
        "arm": arm,
        "signals": len(all_signals),
        "completed": len(completed_rows),
        "open": len(all_signals) - len(completed_rows),
        "gross_wins": sum(value > 0 for value in gross),
        "gross_losses": sum(value < 0 for value in gross),
        "net_wins": sum(value > 0 for value in net),
        "net_losses": sum(value < 0 for value in net),
        "gross_total_r": sum(gross),
        "gross_average_r": statistics.fmean(gross) if gross else 0.0,
        "gross_profit_factor": _profit_factor(gross),
        "net_total_r": net_total,
        "net_average_r": statistics.fmean(net) if net else 0.0,
        "net_profit_factor": _profit_factor(net),
        "estimated_cost_r": sum(costs),
        "net_max_drawdown_r": _max_drawdown(net),
        **concentration,
        "next_milestone": milestone["next"],
        "milestone_progress_pct": milestone["progress_pct"],
        "milestones": milestone,
        "recent_window": recent,
        "alert": _alert(arm, len(completed_rows), net_total, recent),
        "orders_enabled": False,
    }


def _breakdowns(
    arm: str, rows: Sequence[dict[str, Any]], captured_at: datetime
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    dimensions: tuple[tuple[str, str], ...] = (
        ("symbol", "symbol"),
        ("utc_hour", "utc_hour"),
        ("action", "action"),
    )
    for dimension, field in dimensions:
        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            groups[str(row.get(field))].append(row)
        for key, group in sorted(groups.items()):
            gross = [_float(row.get("gross_result_r")) for row in group]
            net = [_float(row.get("net_result_r")) for row in group]
            costs = [_float(row.get("estimated_cost_r")) for row in group]
            output.append(
                {
                    "schema_version": SCHEMA_VERSION,
                    "captured_at": captured_at.isoformat(),
                    "arm": arm,
                    "dimension": dimension,
                    "key": key,
                    "completed": len(group),
                    "gross_total_r": sum(gross),
                    "net_total_r": sum(net),
                    "net_average_r": statistics.fmean(net) if net else 0.0,
                    "net_profit_factor": _profit_factor(net),
                    "estimated_cost_r": sum(costs),
                }
            )
    return output


def _render_dashboard(status: dict[str, Any], breakdowns: Sequence[dict[str, Any]]) -> str:
    cards: list[str] = []
    rows: list[str] = []
    for arm in ARMS:
        item = status["arms"][arm]
        cards.append(
            "<article>"
            f"<h2>{html.escape(arm)}</h2>"
            f"<b>{item['net_total_r']:.3f}R</b><span> net estimate</span>"
            f"<p>Gross {item['gross_total_r']:.3f}R | Costs {item['estimated_cost_r']:.3f}R</p>"
            f"<p>N {item['completed']} | Net PF {item['net_profit_factor']:.3f}</p>"
            f"<p>Peak overlap {item['peak_concurrent']} | Alert {html.escape(item['alert'])}</p>"
            "</article>"
        )
        rows.append(
            "<tr>"
            f"<td>{html.escape(arm)}</td><td>{item['completed']}</td>"
            f"<td>{item['gross_total_r']:.4f}</td><td>{item['estimated_cost_r']:.4f}</td>"
            f"<td>{item['net_total_r']:.4f}</td><td>{item['net_average_r']:.4f}</td>"
            f"<td>{item['net_profit_factor']:.3f}</td><td>{item['net_max_drawdown_r']:.3f}</td>"
            f"<td>{item['next_milestone'] if item['next_milestone'] is not None else 'DONE'}</td>"
            f"<td>{html.escape(item['alert'])}</td>"
            "</tr>"
        )
    best = sorted(
        (row for row in breakdowns if row["dimension"] == "symbol"),
        key=lambda row: _float(row.get("net_total_r")),
        reverse=True,
    )[:15]
    breakdown_rows = "".join(
        "<tr>"
        f"<td>{html.escape(str(row['arm']))}</td><td>{html.escape(str(row['key']))}</td>"
        f"<td>{row['completed']}</td><td>{row['net_total_r']:.4f}</td>"
        f"<td>{row['net_average_r']:.4f}</td><td>{row['net_profit_factor']:.3f}</td>"
        "</tr>"
        for row in best
    )
    assumptions = status["cost_model"]
    return f"""<!doctype html><html lang='ru'><head><meta charset='utf-8'>
<meta name='viewport' content='width=device-width,initial-scale=1'>
<title>TradeMind v1.11.1 Analytics Monitor</title><style>
body{{background:#061724;color:#eaf7ff;font-family:Arial;margin:28px}}h1{{font-size:40px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(270px,1fr));gap:14px}}
article{{background:#0b2a40;border:1px solid #1c5878;border-radius:16px;padding:18px}}
article b{{font-size:34px}}article span{{color:#9bc4da}}table{{width:100%;border-collapse:collapse;margin-top:24px}}
th,td{{padding:9px;border-bottom:1px solid #17425b;text-align:left}}.ok{{color:#29e2a5}}.note{{color:#ffd479}}
</style></head><body><h1>Bybit Shadow Analytics v1.11.1</h1>
<p class='ok'>Read-only analytics. Experiment rules unchanged. Orders OFF.</p>
<p class='note'>Hypothetical cost model: fee {assumptions['fee_bps_per_side']:.2f} bps/side, slippage {assumptions['slippage_bps_per_side']:.2f} bps/side, observed entry spread included.</p>
<section class='grid'>{''.join(cards)}</section>
<h2>Gross vs net estimate</h2><table><thead><tr><th>Arm</th><th>N</th><th>Gross R</th><th>Costs R</th><th>Net R</th><th>Net Avg</th><th>Net PF</th><th>Net DD</th><th>Next N</th><th>Alert</th></tr></thead><tbody>{''.join(rows)}</tbody></table>
<h2>Top symbol rows by net estimate</h2><table><thead><tr><th>Arm</th><th>Symbol</th><th>N</th><th>Net R</th><th>Net Avg</th><th>Net PF</th></tr></thead><tbody>{breakdown_rows}</tbody></table>
</body></html>"""


@dataclass(frozen=True, slots=True)
class MonitorSummary:
    output_dir: Path
    arms: dict[str, dict[str, Any]]
    captured_at: datetime


def run_monitor(
    experiment_dir: Path,
    output_dir: Path,
    *,
    fee_bps_per_side: float = DEFAULT_FEE_BPS_PER_SIDE,
    slippage_bps_per_side: float = DEFAULT_SLIPPAGE_BPS_PER_SIDE,
    now: datetime | None = None,
) -> MonitorSummary:
    captured_at = now or _now()
    experiment_status_path = experiment_dir / "status.json"
    if not experiment_status_path.is_file():
        raise ValueError(f"Experiment status not found: {experiment_status_path}")
    experiment_status = json.loads(experiment_status_path.read_text(encoding="utf-8"))
    if not bool(experiment_status.get("forward_only")):
        raise ValueError("Experiment is not forward-only")
    if bool(experiment_status.get("orders_enabled")):
        raise ValueError("Experiment unexpectedly has orders enabled")

    output_dir.mkdir(parents=True, exist_ok=True)
    summaries: dict[str, dict[str, Any]] = {}
    all_cost_rows: list[dict[str, Any]] = []
    all_breakdowns: list[dict[str, Any]] = []
    for arm in ARMS:
        arm_dir = experiment_dir / arm.lower()
        signals = _load_csv(arm_dir / "signals.csv")
        decisions = _load_csv(arm_dir / "decisions.csv")
        cost_rows = _signal_rows(
            arm,
            signals,
            decisions,
            captured_at=captured_at,
            fee_bps_per_side=fee_bps_per_side,
            slippage_bps_per_side=slippage_bps_per_side,
        )
        summaries[arm] = _summary(arm, signals, cost_rows, captured_at)
        all_cost_rows.extend(cost_rows)
        all_breakdowns.extend(_breakdowns(arm, cost_rows, captured_at))

    status = {
        "schema_version": SCHEMA_VERSION,
        "state": "OK",
        "updated_at": captured_at.isoformat(),
        "source_experiment": str(experiment_dir),
        "experiment_started_at": experiment_status.get("started_at"),
        "forward_only": True,
        "orders_enabled": False,
        "logic_changed": False,
        "cost_model": {
            "type": "hypothetical_round_trip_estimate",
            "fee_bps_per_side": fee_bps_per_side,
            "slippage_bps_per_side": slippage_bps_per_side,
            "observed_entry_spread_included": True,
        },
        "arms": summaries,
        "milestones": list(MILESTONES),
        "dashboard": str(output_dir / "dashboard" / "index.html"),
    }
    _atomic_csv(output_dir / "arm_summary.csv", SUMMARY_FIELDS, list(summaries.values()))
    _atomic_csv(output_dir / "breakdowns.csv", BREAKDOWN_FIELDS, all_breakdowns)
    _atomic_csv(output_dir / "signal_costs.csv", SIGNAL_COST_FIELDS, all_cost_rows)
    _atomic_json(output_dir / "status.json", status)
    _atomic_text(output_dir / "dashboard" / "index.html", _render_dashboard(status, all_breakdowns))
    return MonitorSummary(output_dir=output_dir, arms=summaries, captured_at=captured_at)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="TradeMind v1.11.1 analytics monitor")
    parser.add_argument("--experiment-dir", type=Path, default=Path("data/bybit_shadow_v1_11"))
    parser.add_argument("--output-dir", type=Path, default=Path("data/bybit_shadow_monitor_v1_11_1"))
    parser.add_argument("--fee-bps-per-side", type=float, default=DEFAULT_FEE_BPS_PER_SIDE)
    parser.add_argument(
        "--slippage-bps-per-side", type=float, default=DEFAULT_SLIPPAGE_BPS_PER_SIDE
    )
    parser.add_argument("--open-dashboard", action="store_true")
    args = parser.parse_args(argv)
    try:
        summary = run_monitor(
            args.experiment_dir.expanduser().resolve(),
            args.output_dir.expanduser().resolve(),
            fee_bps_per_side=args.fee_bps_per_side,
            slippage_bps_per_side=args.slippage_bps_per_side,
        )
    except (OSError, ValueError, json.JSONDecodeError, csv.Error) as exc:
        print(f"Analytics monitor failed: {exc}")
        return 1
    print("TradeMind v1.11.1 Analytics Monitor")
    print("Experiment logic unchanged. Read-only. No orders.")
    for arm in ARMS:
        item = summary.arms[arm]
        print(
            f"{arm}: completed={item['completed']} gross_r={item['gross_total_r']:.4f} "
            f"cost_r={item['estimated_cost_r']:.4f} net_r={item['net_total_r']:.4f} "
            f"net_pf={item['net_profit_factor']:.3f} alert={item['alert']}"
        )
    print(f"Output: {summary.output_dir}")
    if args.open_dashboard:
        import os

        os.startfile(summary.output_dir / "dashboard" / "index.html")  # type: ignore[attr-defined]
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
