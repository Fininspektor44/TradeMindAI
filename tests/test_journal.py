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
            tick_volume=100,
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

    assert journal.record(_result(), candles[0]) is True
    assert journal.record(_result(), candles[0]) is False
    assert journal.evaluate("XAUUSD", "M5", candles) == 1

    with journal.path.open("r", encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))

    assert len(rows) == 1
    assert rows[0]["outcome_3"] == "WIN"
    assert float(rows[0]["net_move_3"]) == pytest.approx(2.98)
    assert float(rows[0]["mfe_3"]) == pytest.approx(3.5)
    assert float(rows[0]["mae_3"]) == pytest.approx(0.0)


def test_wait_signal_is_recorded_as_no_trade(tmp_path) -> None:
    candles = _candles()
    journal = SignalJournal(tmp_path, horizons=(3,))

    journal.record(_result(SignalAction.WAIT), candles[0])
    journal.evaluate("XAUUSD", "M5", candles)

    with journal.path.open("r", encoding="utf-8", newline="") as handle:
        row = next(csv.DictReader(handle))

    assert row["outcome_3"] == "NO_TRADE"
    assert row["net_move_3"] == ""
