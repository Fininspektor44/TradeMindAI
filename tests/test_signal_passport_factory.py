from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from trademind.signal_evidence import OutcomeObservation, similarity_key
from trademind.signal_intelligence import EntryOrder, SignalCandidate, TradePlan
from trademind.signal_passport_factory import (
    READY_STATE,
    WAITING_FRESH_STATE,
    WAITING_PUBLISHABLE_STATE,
    run_factory,
)
from trademind.signal_to_risk_bridge import validate_publishable_passport


NOW = datetime(2026, 8, 5, 18, 44, tzinfo=timezone.utc)


def _candidate(*, created_at: datetime | None = None) -> SignalCandidate:
    created = created_at or NOW - timedelta(seconds=30)
    observed = created - timedelta(seconds=3)
    return SignalCandidate(
        observed_at=observed,
        created_at=created,
        symbol="EURUSD",
        timeframe="M5",
        setup_family="SMC_OTE_CONTINUATION",
        scenario="SSL sweep, bullish displacement, OTE retracement",
        plan=TradePlan(
            action="BUY",
            entries=(
                EntryOrder(1.1000, 0.50, "Fibonacci 61.8% retracement"),
                EntryOrder(1.0980, 0.30, "OTE zone"),
                EntryOrder(1.0960, 0.20, "Fibonacci 79% and demand edge"),
            ),
            stop_price=1.0920,
            targets=(1.1070, 1.1120),
            invalidation="Close below protected swing low",
            target_rationale=("Prior high", "External buy-side liquidity"),
        ),
        market_features={
            "structure": {
                "swing_bias": "BULLISH",
                "internal_bias": "BULLISH",
                "bos": True,
            },
            "liquidity": {"ssl_sweep": True, "sweep_depth_atr": 0.32},
            "fibonacci": {"entry_1": 0.618, "entry_2": 0.705, "entry_3": 0.79},
            "volume": {"rvol_20": 1.7, "delta_proxy": 0.22},
            "momentum": {"impulse_atr": 1.4},
            "volatility": {
                "atr": 0.0011,
                "atr_percentile": 55.0,
                "spread_cost_atr": 0.03,
            },
            "confirmation": {"fvg": "BULLISH", "close_confirmed": True},
            "session": {"name": "LONDON_NY_OVERLAP"},
            "execution": {"spread_ok": True},
            "portfolio": {"correlation_load": 0.18},
            "robot_monitoring": {"open_grid_baskets": 2},
        },
        factor_scores={
            "structure": 0.92,
            "liquidity": 0.90,
            "fibonacci": 0.88,
            "volume": 0.82,
            "momentum": 0.84,
            "volatility": 0.78,
            "confirmation": 0.90,
            "session": 0.85,
            "execution": 0.80,
            "portfolio": 0.76,
        },
        factor_reasons={
            "structure": ("bullish BOS", "protected low intact"),
            "liquidity": ("sell-side liquidity swept",),
            "fibonacci": ("OTE retracement",),
            "volume": ("relative volume expansion",),
        },
        provenance=("FX_RESEARCH", "SMC_ENGINE", "VOLUME_COLLECTOR"),
        generated_from_market_data=True,
        robot_context_only={"note": "monitoring context, not a trigger"},
    )


def _evidence_rows(
    candidate: SignalCandidate,
    *,
    wins: int = 35,
    losses: int = 8,
    after_candidate: bool = False,
) -> list[OutcomeObservation]:
    key = similarity_key(candidate)
    rows: list[OutcomeObservation] = []
    base = candidate.created_at + timedelta(minutes=1) if after_candidate else candidate.created_at
    for index in range(losses):
        rows.append(
            OutcomeObservation(
                signal_id=f"LOSS-{after_candidate}-{index}",
                setup_key=key,
                completed_at=(
                    base + timedelta(minutes=index + 1)
                    if after_candidate
                    else base - timedelta(days=wins + losses - index)
                ),
                outcome="LOSS",
                net_r=-0.92,
            )
        )
    for index in range(wins):
        rows.append(
            OutcomeObservation(
                signal_id=f"WIN-{after_candidate}-{index}",
                setup_key=key,
                completed_at=(
                    base + timedelta(minutes=losses + index + 1)
                    if after_candidate
                    else base - timedelta(days=wins - index)
                ),
                outcome="WIN",
                net_r=1.65,
            )
        )
    return rows


def _write_candidates(path: Path, candidates: list[SignalCandidate]) -> None:
    path.write_text(
        "".join(json.dumps(item.as_dict(), sort_keys=True) + "\n" for item in candidates),
        encoding="utf-8",
    )


def _write_outcomes(path: Path, outcomes: list[OutcomeObservation]) -> None:
    path.write_text(
        "".join(json.dumps(item.as_dict(), sort_keys=True) + "\n" for item in outcomes),
        encoding="utf-8",
    )


def test_factory_creates_publishable_passport_from_as_of_evidence(tmp_path: Path) -> None:
    candidate = _candidate()
    candidates_path = tmp_path / "candidates.jsonl"
    outcomes_path = tmp_path / "outcomes.jsonl"
    output_dir = tmp_path / "factory"
    _write_candidates(candidates_path, [candidate])
    _write_outcomes(outcomes_path, _evidence_rows(candidate))

    result = run_factory(
        candidates_path=candidates_path,
        outcomes_path=outcomes_path,
        output_dir=output_dir,
        now=NOW,
    )

    assert result.state == READY_STATE
    assert result.status["publishable"] == 1
    assert result.status["passports_created"] == 1
    passport = json.loads(result.passport_paths[0].read_text(encoding="utf-8"))
    assert passport["signal_id"] == candidate.signal_id
    assert passport["gate_decision"]["state"] == "PUBLISHABLE"
    assert passport["factory"]["future_data_used"] is False
    assert passport["factory"]["historical_outcomes_available_at_cutoff"] == 43


def test_factory_does_not_use_outcomes_completed_after_candidate(tmp_path: Path) -> None:
    candidate = _candidate()
    candidates_path = tmp_path / "candidates.jsonl"
    outcomes_path = tmp_path / "outcomes.jsonl"
    _write_candidates(candidates_path, [candidate])
    prior = _evidence_rows(candidate, wins=8, losses=2)
    future = _evidence_rows(candidate, wins=40, losses=0, after_candidate=True)
    _write_outcomes(outcomes_path, prior + future)

    result = run_factory(
        candidates_path=candidates_path,
        outcomes_path=outcomes_path,
        output_dir=tmp_path / "factory",
        now=NOW,
    )

    assert result.state == WAITING_PUBLISHABLE_STATE
    assert result.status["shadow_only"] == 1
    assert result.evaluations[0]["historical_sample"] == 10
    assert result.evaluations[0]["checks"]["sample"] is False


def test_completed_candidate_is_never_issued_a_passport(tmp_path: Path) -> None:
    candidate = _candidate()
    candidates_path = tmp_path / "candidates.jsonl"
    outcomes_path = tmp_path / "outcomes.jsonl"
    _write_candidates(candidates_path, [candidate])
    completed = OutcomeObservation(
        signal_id=candidate.signal_id,
        setup_key=similarity_key(candidate),
        completed_at=candidate.created_at + timedelta(minutes=5),
        outcome="WIN",
        net_r=1.2,
    )
    _write_outcomes(outcomes_path, [completed])

    result = run_factory(
        candidates_path=candidates_path,
        outcomes_path=outcomes_path,
        output_dir=tmp_path / "factory",
        now=NOW,
    )

    assert result.state == WAITING_FRESH_STATE
    assert result.status["completed_candidates_skipped"] == 1
    assert result.status["fresh_candidates"] == 0


def test_stale_and_future_candidates_are_not_evaluated(tmp_path: Path) -> None:
    stale = _candidate(created_at=NOW - timedelta(seconds=901))
    future = _candidate(created_at=NOW + timedelta(seconds=31))
    candidates_path = tmp_path / "candidates.jsonl"
    outcomes_path = tmp_path / "outcomes.jsonl"
    _write_candidates(candidates_path, [stale, future])
    _write_outcomes(outcomes_path, [])

    result = run_factory(
        candidates_path=candidates_path,
        outcomes_path=outcomes_path,
        output_dir=tmp_path / "factory",
        now=NOW,
    )

    assert result.state == WAITING_FRESH_STATE
    assert result.status["stale_candidates"] == 1
    assert result.status["future_candidates"] == 1
    assert not result.evaluations


def test_factory_is_idempotent_for_existing_passport(tmp_path: Path) -> None:
    candidate = _candidate()
    candidates_path = tmp_path / "candidates.jsonl"
    outcomes_path = tmp_path / "outcomes.jsonl"
    output_dir = tmp_path / "factory"
    _write_candidates(candidates_path, [candidate])
    _write_outcomes(outcomes_path, _evidence_rows(candidate))

    first = run_factory(
        candidates_path=candidates_path,
        outcomes_path=outcomes_path,
        output_dir=output_dir,
        now=NOW,
    )
    original = first.passport_paths[0].read_bytes()
    second = run_factory(
        candidates_path=candidates_path,
        outcomes_path=outcomes_path,
        output_dir=output_dir,
        now=NOW + timedelta(seconds=1),
    )

    assert second.status["passports_created"] == 0
    assert second.status["passports_existing"] == 1
    assert second.passport_paths[0].read_bytes() == original


def test_expired_inbox_passport_is_archived(tmp_path: Path) -> None:
    old_candidate = _candidate(created_at=NOW - timedelta(hours=1))
    candidates_path = tmp_path / "candidates.jsonl"
    outcomes_path = tmp_path / "outcomes.jsonl"
    output_dir = tmp_path / "factory"
    inbox = output_dir / "passports"
    inbox.mkdir(parents=True)
    old_path = inbox / f"{old_candidate.signal_id}.json"
    old_path.write_text(
        json.dumps(
            {"signal_id": old_candidate.signal_id, "candidate": old_candidate.as_dict()}
        ),
        encoding="utf-8",
    )
    _write_candidates(candidates_path, [])
    _write_outcomes(outcomes_path, [])

    result = run_factory(
        candidates_path=candidates_path,
        outcomes_path=outcomes_path,
        output_dir=output_dir,
        now=NOW,
    )

    assert result.status["passports_archived"] == 1
    assert not old_path.exists()
    assert len(list((output_dir / "archive").glob("*.json"))) == 1


def test_bridge_rejects_factory_passport_when_cost_model_differs(tmp_path: Path) -> None:
    candidate = _candidate()
    candidates_path = tmp_path / "candidates.jsonl"
    outcomes_path = tmp_path / "outcomes.jsonl"
    output_dir = tmp_path / "factory"
    _write_candidates(candidates_path, [candidate])
    _write_outcomes(outcomes_path, _evidence_rows(candidate))
    result = run_factory(
        candidates_path=candidates_path,
        outcomes_path=outcomes_path,
        output_dir=output_dir,
        cost_r=0.04,
        now=NOW,
    )

    with pytest.raises(ValueError, match="stored expected value differs"):
        validate_publishable_passport(
            result.passport_paths[0],
            now=NOW,
            cost_r=0.05,
        )
