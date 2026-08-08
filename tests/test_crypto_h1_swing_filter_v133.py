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


def recent_breakout_rows(*, final_close: float = 109.1) -> list[FlowBar]:
    rows = base_history()
    rows.append(flow(24, 109.0, volume=130.0, delta=500.0))
    rows.append(flow(25, 109.3, volume=80.0, delta=-50.0))
    rows.append(flow(26, final_close, volume=90.0, delta=-10.0))
    return rows


def test_version_is_v133() -> None:
    assert VERSION == "1.33.0"


def test_accepts_breakout_from_two_bars_ago_when_level_is_held() -> None:
    result = evaluate_opportunity("BUY", recent_breakout_rows(), snapshot())

    assert result.eligible is True
    assert result.breakout_level == pytest.approx(108.5)
    assert result.entry == pytest.approx(109.1)
    assert result.volume_ratio == pytest.approx(1.3)
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


def test_breakout_bar_must_still_have_volume_and_delta_confirmation() -> None:
    rows = base_history()
    rows.append(flow(24, 109.0, volume=119.0, delta=500.0))
    rows.append(flow(25, 109.2, volume=500.0, delta=999.0))
    rows.append(flow(26, 109.1, volume=500.0, delta=999.0))

    result = evaluate_opportunity("BUY", rows, snapshot())

    assert result.eligible is False
    assert result.reasons == ("M5_VOLUME_BELOW_THRESHOLD",)


def test_safety_contract_remains_read_only() -> None:
    assert safety_contract() == {
        "read_only": True,
        "orders_enabled": False,
        "publication_enabled": False,
        "exchange_api_called": False,
        "future_bars_used": False,
        "account_sizing_calculated": False,
    }
