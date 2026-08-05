"""Coverage-aware public wrapper around the grid basket analytics engine."""

from __future__ import annotations

import argparse
import csv
import json
import os
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

from trademind.grid_basket_analytics import (
    BASKET_FIELDS,
    LEG_RISK_FIELDS,
    SYMBOL_FIELDS,
    GridAnalyticsSummary,
    _atomic_csv,
    _atomic_json,
    _atomic_text,
    _float,
    _int,
    _render_dashboard,
    _text,
    run_grid_analytics,
)


def _read_csv(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def _optional_max(rows: Sequence[dict[str, Any]], field: str) -> float | str:
    values = [_float(row.get(field)) for row in rows if _text(row.get(field))]
    return max(values) if values else ""


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _drawdown_map(legs_path: Path) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in _read_csv(legs_path):
        grouped[_text(row.get("basket_id"))].append(row)
    result: dict[str, dict[str, Any]] = {}
    for basket_id, rows in grouped.items():
        first = rows[0]
        result[basket_id] = {
            "robot": _text(first.get("robot")),
            "magic": _text(first.get("magic")),
            "symbol": _text(first.get("symbol")),
            "side": _text(first.get("side")).upper(),
            "max_legs": max((_int(row.get("leg_no")) for row in rows), default=0),
            "money": _optional_max(rows, "max_drawdown_money"),
            "pct": _optional_max(rows, "max_drawdown_pct"),
            "adverse": _optional_max(rows, "max_adverse_points"),
        }
    return result


def _patch_outputs(
    legs_path: Path,
    output_dir: Path,
    status: dict[str, Any],
) -> dict[str, Any]:
    dd = _drawdown_map(legs_path)
    history_path = output_dir / "basket_history.csv"
    risk_path = output_dir / "risk_by_leg.csv"
    symbol_path = output_dir / "symbol_report.csv"
    history = _read_csv(history_path)
    risk = _read_csv(risk_path)
    symbols = _read_csv(symbol_path)

    for row in history:
        item = dd.get(_text(row.get("basket_id")), {})
        row["max_drawdown_money"] = item.get("money", "")
        row["max_drawdown_pct"] = item.get("pct", "")
        row["max_adverse_points"] = item.get("adverse", "")

    for row in risk:
        leg_no = _int(row.get("leg_no"))
        reached = [item for item in dd.values() if _int(item.get("max_legs")) >= leg_no]
        money = [_float(item["money"]) for item in reached if _text(item.get("money"))]
        pct = [_float(item["pct"]) for item in reached if _text(item.get("pct"))]
        row["average_max_drawdown_money"] = round(_mean(money), 6) if money else ""
        row["worst_max_drawdown_money"] = round(max(money), 6) if money else ""
        row["average_max_drawdown_pct"] = round(_mean(pct), 6) if pct else ""
        row["worst_max_drawdown_pct"] = round(max(pct), 6) if pct else ""

    for row in symbols:
        key = (
            _text(row.get("robot")),
            _text(row.get("magic")),
            _text(row.get("symbol")),
            _text(row.get("side")).upper(),
        )
        group = [
            item
            for item in dd.values()
            if (item["robot"], item["magic"], item["symbol"], item["side"]) == key
        ]
        money = [_float(item["money"]) for item in group if _text(item.get("money"))]
        pct = [_float(item["pct"]) for item in group if _text(item.get("pct"))]
        row["average_max_drawdown_money"] = round(_mean(money), 6) if money else ""
        row["worst_max_drawdown_money"] = round(max(money), 6) if money else ""
        row["average_max_drawdown_pct"] = round(_mean(pct), 6) if pct else ""
        row["worst_max_drawdown_pct"] = round(max(pct), 6) if pct else ""

    money_covered = [item for item in dd.values() if _text(item.get("money"))]
    pct_covered = [item for item in dd.values() if _text(item.get("pct"))]
    total = len(dd)
    status["drawdown_coverage"] = len(money_covered) / total if total else 0.0
    status["drawdown_pct_coverage"] = len(pct_covered) / total if total else 0.0
    status["worst_drawdown_money"] = max(
        (_float(item["money"]) for item in money_covered), default=0.0
    )
    status["worst_drawdown_pct"] = max(
        (_float(item["pct"]) for item in pct_covered), default=0.0
    )
    status["drawdown_missing_is_zero"] = False

    _atomic_csv(history_path, BASKET_FIELDS, history)
    _atomic_csv(risk_path, LEG_RISK_FIELDS, risk)
    _atomic_csv(symbol_path, SYMBOL_FIELDS, symbols)
    _atomic_json(output_dir / "status.json", status)
    _atomic_text(output_dir / "dashboard" / "index.html", _render_dashboard(status, risk, symbols))
    return status


def run_grid_audit(
    legs_path: Path,
    output_dir: Path,
    *,
    now: datetime | None = None,
) -> GridAnalyticsSummary:
    summary = run_grid_analytics(legs_path, output_dir, now=now)
    status = _patch_outputs(legs_path, output_dir, dict(summary.status))
    return GridAnalyticsSummary(
        source_rows=summary.source_rows,
        baskets=summary.baskets,
        completed_baskets=summary.completed_baskets,
        output_dir=summary.output_dir,
        status=status,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="TradeMind v1.15 grid basket risk audit")
    parser.add_argument(
        "--legs",
        type=Path,
        default=Path("data/grid_basket_v1_15/basket_legs.csv"),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("data/grid_basket_analytics_v1_15"),
    )
    parser.add_argument("--open-dashboard", action="store_true")
    args = parser.parse_args(argv)
    try:
        summary = run_grid_audit(
            args.legs.expanduser().resolve(),
            args.output_dir.expanduser().resolve(),
        )
    except (OSError, ValueError, csv.Error, json.JSONDecodeError) as exc:
        print(f"Grid basket audit failed: {exc}")
        return 1

    status = summary.status
    print("TradeMind v1.15 Grid Basket Risk Audit")
    print("Read-only. Signal generation OFF. Orders OFF. Source files unchanged.")
    print(f"Baskets: {summary.baskets}, completed: {summary.completed_baskets}")
    print(f"Net: {status['net_profit']:.2f}, PF: {status['profit_factor']:.3f}")
    print(f"Drawdown coverage: {100 * status['drawdown_coverage']:.1f}%")
    if status["drawdown_coverage"] > 0:
        print(
            f"Worst measured DD: {status['worst_drawdown_money']:.2f} / "
            f"{status['worst_drawdown_pct']:.2f}%"
        )
    else:
        print("Worst DD: NOT MEASURED YET (snapshot collector required)")
    print(f"Output: {summary.output_dir}")
    if args.open_dashboard:
        os.startfile(summary.output_dir / "dashboard" / "index.html")  # type: ignore[attr-defined]
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
