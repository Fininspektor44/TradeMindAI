"""Track TradeMind candidate-state history and important validation transitions."""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from trademind.validation import PatternValidation, validate_symbol_patterns

_DEFAULT_SYMBOLS = "XAUUSD,XAGUSD,.USTECHCash,.US500Cash,.US30Cash,WTI,BRENT"
_STATE_FIELDS = (
    "captured_at",
    "symbol",
    "label",
    "horizon",
    "observations",
    "trades",
    "status",
    "win_rate",
    "profit_factor_atr",
    "avg_net_atr",
    "early_avg_net_atr",
    "late_avg_net_atr",
    "max_drawdown_atr",
    "max_loss_streak",
    "mean_ci_low",
    "mean_ci_high",
    "reasons",
)
_EVENT_FIELDS = (
    "event_time",
    "symbol",
    "label",
    "horizon",
    "event",
    "previous_status",
    "current_status",
    "previous_trades",
    "current_trades",
    "avg_net_atr",
    "early_avg_net_atr",
    "late_avg_net_atr",
    "mean_ci_low",
    "mean_ci_high",
    "reasons",
)


@dataclass(frozen=True)
class CandidateState:
    captured_at: datetime
    symbol: str
    label: str
    horizon: int
    observations: int
    trades: int
    status: str
    win_rate: float
    profit_factor_atr: float
    avg_net_atr: float
    early_avg_net_atr: float
    late_avg_net_atr: float
    max_drawdown_atr: float
    max_loss_streak: int
    mean_ci_low: float
    mean_ci_high: float
    reasons: tuple[str, ...]

    @property
    def key(self) -> tuple[str, str, int]:
        return self.symbol, self.label, self.horizon

    @property
    def fingerprint(self) -> tuple[object, ...]:
        return (
            self.observations,
            self.trades,
            self.status,
            _fingerprint_float(self.win_rate),
            _fingerprint_float(self.profit_factor_atr),
            _fingerprint_float(self.avg_net_atr),
            _fingerprint_float(self.early_avg_net_atr),
            _fingerprint_float(self.late_avg_net_atr),
            _fingerprint_float(self.max_drawdown_atr),
            self.max_loss_streak,
            _fingerprint_float(self.mean_ci_low),
            _fingerprint_float(self.mean_ci_high),
            self.reasons,
        )

    @classmethod
    def from_validation(
        cls,
        item: PatternValidation,
        captured_at: datetime,
    ) -> CandidateState:
        result = item.result
        return cls(
            captured_at=captured_at,
            symbol=item.symbol.upper(),
            label=item.label,
            horizon=item.horizon,
            observations=item.observations,
            trades=result.total.trades,
            status=result.status,
            win_rate=result.total.win_rate,
            profit_factor_atr=result.total.profit_factor_atr,
            avg_net_atr=result.total.avg_net_atr,
            early_avg_net_atr=result.early.avg_net_atr,
            late_avg_net_atr=result.late.avg_net_atr,
            max_drawdown_atr=result.max_drawdown_atr,
            max_loss_streak=result.max_loss_streak,
            mean_ci_low=result.mean_ci_low,
            mean_ci_high=result.mean_ci_high,
            reasons=result.reasons,
        )

    def to_row(self) -> dict[str, str]:
        return {
            "captured_at": self.captured_at.isoformat(),
            "symbol": self.symbol,
            "label": self.label,
            "horizon": str(self.horizon),
            "observations": str(self.observations),
            "trades": str(self.trades),
            "status": self.status,
            "win_rate": _format_float(self.win_rate),
            "profit_factor_atr": _format_float(self.profit_factor_atr),
            "avg_net_atr": _format_float(self.avg_net_atr),
            "early_avg_net_atr": _format_float(self.early_avg_net_atr),
            "late_avg_net_atr": _format_float(self.late_avg_net_atr),
            "max_drawdown_atr": _format_float(self.max_drawdown_atr),
            "max_loss_streak": str(self.max_loss_streak),
            "mean_ci_low": _format_float(self.mean_ci_low),
            "mean_ci_high": _format_float(self.mean_ci_high),
            "reasons": json.dumps(list(self.reasons), ensure_ascii=False),
        }

    @classmethod
    def from_row(cls, row: dict[str, str]) -> CandidateState:
        reasons = json.loads(row.get("reasons", "[]") or "[]")
        return cls(
            captured_at=datetime.fromisoformat(row["captured_at"]),
            symbol=row["symbol"].upper(),
            label=row["label"],
            horizon=int(row["horizon"]),
            observations=int(row["observations"]),
            trades=int(row["trades"]),
            status=row["status"],
            win_rate=float(row["win_rate"]),
            profit_factor_atr=float(row["profit_factor_atr"]),
            avg_net_atr=float(row["avg_net_atr"]),
            early_avg_net_atr=float(row["early_avg_net_atr"]),
            late_avg_net_atr=float(row["late_avg_net_atr"]),
            max_drawdown_atr=float(row["max_drawdown_atr"]),
            max_loss_streak=int(row["max_loss_streak"]),
            mean_ci_low=float(row["mean_ci_low"]),
            mean_ci_high=float(row["mean_ci_high"]),
            reasons=tuple(str(item) for item in reasons),
        )


@dataclass(frozen=True)
class CandidateEvent:
    event_time: datetime
    symbol: str
    label: str
    horizon: int
    event: str
    previous_status: str
    current_status: str
    previous_trades: int
    current_trades: int
    avg_net_atr: float
    early_avg_net_atr: float
    late_avg_net_atr: float
    mean_ci_low: float
    mean_ci_high: float
    reasons: tuple[str, ...]

    def to_row(self) -> dict[str, str]:
        return {
            "event_time": self.event_time.isoformat(),
            "symbol": self.symbol,
            "label": self.label,
            "horizon": str(self.horizon),
            "event": self.event,
            "previous_status": self.previous_status,
            "current_status": self.current_status,
            "previous_trades": str(self.previous_trades),
            "current_trades": str(self.current_trades),
            "avg_net_atr": _format_float(self.avg_net_atr),
            "early_avg_net_atr": _format_float(self.early_avg_net_atr),
            "late_avg_net_atr": _format_float(self.late_avg_net_atr),
            "mean_ci_low": _format_float(self.mean_ci_low),
            "mean_ci_high": _format_float(self.mean_ci_high),
            "reasons": json.dumps(list(self.reasons), ensure_ascii=False),
        }


@dataclass(frozen=True)
class WatchSummary:
    total_states: int
    changed_states: int
    events: tuple[CandidateEvent, ...]
    status_counts: dict[str, int]
    history_path: Path
    latest_path: Path
    events_path: Path


@dataclass(frozen=True)
class WatchPaths:
    history: Path
    latest: Path
    events: Path

    @classmethod
    def under(cls, directory: Path) -> WatchPaths:
        return cls(
            history=directory / "history.csv",
            latest=directory / "latest.csv",
            events=directory / "events.csv",
        )


def _fingerprint_float(value: float) -> str:
    """Return the exact canonical representation persisted to CSV."""
    return _format_float(value)


def _format_float(value: float) -> str:
    if math.isinf(value):
        return "inf" if value > 0 else "-inf"
    if math.isnan(value):
        return "nan"
    return f"{value:.12g}"


def _load_journal(path: Path, schema_version: str) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        return [
            dict(row)
            for row in csv.DictReader(handle)
            if row.get("schema_version", "").strip() == schema_version
        ]


def collect_states(
    rows: list[dict[str, str]],
    symbols: list[str],
    horizons: list[int],
    *,
    candidate_minimum: int,
    research_minimum: int,
    volume_threshold: float,
    spread_atr_threshold: float,
    captured_at: datetime | None = None,
) -> list[CandidateState]:
    """Build current per-symbol validation states from journal rows."""
    timestamp = captured_at or datetime.now(timezone.utc)
    by_symbol: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        by_symbol[row.get("symbol", "UNKNOWN").strip().upper()].append(row)

    states: list[CandidateState] = []
    for symbol in symbols:
        name = symbol.upper()
        validations = validate_symbol_patterns(
            name,
            by_symbol.get(name, []),
            horizons,
            candidate_minimum=candidate_minimum,
            research_minimum=research_minimum,
            volume_threshold=volume_threshold,
            spread_atr_threshold=spread_atr_threshold,
        )
        states.extend(CandidateState.from_validation(item, timestamp) for item in validations)
    return sorted(states, key=lambda item: item.key)


def load_latest_states(path: Path) -> dict[tuple[str, str, int], CandidateState]:
    if not path.is_file():
        return {}
    with path.open("r", encoding="utf-8", newline="") as handle:
        states = [CandidateState.from_row(dict(row)) for row in csv.DictReader(handle)]
    return {state.key: state for state in states}


def classify_transition(
    previous: CandidateState | None,
    current: CandidateState,
    candidate_minimum: int,
) -> str | None:
    """Return an important event name, excluding routine metric drift."""
    if previous is None:
        return None
    if previous.status == current.status:
        return None
    if current.status == "VALIDATED":
        return "VALIDATED"
    if previous.status == "VALIDATED":
        return "VALIDATION_LOST"
    if current.status == "RESEARCH_CANDIDATE":
        return "CANDIDATE_REACHED"
    if previous.status == "RESEARCH_CANDIDATE":
        return "CANDIDATE_LOST"
    if previous.trades < candidate_minimum <= current.trades:
        return "CANDIDATE_THRESHOLD_REJECTED"
    if current.status == "UNSTABLE":
        return "BECAME_UNSTABLE"
    return "STATUS_CHANGED"


def _event(
    name: str,
    previous: CandidateState,
    current: CandidateState,
) -> CandidateEvent:
    return CandidateEvent(
        event_time=current.captured_at,
        symbol=current.symbol,
        label=current.label,
        horizon=current.horizon,
        event=name,
        previous_status=previous.status,
        current_status=current.status,
        previous_trades=previous.trades,
        current_trades=current.trades,
        avg_net_atr=current.avg_net_atr,
        early_avg_net_atr=current.early_avg_net_atr,
        late_avg_net_atr=current.late_avg_net_atr,
        mean_ci_low=current.mean_ci_low,
        mean_ci_high=current.mean_ci_high,
        reasons=current.reasons,
    )


def _append_rows(path: Path, fields: tuple[str, ...], rows: list[dict[str, str]]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    write_header = not path.is_file() or path.stat().st_size == 0
    with path.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        if write_header:
            writer.writeheader()
        writer.writerows(rows)


def _write_latest(path: Path, states: list[CandidateState]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=_STATE_FIELDS)
        writer.writeheader()
        writer.writerows(state.to_row() for state in states)
    temporary.replace(path)


def persist_states(
    states: list[CandidateState],
    paths: WatchPaths,
    *,
    candidate_minimum: int,
) -> WatchSummary:
    """Persist changed snapshots, current state and important transitions."""
    previous = load_latest_states(paths.latest)
    changed: list[CandidateState] = []
    events: list[CandidateEvent] = []

    for state in states:
        old = previous.get(state.key)
        if old is None or old.fingerprint != state.fingerprint:
            changed.append(state)
        event_name = classify_transition(old, state, candidate_minimum)
        if event_name and old is not None:
            events.append(_event(event_name, old, state))

    _append_rows(paths.history, _STATE_FIELDS, [state.to_row() for state in changed])
    _append_rows(paths.events, _EVENT_FIELDS, [event.to_row() for event in events])
    _write_latest(paths.latest, states)
    counts = Counter(state.status for state in states)
    return WatchSummary(
        total_states=len(states),
        changed_states=len(changed),
        events=tuple(events),
        status_counts=dict(counts),
        history_path=paths.history,
        latest_path=paths.latest,
        events_path=paths.events,
    )


def _print_summary(summary: WatchSummary) -> None:
    print("TradeMind candidate watcher")
    print(f"States evaluated: {summary.total_states}")
    print(f"Changed states recorded: {summary.changed_states}")
    print(f"Important transitions: {len(summary.events)}")
    for status in (
        "VALIDATED",
        "RESEARCH_CANDIDATE",
        "UNSTABLE",
        "INSUFFICIENT_SAMPLE",
    ):
        print(f"{status}: {summary.status_counts.get(status, 0)}")
    if summary.events:
        print("Transitions:")
        for event in summary.events:
            print(
                f"  {event.event}: {event.symbol} {event.label} H{event.horizon} "
                f"trades={event.previous_trades}->{event.current_trades} "
                f"status={event.previous_status}->{event.current_status} "
                f"avg={event.avg_net_atr:.3f} "
                f"early={event.early_avg_net_atr:.3f} late={event.late_avg_net_atr:.3f}"
            )
    print(f"History: {summary.history_path.resolve()}")
    print(f"Latest state: {summary.latest_path.resolve()}")
    print(f"Events: {summary.events_path.resolve()}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Track TradeMind candidate history and validation transitions"
    )
    default_journal = Path(os.getenv("TRADEMIND_JOURNAL_DIR", "data/journal_ecn"))
    parser.add_argument("--journal", type=Path, default=default_journal / "signals.csv")
    parser.add_argument("--history-dir", type=Path, default=Path("data/candidate_history"))
    parser.add_argument("--symbols", default=_DEFAULT_SYMBOLS)
    parser.add_argument("--horizon", type=int, action="append")
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
    if args.volume_threshold <= 0:
        parser.error("--volume-threshold must be greater than zero")
    if args.spread_atr_threshold <= 0:
        parser.error("--spread-atr-threshold must be greater than zero")
    journal = args.journal.expanduser().resolve()
    if not journal.is_file():
        print(f"Signal journal not found: {journal}")
        return 1
    symbols = [item.strip().upper() for item in args.symbols.split(",") if item.strip()]
    if not symbols:
        parser.error("--symbols must contain at least one symbol")
    horizons = sorted(set(args.horizon or [3, 6, 12]))
    if any(horizon < 1 for horizon in horizons):
        parser.error("--horizon values must be positive")

    rows = _load_journal(journal, args.schema_version)
    states = collect_states(
        rows,
        symbols,
        horizons,
        candidate_minimum=args.candidate_min,
        research_minimum=args.min_sample,
        volume_threshold=args.volume_threshold,
        spread_atr_threshold=args.spread_atr_threshold,
    )
    history_dir = args.history_dir.expanduser().resolve()
    summary = persist_states(
        states,
        WatchPaths.under(history_dir),
        candidate_minimum=args.candidate_min,
    )
    _print_summary(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
