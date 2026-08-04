"""Presentation-layer collapse of research rows into unique trading ideas.

The research journals intentionally contain one row per scenario and evaluation
horizon. The live console must not present those rows as separate trade calls.
This module groups them by the original source signal and returns one read-only
record per idea while preserving scenarios, horizons and reasons in diagnostics.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import replace
from typing import Iterable

from trademind.live_signal_repository import RepositorySnapshot, SignalRecord

_HORIZON_MINUTES = {
    "M1": 1,
    "M5": 5,
    "M15": 15,
    "M30": 30,
    "H1": 60,
    "H2": 120,
    "H3": 180,
    "H4": 240,
    "H6": 360,
    "H8": 480,
    "H12": 720,
    "D1": 1440,
}


def _horizon_rank(value: str) -> int:
    normalized = value.strip().upper()
    if normalized in _HORIZON_MINUTES:
        return _HORIZON_MINUTES[normalized]
    match = re.fullmatch(r"([MHD])(\d+)", normalized)
    if not match:
        return 0
    unit, amount_text = match.groups()
    amount = int(amount_text)
    multiplier = {"M": 1, "H": 60, "D": 1440}[unit]
    return amount * multiplier


def _idea_key(record: SignalRecord) -> tuple[str, ...]:
    """Return the stable source identity of one underlying market idea."""
    return (
        record.source,
        record.pipeline,
        record.source_id,
        record.symbol,
        record.action,
        record.signal_time.isoformat(),
    )


def _unique_text(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(sorted({value.strip() for value in values if value.strip()}))


def _merge_group(group: list[SignalRecord]) -> SignalRecord:
    if len(group) == 1:
        return group[0]

    scenarios = _unique_text(record.scenario for record in group)
    horizons = tuple(
        sorted(
            {record.horizon.strip() for record in group if record.horizon.strip()},
            key=lambda value: (_horizon_rank(value), value),
        )
    )
    components = tuple(sorted({item for record in group for item in record.components}))

    primary = max(
        group,
        key=lambda record: (
            record.score,
            record.scenario.upper() != "BASE_SIGNAL",
            len(record.components),
            record.scenario,
            record.event_id,
        ),
    )
    terminal = max(
        group,
        key=lambda record: (
            _horizon_rank(record.horizon),
            record.completed,
            record.score,
            record.event_id,
        ),
    )

    scenario_label = primary.scenario
    if len(scenarios) > 1:
        scenario_label = f"{primary.scenario} +{len(scenarios) - 1}"
    horizon_label = "/".join(horizons) or terminal.horizon

    diagnostics = []
    if scenarios:
        diagnostics.append("Scenarios: " + " · ".join(scenarios))
    if horizons:
        diagnostics.append("Horizons: " + " · ".join(horizons))
    diagnostics.extend(_unique_text(record.reasons for record in group))

    return replace(
        primary,
        event_id=terminal.event_id,
        signal_key=(
            f"IDEA:{primary.source}:{primary.pipeline}:{primary.source_id}"
        ),
        scenario=scenario_label,
        scenario_family=(
            primary.scenario_family if len(scenarios) == 1 else "MULTI_SCENARIO"
        ),
        components=components,
        score=max(record.score for record in group),
        horizon=horizon_label,
        outcome=terminal.outcome,
        result=terminal.result,
        mfe=terminal.mfe,
        mae=terminal.mae,
        completed=terminal.completed,
        status=terminal.status,
        stale=any(record.stale for record in group),
        freshness=(
            "STALE"
            if any(record.stale for record in group)
            else "FRESH"
            if all(record.freshness == "FRESH" for record in group)
            else primary.freshness
        ),
        reasons="\n".join(diagnostics),
    )


def collapse_signal_ideas(snapshot: RepositorySnapshot) -> RepositorySnapshot:
    """Collapse scenario/horizon rows without mutating source journals."""
    groups: dict[tuple[str, ...], list[SignalRecord]] = defaultdict(list)
    for record in snapshot.records:
        groups[_idea_key(record)].append(record)

    records = tuple(
        sorted(
            (_merge_group(group) for group in groups.values()),
            key=lambda record: (record.signal_time, record.event_id),
            reverse=True,
        )
    )
    return RepositorySnapshot(
        records=records,
        errors=snapshot.errors,
        loaded_at=snapshot.loaded_at,
    )
