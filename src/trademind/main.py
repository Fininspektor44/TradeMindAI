"""TradeMind AI command-line entry point."""

from __future__ import annotations

import logging

from trademind import __version__
from trademind.config import Settings
from trademind.logging_config import configure_logging
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

    if settings.provider != "mock":
        LOGGER.error("Provider '%s' is not implemented yet", settings.provider)
        return 2

    provider = MockMarketDataProvider()
    if not provider.healthcheck():
        LOGGER.error("Market-data provider healthcheck failed")
        return 1

    engine = SignalEngine()
    for symbol in settings.symbols:
        candles = provider.get_candles(symbol, settings.timeframe, count=60)
        result = engine.analyze(candles)
        LOGGER.info(
            "%s %s action=%s score=%d confidence=%d EMA9=%.5f EMA21=%.5f RSI=%.2f ATR=%.5f",
            result.symbol,
            result.timeframe,
            result.action,
            result.score,
            result.confidence,
            result.ema_fast,
            result.ema_slow,
            result.rsi,
            result.atr,
        )

    LOGGER.info("Signal engine smoke test completed successfully")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
