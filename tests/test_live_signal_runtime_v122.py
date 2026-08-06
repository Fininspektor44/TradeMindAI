from datetime import datetime, timedelta, timezone

import pytest

from trademind.live_signal_runtime_v122 import (
    _watermark_advances,
    select_incremental_observations,
)


def _epoch(value: datetime) -> int:
    return int(value.timestamp())


def _raw(symbol: str, opened_at: datetime) -> dict[str, str]:
    epoch = _epoch(opened_at)
    return {
        "observation_id": f"{symbol}:M5:{epoch}",
        "symbol": symbol,
        "source_bar_time": str(epoch),
    }


def _source(symbol: str, opened_at: datetime) -> dict[str, str]:
    return {
        "symbol": symbol,
        "time": str(_epoch(opened_at)),
        "bar_seconds": "300",
    }


def test_existing_symbol_accepts_only_bars_newer_than_watermark() -> None:
    now = datetime(2026, 8, 6, 4, 20, tzinfo=timezone.utc)
    old_open = now - timedelta(minutes=20)
    watermark_open = now - timedelta(minutes=10)
    fresh_open = now - timedelta(minutes=5)
    built = [
        _raw("EURUSD", old_open),
        _raw("EURUSD", watermark_open),
        _raw("EURUSD", fresh_open),
    ]
    sources = {
        ("EURUSD", _epoch(item)): _source("EURUSD", item)
        for item in (old_open, watermark_open, fresh_open)
    }

    selected = select_incremental_observations(
        built,
        sources,
        previous_watermarks={"EURUSD": _epoch(watermark_open)},
        known_ids=set(),
        captured_at=now,
        bootstrap_lookback_seconds=900,
        server_utc_offset_hours=0,
    )

    assert [row["source_bar_time"] for row in selected] == [str(_epoch(fresh_open))]


def test_new_symbol_receives_bounded_bootstrap_not_full_history() -> None:
    now = datetime(2026, 8, 6, 4, 20, tzinfo=timezone.utc)
    stale_open = now - timedelta(hours=3)
    recent_open = now - timedelta(minutes=10)
    built = [_raw("GBPUSD", stale_open), _raw("GBPUSD", recent_open)]
    sources = {
        ("GBPUSD", _epoch(item)): _source("GBPUSD", item)
        for item in (stale_open, recent_open)
    }

    selected = select_incremental_observations(
        built,
        sources,
        previous_watermarks={"EURUSD": _epoch(now - timedelta(minutes=5))},
        known_ids=set(),
        captured_at=now,
        bootstrap_lookback_seconds=900,
        server_utc_offset_hours=0,
    )

    assert [row["source_bar_time"] for row in selected] == [str(_epoch(recent_open))]


def test_watermark_regression_fails_closed() -> None:
    with pytest.raises(ValueError, match="watermark regressed"):
        _watermark_advances(
            {"EURUSD": 100, "GBPUSD": 300},
            {"EURUSD": 200, "GBPUSD": 250},
        )
