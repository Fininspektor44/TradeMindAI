from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from trademind.signal_intelligence import (
    EntryOrder,
    HistoricalEvidence,
    SignalCandidate,
    TradePlan,
    append_journal_event,
    build_passport,
    evaluate_candidate,
    verify_journal,
    wilson_lower_bound,
)


NOW = datetime(2026, 8, 5, 13, 30, tzinfo=timezone.utc)


def _candidate(
    *,
    generated_from_market_data: bool = True,
    first_target: float = 1.1060,
) -> SignalCandidate:
    plan = TradePlan(
        action="BUY",
        entries=(
            EntryOrder(
                price=1.1000,
                allocation=0.50,
                rationale="Fibonacci 61.8% retracement",
            ),
            EntryOrder(
                price=1.0980,
                allocation=0.30,
                rationale="OTE zone",
            ),
            EntryOrder(
                price=1.0960,
                allocation=0.20,
                rationale="Fibonacci 79% and demand edge",
            ),
        ),
        stop_price=1.0920,
        targets=(first_target, 1.1120),
        invalidation="Close below protected swing low",
        target_rationale=("Prior high", "External buy-side liquidity"),
    )
    return SignalCandidate(
        observed_at=NOW,
        created_at=NOW + timedelta(seconds=3),
        symbol="EURUSD",
        timeframe="M5",
        setup_family="SMC_OTE_CONTINUATION",
        scenario="SSL sweep, bullish displacement, OTE retracement",
        plan=plan,
        market_features={
            "structure": {"swing_bias": "BULLISH", "bos": True},
            "liquidity": {"ssl_sweep": True, "sweep_depth_atr": 0.32},
            "fibonacci": {"entry_1": 0.618, "entry_2": 0.705, "entry_3": 0.79},
            "volume": {"rvol_20": 1.7, "delta_proxy": 0.22},
            "momentum": {"impulse_atr": 1.4},
            "volatility": {"atr": 0.0011, "spread_cost_atr": 0.03},
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
        generated_from_market_data=generated_from_market_data,
        robot_context_only={"note": "monitoring context, not a trigger"},
    )


def _evidence(
    *,
    wins: int = 34,
    losses: int = 8,
    flats: int = 1,
    average_win_r: float = 1.65,
    average_loss_r: float = -0.92,
    recent_win_rate: float = 0.78,
    baseline_win_rate: float = 0.76,
) -> HistoricalEvidence:
    return HistoricalEvidence(
        setup_key="EURUSD|M5|BUY|SMC_OTE_CONTINUATION",
        captured_at=NOW,
        wins=wins,
        losses=losses,
        flats=flats,
        gross_win_r=56.1,
        gross_loss_r=7.36,
        average_win_r=average_win_r,
        average_loss_r=average_loss_r,
        max_drawdown_r=3.2,
        recent_win_rate=recent_win_rate,
        baseline_win_rate=baseline_win_rate,
    )


def test_publishable_passport_uses_market_data_and_conservative_evidence() -> None:
    passport = build_passport(_candidate(), _evidence(), cost_r=0.04, now=NOW)

    assert passport.decision.state == "PUBLISHABLE"
    assert passport.decision.publishable is True
    assert passport.decision.quality_score > 80
    assert passport.decision.conservative_probability < passport.evidence.raw_win_rate
    assert passport.decision.expected_value_r > 0
    assert passport.as_dict()["safety"]["orders_enabled"] is False
    assert (
        passport.as_dict()["safety"]["robot_signals_used_as_primary_trigger"]
        is False
    )


def test_small_sample_remains_shadow_only() -> None:
    evidence = _evidence(wins=9, losses=2, flats=0)
    decision = evaluate_candidate(_candidate(), evidence, now=NOW)

    assert decision.state == "SHADOW_ONLY"
    assert decision.checks["sample"] is False
    assert any("completed sample" in reason for reason in decision.reasons)


def test_negative_expectancy_is_rejected_even_with_high_quality() -> None:
    evidence = _evidence(
        wins=25,
        losses=18,
        flats=0,
        average_win_r=0.45,
        average_loss_r=-1.0,
    )
    decision = evaluate_candidate(_candidate(), evidence, cost_r=0.10, now=NOW)

    assert decision.state == "REJECTED"
    assert decision.checks["expected_value"] is False


def test_robot_derived_candidate_is_rejected() -> None:
    decision = evaluate_candidate(
        _candidate(generated_from_market_data=False),
        _evidence(),
        now=NOW,
    )

    assert decision.state == "REJECTED"
    assert decision.checks["market_generated"] is False


def test_signal_id_is_stable_and_does_not_depend_on_evidence() -> None:
    first = _candidate()
    second = _candidate()
    passport_a = build_passport(first, _evidence(), now=NOW)
    passport_b = build_passport(second, _evidence(wins=40, losses=3), now=NOW)

    assert first.signal_id == second.signal_id
    assert passport_a.candidate.signal_id == passport_b.candidate.signal_id
    assert first.signal_id.startswith("TM-20260805T133000Z-EURUSD-BUY-")


def test_wilson_lower_bound_matches_expected_range() -> None:
    lower = wilson_lower_bound(35, 43)

    assert 0.67 < lower < 0.68


def test_append_only_journal_detects_tampering_and_candidate_mutation(
    tmp_path: Path,
) -> None:
    candidate = _candidate()
    path = tmp_path / "signal_events.jsonl"
    append_journal_event(
        path,
        signal_id=candidate.signal_id,
        event_type="CANDIDATE",
        payload=candidate.as_dict(),
        recorded_at=candidate.created_at,
    )
    append_journal_event(
        path,
        signal_id=candidate.signal_id,
        event_type="GATE_DECISION",
        payload={"state": "PUBLISHABLE"},
        recorded_at=candidate.created_at + timedelta(seconds=1),
    )

    assert verify_journal(path) == (True, "OK")

    with pytest.raises(ValueError, match="immutable candidate mutation"):
        append_journal_event(
            path,
            signal_id=candidate.signal_id,
            event_type="CANDIDATE",
            payload={"signal_id": candidate.signal_id, "changed": True},
            recorded_at=candidate.created_at + timedelta(seconds=2),
        )

    lines = path.read_text(encoding="utf-8").splitlines()
    event = json.loads(lines[0])
    event["payload"]["symbol"] = "GBPUSD"
    lines[0] = json.dumps(event, ensure_ascii=False, sort_keys=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    valid, reason = verify_journal(path)
    assert valid is False
    assert "hash mismatch" in reason


def test_trade_plan_rejects_invalid_stop_geometry() -> None:
    with pytest.raises(ValueError, match="BUY stop"):
        TradePlan(
            action="BUY",
            entries=(EntryOrder(1.10, 1.0, "entry"),),
            stop_price=1.11,
            targets=(1.12,),
            invalidation="invalidated",
        )
