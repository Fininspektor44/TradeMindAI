"""Public Bybit market-intelligence collector for the dynamic top-10 USDT perpetuals."""

from __future__ import annotations

import argparse
import asyncio
import csv
import html
import json
import math
import os
import signal
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from websockets.asyncio.client import connect

REST_BASE = "https://api.bybit.com"
WS_LINEAR = "wss://stream.bybit.com/v5/public/linear"
BAR_MS = 300_000
SCHEMA_VERSION = "1.9"
CORE_SYMBOLS = ("BTCUSDT", "ETHUSDT")
STABLE_BASES = {
    "USDT",
    "USDC",
    "DAI",
    "FDUSD",
    "TUSD",
    "USDE",
    "PYUSD",
    "USDD",
    "USD1",
    "EUR",
}

FIELDNAMES = (
    "schema_version",
    "source_id",
    "symbol",
    "timeframe",
    "start_ms",
    "end_ms",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "turnover",
    "trade_count",
    "buy_trade_count",
    "sell_trade_count",
    "taker_buy_qty",
    "taker_sell_qty",
    "taker_buy_turnover",
    "taker_sell_turnover",
    "delta_qty",
    "delta_turnover",
    "cvd_turnover",
    "largest_trade_turnover",
    "avg_trade_turnover",
    "trade_rate_per_sec",
    "best_bid",
    "best_ask",
    "spread_bps",
    "book_imbalance_5",
    "book_imbalance_10",
    "book_imbalance_50",
    "last_price",
    "mark_price",
    "index_price",
    "basis_bps",
    "open_interest",
    "open_interest_value",
    "funding_rate",
    "next_funding_time",
    "price_24h_pct",
    "turnover_24h",
    "received_at",
)


def _float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _http_json(path: str, params: dict[str, Any] | None = None, timeout: int = 20) -> dict[str, Any]:
    query = urllib.parse.urlencode(params or {})
    url = f"{REST_BASE}{path}"
    if query:
        url = f"{url}?{query}"
    request = urllib.request.Request(url, headers={"User-Agent": "TradeMindAI/1.9"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.load(response)
    if payload.get("retCode") != 0:
        raise RuntimeError(f"Bybit REST error {payload.get('retCode')}: {payload.get('retMsg')}")
    return payload


def fetch_linear_tickers() -> list[dict[str, Any]]:
    return list(_http_json("/v5/market/tickers", {"category": "linear"})["result"]["list"])


def fetch_linear_instruments() -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    cursor = ""
    while True:
        params: dict[str, Any] = {"category": "linear", "limit": 1000}
        if cursor:
            params["cursor"] = cursor
        result = _http_json("/v5/market/instruments-info", params)["result"]
        items.extend(result.get("list", []))
        cursor = str(result.get("nextPageCursor") or "")
        if not cursor:
            return items


def _base_coin(symbol: str) -> str:
    return symbol[:-4] if symbol.endswith("USDT") else symbol


def select_universe(
    tickers: Iterable[dict[str, Any]],
    instruments: Iterable[dict[str, Any]],
    *,
    top_n: int = 10,
    core: tuple[str, ...] = CORE_SYMBOLS,
    min_turnover: float = 5_000_000.0,
) -> list[dict[str, Any]]:
    if top_n < len(core):
        raise ValueError("top_n cannot be smaller than the core universe")

    tradable = {
        str(item.get("symbol", "")).upper()
        for item in instruments
        if str(item.get("status", "")).upper() == "TRADING"
        and str(item.get("settleCoin", "")).upper() == "USDT"
        and "PERPETUAL" in str(item.get("contractType", "")).upper()
    }
    candidates: list[dict[str, Any]] = []
    by_symbol: dict[str, dict[str, Any]] = {}
    for ticker in tickers:
        symbol = str(ticker.get("symbol", "")).upper()
        if symbol not in tradable or not symbol.endswith("USDT"):
            continue
        if _base_coin(symbol) in STABLE_BASES:
            continue
        turnover = _float(ticker.get("turnover24h"))
        if turnover < min_turnover and symbol not in core:
            continue
        row = dict(ticker)
        row["symbol"] = symbol
        row["turnover24h"] = turnover
        by_symbol[symbol] = row
        candidates.append(row)

    selected: list[dict[str, Any]] = []
    for symbol in core:
        if symbol in by_symbol:
            selected.append(by_symbol[symbol])

    used = {row["symbol"] for row in selected}
    for row in sorted(candidates, key=lambda item: item["turnover24h"], reverse=True):
        if row["symbol"] in used:
            continue
        selected.append(row)
        used.add(row["symbol"])
        if len(selected) >= top_n:
            break

    if len(selected) < top_n:
        raise RuntimeError(f"Only {len(selected)} eligible Bybit symbols found for top_n={top_n}")
    return selected[:top_n]


@dataclass
class OrderBook:
    bids: dict[float, float] = field(default_factory=dict)
    asks: dict[float, float] = field(default_factory=dict)
    update_id: int = 0

    def apply(self, message_type: str, data: dict[str, Any]) -> None:
        if message_type == "snapshot" or _int(data.get("u")) == 1:
            self.bids.clear()
            self.asks.clear()
        self._apply_side(self.bids, data.get("b", []))
        self._apply_side(self.asks, data.get("a", []))
        self.update_id = _int(data.get("u"), self.update_id)

    @staticmethod
    def _apply_side(side: dict[float, float], levels: Iterable[list[Any]]) -> None:
        for level in levels:
            if len(level) < 2:
                continue
            price = _float(level[0])
            size = _float(level[1])
            if price <= 0:
                continue
            if size <= 0:
                side.pop(price, None)
            else:
                side[price] = size

    def metrics(self) -> dict[str, float]:
        best_bid = max(self.bids, default=0.0)
        best_ask = min(self.asks, default=0.0)
        midpoint = (best_bid + best_ask) / 2 if best_bid and best_ask else 0.0
        spread_bps = ((best_ask - best_bid) / midpoint * 10_000) if midpoint else 0.0
        metrics = {"best_bid": best_bid, "best_ask": best_ask, "spread_bps": spread_bps}
        bids = sorted(self.bids.items(), reverse=True)
        asks = sorted(self.asks.items())
        for depth in (5, 10, 50):
            bid_value = sum(price * size for price, size in bids[:depth])
            ask_value = sum(price * size for price, size in asks[:depth])
            total = bid_value + ask_value
            metrics[f"book_imbalance_{depth}"] = (bid_value - ask_value) / total if total else 0.0
        return metrics


@dataclass
class BarBucket:
    symbol: str
    start_ms: int
    end_ms: int
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    close: float = 0.0
    volume: float = 0.0
    turnover: float = 0.0
    confirmed: bool = False
    trade_count: int = 0
    buy_trade_count: int = 0
    sell_trade_count: int = 0
    taker_buy_qty: float = 0.0
    taker_sell_qty: float = 0.0
    taker_buy_turnover: float = 0.0
    taker_sell_turnover: float = 0.0
    largest_trade_turnover: float = 0.0

    def add_trade(self, side: str, price: float, size: float) -> None:
        notional = price * size
        self.trade_count += 1
        self.largest_trade_turnover = max(self.largest_trade_turnover, notional)
        if side.upper() == "BUY":
            self.buy_trade_count += 1
            self.taker_buy_qty += size
            self.taker_buy_turnover += notional
        else:
            self.sell_trade_count += 1
            self.taker_sell_qty += size
            self.taker_sell_turnover += notional

    def apply_kline(self, data: dict[str, Any]) -> None:
        self.open = _float(data.get("open"))
        self.high = _float(data.get("high"))
        self.low = _float(data.get("low"))
        self.close = _float(data.get("close"))
        self.volume = _float(data.get("volume"))
        self.turnover = _float(data.get("turnover"))
        self.confirmed = bool(data.get("confirm"))


@dataclass
class SymbolState:
    symbol: str
    book: OrderBook = field(default_factory=OrderBook)
    ticker: dict[str, Any] = field(default_factory=dict)
    buckets: dict[int, BarBucket] = field(default_factory=dict)
    cvd_turnover: float = 0.0

    def bucket(self, start_ms: int) -> BarBucket:
        bucket = self.buckets.get(start_ms)
        if bucket is None:
            bucket = BarBucket(self.symbol, start_ms, start_ms + BAR_MS - 1)
            self.buckets[start_ms] = bucket
        return bucket

    def prune(self, keep: int = 4) -> None:
        for key in sorted(self.buckets)[:-keep]:
            self.buckets.pop(key, None)


class CsvStore:
    def __init__(self, output_dir: Path) -> None:
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.bars_path = output_dir / "bybit_bars.csv"
        self.latest_path = output_dir / "latest.csv"
        self.universe_path = output_dir / "universe.csv"
        self.status_path = output_dir / "status.json"
        self.dashboard_path = output_dir / "dashboard" / "index.html"
        self.keys = self._load_keys()
        self.latest: dict[str, dict[str, Any]] = self._load_latest()

    def _load_keys(self) -> set[tuple[str, int]]:
        if not self.bars_path.exists():
            return set()
        with self.bars_path.open("r", encoding="utf-8", newline="") as handle:
            return {(row["symbol"], _int(row["start_ms"])) for row in csv.DictReader(handle)}

    def _load_latest(self) -> dict[str, dict[str, Any]]:
        if not self.latest_path.exists():
            return {}
        with self.latest_path.open("r", encoding="utf-8", newline="") as handle:
            return {row["symbol"]: row for row in csv.DictReader(handle)}

    def append(self, row: dict[str, Any]) -> bool:
        key = (str(row["symbol"]), _int(row["start_ms"]))
        if key in self.keys:
            return False
        write_header = not self.bars_path.exists() or self.bars_path.stat().st_size == 0
        with self.bars_path.open("a", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=FIELDNAMES)
            if write_header:
                writer.writeheader()
            writer.writerow({name: row.get(name, "") for name in FIELDNAMES})
        self.keys.add(key)
        self.latest[str(row["symbol"])] = dict(row)
        self._write_latest()
        return True

    def _write_latest(self) -> None:
        temp = self.latest_path.with_suffix(".tmp")
        with temp.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=FIELDNAMES)
            writer.writeheader()
            for symbol in sorted(self.latest):
                writer.writerow({name: self.latest[symbol].get(name, "") for name in FIELDNAMES})
        os.replace(temp, self.latest_path)

    def write_universe(self, rows: list[dict[str, Any]]) -> None:
        fields = ("rank", "symbol", "turnover24h", "lastPrice", "price24hPcnt", "selected_at")
        temp = self.universe_path.with_suffix(".tmp")
        with temp.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            selected_at = _iso_now()
            for rank, row in enumerate(rows, 1):
                writer.writerow(
                    {
                        "rank": rank,
                        "symbol": row["symbol"],
                        "turnover24h": row.get("turnover24h", 0),
                        "lastPrice": row.get("lastPrice", ""),
                        "price24hPcnt": row.get("price24hPcnt", ""),
                        "selected_at": selected_at,
                    }
                )
        os.replace(temp, self.universe_path)

    def write_status(self, status: dict[str, Any]) -> None:
        temp = self.status_path.with_suffix(".tmp")
        temp.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp, self.status_path)
        self.render_dashboard(status)

    def render_dashboard(self, status: dict[str, Any]) -> None:
        self.dashboard_path.parent.mkdir(parents=True, exist_ok=True)
        cards = []
        for symbol in status.get("universe", []):
            row = self.latest.get(symbol, {})
            cards.append(
                "<article><h3>{}</h3><p>Close: {}</p><p>Delta turnover: {}</p>"
                "<p>CVD: {}</p><p>Book 10: {}</p><p>Spread: {} bps</p></article>".format(
                    html.escape(symbol),
                    html.escape(str(row.get("close", "-"))),
                    html.escape(str(row.get("delta_turnover", "-"))),
                    html.escape(str(row.get("cvd_turnover", "-"))),
                    html.escape(str(row.get("book_imbalance_10", "-"))),
                    html.escape(str(row.get("spread_bps", "-"))),
                )
            )
        status_class = "ok" if status.get("state") == "RUNNING" else "warn"
        document = f"""<!doctype html><html lang='ru'><meta charset='utf-8'>
<title>TradeMind v1.9 Bybit</title><style>
body{{background:#061a2b;color:#e9f7ff;font-family:Arial;margin:32px}}h1{{font-size:48px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:16px}}
article{{background:#0b2941;border:1px solid #1d5778;border-radius:18px;padding:18px}}
.ok{{color:#24e0a4}}.warn{{color:#ffcc4d}}
</style><h1>Bybit Market Intelligence</h1>
<p class='{status_class}'>State: {html.escape(str(status.get('state')))}</p>
<p>Updated: {html.escape(str(status.get('updated_at')))}</p>
<p>Messages: {html.escape(str(status.get('messages')))} | Bars: {html.escape(str(status.get('bars_written')))} | Reconnects: {html.escape(str(status.get('reconnects')))}</p>
<section class='grid'>{''.join(cards)}</section></html>"""
        temp = self.dashboard_path.with_suffix(".tmp")
        temp.write_text(document, encoding="utf-8")
        os.replace(temp, self.dashboard_path)


class BybitCollector:
    def __init__(
        self,
        output_dir: Path,
        *,
        top_n: int = 10,
        min_turnover: float = 5_000_000.0,
        refresh_hours: float = 6.0,
    ) -> None:
        self.store = CsvStore(output_dir)
        self.top_n = top_n
        self.min_turnover = min_turnover
        self.refresh_seconds = max(900.0, refresh_hours * 3600.0)
        self.universe: list[str] = []
        self.states: dict[str, SymbolState] = {}
        self.stop_event = asyncio.Event()
        self.messages = 0
        self.bars_written = 0
        self.reconnects = 0
        self.last_event_at = ""
        self.started_at = _iso_now()

    def discover(self) -> list[dict[str, Any]]:
        rows = select_universe(
            fetch_linear_tickers(),
            fetch_linear_instruments(),
            top_n=self.top_n,
            min_turnover=self.min_turnover,
        )
        self.universe = [row["symbol"] for row in rows]
        self.states = {symbol: self.states.get(symbol, SymbolState(symbol)) for symbol in self.universe}
        self.store.write_universe(rows)
        return rows

    def _status(self, state: str = "RUNNING", error: str = "") -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "source_id": "BYBIT_LINEAR",
            "state": state,
            "error": error,
            "started_at": self.started_at,
            "updated_at": _iso_now(),
            "last_event_at": self.last_event_at,
            "messages": self.messages,
            "bars_written": self.bars_written,
            "reconnects": self.reconnects,
            "universe": self.universe,
            "bars_path": str(self.store.bars_path),
            "latest_path": str(self.store.latest_path),
            "dashboard": str(self.store.dashboard_path),
            "orders_enabled": False,
        }

    async def run(self, run_seconds: float | None = None) -> None:
        self.discover()
        self.store.write_status(self._status())
        deadline = time.monotonic() + run_seconds if run_seconds else None
        while not self.stop_event.is_set():
            if deadline and time.monotonic() >= deadline:
                break
            try:
                await self._connection(deadline)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.reconnects += 1
                self.store.write_status(self._status("RECONNECTING", str(exc)))
                await asyncio.sleep(min(30.0, 2.0 + self.reconnects))
        self.store.write_status(self._status("STOPPED"))

    async def _connection(self, deadline: float | None) -> None:
        connected_at = time.monotonic()
        async with connect(
            WS_LINEAR,
            ping_interval=20,
            ping_timeout=20,
            open_timeout=15,
            close_timeout=5,
            max_size=8 * 1024 * 1024,
            max_queue=256,
        ) as websocket:
            args = [
                topic
                for symbol in self.universe
                for topic in (
                    f"publicTrade.{symbol}",
                    f"orderbook.50.{symbol}",
                    f"tickers.{symbol}",
                    f"kline.5.{symbol}",
                )
            ]
            await websocket.send(json.dumps({"op": "subscribe", "args": args}))
            heartbeat = asyncio.create_task(self._heartbeat(websocket))
            try:
                while not self.stop_event.is_set():
                    if deadline and time.monotonic() >= deadline:
                        return
                    if time.monotonic() - connected_at >= self.refresh_seconds:
                        self.discover()
                        return
                    try:
                        raw = await asyncio.wait_for(websocket.recv(), timeout=30)
                    except asyncio.TimeoutError:
                        self.store.write_status(self._status())
                        continue
                    self.handle_message(json.loads(raw))
                    if self.messages % 500 == 0:
                        self.store.write_status(self._status())
            finally:
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)

    async def _heartbeat(self, websocket: Any) -> None:
        while True:
            await asyncio.sleep(20)
            await websocket.send(json.dumps({"op": "ping"}))

    def handle_message(self, message: dict[str, Any]) -> None:
        topic = str(message.get("topic", ""))
        if not topic:
            return
        self.messages += 1
        self.last_event_at = _iso_now()
        if topic.startswith("publicTrade."):
            self._handle_trades(message)
        elif topic.startswith("orderbook."):
            self._handle_book(message)
        elif topic.startswith("tickers."):
            self._handle_ticker(message)
        elif topic.startswith("kline.5."):
            self._handle_kline(message)

    def _state(self, symbol: str) -> SymbolState | None:
        return self.states.get(symbol.upper())

    def _handle_trades(self, message: dict[str, Any]) -> None:
        for trade in message.get("data", []):
            symbol = str(trade.get("s", "")).upper()
            state = self._state(symbol)
            if state is None:
                continue
            timestamp = _int(trade.get("T"))
            start_ms = timestamp // BAR_MS * BAR_MS
            state.bucket(start_ms).add_trade(
                str(trade.get("S", "Sell")), _float(trade.get("p")), _float(trade.get("v"))
            )
            state.prune()

    def _handle_book(self, message: dict[str, Any]) -> None:
        data = message.get("data", {})
        symbol = str(data.get("s", "")).upper()
        state = self._state(symbol)
        if state is not None:
            state.book.apply(str(message.get("type", "delta")), data)

    def _handle_ticker(self, message: dict[str, Any]) -> None:
        data = message.get("data", {})
        if isinstance(data, list):
            data = data[0] if data else {}
        symbol = str(data.get("symbol", "")).upper()
        state = self._state(symbol)
        if state is not None:
            for key, value in data.items():
                if value not in (None, ""):
                    state.ticker[key] = value

    def _handle_kline(self, message: dict[str, Any]) -> None:
        symbol = str(message.get("topic", "")).rsplit(".", 1)[-1].upper()
        state = self._state(symbol)
        if state is None:
            return
        for item in message.get("data", []):
            start_ms = _int(item.get("start"))
            bucket = state.bucket(start_ms)
            bucket.apply_kline(item)
            if bucket.confirmed:
                row = self._finalize(state, bucket)
                if self.store.append(row):
                    self.bars_written += 1
                state.buckets.pop(start_ms, None)
        state.prune()

    def _finalize(self, state: SymbolState, bucket: BarBucket) -> dict[str, Any]:
        delta_qty = bucket.taker_buy_qty - bucket.taker_sell_qty
        delta_turnover = bucket.taker_buy_turnover - bucket.taker_sell_turnover
        state.cvd_turnover += delta_turnover
        avg_trade = (
            (bucket.taker_buy_turnover + bucket.taker_sell_turnover) / bucket.trade_count
            if bucket.trade_count
            else 0.0
        )
        book = state.book.metrics()
        ticker = state.ticker
        mark = _float(ticker.get("markPrice"))
        index = _float(ticker.get("indexPrice"))
        basis_bps = ((mark - index) / index * 10_000) if index else 0.0
        row: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "source_id": "BYBIT_LINEAR",
            "symbol": state.symbol,
            "timeframe": "M5",
            "start_ms": bucket.start_ms,
            "end_ms": bucket.end_ms,
            "open": bucket.open,
            "high": bucket.high,
            "low": bucket.low,
            "close": bucket.close,
            "volume": bucket.volume,
            "turnover": bucket.turnover,
            "trade_count": bucket.trade_count,
            "buy_trade_count": bucket.buy_trade_count,
            "sell_trade_count": bucket.sell_trade_count,
            "taker_buy_qty": bucket.taker_buy_qty,
            "taker_sell_qty": bucket.taker_sell_qty,
            "taker_buy_turnover": bucket.taker_buy_turnover,
            "taker_sell_turnover": bucket.taker_sell_turnover,
            "delta_qty": delta_qty,
            "delta_turnover": delta_turnover,
            "cvd_turnover": state.cvd_turnover,
            "largest_trade_turnover": bucket.largest_trade_turnover,
            "avg_trade_turnover": avg_trade,
            "trade_rate_per_sec": bucket.trade_count / 300.0,
            **book,
            "last_price": _float(ticker.get("lastPrice"), bucket.close),
            "mark_price": mark,
            "index_price": index,
            "basis_bps": basis_bps,
            "open_interest": _float(ticker.get("openInterest")),
            "open_interest_value": _float(ticker.get("openInterestValue")),
            "funding_rate": _float(ticker.get("fundingRate")),
            "next_funding_time": _int(ticker.get("nextFundingTime")),
            "price_24h_pct": _float(ticker.get("price24hPcnt")),
            "turnover_24h": _float(ticker.get("turnover24h")),
            "received_at": _iso_now(),
        }
        return {name: row.get(name, 0) for name in FIELDNAMES}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="TradeMind v1.9 public Bybit intelligence")
    parser.add_argument("--output-dir", type=Path, default=Path("data/bybit_v1_9"))
    parser.add_argument("--top-n", type=int, default=10)
    parser.add_argument("--min-turnover", type=float, default=5_000_000.0)
    parser.add_argument("--refresh-hours", type=float, default=6.0)
    parser.add_argument("--run-seconds", type=float)
    parser.add_argument("--discover-only", action="store_true")
    parser.add_argument("--open-dashboard", action="store_true")
    return parser


async def _async_main(args: argparse.Namespace) -> int:
    collector = BybitCollector(
        args.output_dir,
        top_n=args.top_n,
        min_turnover=args.min_turnover,
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
