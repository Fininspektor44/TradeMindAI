"""Health checks for the TradeMind ECN research pipeline."""

from __future__ import annotations

import argparse
import csv
import os
import re
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

from trademind.market.csv_provider import CsvMarketDataProvider

_REQUIRED_COLUMNS = {
    "time",
    "symbol",
    "timeframe",
    "open",
    "high",
    "low",
    "close",
    "tick_volume",
    "spread",
}
_TIMEFRAME_PATTERN = re.compile(r"^(M|H|D)(\d+)$")
_SEVERITY = {"OK": 0, "WARN": 1, "ERROR": 2}


@dataclass(frozen=True)
class DataHealth:
    symbol: str
    status: str
    rows: int = 0
    latest_time: datetime | None = None
    age_minutes: float | None = None
    spread: int | None = None
    tick_volume: int | None = None
    gap_count: int = 0
    max_gap_minutes: float = 0.0
    message: str = ""

    def render(self) -> str:
        latest = self.latest_time.isoformat() if self.latest_time else "n/a"
        age = f"{self.age_minutes:.1f}" if self.age_minutes is not None else "n/a"
        spread = str(self.spread) if self.spread is not None else "n/a"
        volume = str(self.tick_volume) if self.tick_volume is not None else "n/a"
        return (
            f"{self.symbol:<14} status={self.status:<5} rows={self.rows:<5} "
            f"latest={latest} age_min={age:<8} spread={spread:<6} volume={volume:<8} "
            f"gaps={self.gap_count:<3} max_gap_min={self.max_gap_minutes:.1f} "
            f"{self.message}".rstrip()
        )


@dataclass(frozen=True)
class JournalHealth:
    status: str
    rows: int
    schema_rows: int
    duplicate_ids: int
    latest_time: datetime | None
    age_minutes: float | None
    counts: dict[str, int]
    message: str = ""

    def render(self) -> list[str]:
        latest = self.latest_time.isoformat() if self.latest_time else "n/a"
        age = f"{self.age_minutes:.1f}" if self.age_minutes is not None else "n/a"
        lines = [
            (
                f"status={self.status} rows={self.rows} schema_rows={self.schema_rows} "
                f"duplicate_ids={self.duplicate_ids} latest={latest} age_min={age} "
                f"{self.message}"
            ).rstrip()
        ]
        lines.extend(f"  {symbol:<14} schema_rows={count}" for symbol, count in self.counts.items())
        return lines


def _timeframe_minutes(timeframe: str) -> int:
    match = _TIMEFRAME_PATTERN.fullmatch(timeframe.strip().upper())
    if not match:
        raise ValueError(f"Unsupported timeframe: {timeframe}")
    unit, value_text = match.groups()
    value = int(value_text)
    if value <= 0:
        raise ValueError(f"Invalid timeframe: {timeframe}")
    if unit == "M":
        return value
    if unit == "H":
        return value * 60
    return value * 1440


def _spans_weekend(start: datetime, end: datetime) -> bool:
    day = start.date()
    while day <= end.date():
        if day.weekday() >= 5:
            return True
        day += timedelta(days=1)
    return False


def _gap_statistics(
    times: Iterable[datetime],
    timeframe_minutes: int,
    gap_multiple: int,
) -> tuple[int, float]:
    ordered = sorted(set(times))
    threshold = timeframe_minutes * gap_multiple
    gaps: list[float] = []
    for previous, current in zip(ordered, ordered[1:]):
        minutes = (current - previous).total_seconds() / 60.0
        if minutes <= threshold or _spans_weekend(previous, current):
            continue
        gaps.append(minutes)
    return len(gaps), max(gaps, default=0.0)


def _stale_status(now: datetime, age_minutes: float, maximum: int) -> tuple[str, str]:
    if age_minutes <= maximum:
        return "OK", ""
    if now.weekday() >= 5:
        return "WARN", "stale during weekend"
    return "ERROR", f"latest candle older than {maximum} minutes"


def inspect_market_file(
    path: Path,
    symbol: str,
    timeframe: str,
    *,
    now: datetime,
    max_age_minutes: int,
    inspect_rows: int = 500,
    gap_multiple: int = 6,
) -> DataHealth:
    symbol_name = symbol.strip().upper()
    timeframe_name = timeframe.strip().upper()
    if not path.is_file():
        return DataHealth(symbol=symbol_name, status="ERROR", message=f"missing file: {path}")

    recent: deque[tuple[datetime, int, int]] = deque(maxlen=inspect_rows)
    matching_rows = 0
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            columns = set(reader.fieldnames or ())
            missing = _REQUIRED_COLUMNS - columns
            if missing:
                names = ",".join(sorted(missing))
                return DataHealth(
                    symbol=symbol_name,
                    status="ERROR",
                    message=f"missing columns: {names}",
                )
            for row in reader:
                if row["symbol"].strip().upper() != symbol_name:
                    continue
                if row["timeframe"].strip().upper() != timeframe_name:
                    continue
                timestamp = datetime.fromtimestamp(int(row["time"]), tz=timezone.utc)
                recent.append((timestamp, int(row["spread"]), int(row["tick_volume"])))
                matching_rows += 1
    except (OSError, TypeError, ValueError) as exc:
        return DataHealth(symbol=symbol_name, status="ERROR", message=f"invalid CSV: {exc}")

    if not recent:
        return DataHealth(
            symbol=symbol_name,
            status="ERROR",
            rows=matching_rows,
            message="no matching candles",
        )

    latest_time, spread, tick_volume = max(recent, key=lambda item: item[0])
    age_minutes = max(0.0, (now - latest_time).total_seconds() / 60.0)
    status, stale_message = _stale_status(now, age_minutes, max_age_minutes)
    messages = [stale_message] if stale_message else []
    if spread <= 0:
        status = "WARN" if _SEVERITY[status] < _SEVERITY["WARN"] else status
        messages.append("non-positive spread")
    if tick_volume <= 0:
        status = "WARN" if _SEVERITY[status] < _SEVERITY["WARN"] else status
        messages.append("non-positive tick volume")

    gap_count, max_gap = _gap_statistics(
        (item[0] for item in recent),
        _timeframe_minutes(timeframe_name),
        gap_multiple,
    )
    return DataHealth(
        symbol=symbol_name,
        status=status,
        rows=matching_rows,
        latest_time=latest_time,
        age_minutes=age_minutes,
        spread=spread,
        tick_volume=tick_volume,
        gap_count=gap_count,
        max_gap_minutes=max_gap,
        message="; ".join(messages),
    )


def inspect_journal(
    path: Path,
    symbols: list[str],
    *,
    schema_version: str,
    now: datetime,
    max_age_minutes: int,
) -> JournalHealth:
    counts = {symbol.upper(): 0 for symbol in symbols}
    if not path.is_file():
        return JournalHealth(
            status="ERROR",
            rows=0,
            schema_rows=0,
            duplicate_ids=0,
            latest_time=None,
            age_minutes=None,
            counts=counts,
            message=f"missing journal: {path}",
        )

    row_count = 0
    schema_rows = 0
    latest_time: datetime | None = None
    seen_ids: set[str] = set()
    duplicate_ids = 0
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                row_count += 1
                signal_id = row.get("signal_id", "").strip()
                if signal_id:
                    if signal_id in seen_ids:
                        duplicate_ids += 1
                    seen_ids.add(signal_id)
                if row.get("schema_version", "").strip() != schema_version:
                    continue
                symbol = row.get("symbol", "").strip().upper()
                if symbol in counts:
                    counts[symbol] += 1
                signal_time = row.get("signal_time", "").strip()
                if signal_time:
                    parsed = datetime.fromisoformat(signal_time)
                    latest_time = parsed if latest_time is None else max(latest_time, parsed)
                schema_rows += 1
    except (OSError, TypeError, ValueError) as exc:
        return JournalHealth(
            status="ERROR",
            rows=row_count,
            schema_rows=schema_rows,
            duplicate_ids=duplicate_ids,
            latest_time=latest_time,
            age_minutes=None,
            counts=counts,
            message=f"invalid journal: {exc}",
        )

    messages: list[str] = []
    status = "OK"
    missing_symbols = [symbol for symbol, count in counts.items() if count == 0]
    if missing_symbols:
        status = "WARN"
        messages.append(f"no schema {schema_version} rows for {','.join(missing_symbols)}")
    if duplicate_ids:
        status = "ERROR"
        messages.append(f"duplicate signal ids: {duplicate_ids}")

    age_minutes: float | None = None
    if latest_time is None:
        status = "ERROR"
        messages.append(f"no schema {schema_version} observations")
    else:
        age_minutes = max(0.0, (now - latest_time).total_seconds() / 60.0)
        stale_status, stale_message = _stale_status(now, age_minutes, max_age_minutes)
        if _SEVERITY[stale_status] > _SEVERITY[status]:
            status = stale_status
        if stale_message:
            messages.append(stale_message.replace("candle", "journal observation"))

    return JournalHealth(
        status=status,
        rows=row_count,
        schema_rows=schema_rows,
        duplicate_ids=duplicate_ids,
        latest_time=latest_time,
        age_minutes=age_minutes,
        counts=counts,
        message="; ".join(messages),
    )


def build_report(
    data_dir: Path,
    journal_path: Path,
    symbols: list[str],
    timeframe: str,
    *,
    schema_version: str,
    max_age_minutes: int,
    now: datetime | None = None,
) -> tuple[str, int]:
    report_time = now or datetime.now(timezone.utc)
    checks = [
        inspect_market_file(
            data_dir / CsvMarketDataProvider.filename(symbol, timeframe),
            symbol,
            timeframe,
            now=report_time,
            max_age_minutes=max_age_minutes,
        )
        for symbol in symbols
    ]
    journal = inspect_journal(
        journal_path,
        symbols,
        schema_version=schema_version,
        now=report_time,
        max_age_minutes=max_age_minutes,
    )

    severity = max(
        [_SEVERITY[item.status] for item in checks] + [_SEVERITY[journal.status]],
        default=0,
    )
    lines = [
        "TradeMind research health",
        f"Generated UTC: {report_time.isoformat()}",
        f"Data directory: {data_dir}",
        f"Journal: {journal_path}",
        f"Symbols: {','.join(symbols)}",
        f"Timeframe: {timeframe.upper()}",
        f"Maximum data age: {max_age_minutes} minutes",
        "",
        "Market CSV files",
    ]
    lines.extend(item.render() for item in checks)
    lines.extend(["", f"Journal schema {schema_version}"])
    lines.extend(journal.render())
    lines.extend(["", f"Overall status: {('OK', 'WARN', 'ERROR')[severity]}"])
    return "\n".join(lines), severity


def main() -> int:
    parser = argparse.ArgumentParser(description="Check TradeMind ECN research data health")
    default_data_dir = Path(
        os.getenv(
            "TRADEMIND_DATA_DIR",
            Path(os.getenv("APPDATA", ""))
            / "MetaQuotes"
            / "Terminal"
            / "Common"
            / "Files"
            / "TradeMindAI_ECN",
        )
    )
    default_journal = (
        Path(os.getenv("TRADEMIND_JOURNAL_DIR", "data/journal_ecn")) / "signals.csv"
    )
    parser.add_argument("--data-dir", type=Path, default=default_data_dir)
    parser.add_argument("--journal", type=Path, default=default_journal)
    parser.add_argument(
        "--symbols",
        default="XAUUSD,XAGUSD,.USTECHCash,.US500Cash,.US30Cash,WTI,BRENT",
    )
    parser.add_argument("--timeframe", default="M5")
    parser.add_argument("--schema-version", default="1.1")
    parser.add_argument("--max-age-minutes", type=int, default=20)
    args = parser.parse_args()

    if args.max_age_minutes < 1:
        parser.error("--max-age-minutes must be at least 1")
    symbols = [item.strip().upper() for item in args.symbols.split(",") if item.strip()]
    if not symbols:
        parser.error("--symbols must contain at least one symbol")

    report, severity = build_report(
        args.data_dir.expanduser().resolve(),
        args.journal.expanduser().resolve(),
        symbols,
        args.timeframe,
        schema_version=args.schema_version,
        max_age_minutes=args.max_age_minutes,
    )
    print(report)
    return 2 if severity >= _SEVERITY["ERROR"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
