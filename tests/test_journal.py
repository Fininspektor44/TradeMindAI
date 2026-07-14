"""Tests for persistent signal journaling and forward evaluation."""

from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone

import pytest

from trademind.journal import SignalJournal
from trademind.market.models import Candle
from trademind.signals.models import SignalAction, SignalResult


def _candles(count: int = 8) -> list[Candle]:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return [
        Candle(
            symbol="XAUUSD",
            timeframe="M5",
            time=start + timedelta(minutes=5 * index),
            open=100.0 + index,
            high=101.0 + index,
            low=99.5 + index,
            close=100.5 + index,
            tick_volume=100 + index,
            spread=2,
        )
        for index in range(count)
    ]


def _result(action: SignalAction = SignalAction.BUY) -> SignalResult:
    return SignalResult(
        symbol="XAUUSD",
        timeframe="M5",
        action=action,
        score=70 if action is SignalAction.BUY else 0,
        confidence=70 if action is SignalAction.BUY else 0,
        ema_fast=101.0,
        ema_slow=100.0,
        rsi=60.0,
        atr=1.5,
        reasons=("test reason",),
    )


def test_records_once_and_evaluates_future_horizon(tmp_path) -> None:
    candles = _candles()
    journal = SignalJournal(tmp_path, horizons=(3,), point_sizes={"XAUUSD": 0.01})

    assert journal.record(_result(), candles[0], history=candles[:1]) is True
    assert journal.record(_result(), candles[0], history=candles[:1]) is False
    assert journal.evaluate("XAUUSD", "M5", candles) == 1

    with journal.path.open("r", encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))

    assert len(rows) == 1
    assert rows[0]["schema_version"] == "1.0"
    assert rows[0]["outcome_3"] == "WIN"
    assert float(rows[0]["directional_move_3"]) == pytest.approx(3.0)
    assert float(rows[0]["net_move_3"]) == pytest.approx(2.98)
    assert float(rows[0]["progress_atr_3"]) == pytest.approx(2.98 / 1.5)
    assert float(rows[0]["mfe_3"]) == pytest.approx(3.5)
    assert float(rows[0]["mae_3"]) == pytest.approx(0.0)
    assert float(rows[0]["mfe_atr_3"]) == pytest.approx(3.5 / 1.5)
    assert rows[0]["bars_to_mfe_3"] == "3"
    assert rows[0]["bars_to_mae_3"] == ""


def test_records_spread_and_volume_features(tmp_path) -> None:
    candles = _candles(count=22)
    journal = SignalJournal(tmp_path, horizons=(3,), point_sizes={"XAUUSD": 0.01})

    journal.record(_result(), candles[-1], history=candles)

    with journal.path.open("r", encoding="utf-8", newline="") as handle:
        row = next(csv.DictReader(handle))

    expected_mean = sum(candle.tick_volume for candle in candles[1:21]) / 20
    assert int(row["spread_points"]) == 2
    assert float(row["spread_cost"]) == pytest.approx(0.02)
    assert float(row["spread_cost_atr"]) == pytest.approx(0.02 / 1.5)
    assert int(row["tick_volume"]) == 121
    assert float(row["volume_mean_20"]) == pytest.approx(expected_mean)
    assert float(row["volume_ratio_20"]) == pytest.approx(121 / expected_mean)
    assert float(row["volume_change_pct"]) == pytest.approx((121 - 120) / 120 * 100)


def test_wait_signal_is_recorded_as_no_trade(tmp_path) -> None:
    candles = _candles()
    journal = SignalJournal(tmp_path, horizons=(3,))

    journal.record(_result(SignalAction.WAIT), candles[0], history=candles[:1])
    journal.evaluate("XAUUSD", "M5", candles)

    with journal.path.open("r", encoding="utf-8", newline="") as handle:
        row = next(csv.DictReader(handle))

    assert row["outcome_3"] == "NO_TRADE"
    assert row["directional_move_3"] == ""
    assert row["net_move_3"] == ""
    assert row["progress_atr_3"] == ""
