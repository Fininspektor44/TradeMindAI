"""Fixed 20-symbol public Bybit market-intelligence universe."""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
from pathlib import Path
from typing import Any, Iterable

from trademind.bybit_intelligence import (
    BybitCollector,
    SymbolState,
    _float,
    fetch_linear_instruments,
    fetch_linear_tickers,
)

FIXED_SYMBOLS = (
    "BTCUSDT",
    "ETHUSDT",
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


def parse_symbols(value: str | Iterable[str]) -> tuple[str, ...]:
    """Normalize a comma-separated or iterable symbol list without changing order."""
    raw = value.split(",") if isinstance(value, str) else list(value)
    symbols = tuple(dict.fromkeys(str(item).strip().upper() for item in raw if str(item).strip()))
    if not symbols:
        raise ValueError("At least one Bybit symbol is required")
    return symbols


def select_fixed_universe(
    tickers: Iterable[dict[str, Any]],
    instruments: Iterable[dict[str, Any]],
    symbols: Iterable[str] = FIXED_SYMBOLS,
) -> list[dict[str, Any]]:
    """Return requested USDT perpetuals in the exact requested order."""
    requested = parse_symbols(symbols)
    tradable = {
        str(item.get("symbol", "")).upper()
        for item in instruments
        if str(item.get("status", "")).upper() == "TRADING"
        and str(item.get("settleCoin", "")).upper() == "USDT"
        and "PERPETUAL" in str(item.get("contractType", "")).upper()
    }
    by_symbol: dict[str, dict[str, Any]] = {}
    for ticker in tickers:
        symbol = str(ticker.get("symbol", "")).upper()
        if symbol not in requested:
            continue
        row = dict(ticker)
        row["symbol"] = symbol
        row["turnover24h"] = _float(ticker.get("turnover24h"))
        by_symbol[symbol] = row

    missing = [symbol for symbol in requested if symbol not in tradable or symbol not in by_symbol]
    if missing:
        raise RuntimeError("Requested Bybit symbols unavailable: " + ",".join(missing))
    return [by_symbol[symbol] for symbol in requested]


class FixedBybitCollector(BybitCollector):
    """Bybit collector with a stable user-selected universe."""

    def __init__(
        self,
        output_dir: Path,
        *,
        symbols: Iterable[str] = FIXED_SYMBOLS,
        refresh_hours: float = 6.0,
    ) -> None:
        self.fixed_symbols = parse_symbols(symbols)
        super().__init__(
            output_dir,
            top_n=len(self.fixed_symbols),
            min_turnover=0.0,
            refresh_hours=refresh_hours,
        )

    def discover(self) -> list[dict[str, Any]]:
        rows = select_fixed_universe(
            fetch_linear_tickers(),
            fetch_linear_instruments(),
            self.fixed_symbols,
        )
        self.universe = [row["symbol"] for row in rows]
        self.states = {
            symbol: self.states.get(symbol, SymbolState(symbol)) for symbol in self.universe
        }
        self.store.write_universe(rows)
        return rows


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="TradeMind v1.9.1 fixed Bybit universe")
    parser.add_argument("--output-dir", type=Path, default=Path("data/bybit_v1_9"))
    parser.add_argument("--symbols", default=",".join(FIXED_SYMBOLS))
    parser.add_argument("--refresh-hours", type=float, default=6.0)
    parser.add_argument("--run-seconds", type=float)
    parser.add_argument("--discover-only", action="store_true")
    parser.add_argument("--open-dashboard", action="store_true")
    return parser


async def _async_main(args: argparse.Namespace) -> int:
    collector = FixedBybitCollector(
        args.output_dir,
        symbols=parse_symbols(args.symbols),
        refresh_hours=args.refresh_hours,
    )
    if args.discover_only:
        rows = collector.discover()
        collector.store.write_status(collector._status("DISCOVERED"))
        for rank, row in enumerate(rows, 1):
            print(f"{rank:2d}. {row['symbol']:14s} turnover24h={row['turnover24h']:.0f}")
        return 0

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, collector.stop_event.set)
        except (NotImplementedError, RuntimeError):
            pass
    await collector.run(args.run_seconds)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = asyncio.run(_async_main(args))
    if args.open_dashboard:
        os.startfile(args.output_dir / "dashboard" / "index.html")  # type: ignore[attr-defined]
    return result


if __name__ == "__main__":
    raise SystemExit(main())
