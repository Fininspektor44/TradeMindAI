"""Command-line performance summary for the TradeMind signal journal."""

from __future__ import annotations

import argparse
import csv
import os
import re
from collections import defaultdict
from pathlib import Path

_HORIZON_PATTERN = re.compile(r"^outcome_(\d+)$")


def _float(row: dict[str, str], key: str) -> float:
    value = row.get(key, "").strip()
    return float(value) if value else 0.0


def _metrics(rows: list[dict[str, str]], horizon: int) -> dict[str, float]:
    evaluated = [
        row
        for row in rows
        if row.get("action") in {"BUY", "SELL"}
        and row.get(f"outcome_{horizon}") in {"WIN", "LOSS", "FLAT"}
    ]
    wins = sum(row[f"outcome_{horizon}"] == "WIN" for row in evaluated)
    losses = sum(row[f"outcome_{horizon}"] == "LOSS" for row in evaluated)
    flats = len(evaluated) - wins - losses
    net_values = [_float(row, f"net_move_{horizon}") for row in evaluated]
    positive = sum(value for value in net_values if value > 0)
    negative = abs(sum(value for value in net_values if value < 0))
    decided = wins + losses
    return {
        "total": float(len(evaluated)),
        "wins": float(wins),
        "losses": float(losses),
        "flats": float(flats),
        "win_rate": (wins / decided * 100.0) if decided else 0.0,
        "avg_net": (sum(net_values) / len(net_values)) if net_values else 0.0,
        "profit_factor": (positive / negative) if negative else (float("inf") if positive else 0.0),
        "avg_mfe": (
            sum(_float(row, f"mfe_{horizon}") for row in evaluated) / len(evaluated)
            if evaluated
            else 0.0
        ),
        "avg_mae": (
            sum(_float(row, f"mae_{horizon}") for row in evaluated) / len(evaluated)
            if evaluated
            else 0.0
        ),
    }


def _print_metrics(label: str, metrics: dict[str, float]) -> None:
    profit_factor = metrics["profit_factor"]
    pf_text = "inf" if profit_factor == float("inf") else f"{profit_factor:.2f}"
    print(
        f"{label:<12} trades={int(metrics['total']):>4} "
        f"wins={int(metrics['wins']):>4} losses={int(metrics['losses']):>4} "
        f"win_rate={metrics['win_rate']:>6.2f}% PF={pf_text:>6} "
        f"avg_net={metrics['avg_net']:.6f} "
        f"avg_MFE={metrics['avg_mfe']:.6f} avg_MAE={metrics['avg_mae']:.6f}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Summarize evaluated TradeMind signals")
    parser.add_argument(
        "--journal",
        type=Path,
        default=Path(os.getenv("TRADEMIND_JOURNAL_DIR", "data/journal")) / "signals.csv",
        help="Path to signals.csv",
    )
    parser.add_argument("--horizon", type=int, help="Show one forward horizon only")
    args = parser.parse_args()

    journal_path = args.journal.expanduser().resolve()
    if not journal_path.is_file():
        print(f"Signal journal not found: {journal_path}")
        return 1

    with journal_path.open("r", encoding="utf-8", newline="") as handle:
        rows = [dict(row) for row in csv.DictReader(handle)]

    horizons = sorted(
        {
            int(match.group(1))
            for key in (rows[0].keys() if rows else ())
            if (match := _HORIZON_PATTERN.match(key))
        }
    )
    if args.horizon is not None:
        horizons = [args.horizon]

    if not rows or not horizons:
        print("Journal exists, but there are no evaluated signals yet.")
        return 0

    print(f"TradeMind journal: {journal_path}")
    print(f"Recorded rows: {len(rows)}")
    for horizon in horizons:
        print(f"\nForward horizon: {horizon} candles")
        _print_metrics("ALL", _metrics(rows, horizon))
        grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
        for row in rows:
            grouped[row.get("symbol", "UNKNOWN")].append(row)
        for symbol in sorted(grouped):
            _print_metrics(symbol, _metrics(grouped[symbol], horizon))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
