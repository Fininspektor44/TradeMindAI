"""Point-in-time market-data access for Discovery Engine research."""

from __future__ import annotations

import csv
import hashlib
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from trademind.market.models import Candle
from trademind.market.provider import MarketDataProvider


class PointInTimeError(RuntimeError):
    """Raised when a provider cannot satisfy a point-in-time request safely."""


class DatasetIntegrityError(RuntimeError):
    """Raised when a frozen historical artifact cannot be trusted."""


_TIMEFRAME_RE = re.compile(r"^(M|H|D)(\d+)$", re.IGNORECASE)


def _aware_utc(value: datetime, *, label: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")
    return value.astimezone(timezone.utc)


def timeframe_delta(timeframe: str) -> timedelta:
    match = _TIMEFRAME_RE.fullmatch(timeframe.strip())
    if match is None:
        raise ValueError(f"unsupported timeframe: {timeframe!r}")
    unit, raw = match.groups()
    amount = int(raw)
    if amount <= 0:
        raise ValueError("timeframe amount must be positive")
    if unit.upper() == "M":
        return timedelta(minutes=amount)
    if unit.upper() == "H":
        return timedelta(hours=amount)
    return timedelta(days=amount)


def candle_close_time(candle: Candle) -> datetime:
    return _aware_utc(candle.time, label="candle.time") + timeframe_delta(candle.timeframe)


def _closed_at_or_before(candle: Candle, as_of: datetime) -> bool:
    return candle_close_time(candle) <= as_of


class PointInTimeMarketData:
    """Fail-closed wrapper around the legacy latest-count provider contract.

    The legacy provider cannot query arbitrary historical cutoffs. Therefore this
    wrapper only returns a request when the provider's returned window itself
    contains enough fully closed candles at or before ``as_of``. It never pads a
    historical request with newer bars.
    """

    def __init__(self, provider: MarketDataProvider, *, fetch_multiplier: int = 4) -> None:
        if fetch_multiplier < 1:
            raise ValueError("fetch_multiplier must be at least 1")
        self.provider = provider
        self.fetch_multiplier = int(fetch_multiplier)

    def healthcheck(self) -> bool:
        return self.provider.healthcheck()

    def get_candles(
        self,
        symbol: str,
        timeframe: str,
        count: int,
        *,
        as_of: datetime,
    ) -> list[Candle]:
        if count < 1:
            raise ValueError("count must be positive")
        cutoff = _aware_utc(as_of, label="as_of")
        fetched = self.provider.get_candles(
            symbol,
            timeframe,
            max(count, count * self.fetch_multiplier),
        )
        safe = [
            candle
            for candle in fetched
            if candle.symbol.upper() == symbol.upper()
            and candle.timeframe.upper() == timeframe.upper()
            and _closed_at_or_before(candle, cutoff)
        ]
        safe.sort(key=lambda candle: _aware_utc(candle.time, label="candle.time"))
        if len(safe) < count:
            raise PointInTimeError(
                "legacy provider cannot satisfy this as_of safely; "
                "use an immutable historical dataset artifact"
            )
        return safe[-count:]


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class ImmutableHistoricalDatasetReader:
    """Read a frozen candle CSV only after verifying its exact SHA-256."""

    REQUIRED_COLUMNS = frozenset(
        {"symbol", "timeframe", "time", "open", "high", "low", "close"}
    )

    def __init__(self, path: str | Path, *, expected_sha256: str) -> None:
        self.path = Path(path)
        self.expected_sha256 = expected_sha256.lower()
        if not re.fullmatch(r"[0-9a-f]{64}", self.expected_sha256):
            raise ValueError("expected_sha256 must be a SHA-256 hex digest")

    def verify(self) -> None:
        if not self.path.is_file():
            raise DatasetIntegrityError(f"dataset not found: {self.path}")
        actual = sha256_file(self.path)
        if actual != self.expected_sha256:
            raise DatasetIntegrityError(
                f"dataset hash mismatch: expected {self.expected_sha256}, got {actual}"
            )

    @staticmethod
    def _parse_row(row: dict[str, str]) -> Candle:
        try:
            time = datetime.fromisoformat(row["time"])
            tick_volume = int(row.get("tick_volume") or 0)
            spread = int(row.get("spread") or 0)
            return Candle(
                symbol=row["symbol"].strip().upper(),
                timeframe=row["timeframe"].strip().upper(),
                time=time,
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                tick_volume=tick_volume,
                spread=spread,
            )
        except (KeyError, TypeError, ValueError, UnicodeError) as exc:
            raise DatasetIntegrityError(f"invalid candle row: {row!r}") from exc

    def get_candles(
        self,
        *,
        as_of: datetime,
        symbol: str | None = None,
        timeframe: str | None = None,
        count: int | None = None,
    ) -> list[Candle]:
        cutoff = _aware_utc(as_of, label="as_of")
        if count is not None and count < 1:
            raise ValueError("count must be positive when provided")
        self.verify()
        try:
            with self.path.open("r", encoding="utf-8", newline="") as handle:
                reader = csv.DictReader(handle)
                columns = frozenset(reader.fieldnames or ())
                if not self.REQUIRED_COLUMNS.issubset(columns):
                    missing = sorted(self.REQUIRED_COLUMNS - columns)
                    raise DatasetIntegrityError(f"dataset missing required columns: {missing}")
                candles = [self._parse_row(dict(row)) for row in reader]
        except (UnicodeDecodeError, csv.Error, OSError) as exc:
            raise DatasetIntegrityError(f"cannot read dataset safely: {self.path}") from exc

        wanted_symbol = symbol.upper() if symbol else None
        wanted_timeframe = timeframe.upper() if timeframe else None
        safe = []
        for candle in candles:
            if wanted_symbol and candle.symbol.upper() != wanted_symbol:
                continue
            if wanted_timeframe and candle.timeframe.upper() != wanted_timeframe:
                continue
            if _closed_at_or_before(candle, cutoff):
                safe.append(candle)
        safe.sort(key=lambda candle: _aware_utc(candle.time, label="candle.time"))
        return safe[-count:] if count is not None else safe
