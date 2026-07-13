# TradeMind AI

TradeMind AI is an explainable market-screening and trader-analytics platform.

## Current milestone

`v0.3.0` adds a read-only MetaTrader 5 CSV bridge so the signal engine can analyze real closed candles instead of mock prices.

## Development model

- macOS: code, Git, tests, documentation
- Windows SER8: MetaTrader 5 gateway and later 24/7 runtime
- The exporter is read-only and must be attached to a separate chart
- No passwords, broker credentials, or Telegram tokens are stored in Git

## Implemented modules

1. Configuration and logging
2. Mock market-data provider
3. EMA, RSI and ATR signal engine
4. BUY / SELL / WAIT scoring
5. MT5 CSV candle provider
6. Read-only MQL5 candle exporter
7. Automated tests and GitHub Actions checks

## Quick start

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install ".[dev]"
pytest && ruff check .
trademind
```

## Real MT5 candles

See [`docs/MT5_BRIDGE.md`](docs/MT5_BRIDGE.md).

Example CSV-mode launch:

```bash
TRADEMIND_PROVIDER=csv \
TRADEMIND_DATA_DIR=data/mt5 \
TRADEMIND_SYMBOLS=XAUUSD,EURUSD,GBPUSD \
TRADEMIND_TIMEFRAME=M5 \
trademind
```

The first live-data test uses manually copied CSV files. After validation, the bridge will be automated or TradeMind AI will run directly on the Windows SER8.
