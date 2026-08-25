"""TradeMind AI command-line entry point."""

from __future__ import annotations

import logging
from collections.abc import Sequence

from trademind import __version__
from trademind.config import Settings
from trademind.fx_research import build_fx_observations
from trademind.fx_signal_adapter import build_candidates
from trademind.journal import SignalJournal
from trademind.logging_config import configure_logging
from trademind.market.models import Candle
from trademind.market.csv_provider import CsvMarketDataProvider
from trademind.market.mock_provider import MockMarketDataProvider

LOGGER = logging.getLogger("trademind")


def _ote_rows(candles: Sequence[Candle], point: float) -> list[dict[str, str]]:
    if point <= 0:
        raise ValueError("positive point size is required for OTE signal construction")
    return [
        {
            "time": str(int(candle.time.timestamp())),
            "symbol": candle.symbol,
            "timeframe": candle.timeframe,
            "bar_seconds": "300",
            "point": str(point),
            "open": str(candle.open),
            "high": str(candle.high),
            "low": str(candle.low),
            "close": str(candle.close),
            "bar_tick_volume": str(candle.tick_volume),
            "tick_count": str(max(1, candle.tick_volume)),
            "tick_rate_per_sec": str(max(1, candle.tick_volume) / 300.0),
            "spread_mean_points": str(candle.spread),
            "rvol_20": "0",
            "direction_imbalance": "0",
            "tick_copy_status": "CANDLE_PROVIDER",
        }
        for candle in candles
    ]


def main() -> int:
    settings = Settings.from_env()
    configure_logging(settings.log_level)

    LOGGER.info("TradeMind AI v%s starting", __version__)
    LOGGER.info(
        "environment=%s provider=%s timeframe=%s symbols=%s",
        settings.environment,
        settings.provider,
        settings.timeframe,
        ",".join(settings.symbols),
    )

    if settings.provider == "mock":
        provider = MockMarketDataProvider()
    elif settings.provider == "csv":
        provider = CsvMarketDataProvider(
            settings.market_data_dir,
            max_age_seconds=settings.max_data_age_seconds,
        )
        LOGGER.info("MT5 CSV directory=%s", provider.data_dir)
    else:
        LOGGER.error("Provider '%s' is not implemented", settings.provider)
        return 2

    if not provider.healthcheck():
        LOGGER.error("Market-data provider healthcheck failed")
        return 1

    try:
        journal = SignalJournal(
            settings.journal_dir,
            horizons=settings.evaluation_horizons,
            point_sizes=settings.point_sizes,
        )
    except (OSError, ValueError) as exc:
        LOGGER.error("Signal journal initialization failed: %s", exc)
        return 1

    LOGGER.info("Signal journal=%s", journal.path)
    successful_symbols = 0
    failed_symbols = 0

    for symbol in settings.symbols:
        try:
            candles = provider.get_candles(symbol, settings.timeframe, count=60)
            if settings.timeframe != "M5":
                raise ValueError("authoritative FX OTE source requires M5 candles")
            point = (settings.point_sizes or {}).get(symbol.upper(), 0.0)
            observations = build_fx_observations(
                _ote_rows(candles, point),
                symbols=(symbol.upper(),),
                include_forward_outcomes=False,
            )
            candidates, rejected = build_candidates(observations)
        except (FileNotFoundError, ValueError) as exc:
            failed_symbols += 1
            LOGGER.error("%s %s analysis failed: %s", symbol, settings.timeframe, exc)
            continue

        successful_symbols += 1
        LOGGER.info(
            "%s %s authoritative_ote_signals=%d candidates=%d rejected=%d",
            symbol,
            settings.timeframe,
            len(observations),
            len(candidates),
            len(rejected),
        )
        source_candles = {int(candle.time.timestamp()): candle for candle in candles}
        recorded = 0
        try:
            for observation in observations:
                candle = source_candles.get(int(observation["source_bar_time"]))
                if candle is not None and journal.record(observation, candle, history=candles):
                    recorded += 1
            evaluated = journal.evaluate(symbol, settings.timeframe, candles)
            LOGGER.info("%s %s journal recorded=%d evaluations_updated=%d", symbol, settings.timeframe, recorded, evaluated)
        except (OSError, ValueError) as exc:
            LOGGER.error("%s %s journal update failed: %s", symbol, settings.timeframe, exc)

    if successful_symbols == 0:
        LOGGER.error("SMC/OTE analysis failed for all configured symbols")
        return 1
    if failed_symbols:
        LOGGER.warning(
            "SMC/OTE analysis completed with %d healthy and %d failed symbols",
            successful_symbols,
            failed_symbols,
        )
    else:
        LOGGER.info("SMC/OTE analysis completed successfully")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
