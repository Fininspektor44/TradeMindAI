"""TradeMind AI command-line entry point."""

from __future__ import annotations

import logging

from trademind import __version__
from trademind.config import Settings
from trademind.journal import SignalJournal
from trademind.logging_config import configure_logging
from trademind.market.csv_provider import CsvMarketDataProvider
from trademind.market.mock_provider import MockMarketDataProvider
from trademind.signals import SignalEngine

LOGGER = logging.getLogger("trademind")


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
    engine = SignalEngine()
    successful_symbols = 0
    failed_symbols = 0

    for symbol in settings.symbols:
        try:
            candles = provider.get_candles(symbol, settings.timeframe, count=60)
            result = engine.analyze(candles)
        except (FileNotFoundError, ValueError) as exc:
            failed_symbols += 1
            LOGGER.error("%s %s analysis failed: %s", symbol, settings.timeframe, exc)
            continue

        successful_symbols += 1
        LOGGER.info(
            "%s %s action=%s score=%d confidence=%d EMA9=%.5f EMA21=%.5f "
            "RSI=%.2f ATR=%.5f spread=%d volume=%d",
            result.symbol,
            result.timeframe,
            result.action,
            result.score,
            result.confidence,
            result.ema_fast,
            result.ema_slow,
            result.rsi,
            result.atr,
            candles[-1].spread,
            candles[-1].tick_volume,
        )

        try:
            recorded = journal.record(result, candles[-1], history=candles)
            evaluated = journal.evaluate(result.symbol, result.timeframe, candles)
            LOGGER.info(
                "%s %s journal recorded=%s evaluations_updated=%d",
                result.symbol,
                result.timeframe,
                recorded,
                evaluated,
            )
        except (OSError, ValueError) as exc:
            LOGGER.error("%s %s journal update failed: %s", symbol, settings.timeframe, exc)

    if successful_symbols == 0:
        LOGGER.error("Signal engine failed for all configured symbols")
        return 1
    if failed_symbols:
        LOGGER.warning(
            "Signal engine completed with %d healthy and %d failed symbols",
            successful_symbols,
            failed_symbols,
        )
    else:
        LOGGER.info("Signal engine completed successfully")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
