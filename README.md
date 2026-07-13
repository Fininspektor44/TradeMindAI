# TradeMind AI

TradeMind AI is an explainable market-screening and trader-analytics platform.

## Current milestone

`v0.4.0` adds a persistent signal journal and automatic forward evaluation on real
closed MetaTrader 5 candles.

For every XAUUSD, EURUSD and GBPUSD M5 candle the runtime now stores:

- BUY / SELL / WAIT, score and confidence;
- entry price, spread, EMA9, EMA21, RSI, ATR and decision reasons;
- outcomes after 3, 6 and 12 closed candles;
- net directional move after configured spread cost;
- maximum favourable excursion (MFE) and maximum adverse excursion (MAE).

A broken or stale symbol no longer blocks analysis of the remaining healthy symbols.

## Development model

- macOS: code, Git, tests and documentation
- Windows SER8: MetaTrader 5 gateway and 24/7 runtime
- The exporter is read-only and must be attached to a separate chart
- No passwords, broker credentials or Telegram tokens are stored in Git

## Implemented modules

1. Configuration and logging
2. Mock market-data provider
3. EMA, RSI and ATR signal engine
4. BUY / SELL / WAIT scoring
5. MT5 CSV candle provider
6. Read-only MQL5 candle exporter
7. Hidden Windows live watcher and automatic startup
8. Persistent signal journal
9. Forward outcome evaluation and performance statistics
10. Automated tests and GitHub Actions checks

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
TRADEMIND_JOURNAL_DIR=data/journal \
TRADEMIND_EVAL_HORIZONS=3,6,12 \
TRADEMIND_POINT_SIZES=XAUUSD=0.01,EURUSD=0.00001,GBPUSD=0.00001 \
trademind
```

Show accumulated performance:

```bash
trademind-stats
trademind-stats --horizon 6
```

The journal is written to `data/journal/signals.csv`. Live execution remains read-only;
no orders are sent to MetaTrader 5 in v0.4.0.
