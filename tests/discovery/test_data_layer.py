import hashlib
from datetime import datetime, timezone

from trademind.discovery.data_layer import (
    DatasetIntegrityError,
    ImmutableHistoricalDatasetReader,
    PointInTimeError,
    PointInTimeMarketData,
)
from trademind.market.models import Candle


def _candle(minute: int) -> Candle:
    return Candle(
        symbol="BTCUSDT",
        timeframe="M5",
        time=datetime(2026, 1, 1, 0, minute, tzinfo=timezone.utc),
        open=100.0,
        high=101.0,
        low=99.0,
        close=100.5,
    )


class Provider:
    def healthcheck(self) -> bool:
        return True

    def get_candles(self, symbol: str, timeframe: str, count: int):
        return [_candle(0), _candle(5), _candle(10)]


def test_point_in_time_excludes_partial_bar():
    layer = PointInTimeMarketData(Provider())
    candles = layer.get_candles(
        "BTCUSDT",
        "M5",
        2,
        as_of=datetime(2026, 1, 1, 0, 12, tzinfo=timezone.utc),
    )
    assert [c.time.minute for c in candles] == [0, 5]


def test_point_in_time_fails_closed_when_provider_window_is_insufficient():
    layer = PointInTimeMarketData(Provider())
    try:
        layer.get_candles(
            "BTCUSDT",
            "M5",
            2,
            as_of=datetime(2026, 1, 1, 0, 7, tzinfo=timezone.utc),
        )
    except PointInTimeError:
        pass
    else:
        raise AssertionError("unsafe historical request must fail closed")


def test_historical_reader_verifies_hash_and_filters_partial_bar(tmp_path):
    path = tmp_path / "candles.csv"
    path.write_text(
        "symbol,timeframe,time,open,high,low,close,tick_volume,spread\n"
        "BTCUSDT,M5,2026-01-01T00:00:00+00:00,100,101,99,100.5,1,0\n"
        "BTCUSDT,M5,2026-01-01T00:05:00+00:00,100,101,99,100.5,1,0\n"
        "BTCUSDT,M5,2026-01-01T00:10:00+00:00,100,101,99,100.5,1,0\n",
        encoding="utf-8",
    )
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    reader = ImmutableHistoricalDatasetReader(path, expected_sha256=digest)
    candles = reader.get_candles(
        as_of=datetime(2026, 1, 1, 0, 12, tzinfo=timezone.utc),
        symbol="BTCUSDT",
        timeframe="M5",
    )
    assert [c.time.minute for c in candles] == [0, 5]

    path.write_text(path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    try:
        reader.get_candles(as_of=datetime(2026, 1, 1, 1, 0, tzinfo=timezone.utc))
    except DatasetIntegrityError:
        pass
    else:
        raise AssertionError("dataset mutation must be rejected")
