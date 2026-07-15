"""Command-line research report for observation-only SMC features."""

from __future__ import annotations

import argparse
import csv
import os
import re
from collections import defaultdict
from pathlib import Path

from trademind.stats import _float, _non_overlapping

_HORIZON_PATTERN = re.compile(r"^outcome_(\d+)$")
_EVENT_ORDER = (
    "ANY_SMC_EVENT",
    "INTERNAL_BOS",
    "INTERNAL_CHOCH",
    "SWING_BOS",
    "SWING_CHOCH",
    "BSL_SWEEP",
    "SSL_SWEEP",
    "BULLISH_FVG",
    "BEARISH_FVG",
)
_CONTEXT_ORDER = (
    "HIGH_VOLUME",
    "NORMAL_VOLUME",
    "LOW_SPREAD",
    "HIGH_SPREAD",
    "STRUCTURE_ALIGNED",
    "STRUCTURE_CONFLICT",
)


def _truthy(row: dict[str, str], key: str) -> bool:
    return row.get(key, "").strip().lower() in {"1", "true", "yes", "y"}


def _break_label(scope: str, value: str) -> str | None:
    normalized = value.strip().upper()
    if normalized.endswith("_BOS"):
        return f"{scope}_BOS"
    if normalized.endswith("_CHOCH"):
        return f"{scope}_CHOCH"
    return None


def _event_labels(row: dict[str, str]) -> set[str]:
    labels: set[str] = set()

    internal = _break_label("INTERNAL", row.get("internal_break", ""))
    swing = _break_label("SWING", row.get("swing_break", ""))
    if internal:
        labels.add(internal)
    if swing:
        labels.add(swing)
    if _truthy(row, "bsl_sweep"):
        labels.add("BSL_SWEEP")
    if _truthy(row, "ssl_sweep"):
        labels.add("SSL_SWEEP")

    fvg = row.get("fvg_direction", "").strip().upper()
    if fvg == "BULLISH":
        labels.add("BULLISH_FVG")
    elif fvg == "BEARISH":
        labels.add("BEARISH_FVG")

    if labels:
        labels.add("ANY_SMC_EVENT")
    return labels


def _structure_relation(row: dict[str, str]) -> str | None:
    internal = row.get("internal_bias", "").strip().upper()
    swing = row.get("swing_bias", "").strip().upper()
    directional = {"BULLISH", "BEARISH"}
    if internal not in directional or swing not in directional:
        return None
    return "STRUCTURE_ALIGNED" if internal == swing else "STRUCTURE_CONFLICT"


def _sample_status(trades: int, minimum: int) -> str:
    return "RESEARCH_SAMPLE" if trades >= minimum else "INSUFFICIENT_SAMPLE"


def _prepared(
    rows: list[dict[str, str]],
    horizon: int,
    non_overlap: bool,
) -> list[dict[str, str]]:
    return _non_overlapping(rows, horizon) if non_overlap else rows


def _normalized_net(row: dict[str, str], horizon: int) -> float | None:
    stored = row.get(f"progress_atr_{horizon}", "").strip()
    if stored:
        return float(stored)
    atr = _float(row, "atr")
    if atr <= 0:
        return None
    return _float(row, f"net_move_{horizon}") / atr


def _normalized_metrics(rows: list[dict[str, str]], horizon: int) -> dict[str, float]:
    """Return metrics that remain comparable across instruments and price scales."""
    evaluated = [
        row
        for row in rows
        if row.get("action") in {"BUY", "SELL"}
        and row.get(f"outcome_{horizon}") in {"WIN", "LOSS", "FLAT"}
    ]
    values = [
        value
        for row in evaluated
        if (value := _normalized_net(row, horizon)) is not None
    ]
    wins = sum(row[f"outcome_{horizon}"] == "WIN" for row in evaluated)
    losses = sum(row[f"outcome_{horizon}"] == "LOSS" for row in evaluated)
    positive = sum(value for value in values if value > 0)
    negative = abs(sum(value for value in values if value < 0))
    decided = wins + losses
    return {
        "total": float(len(evaluated)),
        "wins": float(wins),
        "losses": float(losses),
        "win_rate": wins / decided * 100.0 if decided else 0.0,
        "avg_net_atr": sum(values) / len(values) if values else 0.0,
        "profit_factor_atr": (
            positive / negative if negative else (float("inf") if positive else 0.0)
        ),
    }


def _print_group(
    label: str,
    rows: list[dict[str, str]],
    horizon: int,
    *,
    minimum_sample: int,
    non_overlap: bool,
) -> None:
    metrics = _normalized_metrics(_prepared(rows, horizon, non_overlap), horizon)
    trades = int(metrics["total"])
    profit_factor = metrics["profit_factor_atr"]
    pf_text = "inf" if profit_factor == float("inf") else f"{profit_factor:.2f}"
    print(
        f"{label:<22} observations={len(rows):>5} trades={trades:>5} "
        f"status={_sample_status(trades, minimum_sample):<19} "
        f"WR={metrics['win_rate']:>6.2f}% PF_ATR={pf_text:>6} "
        f"avg_net/ATR={metrics['avg_net_atr']:>8.3f}"
    )


def _event_groups(rows: list[dict[str, str]]) -> dict[str, list[dict[str, str]]]:
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        for label in _event_labels(row):
            grouped[label].append(row)
    return grouped


def _context_groups(
    rows: list[dict[str, str]],
    *,
    volume_threshold: float,
    spread_atr_threshold: float,
) -> dict[str, list[dict[str, str]]]:
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        volume_ratio = _float(row, "volume_ratio_20")
        if volume_ratio > 0:
            volume_label = "HIGH_VOLUME" if volume_ratio >= volume_threshold else "NORMAL_VOLUME"
            grouped[volume_label].append(row)

        spread_atr = _float(row, "spread_cost_atr")
        if spread_atr > 0:
            spread_label = (
                "HIGH_SPREAD" if spread_atr >= spread_atr_threshold else "LOW_SPREAD"
            )
            grouped[spread_label].append(row)

        relation = _structure_relation(row)
        if relation:
            grouped[relation].append(row)
    return grouped


def _apply_filters(
    rows: list[dict[str, str]],
    *,
    symbol: str | None,
    schema_version: str,
) -> list[dict[str, str]]:
    filtered = [row for row in rows if row.get("schema_version") == schema_version]
    if symbol:
        symbol_name = symbol.upper()
        filtered = [row for row in filtered if row.get("symbol", "").upper() == symbol_name]
    return filtered


def _report_scope(
    rows: list[dict[str, str]],
    horizon: int,
    *,
    minimum_sample: int,
    non_overlap: bool,
    volume_threshold: float,
    spread_atr_threshold: float,
) -> None:
    event_groups = _event_groups(rows)
    context_groups = _context_groups(
        rows,
        volume_threshold=volume_threshold,
        spread_atr_threshold=spread_atr_threshold,
    )

    print("  SMC events")
    for label in _EVENT_ORDER:
        group_rows = event_groups.get(label, [])
        if group_rows:
            _print_group(
                label,
                group_rows,
                horizon,
                minimum_sample=minimum_sample,
                non_overlap=non_overlap,
            )

    print("  Context cuts")
    for label in _CONTEXT_ORDER:
        group_rows = context_groups.get(label, [])
        if group_rows:
            _print_group(
                label,
                group_rows,
                horizon,
                minimum_sample=minimum_sample,
                non_overlap=non_overlap,
            )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Summarize observation-only TradeMind SMC features"
    )
    default_journal = (
        Path(os.getenv("TRADEMIND_JOURNAL_DIR", "data/journal_ecn")) / "signals.csv"
    )
    parser.add_argument("--journal", type=Path, default=default_journal)
    parser.add_argument("--symbol", help="Limit report to one symbol")
    parser.add_argument("--horizon", type=int, help="Show one forward horizon only")
    parser.add_argument("--schema-version", default="1.1")
    parser.add_argument("--min-sample", type=int, default=300)
    parser.add_argument("--volume-threshold", type=float, default=1.2)
    parser.add_argument("--spread-atr-threshold", type=float, default=0.10)
    parser.add_argument(
        "--non-overlap",
        action="store_true",
        help="Count only one fixed-horizon position per symbol at a time",
    )
    parser.add_argument(
        "--by-symbol",
        action="store_true",
        help="Show complete event and context sections separately for every symbol",
    )
    args = parser.parse_args()

    if args.min_sample < 1:
        parser.error("--min-sample must be at least 1")
    if args.volume_threshold <= 0:
        parser.error("--volume-threshold must be greater than zero")
    if args.spread_atr_threshold <= 0:
        parser.error("--spread-atr-threshold must be greater than zero")
    if args.symbol and args.by_symbol:
        parser.error("--symbol and --by-symbol cannot be used together")

    journal_path = args.journal.expanduser().resolve()
    if not journal_path.is_file():
        print(f"Signal journal not found: {journal_path}")
        return 1

    with journal_path.open("r", encoding="utf-8", newline="") as handle:
        rows = [dict(row) for row in csv.DictReader(handle)]

    rows = _apply_filters(
        rows,
        symbol=args.symbol,
        schema_version=args.schema_version,
    )
    if not rows:
        print("Journal exists, but there are no matching SMC observations yet.")
        return 0

    horizons = sorted(
        {
            int(match.group(1))
            for key in rows[0]
            if (match := _HORIZON_PATTERN.fullmatch(key))
        }
    )
    if args.horizon is not None:
        horizons = [args.horizon]
    if not horizons:
        print("SMC observations exist, but no forward-outcome fields were found.")
        return 0

    mode = "NON-OVERLAPPING" if args.non_overlap else "RAW OVERLAPPING"
    print(f"TradeMind SMC journal: {journal_path}")
    print(f"Matching observations: {len(rows)}")
    print(f"Schema version: {args.schema_version}")
    print(f"Mode: {mode}")
    print("Cross-symbol PF unit: ATR-normalized net movement")
    print(f"Minimum research sample: {args.min_sample} evaluated trades")
    print(
        f"Context thresholds: volume>={args.volume_threshold:.2f}x mean, "
        f"spread>={args.spread_atr_threshold:.3f} ATR"
    )
    if args.symbol:
        print(f"Symbol: {args.symbol.upper()}")

    grouped_symbols: dict[str, list[dict[str, str]]] = defaultdict(list)
    if args.by_symbol:
        for row in rows:
            grouped_symbols[row.get("symbol", "UNKNOWN").upper()].append(row)

    for horizon in horizons:
        print(f"\nForward horizon: {horizon} candles")
        print("Portfolio-normalized overview")
        _report_scope(
            rows,
            horizon,
            minimum_sample=args.min_sample,
            non_overlap=args.non_overlap,
            volume_threshold=args.volume_threshold,
            spread_atr_threshold=args.spread_atr_threshold,
        )

        if args.by_symbol:
            for symbol_name in sorted(grouped_symbols):
                print(f"\nSymbol detail: {symbol_name}")
                _report_scope(
                    grouped_symbols[symbol_name],
                    horizon,
                    minimum_sample=args.min_sample,
                    non_overlap=args.non_overlap,
                    volume_threshold=args.volume_threshold,
                    spread_atr_threshold=args.spread_atr_threshold,
                )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
