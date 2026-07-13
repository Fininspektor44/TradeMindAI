"""Environment-based application configuration.

Real credentials must stay in the local `.env` file and must never be committed.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _csv(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


@dataclass(frozen=True, slots=True)
class Settings:
    environment: str = "development"
    log_level: str = "INFO"
    provider: str = "mock"
    symbols: tuple[str, ...] = ("XAUUSD", "EURUSD", "GBPUSD")
    timeframe: str = "M5"
    market_data_dir: Path = Path("data/mt5")
    max_data_age_seconds: int = 0

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
        )
