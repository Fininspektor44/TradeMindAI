"""Read-only repository for the TradeMind live signal console."""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping, Sequence

VALID_ACTIONS = {"BUY", "SELL"}


@dataclass(frozen=True, slots=True)
class SignalRecord:
    event_id: str
    signal_key: str
    signal_time: datetime
    source: str
    pipeline: str
    source_id: str
    symbol: str
    timeframe: str
    action: str
    scenario: str
    scenario_family: str
    components: tuple[str, ...]
    score: int
    entry_price: float | None
    stop_price: float | None
    target_price: float | None
    rr: float | None
    horizon: str
    outcome: str
    result: float | None
    mfe: float | None
    mae: float | None
    completed: bool
    status: str
    stale: bool
    freshness: str
    reasons: str

    def as_dict(self) -> dict[str, object]:
        return {
            "event_id": self.event_id,
            "signal_key": self.signal_key,
            "signal_time": self.signal_time.isoformat(),
            "source": self.source,
            "pipeline": self.pipeline,
            "source_id": self.source_id,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "action": self.action,
            "scenario": self.scenario,
            "scenario_family": self.scenario_family,
            "components": list(self.components),
            "score": self.score,
            "entry_price": self.entry_price,
            "stop_price": self.stop_price,
            "target_price": self.target_price,
            "rr": self.rr,
            "horizon": self.horizon,
            "outcome": self.outcome,
            "result": self.result,
            "mfe": self.mfe,
            "mae": self.mae,
            "completed": self.completed,
            "status": self.status,
            "stale": self.stale,
            "freshness": self.freshness,
            "reasons": self.reasons,
        }


@dataclass(frozen=True, slots=True)
class SourceHealth:
    stale: bool
    freshness: str
    updated_at: datetime | None


@dataclass(frozen=True, slots=True)
class RepositorySnapshot:
    records: tuple[SignalRecord, ...]
    errors: tuple[str, ...]
    loaded_at: datetime

    def as_dict(self) -> dict[str, object]:
        return {
            "loaded_at": self.loaded_at.isoformat(),
            "signals": len(self.records),
            "errors": list(self.errors),
        }


def _text(row: Mapping[str, object], *keys: str) -> str:
    for key in keys:
        value = str(row.get(key, "") or "").strip()
        if value:
            return value
    return ""


def _float(row: Mapping[str, object], *keys: str) -> float | None:
    text = _text(row, *keys)
    if not text:
        return None
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def _int(row: Mapping[str, object], *keys: str) -> int:
    value = _float(row, *keys)
    return int(round(value)) if value is not None else 0


def _truthy(row: Mapping[str, object], *keys: str) -> bool:
    return _text(row, *keys).lower() in {"1", "true", "yes", "y"}


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include timezone information")
    return parsed.astimezone(timezone.utc)


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [
            {key: str(value or "").strip() for key, value in dict(row).items()}
            for row in csv.DictReader(handle)
        ]


def _read_health(path: Path, now: datetime, stale_after_seconds: int) -> SourceHealth:
    if not path.is_file():
        return SourceHealth(stale=True, freshness="MISSING", updated_at=None)
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        timestamp = _text(payload, "updated_at", "generated_at", "captured_at")
        updated_at = _parse_time(timestamp)
    except (OSError, ValueError, json.JSONDecodeError):
        return SourceHealth(stale=True, freshness="INVALID", updated_at=None)
    age = (now - updated_at).total_seconds()
    stale = age < 0 or age > stale_after_seconds
    return SourceHealth(
        stale=stale,
        freshness="STALE" if stale else "FRESH",
        updated_at=updated_at,
    )


def _status(
    row: Mapping[str, object],
    signal_time: datetime,
    now: datetime,
    new_window_seconds: int,
) -> str:
    outcome = _text(row, "outcome").upper()
    completed = _truthy(row, "completed") or outcome in {"WIN", "LOSS", "TIMEOUT", "FLAT"}
    if completed:
        if outcome in {"WIN", "LOSS", "TIMEOUT"}:
            return outcome
        if outcome == "FLAT":
            return "TIMEOUT"
        return "CANCELLED"
    age = max(0.0, (now - signal_time).total_seconds())
    return "NEW" if age <= new_window_seconds else "ACTIVE"


def _components(row: Mapping[str, object]) -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                value.strip()
                for value in _text(row, "components").split("|")
                if value.strip()
            }
        )
    )


def _source_health(source: str, health: Mapping[str, SourceHealth]) -> SourceHealth:
    default = SourceHealth(stale=False, freshness="UNKNOWN", updated_at=None)
    return health.get(source, health.get("*", default))


def _unified_record(
    row: Mapping[str, object],
    *,
    now: datetime,
    health: Mapping[str, SourceHealth],
    new_window_seconds: int,
) -> SignalRecord:
    signal_time = _parse_time(_text(row, "signal_time"))
    action = _text(row, "action").upper()
    if action not in VALID_ACTIONS:
        raise ValueError(f"invalid action: {action or '<empty>'}")
    event_id = _text(row, "event_id")
    signal_key = _text(row, "signal_key")
    source_id = _text(row, "source_id")
    symbol = _text(row, "symbol").upper()
    if not event_id or not signal_key or not source_id or not symbol:
        raise ValueError("missing unified signal identity")
    pipeline = _text(row, "source").upper() or "MT5"
    source = "BYBIT" if "BYBIT" in pipeline else "MT5"
    source_state = _source_health(source, health)
    outcome = _text(row, "outcome").upper()
    completed = _truthy(row, "completed") or outcome in {
        "WIN", "LOSS", "TIMEOUT", "FLAT"
    }
    return SignalRecord(
        event_id=event_id,
        signal_key=signal_key,
        signal_time=signal_time,
        source=source,
        pipeline=pipeline,
        source_id=source_id,
        symbol=symbol,
        timeframe=_text(row, "timeframe").upper(),
        action=action,
        scenario=_text(row, "scenario"),
        scenario_family=_text(row, "scenario_family"),
        components=_components(row),
        score=max(0, min(100, _int(row, "quality_score", "source_score", "score"))),
        entry_price=_float(row, "entry_price"),
        stop_price=_float(row, "stop_price"),
        target_price=_float(row, "target_price"),
        rr=_float(row, "rr"),
        horizon=_text(row, "horizon"),
        outcome=outcome,
        result=_float(row, "result"),
        mfe=_float(row, "mfe"),
        mae=_float(row, "mae"),
        completed=completed,
        status=_status(row, signal_time, now, new_window_seconds),
        stale=source_state.stale,
        freshness=source_state.freshness,
        reasons=_text(row, "reasons"),
    )


def _bybit_record(
    row: Mapping[str, object],
    *,
    now: datetime,
    health: Mapping[str, SourceHealth],
    origin: str,
    new_window_seconds: int,
) -> SignalRecord:
    signal_time = _parse_time(_text(row, "signal_time", "created_at", "started_at"))
    action = _text(row, "action").upper()
    if action not in VALID_ACTIONS:
        raise ValueError(f"invalid action: {action or '<empty>'}")
    source_id = _text(row, "paper_signal_id", "decision_id", "signal_id")
    symbol = _text(row, "symbol").upper()
    if not source_id or not symbol:
        raise ValueError("missing Bybit signal identity")
    scenario = _text(row, "scenario") or "BYBIT_SHADOW"
    event_id = f"BYBIT:{origin}:{source_id}"
    source_state = _source_health("BYBIT", health)
    outcome = _text(row, "outcome").upper()
    completed = _truthy(row, "completed") or outcome in {
        "WIN", "LOSS", "TIMEOUT", "FLAT"
    }
    reasons = _text(row, "reasons", "completion_reason")
    return SignalRecord(
        event_id=event_id,
        signal_key=f"BYBIT:{source_id}",
        signal_time=signal_time,
        source="BYBIT",
        pipeline=origin,
        source_id=source_id,
        symbol=symbol,
        timeframe=_text(row, "timeframe").upper() or "M5",
        action=action,
        scenario=scenario,
        scenario_family="BYBIT_SHADOW",
        components=_components(row),
        score=max(0, min(100, _int(row, "quality_score", "score"))),
        entry_price=_float(row, "entry_price"),
        stop_price=_float(row, "stop_price"),
        target_price=_float(row, "target_price"),
        rr=_float(row, "rr", "planned_rr"),
        horizon=_text(row, "horizon", "completion_reason"),
        outcome=outcome,
        result=_float(row, "result_r", "result"),
        mfe=_float(row, "mfe_r", "mfe"),
        mae=_float(row, "mae_r", "mae"),
        completed=completed,
        status=_status(row, signal_time, now, new_window_seconds),
        stale=source_state.stale,
        freshness=source_state.freshness,
        reasons=reasons,
    )


class LiveSignalRepository:
    """Load MT5 and Bybit research signals without mutating source journals."""

    def __init__(
        self,
        *,
        unified_path: Path | None = None,
        bybit_paths: Sequence[Path] = (),
        status_paths: Mapping[str, Path] | None = None,
        stale_after_seconds: int = 600,
        new_window_seconds: int = 600,
    ) -> None:
        if stale_after_seconds <= 0 or new_window_seconds <= 0:
            raise ValueError("freshness windows must be positive")
        self.unified_path = unified_path
        self.bybit_paths = tuple(bybit_paths)
        self.status_paths = dict(status_paths or {})
        self.stale_after_seconds = stale_after_seconds
        self.new_window_seconds = new_window_seconds

    def load(self, now: datetime | None = None) -> RepositorySnapshot:
        loaded_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        health = {
            source.upper(): _read_health(path, loaded_at, self.stale_after_seconds)
            for source, path in self.status_paths.items()
        }
        records: dict[str, SignalRecord] = {}
        errors: list[str] = []

        if self.unified_path is not None:
            for index, row in enumerate(_read_csv(self.unified_path), start=2):
                try:
                    record = _unified_record(
                        row,
                        now=loaded_at,
                        health=health,
                        new_window_seconds=self.new_window_seconds,
                    )
                    records[record.event_id] = record
                except ValueError as exc:
                    errors.append(f"{self.unified_path}:{index}: {exc}")

        for path in self.bybit_paths:
            origin = path.parent.name.upper() or "BYBIT_SHADOW"
            for index, row in enumerate(_read_csv(path), start=2):
                try:
                    record = _bybit_record(
                        row,
                        now=loaded_at,
                        health=health,
                        origin=origin,
                        new_window_seconds=self.new_window_seconds,
                    )
                    records[record.event_id] = record
                except ValueError as exc:
                    errors.append(f"{path}:{index}: {exc}")

        ordered = tuple(
            sorted(
                records.values(),
                key=lambda item: (item.signal_time, item.event_id),
                reverse=True,
            )
        )
        return RepositorySnapshot(records=ordered, errors=tuple(errors), loaded_at=loaded_at)

    @staticmethod
    def list_records(
        snapshot: RepositorySnapshot,
        *,
        sources: Iterable[str] = (),
        symbols: Iterable[str] = (),
        actions: Iterable[str] = (),
        scenarios: Iterable[str] = (),
        statuses: Iterable[str] = (),
        min_score: int = 0,
        limit: int | None = None,
    ) -> tuple[SignalRecord, ...]:
        source_set = {value.upper() for value in sources}
        symbol_set = {value.upper() for value in symbols}
        action_set = {value.upper() for value in actions}
        scenario_set = {value.upper() for value in scenarios}
        status_set = {value.upper() for value in statuses}
        selected = tuple(
            record
            for record in snapshot.records
            if (not source_set or record.source in source_set)
            and (not symbol_set or record.symbol in symbol_set)
            and (not action_set or record.action in action_set)
            and (not scenario_set or record.scenario.upper() in scenario_set)
            and (not status_set or record.status in status_set)
            and record.score >= min_score
        )
        return selected[:limit] if limit is not None else selected

    @staticmethod
    def get(snapshot: RepositorySnapshot, event_id: str) -> SignalRecord | None:
        return next(
            (record for record in snapshot.records if record.event_id == event_id),
            None,
        )
