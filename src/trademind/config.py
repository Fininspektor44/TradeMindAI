"""Environment-based application configuration.

Real credentials must stay in the local `.env` file and must never be committed.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _csv(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _ints(value: str) -> tuple[int, ...]:
    return tuple(int(item) for item in _csv(value))


def _point_sizes(value: str) -> dict[str, float]:
    result: dict[str, float] = {}
    for item in _csv(value):
        symbol, separator, raw_size = item.partition("=")
        if not separator or not symbol.strip() or not raw_size.strip():
            raise ValueError(f"Invalid point-size item: {item}")
        result[symbol.strip().upper()] = float(raw_size)
    return result


@dataclass(frozen=True, slots=True)
class Settings:
    environment: str = "development"
    log_level: str = "INFO"
    provider: str = "mock"
    symbols: tuple[str, ...] = ("XAUUSD", "EURUSD", "GBPUSD")
    timeframe: str = "M5"
    market_data_dir: Path = Path("data/mt5")
    max_data_age_seconds: int = 0
    journal_dir: Path = Path("data/journal")
    evaluation_horizons: tuple[int, ...] = (3, 6, 12)
    point_sizes: dict[str, float] | None = None

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            environment=os.getenv("TRADEMIND_ENV", "development"),
            log_level=os.getenv("TRADEMIND_LOG_LEVEL", "INFO").upper(),
            provider=os.getenv("TRADEMIND_PROVIDER", "mock").lower(),
            symbols=_csv(os.getenv("TRADEMIND_SYMBOLS", "XAUUSD,EURUSD,GBPUSD")),
            timeframe=os.getenv("TRADEMIND_TIMEFRAME", "M5").upper(),
            market_data_dir=Path(os.getenv("TRADEMIND_DATA_DIR", "data/mt5")),
            max_data_age_seconds=int(os.getenv("TRADEMIND_MAX_DATA_AGE_SECONDS", "0")),
            journal_dir=Path(os.getenv("TRADEMIND_JOURNAL_DIR", "data/journal")),
            evaluation_horizons=_ints(os.getenv("TRADEMIND_EVAL_HORIZONS", "3,6,12")),
            point_sizes=_point_sizes(
                os.getenv(
                    "TRADEMIND_POINT_SIZES",
                    "XAUUSD=0.01,EURUSD=0.00001,GBPUSD=0.00001",
                )
            ),
        )
