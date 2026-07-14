"""Persistent signal journal and forward outcome evaluation."""

from __future__ import annotations

import csv
from collections.abc import Mapping, Sequence
from pathlib import Path

from trademind.market.models import Candle
from trademind.signals.models import SignalAction, SignalResult

_SCHEMA_VERSION = "1.0"
_VOLUME_WINDOW = 20

_BASE_FIELDS = [
    "schema_version",
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
    "spread_cost_atr",
    "spread_price_pct",
    "tick_volume",
    "volume_mean_20",
    "volume_ratio_20",
    "volume_change_pct",
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
                    f"directional_move_{horizon}",
                    f"net_move_{horizon}",
                    f"net_return_pct_{horizon}",
                    f"progress_atr_{horizon}",
                    f"mfe_{horizon}",
                    f"mae_{horizon}",
                    f"mfe_atr_{horizon}",
                    f"mae_atr_{horizon}",
                    f"bars_to_mfe_{horizon}",
                    f"bars_to_mae_{horizon}",
                    f"outcome_{horizon}",
                ]
            )
        return fields

    def record(
        self,
        result: SignalResult,
        candle: Candle,
        history: Sequence[Candle] | None = None,
    ) -> bool:
        """Append a signal unless the same symbol/timeframe/candle already exists."""
        signal_id = self._signal_id(candle)
        rows = self._read_rows()
        if any(row.get("signal_id") == signal_id for row in rows):
            return False

        point_size = self.point_sizes.get(result.symbol.upper(), 0.0)
        spread_cost = candle.spread * point_size if point_size else 0.0
        spread_cost_atr = spread_cost / result.atr if result.atr > 0 else None
        spread_price_pct = spread_cost / candle.close * 100.0 if candle.close else None
        volume = self._volume_features(candle, history or ())

        row = {field: "" for field in self.fieldnames}
        row.update(
            {
                "schema_version": _SCHEMA_VERSION,
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
                "spread_cost_atr": self._optional_number(spread_cost_atr),
                "spread_price_pct": self._optional_number(spread_price_pct),
                "tick_volume": str(candle.tick_volume),
                "volume_mean_20": self._optional_number(volume["mean"]),
                "volume_ratio_20": self._optional_number(volume["ratio"]),
                "volume_change_pct": self._optional_number(volume["change_pct"]),
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
        """Fill pending forward progress and outcomes using newly closed candles."""
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
            entry_atr = float(row.get("atr") or 0.0)
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
                    directional_move = direction * market_move
                    net_move = directional_move - spread_cost
                    mfe, mae, bars_to_mfe, bars_to_mae = self._progress_extremes(
                        action,
                        entry_price,
                        future,
                    )

                    row[f"directional_move_{horizon}"] = self._number(directional_move)
                    row[f"net_move_{horizon}"] = self._number(net_move)
                    row[f"net_return_pct_{horizon}"] = self._optional_number(
                        net_move / entry_price * 100.0 if entry_price else None
                    )
                    row[f"progress_atr_{horizon}"] = self._optional_number(
                        net_move / entry_atr if entry_atr > 0 else None
                    )
                    row[f"mfe_{horizon}"] = self._number(mfe)
                    row[f"mae_{horizon}"] = self._number(mae)
                    row[f"mfe_atr_{horizon}"] = self._optional_number(
                        mfe / entry_atr if entry_atr > 0 else None
                    )
                    row[f"mae_atr_{horizon}"] = self._optional_number(
                        mae / entry_atr if entry_atr > 0 else None
                    )
                    row[f"bars_to_mfe_{horizon}"] = str(bars_to_mfe) if mfe > 0 else ""
                    row[f"bars_to_mae_{horizon}"] = str(bars_to_mae) if mae > 0 else ""
                    row[outcome_key] = self._outcome(net_move)
                updated += 1

        if updated:
            self._write_rows(rows)
        return updated

    @staticmethod
    def _volume_features(
        candle: Candle,
        history: Sequence[Candle],
    ) -> dict[str, float | None]:
        previous = [
            item
            for item in history
            if item.symbol == candle.symbol
            and item.timeframe == candle.timeframe
            and item.time < candle.time
        ][-_VOLUME_WINDOW:]
        if not previous:
            return {"mean": None, "ratio": None, "change_pct": None}

        mean_volume = sum(item.tick_volume for item in previous) / len(previous)
        ratio = candle.tick_volume / mean_volume if mean_volume > 0 else None
        previous_volume = previous[-1].tick_volume
        change_pct = (
            (candle.tick_volume - previous_volume) / previous_volume * 100.0
            if previous_volume > 0
            else None
        )
        return {"mean": mean_volume, "ratio": ratio, "change_pct": change_pct}

    @staticmethod
    def _progress_extremes(
        action: SignalAction,
        entry_price: float,
        future: Sequence[Candle],
    ) -> tuple[float, float, int, int]:
        if action is SignalAction.BUY:
            favorable = [candle.high - entry_price for candle in future]
            adverse = [entry_price - candle.low for candle in future]
        else:
            favorable = [entry_price - candle.low for candle in future]
            adverse = [candle.high - entry_price for candle in future]

        mfe_raw = max(favorable)
        mae_raw = max(adverse)
        bars_to_mfe = favorable.index(mfe_raw) + 1
        bars_to_mae = adverse.index(mae_raw) + 1
        return max(0.0, mfe_raw), max(0.0, mae_raw), bars_to_mfe, bars_to_mae

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

    @classmethod
    def _optional_number(cls, value: float | None) -> str:
        return "" if value is None else cls._number(value)

    @staticmethod
    def _outcome(net_move: float) -> str:
        tolerance = 1e-12
        if net_move > tolerance:
            return "WIN"
        if net_move < -tolerance:
            return "LOSS"
        return "FLAT"
