from __future__ import annotations

import pytest

from trademind.crypto_h1_swing_filter import FlowBar
from trademind.crypto_h1_swing_filter_v133 import (
    VERSION,
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


def base_history() -> list[FlowBar]:
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
    return [flow(index, close) for index, close in enumerate(closes)]


def snapshot(
    *,
    h1_bias: str = "BULLISH",
    m15_bias: str = "BULLISH",
    m15_break: str = "BULLISH",
    target: float = 135.0,
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


def recent_breakout_rows(*, final_close: float = 109.1, breakout_volume: float = 110.0) -> list[FlowBar]:
    rows = base_history()
    rows.append(flow(24, 109.0, volume=breakout_volume, delta=500.0))
    rows.append(flow(25, 109.3, volume=80.0, delta=-50.0))
    rows.append(flow(26, final_close, volume=90.0, delta=-10.0))
    return rows


def test_version_is_v1331() -> None:
    assert VERSION == "1.33.1"


def test_accepts_breakout_from_two_bars_ago_when_level_is_held() -> None:
    result = evaluate_opportunity("BUY", recent_breakout_rows(), snapshot())

    assert result.eligible is True
    assert result.breakout_level == pytest.approx(108.5)
    assert result.entry == pytest.approx(109.1)
    assert result.volume_ratio == pytest.approx(1.1)
    assert result.delta_turnover == pytest.approx(500.0)
    assert "M5_RECENT_EXTREMUM_CLOSE_BREAK" in result.reasons
    assert "M5_BREAKOUT_HELD" in result.reasons


def test_rejects_recent_breakout_when_latest_close_loses_level() -> None:
    result = evaluate_opportunity(
        "BUY",
        recent_breakout_rows(final_close=108.4),
        snapshot(),
    )

    assert result.eligible is False
    assert result.reasons == ("M5_BREAKOUT_NOT_HELD",)


def test_opposite_m15_bias_alone_is_not_a_veto() -> None:
    result = evaluate_opportunity(
        "BUY",
        recent_breakout_rows(),
        snapshot(m15_bias="BEARISH", m15_break="NONE"),
    )

    assert result.eligible is True


def test_opposite_m15_structure_break_remains_a_veto() -> None:
    result = evaluate_opportunity(
        "BUY",
        recent_breakout_rows(),
        snapshot(m15_bias="NEUTRAL", m15_break="BEARISH"),
    )

    assert result.eligible is False
    assert result.reasons == ("M15_BREAK_VETO",)


def test_breakout_bar_must_still_have_delta_confirmation() -> None:
    rows = recent_breakout_rows()
    rows[24] = flow(24, 109.0, volume=110.0, delta=-1.0)

    result = evaluate_opportunity("BUY", rows, snapshot())

    assert result.eligible is False
    assert result.reasons == ("M5_DELTA_NOT_BULLISH",)


def test_breakout_volume_must_meet_one_x_baseline() -> None:
    result = evaluate_opportunity(
        "BUY",
        recent_breakout_rows(breakout_volume=99.0),
        snapshot(),
    )

    assert result.eligible is False
    assert result.reasons == ("M5_VOLUME_BELOW_THRESHOLD",)


def test_shadow_rr_floor_is_1_2r() -> None:
    accepted = evaluate_opportunity("BUY", recent_breakout_rows(), snapshot(target=122.0))
    rejected = evaluate_opportunity("BUY", recent_breakout_rows(), snapshot(target=121.0))

    assert accepted.eligible is True
    assert accepted.target_rr >= 1.2
    assert rejected.eligible is False
    assert rejected.reasons == ("H1_TARGET_BELOW_MINIMUM_RR",)


def test_safety_contract_remains_read_only() -> None:
    assert safety_contract() == {
        "read_only": True,
        "orders_enabled": False,
        "publication_enabled": False,
        "exchange_api_called": False,
        "future_bars_used": False,
        "account_sizing_calculated": False,
    }
