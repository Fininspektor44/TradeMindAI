from __future__ import annotations

import csv
from datetime import datetime, timezone

from trademind.candidate_watch import (
    CandidateState,
    WatchPaths,
    classify_transition,
    load_latest_states,
    persist_states,
)


def _state(
    status: str,
    trades: int,
    *,
    observations: int | None = None,
    captured_at: datetime | None = None,
) -> CandidateState:
    return CandidateState(
        captured_at=captured_at or datetime(2026, 1, 1, tzinfo=timezone.utc),
        symbol="XAUUSD",
        label="INTERNAL_BOS",
        horizon=3,
        observations=observations if observations is not None else trades,
        trades=trades,
        status=status,
        win_rate=60.0,
        profit_factor_atr=1.8,
        avg_net_atr=0.2,
        early_avg_net_atr=0.18,
        late_avg_net_atr=0.22,
        max_drawdown_atr=1.1,
        max_loss_streak=3,
        mean_ci_low=0.01,
        mean_ci_high=0.39,
        reasons=(),
    )


def test_candidate_transition_is_highlighted() -> None:
    previous = _state("INSUFFICIENT_SAMPLE", 29)
    current = _state("RESEARCH_CANDIDATE", 30)

    assert classify_transition(previous, current, 30) == "CANDIDATE_REACHED"


def test_threshold_crossing_can_be_rejected_as_unstable() -> None:
    previous = _state("INSUFFICIENT_SAMPLE", 29)
    current = _state("UNSTABLE", 30)

    assert classify_transition(previous, current, 30) == "CANDIDATE_THRESHOLD_REJECTED"


def test_candidate_loss_is_recorded() -> None:
    previous = _state("RESEARCH_CANDIDATE", 42)
    current = _state("UNSTABLE", 44)

    assert classify_transition(previous, current, 30) == "CANDIDATE_LOST"


def test_persistence_deduplicates_unchanged_snapshots_and_writes_event(tmp_path) -> None:
    paths = WatchPaths.under(tmp_path)
    baseline = _state("INSUFFICIENT_SAMPLE", 29)

    first = persist_states([baseline], paths, candidate_minimum=30)
    second = persist_states([baseline], paths, candidate_minimum=30)
    promoted = _state(
        "RESEARCH_CANDIDATE",
        30,
        captured_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
    )
    third = persist_states([promoted], paths, candidate_minimum=30)

    assert first.changed_states == 1
    assert first.events == ()
    assert second.changed_states == 0
    assert second.events == ()
    assert third.changed_states == 1
    assert [event.event for event in third.events] == ["CANDIDATE_REACHED"]

    with paths.history.open("r", encoding="utf-8", newline="") as handle:
        history_rows = list(csv.DictReader(handle))
    with paths.events.open("r", encoding="utf-8", newline="") as handle:
        event_rows = list(csv.DictReader(handle))

    assert len(history_rows) == 2
    assert len(event_rows) == 1
    assert event_rows[0]["event"] == "CANDIDATE_REACHED"
    assert load_latest_states(paths.latest)[promoted.key].status == "RESEARCH_CANDIDATE"
