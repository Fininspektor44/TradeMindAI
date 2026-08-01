from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from trademind.market.models import Candle
from trademind.ote_engine import _outcome, build_ote_signals, score_signal
from trademind.ote_models import fib_prices, resample
from trademind.ote_report import build_states


def _candle(index: int, open_: float, high: float, low: float, close: float) -> Candle:
    return Candle(
        symbol="EURUSD",
        timeframe="M5",
        time=datetime(2026, 1, 5, tzinfo=timezone.utc) + timedelta(minutes=5 * index),
        open=open_,
        high=high,
        low=low,
        close=close,
    )


def test_fib_prices_match_buy_and_sell_ote_geometry() -> None:
    buy = fib_prices("BUY", 100.0, 110.0)
    sell = fib_prices("SELL", 110.0, 100.0)

    assert buy == pytest.approx({"618": 103.82, "705": 102.95, "790": 102.10})
    assert sell == pytest.approx({"618": 106.18, "705": 107.05, "790": 107.90})


def test_resample_uses_bucket_end_time() -> None:
    candles = [_candle(index, 1.0, 1.2, 0.9, 1.1) for index in range(12)]

    bars = resample(candles, 60)

    assert len(bars) == 1
    assert bars[0].end_time == datetime(2026, 1, 5, 1, 0, tzinfo=timezone.utc)


def test_touch_entry_stop_on_entry_bar_is_pessimistic_loss() -> None:
    candles = [
        _candle(0, 105.0, 106.0, 99.0, 104.0),
        *[_candle(index, 104.0, 106.0, 103.0, 105.0) for index in range(1, 40)],
    ]
    signal = {
        "action": "BUY",
        "entry_price": "104",
        "stop_price": "100",
        "target_price": "110",
        "risk_price": "4",
    }

    outcome = _outcome(signal, candles, 0, 36, touch_entry=True)

    assert outcome[0] == "LOSS"
    assert outcome[3] == -1.0


def test_score_rewards_context_sweep_and_confirmation() -> None:
    weak, _ = score_signal(
        action="BUY",
        variant="TOUCH_618",
        h1_bias="NEUTRAL",
        h4_bias="NEUTRAL",
        sweep=False,
        setup_break="BULLISH_PIVOT_BREAK",
        impulse_atr=1.0,
        fvg_aligned=False,
        session="ASIA",
        rvol=0.8,
        tick_ratio=0.9,
        imbalance=0.0,
        spread_ratio=1.4,
        confirmed=False,
    )
    strong, reasons = score_signal(
        action="BUY",
        variant="TOUCH_705",
        h1_bias="BULLISH",
        h4_bias="BULLISH",
        sweep=True,
        setup_break="BULLISH_CHOCH",
        impulse_atr=2.8,
        fvg_aligned=True,
        session="LONDON_NY_OVERLAP",
        rvol=1.6,
        tick_ratio=1.3,
        imbalance=0.2,
        spread_ratio=1.0,
        confirmed=True,
    )

    assert strong > weak
    assert strong == 100
    assert "body-close confirmation" in reasons


def _synthetic_rows() -> list[dict[str, str]]:
    prices: list[tuple[float, float, float, float]] = []
    for index in range(35):
        base = 1.104 + (index % 6) * 0.0004
        prices.append((base, base + 0.0006, base - 0.0006, base + 0.0001))
    prices.extend(
        [
            (1.107, 1.1100, 1.1065, 1.1085),
            (1.108, 1.1090, 1.1050, 1.1060),
            (1.106, 1.1070, 1.1020, 1.1030),
            (1.103, 1.1040, 1.0990, 1.1010),
            (1.101, 1.1050, 1.1005, 1.1040),
            (1.104, 1.1070, 1.1035, 1.1060),
            (1.106, 1.1090, 1.1055, 1.1080),
            (1.108, 1.1100, 1.1075, 1.1090),
            (1.109, 1.1130, 1.1085, 1.1120),
            (1.112, 1.1150, 1.1110, 1.1140),
            (1.114, 1.1142, 1.1045, 1.1060),
            (1.106, 1.1160, 1.1055, 1.1150),
        ]
    )
    prices.extend((1.115, 1.1155, 1.1146, 1.1151) for _ in range(170))
    start = datetime(2026, 1, 5, tzinfo=timezone.utc)
    rows: list[dict[str, str]] = []
    for index, (open_, high, low, close) in enumerate(prices):
        rows.append(
            {
                "symbol": "EURUSD",
                "timeframe": "M5",
                "time": str(int((start + timedelta(minutes=5 * index)).timestamp())),
                "open": str(open_),
                "high": str(high),
                "low": str(low),
                "close": str(close),
                "bar_tick_volume": "100",
                "tick_count": "100",
                "point": "0.00001",
                "tick_copy_status": "OK",
                "spread_mean_points": "2",
                "tick_rate_per_sec": "1",
                "direction_imbalance": "0.1",
                "rvol_20": "1.3",
            }
        )
    return rows


def test_synthetic_structure_produces_unique_ote_signals() -> None:
    signals = build_ote_signals(_synthetic_rows())

    assert signals
    assert any(row["action"] == "BUY" and row["variant"] == "TOUCH_618" for row in signals)
    assert len({row["signal_id"] for row in signals}) == len(signals)
    assert all(float(row["risk_price"]) > 0 and float(row["reward_price"]) > 0 for row in signals)


def test_states_compare_score_thresholds_without_promoting_small_sample() -> None:
    signal = {
        "symbol": "EURUSD",
        "action": "BUY",
        "variant": "TOUCH_705",
        "score": "75",
        "signal_time": "2026-01-05T10:00:00+00:00",
        "outcome_h3": "WIN",
        "result_r_h3": "1.5",
        "outcome_h6": "WIN",
        "result_r_h6": "1.5",
        "outcome_h12": "WIN",
        "result_r_h12": "1.5",
    }

    states = build_states([signal], datetime(2026, 1, 5, tzinfo=timezone.utc))

    filters = {row["score_filter"] for row in states}
    assert filters == {"ALL", "SCORE_60", "SCORE_70"}
    assert all(row["status"] == "INSUFFICIENT_SAMPLE" for row in states)


def test_smc_ote_contract_is_read_only() -> None:
    text = "\n".join(
        Path(path).read_text(encoding="utf-8")
        for path in (
            "src/trademind/ote_models.py",
            "src/trademind/ote_engine.py",
            "src/trademind/ote_report.py",
            "src/trademind/smc_ote.py",
            "scripts/run_v150_smc_ote.ps1",
            "scripts/install_v150_smc_ote_task.ps1",
        )
    )
    forbidden = (
        "CTrade",
        "OrderSend(",
        ".Buy(",
        ".Sell(",
        "PositionClose(",
        "TRADE_ACTION_DEAL",
    )
    assert all(token not in text for token in forbidden)
