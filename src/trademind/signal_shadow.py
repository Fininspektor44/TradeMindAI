"""Conservative shadow evaluation for TradeMind v1.16 signal candidates.

Candidates are evaluated against future OHLC bars without placing orders. The
first target is the primary v1 outcome. Limit entries touched on a bar are
filled before risk is evaluated, and if stop and target are both touched on the
same bar, the stop wins. This deliberately avoids optimistic intrabar ordering.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from trademind.signal_evidence import similarity_key
from trademind.signal_intelligence import (
    EntryOrder,
    SignalCandidate,
    append_journal_event,
    candidate_from_dict,
)

SHADOW_VERSION = "1.16.0"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _parse_time(value: Any) -> datetime:
    text = _text(value)
    if not text:
        raise ValueError("bar time is required")
    if text.lstrip("-").isdigit():
        return datetime.fromtimestamp(int(text), tz=timezone.utc)
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("bar time must include timezone information")
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class Bar:
    time: datetime
    symbol: str
    timeframe: str
    open: float
    high: float
    low: float
    close: float

    def __post_init__(self) -> None:
        if self.time.tzinfo is None:
            raise ValueError("bar time must include timezone information")
        if not self.symbol or not self.timeframe:
            raise ValueError("bar symbol/timeframe are required")
        if min(self.open, self.high, self.low, self.close) <= 0:
            raise ValueError("bar prices must be positive")
        if self.high < max(self.open, self.low, self.close):
            raise ValueError("bar high is invalid")
        if self.low > min(self.open, self.high, self.close):
            raise ValueError("bar low is invalid")

    @classmethod
    def from_dict(cls, row: Mapping[str, Any]) -> Bar:
        return cls(
            time=_parse_time(row.get("time", row.get("bar_time"))),
            symbol=_text(row.get("symbol")).upper(),
            timeframe=_text(row.get("timeframe")).upper(),
            open=_float(row.get("open")),
            high=_float(row.get("high")),
            low=_float(row.get("low")),
            close=_float(row.get("close")),
        )


@dataclass(frozen=True, slots=True)
class ShadowOutcome:
    signal_id: str
    setup_key: str
    completed_at: datetime
    outcome: str
    net_r: float
    exit_reason: str
    exit_price: float
    filled_entries: int
    allocation_filled: float
    average_entry: float
    mfe_r: float
    mae_r: float
    bars_observed: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SHADOW_VERSION,
            "signal_id": self.signal_id,
            "setup_key": self.setup_key,
            "completed_at": self.completed_at.astimezone(timezone.utc).isoformat(),
            "outcome": self.outcome,
            "net_r": self.net_r,
            "exit_reason": self.exit_reason,
            "exit_price": self.exit_price,
            "filled_entries": self.filled_entries,
            "allocation_filled": self.allocation_filled,
            "average_entry": self.average_entry,
            "mfe_r": self.mfe_r,
            "mae_r": self.mae_r,
            "bars_observed": self.bars_observed,
        }


def _touches(bar: Bar, price: float) -> bool:
    return bar.low <= price <= bar.high


def _average_entry(entries: Sequence[EntryOrder]) -> float:
    allocation = sum(item.allocation for item in entries)
    if allocation <= 0:
        raise ValueError("filled entry allocation is zero")
    return sum(item.price * item.allocation for item in entries) / allocation


def _net_r(
    candidate: SignalCandidate,
    filled: Sequence[EntryOrder],
    exit_price: float,
    cost_r: float,
) -> float:
    average = _average_entry(filled)
    risk = abs(average - candidate.plan.stop_price)
    if risk <= 0:
        raise ValueError("shadow risk distance is zero")
    direction = 1.0 if candidate.plan.action == "BUY" else -1.0
    allocation = sum(item.allocation for item in filled)
    gross = direction * (exit_price - average) / risk
    return gross * allocation - max(0.0, cost_r) * allocation


def evaluate_shadow_candidate(
    candidate: SignalCandidate,
    bars: Sequence[Bar],
    *,
    max_bars: int = 72,
    target_index: int = 0,
    cost_r: float = 0.0,
) -> ShadowOutcome | None:
    if max_bars < 1:
        raise ValueError("max_bars must be positive")
    if target_index < 0 or target_index >= len(candidate.plan.targets):
        raise ValueError("target_index is out of range")

    relevant = [
        bar
        for bar in bars
        if bar.symbol == candidate.symbol
        and bar.timeframe == candidate.timeframe
        and bar.time > candidate.observed_at.astimezone(timezone.utc)
    ]
    relevant.sort(key=lambda item: item.time)
    if not relevant:
        return None

    filled: list[EntryOrder] = [
        item for item in candidate.plan.entries if item.order_type == "MARKET"
    ]
    pending = [item for item in candidate.plan.entries if item.order_type != "MARKET"]
    target = candidate.plan.targets[target_index]
    best_mfe_r = 0.0
    worst_mae_r = 0.0

    for index, bar in enumerate(relevant[:max_bars], start=1):
        newly_filled = [item for item in pending if _touches(bar, item.price)]
        if newly_filled:
            filled.extend(newly_filled)
            pending = [item for item in pending if item not in newly_filled]

        if not filled:
            if index == max_bars:
                return ShadowOutcome(
                    signal_id=candidate.signal_id,
                    setup_key=similarity_key(candidate),
                    completed_at=bar.time,
                    outcome="FLAT",
                    net_r=0.0,
                    exit_reason="NO_FILL_TIMEOUT",
                    exit_price=bar.close,
                    filled_entries=0,
                    allocation_filled=0.0,
                    average_entry=0.0,
                    mfe_r=0.0,
                    mae_r=0.0,
                    bars_observed=index,
                )
            continue

        average = _average_entry(filled)
        risk = abs(average - candidate.plan.stop_price)
        if risk <= 0:
            raise ValueError("shadow risk distance is zero")
        if candidate.plan.action == "BUY":
            favorable = max(0.0, bar.high - average) / risk
            adverse = max(0.0, average - bar.low) / risk
            stop_hit = bar.low <= candidate.plan.stop_price
            target_hit = bar.high >= target
        else:
            favorable = max(0.0, average - bar.low) / risk
            adverse = max(0.0, bar.high - average) / risk
            stop_hit = bar.high >= candidate.plan.stop_price
            target_hit = bar.low <= target
        best_mfe_r = max(best_mfe_r, favorable)
        worst_mae_r = max(worst_mae_r, adverse)

        if stop_hit:
            exit_price = candidate.plan.stop_price
            return ShadowOutcome(
                signal_id=candidate.signal_id,
                setup_key=similarity_key(candidate),
                completed_at=bar.time,
                outcome="LOSS",
                net_r=round(_net_r(candidate, filled, exit_price, cost_r), 6),
                exit_reason=(
                    "STOP_FIRST_CONSERVATIVE" if target_hit else "STOP"
                ),
                exit_price=exit_price,
                filled_entries=len(filled),
                allocation_filled=round(sum(item.allocation for item in filled), 6),
                average_entry=round(average, 10),
                mfe_r=round(best_mfe_r, 6),
                mae_r=round(worst_mae_r, 6),
                bars_observed=index,
            )
        if target_hit:
            exit_price = target
            return ShadowOutcome(
                signal_id=candidate.signal_id,
                setup_key=similarity_key(candidate),
                completed_at=bar.time,
                outcome="WIN",
                net_r=round(_net_r(candidate, filled, exit_price, cost_r), 6),
                exit_reason=f"TARGET_{target_index + 1}",
                exit_price=exit_price,
                filled_entries=len(filled),
                allocation_filled=round(sum(item.allocation for item in filled), 6),
                average_entry=round(average, 10),
                mfe_r=round(best_mfe_r, 6),
                mae_r=round(worst_mae_r, 6),
                bars_observed=index,
            )

        if index == max_bars:
            result = _net_r(candidate, filled, bar.close, cost_r)
            outcome = "WIN" if result > 1e-9 else "LOSS" if result < -1e-9 else "FLAT"
            return ShadowOutcome(
                signal_id=candidate.signal_id,
                setup_key=similarity_key(candidate),
                completed_at=bar.time,
                outcome=outcome,
                net_r=round(result, 6),
                exit_reason="TIMEOUT_MARK_TO_MARKET",
                exit_price=bar.close,
                filled_entries=len(filled),
                allocation_filled=round(sum(item.allocation for item in filled), 6),
                average_entry=round(average, 10),
                mfe_r=round(best_mfe_r, 6),
                mae_r=round(worst_mae_r, 6),
                bars_observed=index,
            )

    return None


def load_candidates(path: Path) -> list[SignalCandidate]:
    if not path.is_file():
        raise ValueError(f"candidate file not found: {path}")
    candidates: list[SignalCandidate] = []
    seen: set[str] = set()
    for line_number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            candidate = candidate_from_dict(json.loads(line))
        except (ValueError, json.JSONDecodeError) as exc:
            raise ValueError(f"candidate line {line_number}: {exc}") from exc
        if candidate.signal_id in seen:
            continue
        seen.add(candidate.signal_id)
        candidates.append(candidate)
    return candidates


def load_bars(path: Path) -> list[Bar]:
    if not path.is_file():
        raise ValueError(f"bar file not found: {path}")
    bars: list[Bar] = []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        for line_number, row in enumerate(csv.DictReader(handle), start=2):
            try:
                bars.append(Bar.from_dict(row))
            except ValueError as exc:
                raise ValueError(f"bar line {line_number}: {exc}") from exc
    bars.sort(key=lambda item: (item.symbol, item.timeframe, item.time))
    return bars


def _load_existing_outcomes(path: Path) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    if not path.is_file():
        return result
    for line_number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"outcome line {line_number}: invalid JSON") from exc
        signal_id = _text(payload.get("signal_id"))
        if not signal_id:
            raise ValueError(f"outcome line {line_number}: missing signal_id")
        if signal_id in result and result[signal_id] != payload:
            raise ValueError(f"conflicting outcome for signal_id: {signal_id}")
        result[signal_id] = payload
    return result


def append_outcomes(path: Path, outcomes: Sequence[ShadowOutcome]) -> int:
    existing = _load_existing_outcomes(path)
    added = 0
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        for outcome in outcomes:
            payload = outcome.as_dict()
            current = existing.get(outcome.signal_id)
            if current is not None:
                if current != payload:
                    raise ValueError(
                        f"immutable outcome mutation detected: {outcome.signal_id}"
                    )
                continue
            handle.write(
                json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                + "\n"
            )
            existing[outcome.signal_id] = payload
            added += 1
    return added


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Evaluate TradeMind v1.16 candidates in conservative shadow mode"
    )
    parser.add_argument(
        "--candidates",
        type=Path,
        default=Path("data/signal_intelligence_v1_16/candidates.jsonl"),
    )
    parser.add_argument(
        "--bars",
        type=Path,
        default=Path("data/volume_v1_4/volume_bars.csv"),
    )
    parser.add_argument(
        "--outcomes",
        type=Path,
        default=Path("data/signal_intelligence_v1_16/outcomes.jsonl"),
    )
    parser.add_argument("--journal", type=Path)
    parser.add_argument("--max-bars", type=int, default=72)
    parser.add_argument("--target-index", type=int, default=0)
    parser.add_argument("--cost-r", type=float, default=0.04)
    args = parser.parse_args(argv)
    try:
        candidates = load_candidates(args.candidates.expanduser().resolve())
        bars = load_bars(args.bars.expanduser().resolve())
        existing = _load_existing_outcomes(args.outcomes.expanduser().resolve())
        completed: list[ShadowOutcome] = []
        active = 0
        for candidate in candidates:
            if candidate.signal_id in existing:
                continue
            outcome = evaluate_shadow_candidate(
                candidate,
                bars,
                max_bars=args.max_bars,
                target_index=args.target_index,
                cost_r=args.cost_r,
            )
            if outcome is None:
                active += 1
                continue
            completed.append(outcome)
        added = append_outcomes(args.outcomes.expanduser().resolve(), completed)
        if args.journal is not None:
            for outcome in completed:
                append_journal_event(
                    args.journal.expanduser().resolve(),
                    signal_id=outcome.signal_id,
                    event_type="OUTCOME",
                    payload=outcome.as_dict(),
                    recorded_at=outcome.completed_at,
                )
    except (OSError, ValueError, csv.Error, json.JSONDecodeError) as exc:
        print(f"Signal shadow evaluation failed: {exc}")
        return 1

    wins = sum(item.outcome == "WIN" for item in completed)
    losses = sum(item.outcome == "LOSS" for item in completed)
    flats = sum(item.outcome == "FLAT" for item in completed)
    print("TradeMind v1.16 Signal Shadow Evaluator")
    print("No orders. No publication. Stop-first intrabar rule.")
    print(f"Candidates: {len(candidates)}")
    print(f"New outcomes: {added} (W/L/F={wins}/{losses}/{flats})")
    print(f"Still active or awaiting bars: {active}")
    print(f"Output: {args.outcomes}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
