"""Validate TradeMind research patterns across time without changing signal weights."""

from __future__ import annotations

import argparse
import csv
import os
import statistics
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

from trademind.smc_stats import (
    _CONTEXT_ORDER,
    _EVENT_ORDER,
    _context_groups,
    _event_groups,
    _normalized_metrics,
    _normalized_net,
    _prepared,
)

_VALID_OUTCOMES = {"WIN", "LOSS", "FLAT"}


@dataclass(frozen=True)
class SegmentMetrics:
    trades: int
    win_rate: float
    profit_factor_atr: float
    avg_net_atr: float


@dataclass(frozen=True)
class ValidationResult:
    status: str
    total: SegmentMetrics
    early: SegmentMetrics
    late: SegmentMetrics
    max_drawdown_atr: float
    max_loss_streak: int
    mean_ci_low: float
    mean_ci_high: float
    reasons: tuple[str, ...]

    @property
    def stable(self) -> bool:
        return self.status in {"RESEARCH_CANDIDATE", "VALIDATED"}


@dataclass(frozen=True)
class PatternValidation:
    symbol: str
    label: str
    horizon: int
    observations: int
    result: ValidationResult


def _signal_time(row: dict[str, str]) -> datetime:
    return datetime.fromisoformat(row["signal_time"])


def _evaluated_rows(rows: list[dict[str, str]], horizon: int) -> list[dict[str, str]]:
    prepared = _prepared(rows, horizon, True)
    evaluated = [
        row
        for row in prepared
        if row.get("action") in {"BUY", "SELL"}
        and row.get(f"outcome_{horizon}") in _VALID_OUTCOMES
        and _normalized_net(row, horizon) is not None
    ]
    return sorted(evaluated, key=_signal_time)


def _segment_metrics(rows: list[dict[str, str]], horizon: int) -> SegmentMetrics:
    metrics = _normalized_metrics(rows, horizon)
    return SegmentMetrics(
        trades=int(metrics["total"]),
        win_rate=metrics["win_rate"],
        profit_factor_atr=metrics["profit_factor_atr"],
        avg_net_atr=metrics["avg_net_atr"],
    )


def _values(rows: list[dict[str, str]], horizon: int) -> list[float]:
    return [
        value
        for row in rows
        if (value := _normalized_net(row, horizon)) is not None
    ]


def _mean_ci95(values: list[float]) -> tuple[float, float]:
    if not values:
        return 0.0, 0.0
    mean = statistics.fmean(values)
    if len(values) < 2:
        return mean, mean
    margin = 1.96 * statistics.stdev(values) / (len(values) ** 0.5)
    return mean - margin, mean + margin


def _max_drawdown(values: list[float]) -> float:
    equity = 0.0
    peak = 0.0
    drawdown = 0.0
    for value in values:
        equity += value
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    return drawdown


def _max_loss_streak(values: list[float]) -> int:
    longest = 0
    current = 0
    for value in values:
        if value < 0:
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    return longest


def validate_rows(
    rows: list[dict[str, str]],
    horizon: int,
    *,
    candidate_minimum: int = 30,
    research_minimum: int = 300,
) -> ValidationResult:
    """Validate one feature group using chronological non-overlapping trades."""
    evaluated = _evaluated_rows(rows, horizon)
    midpoint = len(evaluated) // 2
    early_rows = evaluated[:midpoint]
    late_rows = evaluated[midpoint:]
    values = _values(evaluated, horizon)

    total = _segment_metrics(evaluated, horizon)
    early = _segment_metrics(early_rows, horizon)
    late = _segment_metrics(late_rows, horizon)
    ci_low, ci_high = _mean_ci95(values)
    half_minimum = max(10, candidate_minimum // 3)
    reasons: list[str] = []

    if total.trades < candidate_minimum:
        reasons.append(f"need at least {candidate_minimum} non-overlapping trades")
        status = "INSUFFICIENT_SAMPLE"
    elif early.trades < half_minimum or late.trades < half_minimum:
        reasons.append(f"need at least {half_minimum} trades in both time halves")
        status = "INSUFFICIENT_SAMPLE"
    else:
        if total.avg_net_atr <= 0 or total.profit_factor_atr <= 1.0:
            reasons.append("overall result is not positive after spread")
        if early.avg_net_atr <= 0 or early.profit_factor_atr <= 1.0:
            reasons.append("early half is not positive")
        if late.avg_net_atr <= 0 or late.profit_factor_atr <= 1.0:
            reasons.append("late half is not positive")

        if reasons:
            status = "UNSTABLE"
        elif total.trades >= research_minimum and ci_low > 0:
            status = "VALIDATED"
        else:
            if total.trades < research_minimum:
                reasons.append(f"research threshold is {research_minimum} trades")
            if ci_low <= 0:
                reasons.append("95% mean interval still includes zero")
            status = "RESEARCH_CANDIDATE"

    return ValidationResult(
        status=status,
        total=total,
        early=early,
        late=late,
        max_drawdown_atr=_max_drawdown(values),
        max_loss_streak=_max_loss_streak(values),
        mean_ci_low=ci_low,
        mean_ci_high=ci_high,
        reasons=tuple(reasons),
    )


def portfolio_only(result: ValidationResult) -> ValidationResult:
    """Mark a cross-symbol aggregate as informational rather than a tradable pattern."""
    return replace(
        result,
        status="PORTFOLIO_ONLY",
        reasons=("cross-symbol aggregate is informational only",),
    )


def _feature_groups(
    rows: list[dict[str, str]],
    *,
    volume_threshold: float,
    spread_atr_threshold: float,
) -> list[tuple[str, list[dict[str, str]]]]:
    event_groups = _event_groups(rows)
    context_groups = _context_groups(
        rows,
        volume_threshold=volume_threshold,
        spread_atr_threshold=spread_atr_threshold,
    )
    result: list[tuple[str, list[dict[str, str]]]] = []
    for label in _EVENT_ORDER:
        if group := event_groups.get(label):
            result.append((label, group))
    for label in _CONTEXT_ORDER:
        if group := context_groups.get(label):
            result.append((label, group))
    return result


def validate_symbol_patterns(
    symbol: str,
    rows: list[dict[str, str]],
    horizons: list[int],
    *,
    candidate_minimum: int,
    research_minimum: int,
    volume_threshold: float,
    spread_atr_threshold: float,
) -> list[PatternValidation]:
    output: list[PatternValidation] = []
    for label, group in _feature_groups(
        rows,
        volume_threshold=volume_threshold,
        spread_atr_threshold=spread_atr_threshold,
    ):
        for horizon in horizons:
            output.append(
                PatternValidation(
                    symbol=symbol.upper(),
                    label=label,
                    horizon=horizon,
                    observations=len(group),
                    result=validate_rows(
                        group,
                        horizon,
                        candidate_minimum=candidate_minimum,
                        research_minimum=research_minimum,
                    ),
                )
            )
    return output


def _pf(value: float) -> str:
    return "inf" if value == float("inf") else f"{value:.2f}"


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate TradeMind SMC patterns across time")
    default_journal = Path(os.getenv("TRADEMIND_JOURNAL_DIR", "data/journal_ecn")) / "signals.csv"
    parser.add_argument("--journal", type=Path, default=default_journal)
    parser.add_argument("--symbol", help="Limit validation to one symbol")
    parser.add_argument("--horizon", type=int, action="append", help="Repeat to select horizons")
    parser.add_argument("--schema-version", default="1.1")
    parser.add_argument("--candidate-min", type=int, default=30)
    parser.add_argument("--min-sample", type=int, default=300)
    parser.add_argument("--volume-threshold", type=float, default=1.2)
    parser.add_argument("--spread-atr-threshold", type=float, default=0.10)
    args = parser.parse_args()

    if args.candidate_min < 2:
        parser.error("--candidate-min must be at least 2")
    if args.min_sample < args.candidate_min:
        parser.error("--min-sample must be at least --candidate-min")
    if not args.journal.is_file():
        print(f"Signal journal not found: {args.journal.resolve()}")
        return 1

    with args.journal.open("r", encoding="utf-8", newline="") as handle:
        rows = [
            dict(row)
            for row in csv.DictReader(handle)
            if row.get("schema_version", "").strip() == args.schema_version
        ]
    if args.symbol:
        name = args.symbol.upper()
        rows = [row for row in rows if row.get("symbol", "").upper() == name]

    by_symbol: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        by_symbol[row.get("symbol", "UNKNOWN").upper()].append(row)
    horizons = sorted(set(args.horizon or [3, 6, 12]))

    print(f"TradeMind validation journal: {args.journal.resolve()}")
    print(f"Candidate minimum: {args.candidate_min}; research minimum: {args.min_sample}")
    print("Validation is per symbol only; cross-symbol aggregates are excluded.")
    for symbol in sorted(by_symbol):
        print(f"\nSymbol: {symbol}")
        validations = validate_symbol_patterns(
            symbol,
            by_symbol[symbol],
            horizons,
            candidate_minimum=args.candidate_min,
            research_minimum=args.min_sample,
            volume_threshold=args.volume_threshold,
            spread_atr_threshold=args.spread_atr_threshold,
        )
        for item in validations:
            result = item.result
            print(
                f"{item.label:<20} H{item.horizon:<2} trades={result.total.trades:>4} "
                f"status={result.status:<20} WR={result.total.win_rate:>6.2f}% "
                f"PF_ATR={_pf(result.total.profit_factor_atr):>6} "
                f"avg={result.total.avg_net_atr:>7.3f} "
                f"early={result.early.avg_net_atr:>7.3f} late={result.late.avg_net_atr:>7.3f} "
                f"DD={result.max_drawdown_atr:>7.3f} Lstreak={result.max_loss_streak:>2} "
                f"CI95=[{result.mean_ci_low:.3f},{result.mean_ci_high:.3f}]"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
