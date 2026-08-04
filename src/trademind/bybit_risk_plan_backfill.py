"""Historical backfill for the v1.13 Bybit stop/target plan experiments.

The backfill replays only eligible STRICT_SELL decisions that happened before
the live risk-plan experiment cutoff. Results are written to a separate output
directory and never modify CONTROL, BUY_ONLY, STRICT_SELL or the forward v1.13
journals. No order API is imported or called.
"""

from __future__ import annotations

import argparse
import html
import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from trademind.bybit_risk_plan_experiments import (
    ARMS,
    DEFAULT_FEE_BPS_PER_SIDE,
    DEFAULT_SLIPPAGE_BPS_PER_SIDE,
    RISK_DECISION_FIELDS,
    RISK_PAPER_FIELDS,
    _metrics,
    _new_signal,
    apply_risk_plan,
)
from trademind.bybit_shadow import (
    _atomic_csv,
    _atomic_json,
    _atomic_text,
    _int,
    _iso_from_ms,
    _load_csv,
    _normalized_bars,
    _update_outcome,
)

SCHEMA_VERSION = "1.13.1"
MODE = "BACKFILL"
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
    "timeouts",
    "gross_total_r",
    "gross_average_r",
    "gross_profit_factor",
    "estimated_cost_r",
    "net_total_r",
    "net_average_r",
    "net_profit_factor",
    "net_max_drawdown_r",
    "average_position_size_factor",
    "orders_enabled",
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _load_cutoff(path: Path) -> int:
    if not path.is_file():
        raise ValueError(f"Forward experiment metadata not found: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    cutoff = _int(payload.get("started_at_ms"))
    if cutoff <= 0:
        raise ValueError(f"Invalid forward experiment cutoff in: {path}")
    if not bool(payload.get("forward_only")):
        raise ValueError("Forward experiment metadata is not marked forward-only")
    if bool(payload.get("orders_enabled")):
        raise ValueError("Forward experiment metadata unexpectedly enables orders")
    return cutoff


def _render_dashboard(status: dict[str, Any]) -> str:
    cards: list[str] = []
    rows: list[str] = []
    for arm in ARMS:
        item = status["arms"][arm]
        cards.append(
            "<article>"
            f"<h2>{html.escape(arm)}</h2>"
            f"<b>{item['net_total_r']:.3f}R</b><span> net estimate</span>"
            f"<p>N {item['completed']} | Gross {item['gross_total_r']:.3f}R</p>"
            f"<p>Costs {item['estimated_cost_r']:.3f}R | Net PF {item['net_profit_factor']:.3f}</p>"
            f"<p>Net DD {item['net_max_drawdown_r']:.3f}R | Size {item['average_position_size_factor']:.3f}</p>"
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
<title>TradeMind v1.13.1 Risk Plan Backfill</title><style>
body{{background:#061724;color:#eaf7ff;font-family:Arial;margin:28px}}h1{{font-size:40px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(270px,1fr));gap:14px}}
article{{background:#0b2a40;border:1px solid #1c5878;border-radius:16px;padding:18px}}
article b{{font-size:32px}}article span{{color:#9bc4da}}table{{width:100%;border-collapse:collapse;margin-top:24px}}
th,td{{padding:9px;border-bottom:1px solid #17425b;text-align:left}}.ok{{color:#29e2a5}}.note{{color:#ffd479}}
</style></head><body><h1>Bybit Risk Plan Backfill v1.13.1</h1>
<p class='ok'>Historical replay only. Read-only. Orders OFF. Forward journals untouched.</p>
<p class='note'>Cutoff is exclusive: {html.escape(str(status['cutoff_exclusive']))}. Same-bar stop and target is counted as STOP first.</p>
<p>Historical STRICT_SELL candidates: {status['source_candidates']} | Completed base signals: {status['arms']['BASE_STRICT']['completed']}</p>
<section class='grid'>{''.join(cards)}</section>
<table><thead><tr><th>Arm</th><th>Signals</th><th>N</th><th>Wins</th><th>Losses</th><th>Timeouts</th>
<th>Gross R</th><th>Cost R</th><th>Net R</th><th>Net Avg</th><th>Net PF</th><th>Net DD</th><th>Size</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table></body></html>"""


@dataclass(frozen=True, slots=True)
class BackfillSummary:
    cutoff_ms: int
    source_candidates: int
    output_dir: Path
    arms: dict[str, dict[str, Any]]


def run_backfill(
    bars_path: Path,
    strict_decisions_path: Path,
    forward_meta_path: Path,
    output_dir: Path,
    *,
    fee_bps_per_side: float = DEFAULT_FEE_BPS_PER_SIDE,
    slippage_bps_per_side: float = DEFAULT_SLIPPAGE_BPS_PER_SIDE,
    now: datetime | None = None,
) -> BackfillSummary:
    captured_at = now or _now()
    cutoff_ms = _load_cutoff(forward_meta_path)
    bars = _normalized_bars(_load_csv(bars_path))
    if not bars:
        raise ValueError(f"No Bybit M5 bars found: {bars_path}")

    source_decisions = [
        row
        for row in _load_csv(strict_decisions_path)
        if _int(row.get("eligible")) == 1
        and str(row.get("action")) == "SELL"
        and _int(row.get("start_ms")) < cutoff_ms
    ]
    source_decisions = sorted(
        source_decisions,
        key=lambda row: (_int(row.get("start_ms")), str(row.get("decision_id"))),
    )
    if not source_decisions:
        raise ValueError("No historical STRICT_SELL candidates exist before the forward cutoff")

    by_symbol: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for bar in bars:
        by_symbol[str(bar["symbol"])].append(bar)

    output_dir.mkdir(parents=True, exist_ok=True)
    arm_status: dict[str, dict[str, Any]] = {}
    summary_rows: list[dict[str, Any]] = []
    for arm in ARMS:
        decisions: list[dict[str, Any]] = []
        signals: list[dict[str, Any]] = []
        for source in source_decisions:
            symbol_bars = by_symbol.get(str(source.get("symbol") or ""), [])
            plan = apply_risk_plan(
                source,
                arm,
                symbol_bars,
                fee_bps_per_side=fee_bps_per_side,
                slippage_bps_per_side=slippage_bps_per_side,
            )
            plan["reasons"] = " | ".join(
                part
                for part in (
                    str(plan.get("reasons") or ""),
                    f"BACKFILL_ONLY cutoff_exclusive={_iso_from_ms(cutoff_ms)}",
                )
                if part
            )
            decisions.append(plan)
            signal = _new_signal(plan, captured_at)
            signals.append(_update_outcome(signal, symbol_bars, captured_at))

        decisions = sorted(
            decisions,
            key=lambda row: (_int(row.get("start_ms")), str(row.get("decision_id"))),
        )
        signals = sorted(
            signals,
            key=lambda row: (_int(row.get("start_ms")), str(row.get("paper_signal_id"))),
        )
        arm_dir = output_dir / arm.lower()
        _atomic_csv(arm_dir / "decisions.csv", RISK_DECISION_FIELDS, decisions)
        _atomic_csv(arm_dir / "signals.csv", RISK_PAPER_FIELDS, signals)
        metrics = {
            **_metrics(signals),
            "orders_enabled": False,
            "historical_only": True,
            "signals_path": str(arm_dir / "signals.csv"),
        }
        arm_status[arm] = metrics
        arm_payload = {
            "schema_version": SCHEMA_VERSION,
            "state": "OK",
            "updated_at": captured_at.isoformat(),
            "mode": MODE,
            "arm": arm,
            "cutoff_exclusive": _iso_from_ms(cutoff_ms),
            **metrics,
        }
        _atomic_json(arm_dir / "status.json", arm_payload)
        summary_rows.append(
            {
                "schema_version": SCHEMA_VERSION,
                "captured_at": captured_at.isoformat(),
                "mode": MODE,
                "arm": arm,
                **metrics,
            }
        )

    dashboard = output_dir / "dashboard" / "index.html"
    start_values = [_int(row.get("start_ms")) for row in source_decisions]
    status = {
        "schema_version": SCHEMA_VERSION,
        "state": "OK",
        "updated_at": captured_at.isoformat(),
        "mode": MODE,
        "historical_only": True,
        "cutoff_ms": cutoff_ms,
        "cutoff_exclusive": _iso_from_ms(cutoff_ms),
        "source_started_at": _iso_from_ms(min(start_values)),
        "source_ended_at": _iso_from_ms(max(start_values)),
        "source_candidates": len(source_decisions),
        "source_decisions_path": str(strict_decisions_path),
        "forward_meta_path": str(forward_meta_path),
        "forward_journals_modified": False,
        "orders_enabled": False,
        "logic_changed": False,
        "same_entry_logic": True,
        "same_bar_rule": "STOP_FIRST_CONSERVATIVE",
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
    _atomic_csv(output_dir / "comparison.csv", SUMMARY_FIELDS, summary_rows)
    _atomic_json(output_dir / "status.json", status)
    _atomic_text(dashboard, _render_dashboard(status))
    return BackfillSummary(
        cutoff_ms=cutoff_ms,
        source_candidates=len(source_decisions),
        output_dir=output_dir,
        arms=arm_status,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="TradeMind v1.13.1 risk-plan backfill")
    parser.add_argument("--bars", type=Path, default=Path("data/bybit_v1_9/bybit_bars.csv"))
    parser.add_argument(
        "--strict-decisions",
        type=Path,
        default=Path("data/bybit_shadow_v1_11/strict_sell/decisions.csv"),
    )
    parser.add_argument(
        "--forward-meta",
        type=Path,
        default=Path("data/bybit_risk_plans_v1_13/experiment_meta.json"),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("data/bybit_risk_plans_backfill_v1_13_1"),
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
        summary = run_backfill(
            args.bars.expanduser().resolve(),
            args.strict_decisions.expanduser().resolve(),
            args.forward_meta.expanduser().resolve(),
            args.output_dir.expanduser().resolve(),
            fee_bps_per_side=args.fee_bps_per_side,
            slippage_bps_per_side=args.slippage_bps_per_side,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"Risk-plan backfill failed: {exc}")
        return 1

    print("TradeMind v1.13.1 Risk Plan Backfill")
    print("Historical STRICT_SELL only. Forward journals untouched. Read-only. No orders.")
    print(f"Cutoff exclusive: {_iso_from_ms(summary.cutoff_ms)}")
    print(f"Historical candidates: {summary.source_candidates}")
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
