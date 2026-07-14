"""Command-line performance summary for the TradeMind signal journal."""

from __future__ import annotations

import argparse
import csv
import os
import re
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

_HORIZON_PATTERN = re.compile(r"^outcome_(\d+)$")
_TIMEFRAME_PATTERN = re.compile(r"^(M|H|D)(\d+)$")


def _float(row: dict[str, str], key: str) -> float:
    value = row.get(key, "").strip()
    return float(value) if value else 0.0


def _confidence_bucket(row: dict[str, str]) -> str:
    confidence = int(float(row.get("confidence") or 0))
    if confidence >= 90:
        return "90-100"
    if confidence >= 70:
        return "70-89"
    if confidence >= 50:
        return "50-69"
    return "35-49"


def _bar_duration(timeframe: str) -> timedelta:
    match = _TIMEFRAME_PATTERN.fullmatch(timeframe.strip().upper())
    if not match:
        raise ValueError(f"Unsupported timeframe for non-overlap filtering: {timeframe}")
    unit, value_text = match.groups()
    value = int(value_text)
    if value <= 0:
        raise ValueError(f"Invalid timeframe: {timeframe}")
    if unit == "M":
        return timedelta(minutes=value)
    if unit == "H":
        return timedelta(hours=value)
    return timedelta(days=value)


def _non_overlapping(rows: list[dict[str, str]], horizon: int) -> list[dict[str, str]]:
    """Keep at most one open fixed-horizon trade per symbol."""
    selected: list[dict[str, str]] = []
    next_allowed: dict[str, datetime] = {}

    def sort_key(row: dict[str, str]) -> datetime:
        return datetime.fromisoformat(row["signal_time"])

    for row in sorted(rows, key=sort_key):
        if row.get("action") not in {"BUY", "SELL"}:
            continue
        symbol = row.get("symbol", "UNKNOWN").upper()
        signal_time = datetime.fromisoformat(row["signal_time"])
        if signal_time < next_allowed.get(symbol, datetime.min.replace(tzinfo=signal_time.tzinfo)):
            continue
        selected.append(row)
        next_allowed[symbol] = signal_time + _bar_duration(row.get("timeframe", "M5")) * horizon
    return selected


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
    normalized_values = [
        _float(row, f"net_move_{horizon}") / atr
        for row in evaluated
        if (atr := _float(row, "atr")) > 0
    ]
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
        "avg_net_atr": (
            sum(normalized_values) / len(normalized_values) if normalized_values else 0.0
        ),
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
        f"{label:<16} trades={int(metrics['total']):>4} "
        f"wins={int(metrics['wins']):>4} losses={int(metrics['losses']):>4} "
        f"win_rate={metrics['win_rate']:>6.2f}% PF={pf_text:>6} "
        f"avg_net={metrics['avg_net']:.6f} avg_net/ATR={metrics['avg_net_atr']:.3f} "
        f"avg_MFE={metrics['avg_mfe']:.6f} avg_MAE={metrics['avg_mae']:.6f}"
    )


def _apply_filters(
    rows: list[dict[str, str]], symbol: str | None, min_confidence: int
) -> list[dict[str, str]]:
    filtered = rows
    if symbol:
        symbol_name = symbol.upper()
        filtered = [row for row in filtered if row.get("symbol", "").upper() == symbol_name]
    if min_confidence:
        filtered = [
            row for row in filtered if int(float(row.get("confidence") or 0)) >= min_confidence
        ]
    return filtered


def _report_groups(
    rows: list[dict[str, str]],
    horizon: int,
    *,
    group_confidence: bool,
    group_action: bool,
    non_overlap: bool,
) -> None:
    def prepared(group_rows: list[dict[str, str]]) -> list[dict[str, str]]:
        return _non_overlapping(group_rows, horizon) if non_overlap else group_rows

    if group_confidence:
        print("  Confidence buckets")
        grouped_confidence: dict[str, list[dict[str, str]]] = defaultdict(list)
        for row in rows:
            grouped_confidence[_confidence_bucket(row)].append(row)
        for bucket in ("35-49", "50-69", "70-89", "90-100"):
            if bucket in grouped_confidence:
                _print_metrics(f"CONF {bucket}", _metrics(prepared(grouped_confidence[bucket]), horizon))

    if group_action:
        print("  Direction")
        for action in ("BUY", "SELL"):
            action_rows = [row for row in rows if row.get("action") == action]
            _print_metrics(action, _metrics(prepared(action_rows), horizon))


def main() -> int:
    parser = argparse.ArgumentParser(description="Summarize evaluated TradeMind signals")
    parser.add_argument(
        "--journal",
        type=Path,
        default=Path(os.getenv("TRADEMIND_JOURNAL_DIR", "data/journal")) / "signals.csv",
        help="Path to signals.csv",
    )
    parser.add_argument("--horizon", type=int, help="Show one forward horizon only")
    parser.add_argument("--symbol", help="Limit report to one symbol, for example XAUUSD")
    parser.add_argument(
        "--min-confidence",
        type=int,
        default=0,
        help="Ignore signals below this confidence",
    )
    parser.add_argument(
        "--non-overlap",
        action="store_true",
        help="Count only one fixed-horizon position per symbol at a time",
    )
    parser.add_argument(
        "--group-confidence",
        action="store_true",
        help="Show 35-49, 50-69, 70-89 and 90-100 confidence buckets",
    )
    parser.add_argument(
        "--group-action",
        action="store_true",
        help="Show BUY and SELL separately",
    )
    args = parser.parse_args()

    if args.min_confidence < 0 or args.min_confidence > 100:
        parser.error("--min-confidence must be between 0 and 100")

    journal_path = args.journal.expanduser().resolve()
    if not journal_path.is_file():
        print(f"Signal journal not found: {journal_path}")
        return 1

    with journal_path.open("r", encoding="utf-8", newline="") as handle:
        rows = [dict(row) for row in csv.DictReader(handle)]

    rows = _apply_filters(rows, args.symbol, args.min_confidence)
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
        print("Journal exists, but there are no matching evaluated signals yet.")
        return 0

    mode = "NON-OVERLAPPING" if args.non_overlap else "RAW OVERLAPPING"
    print(f"TradeMind journal: {journal_path}")
    print(f"Matching rows: {len(rows)}")
    print(f"Mode: {mode}")
    if args.symbol:
        print(f"Symbol: {args.symbol.upper()}")
    if args.min_confidence:
        print(f"Minimum confidence: {args.min_confidence}")

    for horizon in horizons:
        print(f"\nForward horizon: {horizon} candles")
        report_rows = _non_overlapping(rows, horizon) if args.non_overlap else rows
        _print_metrics("FILTERED", _metrics(report_rows, horizon))

        if not args.symbol:
            grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
            for row in rows:
                grouped[row.get("symbol", "UNKNOWN")].append(row)
            for symbol in sorted(grouped):
                symbol_rows = (
                    _non_overlapping(grouped[symbol], horizon)
                    if args.non_overlap
                    else grouped[symbol]
                )
                _print_metrics(symbol, _metrics(symbol_rows, horizon))

        _report_groups(
            rows,
            horizon,
            group_confidence=args.group_confidence,
            group_action=args.group_action,
            non_overlap=args.non_overlap,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
