"""Machine-readable signal statistics report for AI-agent consumption."""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence

from trademind.validation import PatternValidation, validate_symbol_patterns

REPORT_SCHEMA_VERSION = "signal-statistics-report-v1"
DEFAULT_HORIZONS = (3, 6, 12)


def _result_payload(item: PatternValidation) -> dict[str, object]:
    result = item.result
    return {
        "symbol": item.symbol,
        "pattern": item.label,
        "horizon": item.horizon,
        "observations": item.observations,
        "status": result.status,
        "trades": result.total.trades,
        "win_rate": result.total.win_rate,
        "profit_factor_atr": result.total.profit_factor_atr,
        "avg_net_atr": result.total.avg_net_atr,
        "early": {
            "trades": result.early.trades,
            "win_rate": result.early.win_rate,
            "profit_factor_atr": result.early.profit_factor_atr,
            "avg_net_atr": result.early.avg_net_atr,
        },
        "late": {
            "trades": result.late.trades,
            "win_rate": result.late.win_rate,
            "profit_factor_atr": result.late.profit_factor_atr,
            "avg_net_atr": result.late.avg_net_atr,
        },
        "max_drawdown_atr": result.max_drawdown_atr,
        "max_loss_streak": result.max_loss_streak,
        "mean_ci95": [result.mean_ci_low, result.mean_ci_high],
        "reasons": list(result.reasons),
    }


def build_report(
    rows: Iterable[dict[str, str]],
    *,
    horizons: Sequence[int] = DEFAULT_HORIZONS,
    schema_version: str = "1.1",
    candidate_minimum: int = 30,
    research_minimum: int = 300,
    volume_threshold: float = 1.2,
    spread_atr_threshold: float = 0.10,
    generated_at: datetime | None = None,
) -> dict[str, object]:
    """Return a stable JSON-ready snapshot without changing signal weights or execution state."""
    filtered = [
        dict(row)
        for row in rows
        if row.get("schema_version", "").strip() == schema_version
    ]
    by_symbol: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in filtered:
        by_symbol[row.get("symbol", "UNKNOWN").strip().upper()].append(row)

    validations: list[PatternValidation] = []
    selected_horizons = tuple(sorted(set(int(value) for value in horizons)))
    for symbol in sorted(by_symbol):
        validations.extend(
            validate_symbol_patterns(
                symbol,
                by_symbol[symbol],
                list(selected_horizons),
                candidate_minimum=candidate_minimum,
                research_minimum=research_minimum,
                volume_threshold=volume_threshold,
                spread_atr_threshold=spread_atr_threshold,
            )
        )

    pattern_payloads = [_result_payload(item) for item in validations]
    status_counts = Counter(str(item["status"]) for item in pattern_payloads)
    now = generated_at or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "generated_at": now.astimezone(timezone.utc).isoformat(),
        "source_schema_version": schema_version,
        "read_only": True,
        "orders_enabled": False,
        "journal_rows": len(filtered),
        "symbols": sorted(by_symbol),
        "horizons": list(selected_horizons),
        "thresholds": {
            "candidate_minimum": candidate_minimum,
            "research_minimum": research_minimum,
            "volume_threshold": volume_threshold,
            "spread_atr_threshold": spread_atr_threshold,
        },
        "status_counts": dict(sorted(status_counts.items())),
        "patterns": pattern_payloads,
    }


def load_report_from_journal(
    journal_path: Path,
    **kwargs: object,
) -> dict[str, object]:
    with journal_path.open("r", encoding="utf-8", newline="") as handle:
        rows = [dict(row) for row in csv.DictReader(handle)]
    return build_report(rows, **kwargs)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Export TradeMind validation results as machine-readable JSON"
    )
    parser.add_argument("--journal", type=Path, default=Path("data/journal_ecn/signals.csv"))
    parser.add_argument("--schema-version", default="1.1")
    parser.add_argument("--candidate-min", type=int, default=30)
    parser.add_argument("--min-sample", type=int, default=300)
    parser.add_argument("--horizon", type=int, action="append")
    parser.add_argument("--volume-threshold", type=float, default=1.2)
    parser.add_argument("--spread-atr-threshold", type=float, default=0.10)
    parser.add_argument("--output", type=Path, help="Optional JSON file. Stdout is used otherwise.")
    args = parser.parse_args(argv)

    if not args.journal.is_file():
        parser.error(f"journal not found: {args.journal}")
    if args.candidate_min < 2:
        parser.error("--candidate-min must be at least 2")
    if args.min_sample < args.candidate_min:
        parser.error("--min-sample must be at least --candidate-min")

    report = load_report_from_journal(
        args.journal,
        horizons=args.horizon or DEFAULT_HORIZONS,
        schema_version=args.schema_version,
        candidate_minimum=args.candidate_min,
        research_minimum=args.min_sample,
        volume_threshold=args.volume_threshold,
        spread_atr_threshold=args.spread_atr_threshold,
    )
    encoded = json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded + "\n", encoding="utf-8")
    else:
        print(encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
