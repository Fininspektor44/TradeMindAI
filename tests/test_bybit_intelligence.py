from __future__ import annotations

import csv
from pathlib import Path

from trademind.bybit_intelligence import (
    BAR_MS,
    BybitCollector,
    FIELDNAMES,
    OrderBook,
    SymbolState,
    select_universe,
)


def _instruments(symbols: list[str]) -> list[dict[str, str]]:
    return [
        {
            "symbol": symbol,
            "status": "Trading",
            "settleCoin": "USDT",
            "contractType": "LinearPerpetual",
        }
        for symbol in symbols
    ]


def test_select_top10_keeps_core_and_liquid_alts() -> None:
    symbols = [
        "BTCUSDT",
        "ETHUSDT",
        "SOLUSDT",
        "XRPUSDT",
        "ADAUSDT",
        "DOGEUSDT",
        "LINKUSDT",
        "SUIUSDT",
        "AVAXUSDT",
        "BNBUSDT",
        "PEPEUSDT",
        "USDCUSDT",
    ]
    tickers = [
        {"symbol": symbol, "turnover24h": str((index + 1) * 10_000_000)}
        for index, symbol in enumerate(symbols)
    ]

    rows = select_universe(tickers, _instruments(symbols), top_n=10, min_turnover=0)
    picked = [row["symbol"] for row in rows]

    assert picked[:2] == ["BTCUSDT", "ETHUSDT"]
    assert len(picked) == 10
    assert "USDCUSDT" not in picked
    assert "PEPEUSDT" in picked


def test_select_rejects_nontrading_symbols() -> None:
    tickers = [
        {"symbol": "BTCUSDT", "turnover24h": "10"},
        {"symbol": "ETHUSDT", "turnover24h": "9"},
        {"symbol": "SOLUSDT", "turnover24h": "8"},
    ]

    try:
        select_universe(tickers, _instruments(["BTCUSDT", "ETHUSDT"]), top_n=3, min_turnover=0)
    except RuntimeError as exc:
        assert "Only 2" in str(exc)
    else:
        raise AssertionError("Expected a RuntimeError for an undersized tradable universe")


def test_orderbook_snapshot_delta_metrics() -> None:
    book = OrderBook()
    book.apply(
        "snapshot",
        {
            "u": 2,
            "b": [["100", "2"], ["99", "3"]],
            "a": [["101", "1"], ["102", "4"]],
        },
    )
    first = book.metrics()

    assert first["best_bid"] == 100
    assert first["best_ask"] == 101
    assert first["book_imbalance_5"] < 0

    book.apply("delta", {"u": 3, "b": [["100", "0"], ["100.5", "5"]], "a": []})
    assert book.metrics()["best_bid"] == 100.5


def test_collector_builds_delta_row_and_store_is_idempotent(tmp_path: Path) -> None:
    collector = BybitCollector(tmp_path, top_n=2, min_turnover=0)
    collector.universe = ["BTCUSDT", "ETHUSDT"]
    collector.states = {symbol: SymbolState(symbol) for symbol in collector.universe}
    start = 1_700_000_000_000 // BAR_MS * BAR_MS

    collector.handle_message(
        {
            "topic": "tickers.BTCUSDT",
            "data": {
                "symbol": "BTCUSDT",
                "markPrice": "101",
                "indexPrice": "100",
                "fundingRate": "0.0001",
            },
        }
    )
    collector.handle_message(
        {
            "topic": "orderbook.50.BTCUSDT",
            "type": "snapshot",
            "data": {"s": "BTCUSDT", "u": 2, "b": [["99", "2"]], "a": [["101", "1"]]},
        }
    )
    collector.handle_message(
        {
            "topic": "publicTrade.BTCUSDT",
            "data": [
                {"s": "BTCUSDT", "S": "Buy", "p": "100", "v": "2", "T": start + 1},
                {"s": "BTCUSDT", "S": "Sell", "p": "100", "v": "1", "T": start + 2},
            ],
        }
    )
    kline = {
        "start": start,
        "open": "98",
        "high": "103",
        "low": "97",
        "close": "102",
        "volume": "3",
        "turnover": "300",
        "confirm": True,
    }
    collector.handle_message({"topic": "kline.5.BTCUSDT", "data": [kline]})
    collector.handle_message({"topic": "kline.5.BTCUSDT", "data": [kline]})

    with (tmp_path / "bybit_bars.csv").open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))

    assert len(rows) == 1
    row = rows[0]
    assert float(row["delta_turnover"]) == 100
    assert float(row["basis_bps"]) == 100
    assert int(row["trade_count"]) == 2
    assert collector.bars_written == 1
    assert set(row) == set(FIELDNAMES)


def test_bybit_collector_is_read_only() -> None:
    source = Path("src/trademind/bybit_intelligence.py").read_text(encoding="utf-8").lower()
    forbidden = ("api_key", "api_secret", "order.create", "place_order", "order_send")
    for token in forbidden:
        assert token not in source
