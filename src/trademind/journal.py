"""Persistent signal journal and forward outcome evaluation."""

from __future__ import annotations

import csv
from collections.abc import Mapping, Sequence
from pathlib import Path

from trademind.market.models import Candle
from trademind.signals.models import SignalAction, SignalResult

_BASE_FIELDS = [
    "signal_id",
    "signal_time",
    "symbol",
    "timeframe",
    "action",
    "score",
    "confidence",
    "entry_price",
    "spread_points",
    "point_size",
    "spread_cost",
    "ema_fast",
    "ema_slow",
    "rsi",
    "atr",
    "reasons",
]


class SignalJournal:
    """Stores unique signals and evaluates them after future candles arrive."""

    def __init__(
        self,
        directory: str | Path,
        horizons: Sequence[int] = (3, 6, 12),
        point_sizes: Mapping[str, float] | None = None,
    ) -> None:
        self.directory = Path(directory).expanduser().resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / "signals.csv"
        self.horizons = tuple(sorted(set(horizons)))
        if not self.horizons or any(horizon <= 0 for horizon in self.horizons):
            raise ValueError("Evaluation horizons must contain positive integers")
        self.point_sizes = {
            symbol.upper(): float(value) for symbol, value in (point_sizes or {}).items()
        }
        if any(value <= 0 for value in self.point_sizes.values()):
            raise ValueError("Point sizes must be greater than zero")

    @property
    def fieldnames(self) -> list[str]:
        fields = list(_BASE_FIELDS)
        for horizon in self.horizons:
            fields.extend(
                [
                    f"exit_time_{horizon}",
                    f"exit_price_{horizon}",
                    f"market_move_{horizon}",
                    f"net_move_{horizon}",
                    f"mfe_{horizon}",
                    f"mae_{horizon}",
                    f"outcome_{horizon}",
                ]
            )
        return fields

    def record(self, result: SignalResult, candle: Candle) -> bool:
        """Append a signal unless the same symbol/timeframe/candle already exists."""
        signal_id = self._signal_id(candle)
        rows = self._read_rows()
        if any(row.get("signal_id") == signal_id for row in rows):
            return False

        point_size = self.point_sizes.get(result.symbol.upper(), 0.0)
        spread_cost = candle.spread * point_size if point_size else 0.0
        row = {field: "" for field in self.fieldnames}
        row.update(
            {
                "signal_id": signal_id,
                "signal_time": candle.time.isoformat(),
                "symbol": result.symbol,
                "timeframe": result.timeframe,
                "action": result.action.value,
                "score": str(result.score),
                "confidence": str(result.confidence),
                "entry_price": self._number(candle.close),
                "spread_points": str(candle.spread),
                "point_size": self._number(point_size),
                "spread_cost": self._number(spread_cost),
                "ema_fast": self._number(result.ema_fast),
                "ema_slow": self._number(result.ema_slow),
                "rsi": self._number(result.rsi),
                "atr": self._number(result.atr),
                "reasons": " | ".join(result.reasons),
            }
        )
        rows.append(row)
        self._write_rows(rows)
        return True

    def evaluate(self, symbol: str, timeframe: str, candles: Sequence[Candle]) -> int:
        """Fill pending forward outcomes using newly available closed candles."""
        if not candles:
            return 0

        symbol = symbol.upper()
        timeframe = timeframe.upper()
        index_by_time = {candle.time.isoformat(): index for index, candle in enumerate(candles)}
        rows = self._read_rows()
        updated = 0

        for row in rows:
            if row.get("symbol", "").upper() != symbol:
                continue
            if row.get("timeframe", "").upper() != timeframe:
                continue

            signal_index = index_by_time.get(row.get("signal_time", ""))
            if signal_index is None:
                continue

            entry_price = float(row["entry_price"])
            spread_cost = float(row.get("spread_cost") or 0.0)
            action = SignalAction(row["action"])

            for horizon in self.horizons:
                outcome_key = f"outcome_{horizon}"
                if row.get(outcome_key):
                    continue
                exit_index = signal_index + horizon
                if exit_index >= len(candles):
                    continue

                future = candles[signal_index + 1 : exit_index + 1]
                exit_candle = candles[exit_index]
                market_move = exit_candle.close - entry_price
                row[f"exit_time_{horizon}"] = exit_candle.time.isoformat()
                row[f"exit_price_{horizon}"] = self._number(exit_candle.close)
                row[f"market_move_{horizon}"] = self._number(market_move)

                if action is SignalAction.WAIT:
                    row[outcome_key] = "NO_TRADE"
                else:
                    direction = 1.0 if action is SignalAction.BUY else -1.0
                    gross_move = direction * market_move
                    net_move = gross_move - spread_cost
                    if action is SignalAction.BUY:
                        mfe = max(candle.high - entry_price for candle in future)
                        mae = max(entry_price - candle.low for candle in future)
                    else:
                        mfe = max(entry_price - candle.low for candle in future)
                        mae = max(candle.high - entry_price for candle in future)

                    row[f"net_move_{horizon}"] = self._number(net_move)
                    row[f"mfe_{horizon}"] = self._number(max(0.0, mfe))
                    row[f"mae_{horizon}"] = self._number(max(0.0, mae))
                    row[outcome_key] = self._outcome(net_move)
                updated += 1

        if updated:
            self._write_rows(rows)
        return updated

    def _read_rows(self) -> list[dict[str, str]]:
        if not self.path.is_file():
            return []
        with self.path.open("r", encoding="utf-8", newline="") as handle:
            return [dict(row) for row in csv.DictReader(handle)]

    def _write_rows(self, rows: Sequence[Mapping[str, str]]) -> None:
        temporary = self.path.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=self.fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        temporary.replace(self.path)

    @staticmethod
    def _signal_id(candle: Candle) -> str:
        return f"{candle.symbol}:{candle.timeframe}:{int(candle.time.timestamp())}"

    @staticmethod
    def _number(value: float) -> str:
        return f"{value:.10f}".rstrip("0").rstrip(".") or "0"

    @staticmethod
    def _outcome(net_move: float) -> str:
        tolerance = 1e-12
        if net_move > tolerance:
            return "WIN"
        if net_move < -tolerance:
            return "LOSS"
        return "FLAT"
