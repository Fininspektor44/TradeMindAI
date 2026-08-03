from __future__ import annotations

from pathlib import Path

from trademind.bybit_fixed20 import (
    FIXED_SYMBOLS,
    FixedBybitCollector,
    parse_symbols,
    select_fixed_universe,
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


def test_fixed_universe_contains_user_requested_20_symbols() -> None:
    assert len(FIXED_SYMBOLS) == 20
    assert FIXED_SYMBOLS[:2] == ("BTCUSDT", "ETHUSDT")
    assert FIXED_SYMBOLS[2:] == (
        "UNIUSDT",
        "JTOUSDT",
        "SOLUSDT",
        "BZUSDT",
        "NEARUSDT",
        "AKEUSDT",
        "ONDOUSDT",
        "POPCATUSDT",
        "XMRUSDT",
        "MYXUSDT",
        "AAVEUSDT",
        "ZECUSDT",
        "HYPEUSDT",
        "LDOUSDT",
        "PUMPFUNUSDT",
        "GRASSUSDT",
        "XAUTUSDT",
        "1000PEPEUSDT",
    )
    assert len(set(FIXED_SYMBOLS)) == 20


def test_parse_symbols_normalizes_and_deduplicates() -> None:
    assert parse_symbols(" btcusdt,ETHUSDT,btcusdt ") == ("BTCUSDT", "ETHUSDT")


def test_select_fixed_universe_preserves_requested_order() -> None:
    tickers = [
        {"symbol": symbol, "turnover24h": str((index + 1) * 1_000_000)}
        for index, symbol in enumerate(reversed(FIXED_SYMBOLS))
    ]
    rows = select_fixed_universe(tickers, _instruments(list(FIXED_SYMBOLS)))
    assert [row["symbol"] for row in rows] == list(FIXED_SYMBOLS)


def test_select_fixed_universe_rejects_missing_symbol() -> None:
    symbols = ("BTCUSDT", "ETHUSDT", "SOLUSDT")
    tickers = [
        {"symbol": "BTCUSDT", "turnover24h": "10"},
        {"symbol": "ETHUSDT", "turnover24h": "9"},
    ]
    try:
        select_fixed_universe(tickers, _instruments(["BTCUSDT", "ETHUSDT"]), symbols)
    except RuntimeError as exc:
        assert "SOLUSDT" in str(exc)
    else:
        raise AssertionError("Expected unavailable fixed symbol to fail validation")


def test_fixed_collector_uses_requested_symbols(tmp_path: Path) -> None:
    collector = FixedBybitCollector(tmp_path, symbols=("BTCUSDT", "ETHUSDT"))
    assert collector.fixed_symbols == ("BTCUSDT", "ETHUSDT")
    assert collector.top_n == 2


def test_fixed_bybit_collector_is_read_only() -> None:
    source = Path("src/trademind/bybit_fixed20.py").read_text(encoding="utf-8").lower()
    forbidden = ("api_key", "api_secret", "order.create", "place_order", "order_send")
    for token in forbidden:
        assert token not in source
