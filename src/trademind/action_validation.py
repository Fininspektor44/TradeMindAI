"""Action-aware research validation for TradeMind AI v1.3.

The v1.2 validator grouped observations by instrument, feature and horizon. This
module adds the missing trade direction to the key and splits directional
structure events. It remains research-only and never sends orders.
"""

from __future__ import annotations

import argparse
import csv
import math
import os
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from statistics import NormalDist

_VALID_ACTIONS = ("BUY", "SELL")
_VALID_OUTCOMES = {"WIN", "LOSS", "FLAT"}
_TIMEFRAME_UNITS = {"M": "minutes", "H": "hours", "D": "days"}
_ACTION_STATE_FIELDS = (
    "captured_at",
    "symbol",
    "label",
    "action",
    "horizon",
    "observations",
    "trades",
    "trading_days",
    "status",
    "win_rate",
    "profit_factor_atr",
    "avg_net_atr",
    "early_avg_net_atr",
    "late_avg_net_atr",
    "late_to_early_ratio",
    "max_drawdown_atr",
    "max_loss_streak",
    "mean_ci_low",
    "mean_ci_high",
    "p_value",
    "q_value",
    "reasons",
)


@dataclass(frozen=True)
class SegmentMetrics:
    trades: int
    win_rate: float
    profit_factor_atr: float
    avg_net_atr: float


@dataclass(frozen=True)
class ActionValidationResult:
    status: str
    total: SegmentMetrics
    early: SegmentMetrics
    late: SegmentMetrics
    trading_days: int
    late_to_early_ratio: float
    max_drawdown_atr: float
    max_loss_streak: int
    mean_ci_low: float
    mean_ci_high: float
    p_value: float
    q_value: float
    reasons: tuple[str, ...]

    @property
    def stable(self) -> bool:
        return self.status in {"RESEARCH_CANDIDATE", "VALIDATED"}


@dataclass(frozen=True)
class ActionPatternValidation:
    symbol: str
    label: str
    action: str
    horizon: int
    observations: int
    result: ActionValidationResult

    @property
    def key(self) -> tuple[str, str, str, int]:
        return self.symbol, self.label, self.action, self.horizon

    def to_row(self, captured_at: datetime) -> dict[str, str]:
        result = self.result
        return {
            "captured_at": captured_at.isoformat(),
            "symbol": self.symbol,
            "label": self.label,
            "action": self.action,
            "horizon": str(self.horizon),
            "observations": str(self.observations),
            "trades": str(result.total.trades),
            "trading_days": str(result.trading_days),
            "status": result.status,
            "win_rate": _format_float(result.total.win_rate),
            "profit_factor_atr": _format_float(result.total.profit_factor_atr),
            "avg_net_atr": _format_float(result.total.avg_net_atr),
            "early_avg_net_atr": _format_float(result.early.avg_net_atr),
            "late_avg_net_atr": _format_float(result.late.avg_net_atr),
            "late_to_early_ratio": _format_float(result.late_to_early_ratio),
            "max_drawdown_atr": _format_float(result.max_drawdown_atr),
            "max_loss_streak": str(result.max_loss_streak),
            "mean_ci_low": _format_float(result.mean_ci_low),
            "mean_ci_high": _format_float(result.mean_ci_high),
            "p_value": _format_float(result.p_value),
            "q_value": _format_float(result.q_value),
            "reasons": " | ".join(result.reasons),
        }


def _format_float(value: float) -> str:
    if math.isinf(value):
        return "inf" if value > 0 else "-inf"
    if math.isnan(value):
        return "nan"
    return f"{value:.12g}"


def _float(row: dict[str, str], key: str) -> float:
    raw = str(row.get(key, "") or "").strip()
    return float(raw) if raw else 0.0


def _truthy(row: dict[str, str], key: str) -> bool:
    return str(row.get(key, "") or "").strip().lower() in {"1", "true", "yes", "y"}


def _signal_time(row: dict[str, str]) -> datetime:
    return datetime.fromisoformat(row["signal_time"])


def _bar_duration(timeframe: str) -> timedelta:
    text = timeframe.strip().upper()
    if len(text) < 2 or text[0] not in _TIMEFRAME_UNITS:
        raise ValueError(f"Unsupported timeframe: {timeframe}")
    value = int(text[1:])
    if value <= 0:
        raise ValueError(f"Invalid timeframe: {timeframe}")
    return timedelta(**{_TIMEFRAME_UNITS[text[0]]: value})


def _normalized_net(row: dict[str, str], horizon: int) -> float | None:
    stored = str(row.get(f"progress_atr_{horizon}", "") or "").strip()
    if stored:
        return float(stored)
    atr = _float(row, "atr")
    if atr <= 0:
        return None
    net = str(row.get(f"net_move_{horizon}", "") or "").strip()
    return float(net) / atr if net else None


def non_overlapping_rows(rows: list[dict[str, str]], horizon: int) -> list[dict[str, str]]:
    """Keep at most one fixed-horizon paper position per symbol and action."""
    selected: list[dict[str, str]] = []
    next_allowed: dict[tuple[str, str], datetime] = {}
    for row in sorted(rows, key=_signal_time):
        action = str(row.get("action", "")).upper()
        if action not in _VALID_ACTIONS:
            continue
        symbol = str(row.get("symbol", "UNKNOWN")).upper()
        signal_time = _signal_time(row)
        key = (symbol, action)
        if signal_time < next_allowed.get(key, datetime.min.replace(tzinfo=signal_time.tzinfo)):
            continue
        selected.append(row)
        next_allowed[key] = signal_time + _bar_duration(str(row.get("timeframe", "M5"))) * horizon
    return selected


def _evaluated_rows(
    rows: list[dict[str, str]], horizon: int, action: str
) -> list[dict[str, str]]:
    direction = action.upper()
    prepared = non_overlapping_rows(
        [row for row in rows if str(row.get("action", "")).upper() == direction], horizon
    )
    evaluated = [
        row
        for row in prepared
        if row.get(f"outcome_{horizon}") in _VALID_OUTCOMES
        and _normalized_net(row, horizon) is not None
    ]
    return sorted(evaluated, key=_signal_time)


def _segment_metrics(rows: list[dict[str, str]], horizon: int) -> SegmentMetrics:
    values = [value for row in rows if (value := _normalized_net(row, horizon)) is not None]
    wins = sum(row.get(f"outcome_{horizon}") == "WIN" for row in rows)
    losses = sum(row.get(f"outcome_{horizon}") == "LOSS" for row in rows)
    positive = sum(value for value in values if value > 0)
    negative = abs(sum(value for value in values if value < 0))
    decided = wins + losses
    return SegmentMetrics(
        trades=len(values),
        win_rate=wins / decided * 100.0 if decided else 0.0,
        profit_factor_atr=(
            positive / negative if negative else (float("inf") if positive else 0.0)
        ),
        avg_net_atr=statistics.fmean(values) if values else 0.0,
    )


def _values(rows: list[dict[str, str]], horizon: int) -> list[float]:
    return [value for row in rows if (value := _normalized_net(row, horizon)) is not None]


def _mean_ci95(values: list[float]) -> tuple[float, float]:
    if not values:
        return 0.0, 0.0
    mean = statistics.fmean(values)
    if len(values) < 2:
        return mean, mean
    margin = 1.96 * statistics.stdev(values) / math.sqrt(len(values))
    return mean - margin, mean + margin


def _one_sided_p_value(values: list[float]) -> float:
    """Approximate one-sided probability that the mean is not positive."""
    if not values:
        return 1.0
    mean = statistics.fmean(values)
    if len(values) < 2:
        return 0.0 if mean > 0 else 1.0
    deviation = statistics.stdev(values)
    if deviation == 0:
        return 0.0 if mean > 0 else 1.0
    z_score = mean / (deviation / math.sqrt(len(values)))
    return 1.0 - NormalDist().cdf(z_score)


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


def _trading_days(rows: list[dict[str, str]]) -> int:
    return len({_signal_time(row).date() for row in rows})


def validate_action_rows(
    rows: list[dict[str, str]],
    horizon: int,
    action: str,
    *,
    candidate_minimum: int = 30,
    research_minimum: int = 300,
    minimum_trading_days: int = 10,
    maximum_drawdown_atr: float = 25.0,
    maximum_loss_streak: int = 10,
    minimum_late_ratio: float = 0.20,
) -> ActionValidationResult:
    """Validate one exact feature/action group on chronological non-overlapping trades."""
    direction = action.upper()
    if direction not in _VALID_ACTIONS:
        raise ValueError(f"Unsupported action: {action}")
    evaluated = _evaluated_rows(rows, horizon, direction)
    midpoint = len(evaluated) // 2
    early_rows = evaluated[:midpoint]
    late_rows = evaluated[midpoint:]
    values = _values(evaluated, horizon)
    total = _segment_metrics(evaluated, horizon)
    early = _segment_metrics(early_rows, horizon)
    late = _segment_metrics(late_rows, horizon)
    trading_days = _trading_days(evaluated)
    ci_low, ci_high = _mean_ci95(values)
    p_value = _one_sided_p_value(values)
    drawdown = _max_drawdown(values)
    loss_streak = _max_loss_streak(values)
    late_ratio = (
        late.avg_net_atr / early.avg_net_atr
        if early.avg_net_atr > 0
        else (float("inf") if late.avg_net_atr > 0 else 0.0)
    )
    half_minimum = max(10, candidate_minimum // 3)
    reasons: list[str] = []

    if total.trades < candidate_minimum:
        reasons.append(f"need at least {candidate_minimum} non-overlapping trades")
        status = "INSUFFICIENT_SAMPLE"
    elif early.trades < half_minimum or late.trades < half_minimum:
        reasons.append(f"need at least {half_minimum} trades in both time halves")
        status = "INSUFFICIENT_SAMPLE"
    elif trading_days < minimum_trading_days:
        reasons.append(f"need at least {minimum_trading_days} distinct trading days")
        status = "INSUFFICIENT_SAMPLE"
    else:
        if total.avg_net_atr <= 0 or total.profit_factor_atr <= 1.0:
            reasons.append("overall result is not positive after spread")
        if early.avg_net_atr <= 0 or early.profit_factor_atr <= 1.0:
            reasons.append("early half is not positive")
        if late.avg_net_atr <= 0 or late.profit_factor_atr <= 1.0:
            reasons.append("late half is not positive")
        if early.avg_net_atr > 0 and late_ratio < minimum_late_ratio:
            reasons.append(
                "late-half edge retained only "
                f"{late_ratio:.3f}; minimum is {minimum_late_ratio:.3f}"
            )
        if drawdown > maximum_drawdown_atr:
            reasons.append(
                f"drawdown {drawdown:.3f} ATR exceeds limit {maximum_drawdown_atr:.3f}"
            )
        if loss_streak > maximum_loss_streak:
            reasons.append(
                f"loss streak {loss_streak} exceeds limit {maximum_loss_streak}"
            )

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

    return ActionValidationResult(
        status=status,
        total=total,
        early=early,
        late=late,
        trading_days=trading_days,
        late_to_early_ratio=late_ratio,
        max_drawdown_atr=drawdown,
        max_loss_streak=loss_streak,
        mean_ci_low=ci_low,
        mean_ci_high=ci_high,
        p_value=p_value,
        q_value=1.0,
        reasons=tuple(reasons),
    )


def feature_labels(row: dict[str, str]) -> set[str]:
    """Return aggregate and direction-specific SMC/context labels for one row."""
    labels: set[str] = set()
    event_found = False
    for scope, key in (("INTERNAL", "internal_break"), ("SWING", "swing_break")):
        value = str(row.get(key, "") or "").strip().upper()
        parts = value.split("_", 1)
        if len(parts) == 2 and parts[0] in {"BULLISH", "BEARISH"}:
            direction, kind = parts
            if kind in {"BOS", "CHOCH", "BREAK"}:
                labels.add(f"{scope}_{kind}")
                labels.add(f"{direction}_{scope}_{kind}")
                event_found = True

    if _truthy(row, "bsl_sweep"):
        labels.add("BSL_SWEEP")
        event_found = True
    if _truthy(row, "ssl_sweep"):
        labels.add("SSL_SWEEP")
        event_found = True

    fvg = str(row.get("fvg_direction", "") or "").strip().upper()
    if fvg in {"BULLISH", "BEARISH"}:
        labels.add(f"{fvg}_FVG")
        event_found = True
    if event_found:
        labels.add("ANY_SMC_EVENT")

    volume_ratio = _float(row, "volume_ratio_20")
    if volume_ratio > 0:
        labels.add("HIGH_VOLUME" if volume_ratio >= 1.2 else "NORMAL_VOLUME")

    spread_atr = _float(row, "spread_cost_atr")
    if spread_atr > 0:
        labels.add("HIGH_SPREAD" if spread_atr >= 0.10 else "LOW_SPREAD")

    internal = str(row.get("internal_bias", "") or "").strip().upper()
    swing = str(row.get("swing_bias", "") or "").strip().upper()
    if internal in {"BULLISH", "BEARISH"} and swing in {"BULLISH", "BEARISH"}:
        labels.add("STRUCTURE_ALIGNED" if internal == swing else "STRUCTURE_CONFLICT")
    return labels


def group_feature_rows(rows: list[dict[str, str]]) -> dict[str, list[dict[str, str]]]:
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        for label in feature_labels(row):
            grouped[label].append(row)
    return grouped


def validate_action_patterns(
    symbol: str,
    rows: list[dict[str, str]],
    horizons: list[int],
    *,
    candidate_minimum: int = 30,
    research_minimum: int = 300,
    minimum_trading_days: int = 10,
    maximum_drawdown_atr: float = 25.0,
    maximum_loss_streak: int = 10,
    minimum_late_ratio: float = 0.20,
) -> list[ActionPatternValidation]:
    output: list[ActionPatternValidation] = []
    for label, group in sorted(group_feature_rows(rows).items()):
        for action in _VALID_ACTIONS:
            action_rows = [
                row for row in group if str(row.get("action", "")).upper() == action
            ]
            if not action_rows:
                continue
            for horizon in horizons:
                output.append(
                    ActionPatternValidation(
                        symbol=symbol.upper(),
                        label=label,
                        action=action,
                        horizon=horizon,
                        observations=len(action_rows),
                        result=validate_action_rows(
                            action_rows,
                            horizon,
                            action,
                            candidate_minimum=candidate_minimum,
                            research_minimum=research_minimum,
                            minimum_trading_days=minimum_trading_days,
                            maximum_drawdown_atr=maximum_drawdown_atr,
                            maximum_loss_streak=maximum_loss_streak,
                            minimum_late_ratio=minimum_late_ratio,
                        ),
                    )
                )
    return output


def apply_benjamini_hochberg(
    validations: list[ActionPatternValidation],
    *,
    fdr_alpha: float = 0.10,
) -> list[ActionPatternValidation]:
    """Attach BH q-values and require FDR control before VALIDATED status."""
    if not validations:
        return []
    ranked = sorted(enumerate(validations), key=lambda item: item[1].result.p_value)
    count = len(ranked)
    raw_q: list[float] = [1.0] * count
    for rank, (_, item) in enumerate(ranked, start=1):
        raw_q[rank - 1] = min(1.0, item.result.p_value * count / rank)
    adjusted_ranked = [1.0] * count
    running = 1.0
    for index in range(count - 1, -1, -1):
        running = min(running, raw_q[index])
        adjusted_ranked[index] = running

    output = list(validations)
    for rank_index, (original_index, item) in enumerate(ranked):
        result = replace(item.result, q_value=adjusted_ranked[rank_index])
        if result.status == "VALIDATED" and result.q_value > fdr_alpha:
            result = replace(
                result,
                status="RESEARCH_CANDIDATE",
                reasons=result.reasons
                + (f"BH q-value {result.q_value:.4f} exceeds FDR limit {fdr_alpha:.4f}",),
            )
        output[original_index] = replace(item, result=result)
    return output


def collect_action_validations(
    rows: list[dict[str, str]],
    symbols: list[str],
    horizons: list[int],
    **validation_kwargs: object,
) -> list[ActionPatternValidation]:
    by_symbol: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        by_symbol[str(row.get("symbol", "UNKNOWN")).upper()].append(row)
    validations: list[ActionPatternValidation] = []
    for symbol in symbols:
        validations.extend(
            validate_action_patterns(
                symbol,
                by_symbol.get(symbol.upper(), []),
                horizons,
                **validation_kwargs,
            )
        )
    return validations


def write_latest_csv(
    path: Path, validations: list[ActionPatternValidation], captured_at: datetime
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=_ACTION_STATE_FIELDS)
        writer.writeheader()
        ordered = sorted(validations, key=lambda item: item.key)
        writer.writerows(item.to_row(captured_at) for item in ordered)
    temporary.replace(path)


def _load_rows(path: Path, schema_version: str) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        return [
            dict(row)
            for row in csv.DictReader(handle)
            if str(row.get("schema_version", "")).strip() == schema_version
        ]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate TradeMind features by symbol, pattern, BUY/SELL action and horizon"
    )
    default_journal = Path(os.getenv("TRADEMIND_JOURNAL_DIR", "data/journal_ecn")) / "signals.csv"
    parser.add_argument("--journal", type=Path, default=default_journal)
    parser.add_argument("--output", type=Path, default=Path("data/action_validation/latest.csv"))
    parser.add_argument("--symbols", default="")
    parser.add_argument("--horizon", type=int, action="append")
    parser.add_argument("--schema-version", default="1.1")
    parser.add_argument("--candidate-min", type=int, default=30)
    parser.add_argument("--min-sample", type=int, default=300)
    parser.add_argument("--min-trading-days", type=int, default=10)
    parser.add_argument("--max-drawdown-atr", type=float, default=25.0)
    parser.add_argument("--max-loss-streak", type=int, default=10)
    parser.add_argument("--min-late-ratio", type=float, default=0.20)
    parser.add_argument("--fdr-alpha", type=float, default=0.10)
    args = parser.parse_args()

    if not args.journal.is_file():
        print(f"Signal journal not found: {args.journal.resolve()}")
        return 1
    if args.candidate_min < 2:
        parser.error("--candidate-min must be at least 2")
    if args.min_sample < args.candidate_min:
        parser.error("--min-sample must be at least --candidate-min")
    if args.min_trading_days < 1:
        parser.error("--min-trading-days must be positive")
    if args.max_drawdown_atr <= 0 or args.max_loss_streak < 1:
        parser.error("risk limits must be positive")
    if not 0 <= args.min_late_ratio <= 1:
        parser.error("--min-late-ratio must be between 0 and 1")
    if not 0 < args.fdr_alpha <= 1:
        parser.error("--fdr-alpha must be between 0 and 1")

    rows = _load_rows(args.journal.expanduser().resolve(), args.schema_version)
    symbols = (
        [item.strip().upper() for item in args.symbols.split(",") if item.strip()]
        if args.symbols
        else sorted({str(row.get("symbol", "UNKNOWN")).upper() for row in rows})
    )
    horizons = sorted(set(args.horizon or [3, 6, 12]))
    validations = collect_action_validations(
        rows,
        symbols,
        horizons,
        candidate_minimum=args.candidate_min,
        research_minimum=args.min_sample,
        minimum_trading_days=args.min_trading_days,
        maximum_drawdown_atr=args.max_drawdown_atr,
        maximum_loss_streak=args.max_loss_streak,
        minimum_late_ratio=args.min_late_ratio,
    )
    validations = apply_benjamini_hochberg(validations, fdr_alpha=args.fdr_alpha)
    captured_at = datetime.now().astimezone()
    output = args.output.expanduser().resolve()
    write_latest_csv(output, validations, captured_at)

    counts = Counter(item.result.status for item in validations)
    print("TradeMind v1.3 action-aware validation")
    print(f"Journal: {args.journal.expanduser().resolve()}")
    print(f"States: {len(validations)}")
    for status in ("VALIDATED", "RESEARCH_CANDIDATE", "UNSTABLE", "INSUFFICIENT_SAMPLE"):
        print(f"{status}: {counts.get(status, 0)}")
    print(f"Latest state: {output}")
    print("No orders were sent.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
