"""TradeMind v1.11 forward-only Bybit shadow experiments.

All arms start at one shared timestamp and consume the same future M5 bars:
CONTROL reproduces the v1.10 policy, BUY_ONLY accepts only v1.10 BUY
candidates, and STRICT_SELL accepts only strongly confirmed v1.10 SELL
candidates. The module is read-only and never sends orders.
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
    AGG_FIELDS,
    BAR_MS,
    DECISION_FIELDS,
    H1_MS,
    M15_MS,
    PAPER_FIELDS,
    STATE_FIELDS,
    VALID_STATUSES,
    _atomic_csv,
    _atomic_json,
    _atomic_text,
    _float,
    _int,
    _iso_from_ms,
    _load_csv,
    _load_or_create_start,
    _mark_duplicate_candidates,
    _new_paper_row,
    _normalized_bars,
    _score_decision,
    _update_outcome,
    aggregate_bars,
    build_states,
)

SCHEMA_VERSION = "1.11.0"
SOURCE_ID = "BYBIT_LINEAR_SHADOW_EXPERIMENTS"
ARMS = ("CONTROL", "BUY_ONLY", "STRICT_SELL")
STRICT_SELL_MIN_SCORE = 85
STRICT_SELL_COMPONENTS = frozenset(
    {
        "H1_PRICE_TREND",
        "H1_DELTA",
        "M15_PRICE",
        "M15_DELTA",
        "M15_BOOK",
        "M5_DELTA_IMPULSE",
        "M5_BOOK",
        "SPREAD_OK",
        "FUNDING_OK",
        "BASIS_OK",
    }
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _append_reason(row: dict[str, Any], reason: str) -> None:
    row["reasons"] = " | ".join(
        part for part in (str(row.get("reasons") or ""), reason) if part
    )


def apply_arm_policy(base: dict[str, Any], arm: str) -> dict[str, Any]:
    """Clone one v1.10 decision and apply a non-relaxing experiment policy."""
    if arm not in ARMS:
        raise ValueError(f"Unknown experiment arm: {arm}")
    row = dict(base)
    original_id = str(row["decision_id"])
    original_scenario = str(row["scenario"])
    row["schema_version"] = SCHEMA_VERSION
    row["source_id"] = SOURCE_ID
    row["decision_id"] = f"{original_id}:{arm}"
    row["scenario"] = f"{original_scenario}__{arm}"
    row["orders_enabled"] = 0

    original_candidate = _int(row.get("eligible")) == 1
    if arm == "CONTROL":
        return row

    if arm == "BUY_ONLY":
        if original_candidate and str(row.get("action")) != "BUY":
            row["gate_status"] = "REJECTED"
            row["eligible"] = 0
            _append_reason(row, "BUY_ONLY policy rejected non-BUY candidate")
        return row

    components = {
        item for item in str(row.get("components") or "").split("|") if item
    }
    strict_ok = (
        original_candidate
        and str(row.get("action")) == "SELL"
        and _int(row.get("quality_score")) >= STRICT_SELL_MIN_SCORE
        and STRICT_SELL_COMPONENTS.issubset(components)
        and _float(row.get("h1_return_pct")) < 0
        and _float(row.get("h1_delta_turnover")) < 0
        and _float(row.get("m15_return_pct")) < 0
        and _float(row.get("m15_delta_turnover")) < 0
        and _float(row.get("m15_book_imbalance_10")) <= -0.02
        and _float(row.get("m5_delta_turnover")) < 0
        and _float(row.get("m5_book_imbalance_10")) <= -0.05
    )
    if strict_ok:
        row["gate_status"] = "CANDIDATE"
        row["eligible"] = 1
        _append_reason(row, "STRICT_SELL confirmations passed")
    elif original_candidate:
        row["gate_status"] = "REJECTED"
        row["eligible"] = 0
        missing = sorted(STRICT_SELL_COMPONENTS - components)
        detail = (
            f"STRICT_SELL rejected candidate; score={row.get('quality_score')} "
            f"missing={','.join(missing) if missing else 'numeric confirmation'}"
        )
        _append_reason(row, detail)
    return row


def _arm_metrics(journal: Sequence[dict[str, Any]]) -> dict[str, Any]:
    completed = [row for row in journal if _int(row.get("completed")) == 1]
    values = [_float(row.get("result_r")) for row in completed]
    wins = sum(value > 0 for value in values)
    losses = sum(value < 0 for value in values)
    timeouts = sum(str(row.get("outcome")) == "TIMEOUT" for row in completed)
    gross_profit = sum(value for value in values if value > 0)
    gross_loss = -sum(value for value in values if value < 0)
    pf = gross_profit / gross_loss if gross_loss else (999.0 if gross_profit else 0.0)
    total = sum(values)
    average = statistics.fmean(values) if values else 0.0
    equity = peak = drawdown = 0.0
    for value in values:
        equity += value
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    return {
        "signals": len(journal),
        "completed": len(completed),
        "open": len(journal) - len(completed),
        "wins": wins,
        "losses": losses,
        "timeouts": timeouts,
        "win_rate": wins / len(completed) if completed else 0.0,
        "total_r": total,
        "average_r": average,
        "profit_factor": pf,
        "max_drawdown_r": drawdown,
    }


def _render_dashboard(status: dict[str, Any], started_at_ms: int) -> str:
    cards: list[str] = []
    rows: list[str] = []
    for arm in ARMS:
        item = status["arms"][arm]
        cards.append(
            "<article>"
            f"<h2>{html.escape(arm)}</h2>"
            f"<b>{item['completed']}</b><span> completed</span>"
            f"<p>Signals: {item['signals']} | Open: {item['open']}</p>"
            f"<p>Total R: {item['total_r']:.3f} | Avg R: {item['average_r']:.3f}</p>"
            f"<p>Win rate: {item['win_rate'] * 100:.1f}% | PF: {item['profit_factor']:.3f}</p>"
            "</article>"
        )
        rows.append(
            "<tr>"
            f"<td>{html.escape(arm)}</td>"
            f"<td>{item['signals']}</td><td>{item['completed']}</td>"
            f"<td>{item['wins']}</td><td>{item['losses']}</td><td>{item['timeouts']}</td>"
            f"<td>{item['win_rate'] * 100:.2f}%</td>"
            f"<td>{item['total_r']:.4f}</td><td>{item['average_r']:.4f}</td>"
            f"<td>{item['profit_factor']:.3f}</td><td>{item['max_drawdown_r']:.3f}</td>"
            "</tr>"
        )
    return f"""<!doctype html><html lang='ru'><head><meta charset='utf-8'>
<meta name='viewport' content='width=device-width,initial-scale=1'>
<title>TradeMind v1.11 Shadow Experiments</title><style>
body{{background:#061724;color:#eaf7ff;font-family:Arial;margin:28px}}h1{{font-size:42px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:14px}}
article{{background:#0b2a40;border:1px solid #1c5878;border-radius:16px;padding:18px}}
article b{{font-size:34px}}article span{{color:#9bc4da}}table{{width:100%;border-collapse:collapse;margin-top:24px}}
th,td{{padding:9px;border-bottom:1px solid #17425b;text-align:left}}.ok{{color:#29e2a5}}
</style></head><body><h1>Bybit Shadow Experiments v1.11</h1>
<p class='ok'>Forward-only, read-only. CONTROL vs BUY_ONLY vs STRICT_SELL. Orders OFF.</p>
<p>Equal-start experiment: {html.escape(_iso_from_ms(started_at_ms))}</p>
<section class='grid'>{''.join(cards)}</section><table><thead><tr><th>Arm</th><th>Signals</th>
<th>Completed</th><th>Wins</th><th>Losses</th><th>Timeouts</th><th>Win rate</th>
<th>Total R</th><th>Avg R</th><th>PF</th><th>Max DD R</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table></body></html>"""


@dataclass(frozen=True, slots=True)
class ExperimentSummary:
    source_bars: int
    m15_bars: int
    h1_bars: int
    started_at_ms: int
    output_dir: Path
    arms: dict[str, dict[str, Any]]


def run_experiments(
    bars_path: Path, output_dir: Path, now: datetime | None = None
) -> ExperimentSummary:
    captured_at = now or _now()
    source = _normalized_bars(_load_csv(bars_path))
    if not source:
        raise ValueError(f"No Bybit M5 bars found: {bars_path}")
    output_dir.mkdir(parents=True, exist_ok=True)
    started_at_ms = _load_or_create_start(output_dir / "experiment_meta.json", captured_at)
    m15 = aggregate_bars(source, M15_MS, "M15")
    h1 = aggregate_bars(source, H1_MS, "H1")

    by_m5: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_m15: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_h1: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in source:
        by_m5[str(row["symbol"])].append(row)
    for row in m15:
        by_m15[str(row["symbol"])].append(row)
    for row in h1:
        by_h1[str(row["symbol"])].append(row)

    base_decisions: list[dict[str, Any]] = []
    for symbol, rows in sorted(by_m5.items()):
        for index, trigger in enumerate(rows):
            if _int(trigger["end_ms"]) < started_at_ms:
                continue
            m15_history = [
                row for row in by_m15[symbol]
                if _int(row["end_ms"]) <= _int(trigger["end_ms"])
            ]
            h1_history = [
                row for row in by_h1[symbol]
                if _int(row["end_ms"]) <= _int(trigger["end_ms"])
            ]
            base_decisions.append(
                _score_decision(rows[: index + 1], m15_history, h1_history, captured_at)
            )

    arm_status: dict[str, dict[str, Any]] = {}
    for arm in ARMS:
        arm_dir = output_dir / arm.lower()
        decisions_path = arm_dir / "decisions.csv"
        signals_path = arm_dir / "signals.csv"
        previous = _load_csv(decisions_path)
        known = {str(row.get("decision_id") or "") for row in previous}
        new_rows = [apply_arm_policy(row, arm) for row in base_decisions]
        new_rows = [row for row in new_rows if str(row["decision_id"]) not in known]
        existing_signals = _load_csv(signals_path)
        _mark_duplicate_candidates(new_rows, existing_signals)
        decisions = sorted(
            [*previous, *new_rows],
            key=lambda row: (_int(row.get("start_ms")), str(row.get("decision_id"))),
        )
        journal = {
            str(row["paper_signal_id"]): dict(row)
            for row in existing_signals if row.get("paper_signal_id")
        }
        for decision in new_rows:
            if _int(decision.get("eligible")) == 1:
                journal[str(decision["decision_id"])] = _new_paper_row(decision, captured_at)
        updated = sorted(
            [
                _update_outcome(row, by_m5.get(str(row["symbol"]), []), captured_at)
                for row in journal.values()
            ],
            key=lambda row: (_int(row.get("start_ms")), str(row.get("paper_signal_id"))),
        )
        states = build_states(updated, captured_at)
        _atomic_csv(decisions_path, DECISION_FIELDS, decisions)
        _atomic_csv(signals_path, PAPER_FIELDS, updated)
        _atomic_csv(arm_dir / "states.csv", STATE_FIELDS, states)
        counts = {
            status: sum(str(row.get("gate_status")) == status for row in decisions)
            for status in VALID_STATUSES
        }
        metrics = _arm_metrics(updated)
        metrics.update(
            {
                "decisions": len(decisions),
                "new_decisions": len(new_rows),
                "gate_counts": counts,
                "orders_enabled": False,
                "forward_only": True,
                "signals_path": str(signals_path),
            }
        )
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

    _atomic_csv(output_dir / "aggregate_m15.csv", AGG_FIELDS, m15)
    _atomic_csv(output_dir / "aggregate_h1.csv", AGG_FIELDS, h1)
    dashboard = output_dir / "dashboard" / "index.html"
    status = {
        "schema_version": SCHEMA_VERSION,
        "state": "OK",
        "updated_at": captured_at.isoformat(),
        "started_at_ms": started_at_ms,
        "started_at": _iso_from_ms(started_at_ms),
        "source_id": SOURCE_ID,
        "forward_only": True,
        "orders_enabled": False,
        "source_bars": len(source),
        "m15_bars": len(m15),
        "h1_bars": len(h1),
        "arms": arm_status,
        "bars_path": str(bars_path),
        "output_dir": str(output_dir),
        "dashboard": str(dashboard),
    }
    _atomic_text(dashboard, _render_dashboard(status, started_at_ms))
    _atomic_json(output_dir / "status.json", status)
    return ExperimentSummary(
        source_bars=len(source),
        m15_bars=len(m15),
        h1_bars=len(h1),
        started_at_ms=started_at_ms,
        output_dir=output_dir,
        arms=arm_status,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="TradeMind v1.11 Bybit Shadow Experiments")
    parser.add_argument("--bars", type=Path, default=Path("data/bybit_v1_9/bybit_bars.csv"))
    parser.add_argument("--output-dir", type=Path, default=Path("data/bybit_shadow_v1_11"))
    parser.add_argument("--open-dashboard", action="store_true")
    args = parser.parse_args(argv)
    bars = args.bars.expanduser().resolve()
    output = args.output_dir.expanduser().resolve()
    try:
        summary = run_experiments(bars, output)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"Bybit Shadow Experiments failed: {exc}")
        return 1
    print("TradeMind v1.11 Bybit Shadow Experiments")
    print(f"M5/M15/H1: {summary.source_bars}/{summary.m15_bars}/{summary.h1_bars}")
    print(f"Equal-start: {_iso_from_ms(summary.started_at_ms)}")
    for arm in ARMS:
        item = summary.arms[arm]
        print(
            f"{arm}: signals={item['signals']} completed={item['completed']} "
            f"total_r={item['total_r']:.4f} orders_enabled=False"
        )
    print(f"Output: {summary.output_dir}")
    print("No orders were sent.")
    if args.open_dashboard:
        import os

        os.startfile(output / "dashboard" / "index.html")  # type: ignore[attr-defined]
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
