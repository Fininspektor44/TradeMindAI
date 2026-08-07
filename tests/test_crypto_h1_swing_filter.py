from __future__ import annotations

from datetime import datetime, timezone

import pytest

from trademind.crypto_h1_swing_filter import (
    SETUP_FAMILY,
    FlowBar,
    Opportunity,
    build_candidate,
    evaluate_opportunity,
    safety_contract,
)


def flow(
    index: int,
    close: float,
    *,
    volume: float = 100.0,
    delta: float = 0.0,
    high_extra: float = 0.5,
    low_extra: float = 0.5,
) -> FlowBar:
    return FlowBar(
        start_ms=1_786_003_200_000 + index * 300_000,
        open=close - 0.2,
        high=close + high_extra,
        low=close - low_extra,
        close=close,
        volume=volume,
        delta_turnover=delta,
    )


def bullish_bars(*, trigger_close: float = 109.0, volume: float = 130.0, delta: float = 500.0):
    closes = [
        100,
        101,
        102,
        104,
        106,
        104,
        102,
        100,
        98,
        100,
        102,
        104,
        107,
        105,
        103,
        101,
        99,
        101,
        104,
        108,
        105,
        103,
        101,
        104,
    ]
    rows = [flow(index, close) for index, close in enumerate(closes)]
    rows.append(flow(len(rows), trigger_close, volume=volume, delta=delta))
    return rows


def snapshot(
    *,
    h1_bias: str = "BULLISH",
    m15_bias: str = "BULLISH",
    m15_break: str = "BULLISH",
    target: float = 130.0,
    atr_h1: float = 20.0,
):
    return {
        "state": "OK",
        "timeframes": {
            "H1": {
                "bias": h1_bias,
                "break": "BULLISH_BOS",
                "break_direction": "BULLISH",
                "last_swing_high": target,
                "last_swing_low": 90.0,
            },
            "M15": {
                "bias": m15_bias,
                "break": "BULLISH_BOS" if m15_break == "BULLISH" else "BEARISH_BOS",
                "break_direction": m15_break,
            },
        },
        "volatility": {"atr_h1": atr_h1},
    }


def test_accepts_only_clean_h1_aligned_m5_volume_breakout() -> None:
    result = evaluate_opportunity("BUY", bullish_bars(), snapshot())

    assert isinstance(result, Opportunity)
    assert result.eligible is True
    assert result.breakout_level == pytest.approx(108.5)
    assert result.volume_ratio == pytest.approx(1.3)
    assert result.entry == 109.0
    assert result.stop == pytest.approx(98.5)
    assert result.target == 130.0
    assert result.target_rr >= 1.8
    assert result.target_atr_h1 >= 0.7


def test_wick_without_close_is_not_a_breakout() -> None:
    rows = bullish_bars(trigger_close=108.0)
    rows[-1] = flow(24, 108.0, volume=150.0, delta=500.0, high_extra=2.0)

    result = evaluate_opportunity("BUY", rows, snapshot())

    assert result.eligible is False
    assert result.reasons == ("M5_CLOSE_DID_NOT_BREAK_LAST_EXTREMUM",)


def test_rejects_breakout_without_volume_confirmation() -> None:
    result = evaluate_opportunity("BUY", bullish_bars(volume=119.0), snapshot())

    assert result.eligible is False
    assert result.reasons == ("M5_VOLUME_BELOW_THRESHOLD",)


def test_rejects_breakout_with_opposite_delta() -> None:
    result = evaluate_opportunity("BUY", bullish_bars(delta=-1.0), snapshot())

    assert result.eligible is False
    assert result.reasons == ("M5_DELTA_NOT_BULLISH",)


def test_m15_is_only_a_veto_layer() -> None:
    neutral = evaluate_opportunity(
        "BUY",
        bullish_bars(),
        snapshot(m15_bias="NEUTRAL", m15_break="NONE"),
    )
    opposed = evaluate_opportunity(
        "BUY",
        bullish_bars(),
        snapshot(m15_bias="BEARISH", m15_break="BEARISH"),
    )

    assert neutral.eligible is True
    assert opposed.eligible is False
    assert opposed.reasons == ("M15_BIAS_VETO",)


def test_rejects_small_h1_opportunity_even_when_rr_is_large_enough() -> None:
    result = evaluate_opportunity(
        "BUY",
        bullish_bars(),
        snapshot(target=130.0, atr_h1=100.0),
    )

    assert result.target_rr == 0.0
    assert result.eligible is False
    assert result.reasons == ("H1_TARGET_DISTANCE_TOO_SMALL",)


def test_candidate_uses_structural_m5_stop_and_h1_target() -> None:
    opportunity = evaluate_opportunity("BUY", bullish_bars(), snapshot())
    row = {
        "decision_id": "BTCUSDT:1786003200000:MTF_FLOW_ALIGNMENT",
        "signal_time": datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc).isoformat(),
        "symbol": "BTCUSDT",
        "action": "BUY",
        "entry_price": "109",
        "stop_price": "108",
        "target_price": "110.5",
        "quality_score": "80",
    }

    candidate = build_candidate(row, snapshot(), opportunity)

    assert candidate.setup_family == SETUP_FAMILY
    assert candidate.plan.average_entry == 109.0
    assert candidate.plan.stop_price == pytest.approx(98.5)
    assert candidate.plan.targets == (130.0,)
    assert candidate.plan.first_target_rr >= 1.8
    assert candidate.market_features["confirmation"]["close_confirmed"] is True
    assert candidate.market_features["custom"]["opportunity_eligible"] is True


def test_safety_contract_is_strictly_read_only() -> None:
    assert safety_contract() == {
        "read_only": True,
        "orders_enabled": False,
        "publication_enabled": False,
        "exchange_api_called": False,
        "future_bars_used": False,
        "account_sizing_calculated": False,
    }
