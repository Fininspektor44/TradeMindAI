"""CSV market-data provider for files exported by MetaTrader 5."""

from __future__ import annotations

import csv
import re
from datetime import datetime, timezone
from pathlib import Path

from trademind.market.models import Candle

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


class CsvMarketDataProvider:
    """Reads closed candles exported by the read-only MT5 bridge."""

    def __init__(self, data_dir: str | Path, max_age_seconds: int = 0) -> None:
        self.data_dir = Path(data_dir).expanduser().resolve()
        if max_age_seconds < 0:
            raise ValueError("max_age_seconds cannot be negative")
        self.max_age_seconds = max_age_seconds

    def healthcheck(self) -> bool:
        return self.data_dir.is_dir()

    def get_candles(self, symbol: str, timeframe: str, count: int) -> list[Candle]:
        if count <= 0:
            raise ValueError("count must be greater than zero")

        symbol = symbol.upper()
        timeframe = timeframe.upper()
        path = self.data_dir / self.filename(symbol, timeframe)
        if not path.is_file():
            raise FileNotFoundError(f"MT5 candle file not found: {path}")

        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            columns = set(reader.fieldnames or ())
            missing = _REQUIRED_COLUMNS - columns
            if missing:
                names = ", ".join(sorted(missing))
                raise ValueError(f"CSV file is missing required columns: {names}")

            by_time: dict[datetime, Candle] = {}
            for row_number, row in enumerate(reader, start=2):
                try:
                    row_symbol = row["symbol"].strip().upper()
                    row_timeframe = row["timeframe"].strip().upper()
                    if row_symbol != symbol or row_timeframe != timeframe:
                        continue

                    timestamp = datetime.fromtimestamp(int(row["time"]), tz=timezone.utc)
                    candle = Candle(
                        symbol=row_symbol,
                        timeframe=row_timeframe,
                        time=timestamp,
                        open=float(row["open"]),
                        high=float(row["high"]),
                        low=float(row["low"]),
                        close=float(row["close"]),
                        tick_volume=int(row["tick_volume"]),
                        spread=int(row["spread"]),
                    )
                except (KeyError, TypeError, ValueError) as exc:
                    raise ValueError(f"Invalid candle at {path}:{row_number}: {exc}") from exc
                by_time[timestamp] = candle

        candles = [by_time[key] for key in sorted(by_time)]
        if len(candles) < count:
            raise ValueError(
                f"Not enough candles in {path}: required {count}, available {len(candles)}"
            )

        result = candles[-count:]
        self._validate_freshness(result[-1], path)
        return result

    def _validate_freshness(self, latest: Candle, path: Path) -> None:
        if self.max_age_seconds == 0:
            return
        age_seconds = (datetime.now(timezone.utc) - latest.time).total_seconds()
        if age_seconds > self.max_age_seconds:
            raise ValueError(
                f"Stale MT5 data in {path}: latest candle is {int(age_seconds)} seconds old"
            )

    @staticmethod
    def filename(symbol: str, timeframe: str) -> str:
        safe_symbol = re.sub(r"[^A-Za-z0-9._-]+", "_", symbol.strip())
        safe_timeframe = re.sub(r"[^A-Za-z0-9._-]+", "_", timeframe.strip())
        return f"{safe_symbol}_{safe_timeframe}.csv"
