from trademind.market.mock_provider import MockMarketDataProvider


def test_mock_provider_returns_ordered_candles() -> None:
    provider = MockMarketDataProvider()

    candles = provider.get_candles("XAUUSD", "M5", count=10)

    assert len(candles) == 10
    assert candles[0].time < candles[-1].time
    assert all(candle.symbol == "XAUUSD" for candle in candles)
    assert all(candle.timeframe == "M5" for candle in candles)


def test_mock_provider_rejects_invalid_count() -> None:
    provider = MockMarketDataProvider()

    try:
        provider.get_candles("XAUUSD", "M5", count=0)
    except ValueError as error:
        assert "count" in str(error)
    else:
        raise AssertionError("Expected ValueError for count=0")
