"""Tests for MetaTrader 5 CSV candle ingestion."""

from __future__ import annotations

import csv
from datetime import datetime, timezone

import pytest

from trademind.market.csv_provider import CsvMarketDataProvider


def _write_candles(path, count: int = 30) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "time",
                "symbol",
                "timeframe",
                "open",
                "high",
                "low",
                "close",
                "tick_volume",
                "spread",
            ]
        )
        for index in range(count):
            writer.writerow(
                [
                    1_700_000_000 + index * 300,
                    "XAUUSD",
                    "M5",
                    2400.0 + index,
                    2401.0 + index,
                    2399.0 + index,
                    2400.5 + index,
                    100 + index,
                    20,
                ]
            )


def test_reads_latest_requested_candles(tmp_path) -> None:
    path = tmp_path / "XAUUSD_M5.csv"
    _write_candles(path)

    provider = CsvMarketDataProvider(tmp_path)
    candles = provider.get_candles("xauusd", "m5", count=21)

    assert len(candles) == 21
    assert candles[0].close == pytest.approx(2409.5)
    assert candles[-1].close == pytest.approx(2429.5)
    assert candles[-1].time == datetime.fromtimestamp(
        1_700_000_000 + 29 * 300, tz=timezone.utc
    )


def test_missing_file_is_reported(tmp_path) -> None:
    provider = CsvMarketDataProvider(tmp_path)

    with pytest.raises(FileNotFoundError, match="MT5 candle file not found"):
        provider.get_candles("EURUSD", "M5", count=21)


def test_rejects_insufficient_history(tmp_path) -> None:
    path = tmp_path / "XAUUSD_M5.csv"
    _write_candles(path, count=10)

    provider = CsvMarketDataProvider(tmp_path)
    with pytest.raises(ValueError, match="Not enough candles"):
        provider.get_candles("XAUUSD", "M5", count=21)
