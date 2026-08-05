from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from trademind.signal_intelligence import EntryOrder, SignalCandidate, TradePlan
from trademind.signal_shadow import Bar, append_outcomes, evaluate_shadow_candidate


NOW = datetime(2026, 8, 5, 13, 30, tzinfo=timezone.utc)


def _candidate() -> SignalCandidate:
    return SignalCandidate(
        observed_at=NOW,
        created_at=NOW,
        symbol="EURUSD",
        timeframe="M5",
        setup_family="SMC_OTE_CONTINUATION",
        scenario="shadow test",
        plan=TradePlan(
            action="BUY",
            entries=(
                EntryOrder(1.1000, 0.50, "confirmation", "MARKET"),
                EntryOrder(1.0980, 0.30, "OTE 70.5%", "LIMIT"),
                EntryOrder(1.0960, 0.20, "OTE 79%", "LIMIT"),
            ),
            stop_price=1.0920,
            targets=(1.1080, 1.1120),
            invalidation="protected low broken",
        ),
        market_features={
            "structure": {"swing_bias": "BULLISH"},
            "liquidity": {"ssl_sweep": True},
            "fibonacci": {"retracement": 0.70},
            "volume": {"rvol_20": 1.5},
            "momentum": {"impulse_atr": 1.2},
            "volatility": {"atr_percentile": 50, "spread_cost_atr": 0.04},
            "confirmation": {"fvg": "BULLISH"},
            "session": {"name": "LONDON"},
        },
        factor_scores={
            "structure": 0.9,
            "liquidity": 0.9,
            "fibonacci": 0.9,
            "volume": 0.8,
            "momentum": 0.8,
            "volatility": 0.8,
            "confirmation": 0.9,
            "session": 0.9,
            "execution": 0.8,
            "portfolio": 0.5,
        },
        factor_reasons={"structure": ("aligned",)},
        provenance=("TEST",),
    )


def _bar(minutes: int, low: float, high: float, close: float) -> Bar:
    return Bar(
        time=NOW + timedelta(minutes=minutes),
        symbol="EURUSD",
        timeframe="M5",
        open=close,
        high=high,
        low=low,
        close=close,
    )


def test_shadow_fills_ladder_and_records_target_win() -> None:
    candidate = _candidate()
    bars = [
        _bar(5, 1.0975, 1.1010, 1.0990),
        _bar(10, 1.0955, 1.1020, 1.1010),
        _bar(15, 1.1010, 1.1090, 1.1085),
    ]

    outcome = evaluate_shadow_candidate(candidate, bars, max_bars=12, cost_r=0.04)

    assert outcome is not None
    assert outcome.outcome == "WIN"
    assert outcome.exit_reason == "TARGET_1"
    assert outcome.filled_entries == 3
    assert outcome.allocation_filled == 1.0
    assert outcome.net_r > 1.0
    assert outcome.mfe_r > 1.0


def test_same_bar_stop_and_target_is_counted_as_stop() -> None:
    candidate = _candidate()
    bars = [_bar(5, 1.0910, 1.1090, 1.1000)]

    outcome = evaluate_shadow_candidate(candidate, bars, max_bars=12)

    assert outcome is not None
    assert outcome.outcome == "LOSS"
    assert outcome.exit_reason == "STOP_FIRST_CONSERVATIVE"
    assert outcome.net_r < 0


def test_incomplete_future_window_stays_active() -> None:
    candidate = _candidate()
    bars = [_bar(5, 1.0990, 1.1020, 1.1010)]

    outcome = evaluate_shadow_candidate(candidate, bars, max_bars=3)

    assert outcome is None


def test_timeout_marks_to_market_after_required_bars() -> None:
    candidate = _candidate()
    bars = [
        _bar(5, 1.0990, 1.1020, 1.1010),
        _bar(10, 1.0985, 1.1030, 1.1020),
        _bar(15, 1.0995, 1.1040, 1.1030),
    ]

    outcome = evaluate_shadow_candidate(candidate, bars, max_bars=3, cost_r=0.04)

    assert outcome is not None
    assert outcome.exit_reason == "TIMEOUT_MARK_TO_MARKET"
    assert outcome.outcome == "WIN"
    assert outcome.bars_observed == 3


def test_outcome_file_is_append_only(tmp_path: Path) -> None:
    candidate = _candidate()
    outcome = evaluate_shadow_candidate(
        candidate,
        [_bar(5, 1.0970, 1.1090, 1.1080)],
        max_bars=3,
    )
    assert outcome is not None
    path = tmp_path / "outcomes.jsonl"

    assert append_outcomes(path, [outcome]) == 1
    assert append_outcomes(path, [outcome]) == 0

    changed = type(outcome)(
        **{**outcome.as_dict(), "net_r": outcome.net_r + 1.0}
    )
    with pytest.raises((TypeError, ValueError)):
        append_outcomes(path, [changed])
