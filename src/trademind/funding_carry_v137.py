"""TradeMind v1.37 delta-neutral funding-carry research.

Public Bybit data only. No keys, no orders, no publication.

Strategy:
* long spot + short same-symbol USDT perpetual;
* use only POSITIVE funding carry, so no spot borrowing is required;
* signal for the NEXT funding interval uses the funding rate that has just settled;
  this avoids using the next funding payment before it is known;
* entry when settled funding >= fixed threshold (default 0.01%);
* exit after a settlement when the rate falls below the threshold;
* equal spot/perp notionals, 1x fully collateralised perp margin;
* report returns on total deployed capital = spot notional + perp collateral;
* include configurable fees + slippage on both legs.

The code intentionally does not optimise the threshold on the test period.
"""

from __future__ import annotations

import argparse
import bisect
import csv
import json
import math
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from trademind.bybit_intelligence import _http_json

VERSION = "1.37.0"
HOUR_MS = 60 * 60 * 1000
YEAR_SECONDS = 365.25 * 24 * 60 * 60


@dataclass(frozen=True, slots=True)
class PriceBar:
    start_ms: int
    close: float

    @property
    def end_ms(self) -> int:
        return self.start_ms + HOUR_MS


@dataclass(frozen=True, slots=True)
class FundingEvent:
    ts_ms: int
    rate: float


@dataclass(frozen=True, slots=True)
class Result:
    symbol: str
    period: str
    start_ms: int
    end_ms: int
    funding_events: int
    active_intervals: int
    entries: int
    exits: int
    positive_funding_intervals: int
    total_return: float
    annualized_return: float
    sharpe: float
    max_drawdown: float
    funding_component: float
    basis_component: float
    trading_cost_component: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": VERSION,
            "symbol": self.symbol,
            "period": self.period,
            "start": _iso(self.start_ms),
            "end": _iso(self.end_ms),
            "funding_events": self.funding_events,
            "active_intervals": self.active_intervals,
            "entries": self.entries,
            "exits": self.exits,
            "positive_funding_intervals": self.positive_funding_intervals,
            "total_return": self.total_return,
            "annualized_return": self.annualized_return,
            "sharpe": self.sharpe,
            "max_drawdown": self.max_drawdown,
            "funding_component": self.funding_component,
            "basis_component": self.basis_component,
            "trading_cost_component": self.trading_cost_component,
        }


def _iso(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc).isoformat()


def _parse_iso(value: str) -> int:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.astimezone(timezone.utc).timestamp() * 1000)


def _number(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def fetch_funding(symbol: str, start_ms: int, end_ms: int) -> list[FundingEvent]:
    rows: dict[int, FundingEvent] = {}
    cursor_end = end_ms
    while cursor_end >= start_ms:
        result = _http_json(
            "/v5/market/funding/history",
            {"category": "linear", "symbol": symbol, "endTime": cursor_end, "limit": 200},
        )["result"]
        batch = list(result.get("list", []))
        if not batch:
            break
        oldest = cursor_end
        for row in batch:
            ts = int(row.get("fundingRateTimestamp") or 0)
            rate = _number(row.get("fundingRate"))
            oldest = min(oldest, ts) if ts > 0 else oldest
            if start_ms <= ts <= end_ms:
                rows[ts] = FundingEvent(ts, rate)
        if oldest <= start_ms or oldest >= cursor_end:
            break
        cursor_end = oldest - 1
        time.sleep(0.03)
    return [rows[key] for key in sorted(rows)]


def fetch_hourly(symbol: str, category: str, start_ms: int, end_ms: int) -> list[PriceBar]:
    rows: dict[int, PriceBar] = {}
    cursor_end = end_ms
    while cursor_end >= start_ms:
        result = _http_json(
            "/v5/market/kline",
            {
                "category": category,
                "symbol": symbol,
                "interval": "60",
                "start": start_ms,
                "end": cursor_end,
                "limit": 1000,
            },
        )["result"]
        batch = list(result.get("list", []))
        if not batch:
            break
        oldest = cursor_end
        for row in batch:
            if not isinstance(row, Sequence) or len(row) < 5:
                continue
            ts = int(row[0])
            close = _number(row[4])
            oldest = min(oldest, ts)
            if close > 0 and start_ms - HOUR_MS <= ts <= end_ms:
                rows[ts] = PriceBar(ts, close)
        if oldest <= start_ms or oldest >= cursor_end:
            break
        cursor_end = oldest - 1
        time.sleep(0.03)
    return [rows[key] for key in sorted(rows)]


def write_prices(path: Path, rows: Sequence[PriceBar]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["start_ms", "time_utc", "close"])
        w.writeheader()
        for r in rows:
            w.writerow({"start_ms": r.start_ms, "time_utc": _iso(r.start_ms), "close": r.close})


def read_prices(path: Path) -> list[PriceBar]:
    out: list[PriceBar] = []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            ts = int(r["start_ms"])
            close = _number(r["close"])
            if ts > 0 and close > 0:
                out.append(PriceBar(ts, close))
    return sorted(out, key=lambda x: x.start_ms)


def write_funding(path: Path, rows: Sequence[FundingEvent]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["ts_ms", "time_utc", "funding_rate"])
        w.writeheader()
        for r in rows:
            w.writerow({"ts_ms": r.ts_ms, "time_utc": _iso(r.ts_ms), "funding_rate": r.rate})


def read_funding(path: Path) -> list[FundingEvent]:
    out: list[FundingEvent] = []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            ts = int(r["ts_ms"])
            if ts > 0:
                out.append(FundingEvent(ts, _number(r["funding_rate"])))
    return sorted(out, key=lambda x: x.ts_ms)


def ensure_data(root: Path, symbol: str, start_ms: int, end_ms: int, refresh: bool) -> tuple[list[FundingEvent], list[PriceBar], list[PriceBar]]:
    root.mkdir(parents=True, exist_ok=True)
    fp = root / f"{symbol}_funding.csv"
    sp = root / f"{symbol}_spot_60.csv"
    pp = root / f"{symbol}_perp_60.csv"
    if refresh or not fp.is_file():
        print(f"download {symbol} funding")
        write_funding(fp, fetch_funding(symbol, start_ms, end_ms))
    if refresh or not sp.is_file():
        print(f"download {symbol} spot H1")
        write_prices(sp, fetch_hourly(symbol, "spot", start_ms, end_ms))
    if refresh or not pp.is_file():
        print(f"download {symbol} perp H1")
        write_prices(pp, fetch_hourly(symbol, "linear", start_ms, end_ms))
    return read_funding(fp), read_prices(sp), read_prices(pp)


class PriceLookup:
    def __init__(self, rows: Sequence[PriceBar]) -> None:
        self.rows = list(rows)
        self.ends = [r.end_ms for r in self.rows]

    def at_or_before(self, ts_ms: int) -> float | None:
        i = bisect.bisect_right(self.ends, ts_ms) - 1
        if i < 0:
            return None
        bar = self.rows[i]
        if ts_ms - bar.end_ms > 2 * HOUR_MS:
            return None
        return bar.close


def _metrics(returns: Sequence[tuple[int, float]], start_ms: int, end_ms: int) -> tuple[float, float, float, float]:
    equity = 1.0
    peak = 1.0
    max_dd = 0.0
    values: list[float] = []
    for _, r in returns:
        values.append(r)
        equity *= max(1e-9, 1.0 + r)
        peak = max(peak, equity)
        max_dd = max(max_dd, 1.0 - equity / peak)
    total = equity - 1.0
    years = max((end_ms - start_ms) / 1000.0 / YEAR_SECONDS, 1 / 365.25)
    annual = equity ** (1.0 / years) - 1.0 if equity > 0 else -1.0
    if len(values) >= 3:
        mean = sum(values) / len(values)
        variance = sum((x - mean) ** 2 for x in values) / len(values)
        sigma = math.sqrt(variance)
        intervals_per_year = len(values) / years
        sharpe = mean / sigma * math.sqrt(intervals_per_year) if sigma > 1e-12 else 0.0
    else:
        sharpe = 0.0
    return total, annual, sharpe, max_dd


def backtest(
    symbol: str,
    period: str,
    funding: Sequence[FundingEvent],
    spot: Sequence[PriceBar],
    perp: Sequence[PriceBar],
    start_ms: int,
    end_ms: int,
    *,
    threshold: float,
    fee_bps_per_execution: float,
    slippage_bps_per_execution: float,
) -> Result:
    events = [x for x in funding if start_ms <= x.ts_ms <= end_ms]
    spot_px = PriceLookup(spot)
    perp_px = PriceLookup(perp)
    cost = (fee_bps_per_execution + slippage_bps_per_execution) / 10_000.0
    # One state transition trades two legs. On fully collateralised capital (2N),
    # its capital-return cost is exactly one per-execution rate `cost`.
    active = False
    entries = exits = active_intervals = positive = 0
    funding_component = basis_component = trading_cost_component = 0.0
    returns: list[tuple[int, float]] = []

    for i in range(len(events) - 1):
        cur, nxt = events[i], events[i + 1]
        if nxt.ts_ms > end_ms:
            break
        s0, s1 = spot_px.at_or_before(cur.ts_ms), spot_px.at_or_before(nxt.ts_ms)
        p0, p1 = perp_px.at_or_before(cur.ts_ms), perp_px.at_or_before(nxt.ts_ms)
        if not all(v is not None and v > 0 for v in (s0, s1, p0, p1)):
            continue
        desired = cur.rate >= threshold
        step = 0.0
        if desired != active:
            step -= cost
            trading_cost_component -= cost
            if desired:
                entries += 1
            else:
                exits += 1
            active = desired
        if active:
            active_intervals += 1
            if nxt.rate > 0:
                positive += 1
            spot_ret = float(s1) / float(s0) - 1.0
            perp_ret = float(p1) / float(p0) - 1.0
            basis = (spot_ret - perp_ret) / 2.0
            funding_ret = (nxt.rate * (float(p1) / float(p0))) / 2.0
            basis_component += basis
            funding_component += funding_ret
            step += basis + funding_ret
        returns.append((nxt.ts_ms, step))

    if active:
        exits += 1
        trading_cost_component -= cost
        if returns:
            ts, last = returns[-1]
            returns[-1] = (ts, last - cost)
        else:
            returns.append((end_ms, -cost))

    total, annual, sharpe, max_dd = _metrics(returns, start_ms, end_ms)
    return Result(
        symbol=symbol,
        period=period,
        start_ms=start_ms,
        end_ms=end_ms,
        funding_events=len(events),
        active_intervals=active_intervals,
        entries=entries,
        exits=exits,
        positive_funding_intervals=positive,
        total_return=total,
        annualized_return=annual,
        sharpe=sharpe,
        max_drawdown=max_dd,
        funding_component=funding_component,
        basis_component=basis_component,
        trading_cost_component=trading_cost_component,
    )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="TradeMind v1.37 delta-neutral Bybit funding carry backtest")
    p.add_argument("--symbols", default="BTCUSDT,ETHUSDT")
    p.add_argument("--start", default="2023-01-01T00:00:00+00:00")
    p.add_argument("--holdout-start", default="2025-01-01T00:00:00+00:00")
    p.add_argument("--end", default="2026-08-01T00:00:00+00:00")
    p.add_argument("--threshold", type=float, default=0.0001, help="Settled funding threshold; 0.0001 = 0.01%")
    p.add_argument("--fee-bps", type=float, default=5.5)
    p.add_argument("--slippage-bps", type=float, default=1.0)
    p.add_argument("--data-dir", type=Path, default=Path("data/funding_carry_v137"))
    p.add_argument("--refresh", action="store_true")
    return p


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    start_ms, holdout_ms, end_ms = map(_parse_iso, (args.start, args.holdout_start, args.end))
    if not start_ms < holdout_ms < end_ms:
        raise SystemExit("require start < holdout-start < end")
    symbols = [x.strip().upper() for x in args.symbols.split(",") if x.strip()]
    out_rows: list[dict[str, Any]] = []
    print("TradeMind v1.37 DELTA-NEUTRAL FUNDING CARRY")
    print(f"Fixed threshold: {args.threshold:.6f} ({args.threshold*100:.4f}%)")
    print(f"Costs: fee={args.fee_bps:.2f} bps + slippage={args.slippage_bps:.2f} bps per execution")
    print("Signal uses settled funding only for the NEXT interval. No look-ahead. READ-ONLY.")

    for symbol in symbols:
        print(f"\n===== {symbol} =====")
        funding, spot, perp = ensure_data(args.data_dir / "raw", symbol, start_ms, end_ms, args.refresh)
        print(f"funding={len(funding)} spotH1={len(spot)} perpH1={len(perp)}")
        for label, a, b in (("TRAIN", start_ms, holdout_ms), ("HOLDOUT", holdout_ms, end_ms)):
            r = backtest(
                symbol, label, funding, spot, perp, a, b,
                threshold=args.threshold,
                fee_bps_per_execution=args.fee_bps,
                slippage_bps_per_execution=args.slippage_bps,
            )
            out_rows.append(r.as_dict())
            print(
                f"{label}: events={r.funding_events} active={r.active_intervals} entries={r.entries} "
                f"return={r.total_return*100:.2f}% ann={r.annualized_return*100:.2f}% "
                f"Sharpe={r.sharpe:.3f} maxDD={r.max_drawdown*100:.2f}% "
                f"funding={r.funding_component*100:.2f}% basis={r.basis_component*100:.2f}% costs={r.trading_cost_component*100:.2f}%"
            )

    args.data_dir.mkdir(parents=True, exist_ok=True)
    status = {
        "version": VERSION,
        "strategy": "LONG_SPOT_SHORT_PERP_POSITIVE_FUNDING_CARRY",
        "threshold": args.threshold,
        "fee_bps_per_execution": args.fee_bps,
        "slippage_bps_per_execution": args.slippage_bps,
        "capital_assumption": "spot fully funded + perp 1x collateral; returns reported on combined 2N capital",
        "lookahead_rule": "funding settled at event t may only control exposure from t to next funding event",
        "rows": out_rows,
    }
    (args.data_dir / "status.json").write_text(json.dumps(status, indent=2), encoding="utf-8")
    print("\n===== V1.37 SUMMARY =====")
    for row in out_rows:
        print(
            f"{row['symbol']:8s} {row['period']:7s} | return={row['total_return']*100:7.2f}% "
            f"ann={row['annualized_return']*100:7.2f}% Sharpe={row['sharpe']:6.3f} "
            f"DD={row['max_drawdown']*100:6.2f}% entries={row['entries']}"
        )
    print(f"Output: {args.data_dir / 'status.json'}")
    print("READ-ONLY. No orders. No API keys. Public market data only.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
