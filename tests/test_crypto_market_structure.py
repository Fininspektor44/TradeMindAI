from __future__ import annotations

from datetime import datetime, timezone

from trademind.crypto_market_structure import (
    Bar,
    MarketStructureEngine,
    Pivot,
    _fibonacci_geometry,
    _latest_break,
    _latest_fvg,
    _latest_sweep,
    resample_bars,
    safety_contract,
)


def bar(index: int, open_price: float, high: float, low: float, close: float) -> Bar:
    return Bar(
        start_ms=1_786_003_200_000 + index * 300_000,
        interval_minutes=5,
        open=open_price,
        high=high,
        low=low,
        close=close,
    )


def test_resample_uses_only_complete_contiguous_buckets() -> None:
    rows = [
        bar(0, 100, 102, 99, 101),
        bar(1, 101, 103, 100, 102),
        bar(2, 102, 104, 101, 103),
        bar(3, 103, 105, 102, 104),
        bar(5, 105, 107, 104, 106),
    ]
    result = resample_bars(rows, 15)
    assert len(result) == 1
    assert result[0].open == 100
    assert result[0].high == 104
    assert result[0].close == 103


def test_break_labels_countertrend_cross_as_choch() -> None:
    rows = [
        bar(0, 100, 101, 99, 100),
        bar(1, 100, 103, 99, 102),
        bar(2, 102, 105, 101, 104),
        bar(3, 104, 104.5, 102, 103),
        bar(4, 103, 104.5, 102, 104),
        bar(5, 104, 106.5, 103.5, 106),
    ]
    result = _latest_break(
        rows,
        [Pivot(index=2, price=105, start_ms=rows[2].start_ms)],
        [],
        "BEARISH",
    )
    assert result["type"] == "BULLISH_CHOCH"
    assert result["level"] == 105


def test_detects_liquidity_sweep_and_unfilled_fvg() -> None:
    sweep_rows = [
        bar(0, 10, 10.5, 9.5, 10),
        bar(1, 9.5, 10, 8.8, 9),
        bar(2, 9, 9.5, 8, 8.5),
        bar(3, 8.5, 9.5, 8.2, 9),
        bar(4, 9, 10, 8.5, 9.5),
        bar(5, 9.5, 10, 7.5, 8.7),
    ]
    sweep = _latest_sweep(sweep_rows, atr=1.0)
    assert sweep["type"] == "SSL_SWEEP"
    assert sweep["depth_atr"] == 0.5

    fvg_rows = [
        bar(0, 9.5, 10, 9, 9.8),
        bar(1, 10, 11, 9.8, 10.8),
        bar(2, 11.2, 12, 11.1, 11.8),
        bar(3, 11.8, 12.2, 10.5, 11.5),
    ]
    fvg = _latest_fvg(fvg_rows, atr=1.0)
    assert fvg["type"] == "BULLISH_FVG"
    assert fvg["lower"] == 10
    assert fvg["upper"] == 11.1


def test_fibonacci_uses_confirmed_impulse_and_marks_ote() -> None:
    prices = [
        (100, 101, 99, 100),
        (96, 97, 94, 95),
        (91, 92, 89, 90),
        (96, 97, 95, 96),
        (102, 103, 101, 102),
        (108, 109, 107, 108),
        (110, 111, 109, 110),
        (106, 107, 105, 106),
        (101, 102, 100, 101),
        (97, 98, 96, 97),
        (96, 97, 95, 96),
    ]
    rows = [bar(index, *values) for index, values in enumerate(prices)]
    result = _fibonacci_geometry(rows, "BUY")
    assert result["available"] is True
    assert result["ote_hit"] is True
    assert 0.618 <= result["retracement"] <= 0.790
    assert result["level_618"] > result["level_790"]


def test_snapshot_never_uses_a_future_candle() -> None:
    rows = []
    for index in range(240):
        close = 100 + index * 0.02 + ((index % 12) - 6) * 0.08
        rows.append(bar(index, close - 0.05, close + 0.25, close - 0.25, close))
    future = bar(240, 200, 220, 190, 210)
    engine = MarketStructureEngine({"BTCUSDT": [*rows, future]})
    as_of = datetime.fromtimestamp(rows[-1].end_ms / 1000, tz=timezone.utc)
    snapshot = engine.snapshot("BTCUSDT", as_of, "BUY")
    assert snapshot["bar_counts"]["M5"] == 240
    assert snapshot["safety"]["future_bars_used"] is False
    assert snapshot["state"] == "OK"


def test_safety_contract_is_read_only_and_point_in_time() -> None:
    assert safety_contract() == {
        "read_only": True,
        "orders_enabled": False,
        "publication_enabled": False,
        "exchange_api_called": False,
        "future_bars_used": False,
    }
