from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from trademind.signal_evidence import (
    OutcomeObservation,
    aggregate_evidence,
    load_outcomes,
    similarity_dimensions,
    similarity_key,
)
from trademind.signal_intelligence import EntryOrder, SignalCandidate, TradePlan


NOW = datetime(2026, 8, 5, 13, 30, tzinfo=timezone.utc)


def _candidate() -> SignalCandidate:
    return SignalCandidate(
        observed_at=NOW,
        created_at=NOW + timedelta(seconds=2),
        symbol="EURUSD",
        timeframe="M5",
        setup_family="SMC_OTE_CONTINUATION",
        scenario="bullish continuation",
        plan=TradePlan(
            action="BUY",
            entries=(EntryOrder(1.10, 1.0, "OTE entry"),),
            stop_price=1.095,
            targets=(1.1075,),
            invalidation="protected low broken",
        ),
        market_features={
            "structure": {
                "swing_bias": "BULLISH",
                "internal_bias": "BULLISH",
            },
            "liquidity": {"ssl_sweep": True},
            "fibonacci": {"retracement": 0.68},
            "volume": {"rvol_20": 1.45},
            "momentum": {"impulse_atr": 1.25},
            "volatility": {"atr_percentile": 64, "spread_cost_atr": 0.04},
            "confirmation": {"fvg": "BULLISH"},
            "session": {"name": "LONDON_NY_OVERLAP"},
        },
        factor_scores={
            "structure": 0.9,
            "liquidity": 0.9,
            "fibonacci": 0.8,
            "volume": 0.8,
            "momentum": 0.8,
            "volatility": 0.8,
            "confirmation": 0.9,
            "session": 0.9,
            "execution": 0.8,
            "portfolio": 0.7,
        },
        factor_reasons={"structure": ("aligned",)},
        provenance=("FX_RESEARCH",),
    )


def test_similarity_key_is_versioned_and_captures_market_regimes() -> None:
    candidate = _candidate()
    dimensions = similarity_dimensions(candidate)
    key = similarity_key(candidate)

    assert dimensions["version"] == "SIM_V1"
    assert dimensions["liquidity"] == "SSL"
    assert dimensions["fibonacci"] == "OTE_618_705"
    assert dimensions["volume_regime"] == "HIGH"
    assert dimensions["momentum_regime"] == "STRONG"
    assert "setup_family=SMC_OTE_CONTINUATION" in key
    assert "session=LONDON_NY_OVERLAP" in key


def test_aggregate_evidence_uses_only_matching_completed_setups() -> None:
    candidate = _candidate()
    key = similarity_key(candidate)
    observations = [
        OutcomeObservation(
            signal_id=f"S{i}",
            setup_key=key,
            completed_at=NOW + timedelta(minutes=i),
            outcome="WIN" if i < 8 else "LOSS",
            net_r=1.5 if i < 8 else -1.0,
        )
        for i in range(10)
    ]
    observations.append(
        OutcomeObservation(
            signal_id="OTHER",
            setup_key="SIM_V1|other",
            completed_at=NOW,
            outcome="LOSS",
            net_r=-5.0,
        )
    )

    evidence = aggregate_evidence(candidate, observations, captured_at=NOW)

    assert evidence.completed == 10
    assert evidence.wins == 8
    assert evidence.losses == 2
    assert evidence.raw_win_rate == 0.8
    assert evidence.gross_win_r == 12.0
    assert evidence.gross_loss_r == 2.0
    assert evidence.average_win_r == 1.5
    assert evidence.average_loss_r == -1.0
    assert evidence.profit_factor_r == 6.0


def test_maximum_drawdown_is_calculated_in_completion_order() -> None:
    candidate = _candidate()
    key = similarity_key(candidate)
    outcomes = [
        OutcomeObservation("A", key, NOW, "WIN", 2.0),
        OutcomeObservation("B", key, NOW + timedelta(minutes=1), "LOSS", -1.0),
        OutcomeObservation("C", key, NOW + timedelta(minutes=2), "LOSS", -1.5),
        OutcomeObservation("D", key, NOW + timedelta(minutes=3), "WIN", 1.0),
    ]

    evidence = aggregate_evidence(candidate, outcomes, captured_at=NOW)

    assert evidence.max_drawdown_r == 2.5


def test_load_outcomes_rejects_duplicate_signal_ids(tmp_path: Path) -> None:
    path = tmp_path / "outcomes.jsonl"
    path.write_text(
        '{"signal_id":"A","setup_key":"K","completed_at":"2026-08-05T13:00:00+00:00","outcome":"WIN","net_r":1.0}\n'
        '{"signal_id":"A","setup_key":"K","completed_at":"2026-08-05T14:00:00+00:00","outcome":"LOSS","net_r":-1.0}\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="duplicate outcome"):
        load_outcomes(path)


def test_outcome_sign_must_match_label() -> None:
    with pytest.raises(ValueError, match="WIN must have positive"):
        OutcomeObservation(
            signal_id="A",
            setup_key="K",
            completed_at=NOW,
            outcome="WIN",
            net_r=-1.0,
        )
