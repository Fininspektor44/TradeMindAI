# TradeMind AI

TradeMind AI is an explainable market-screening and trader-analytics platform.

## Current milestone

`v0.6.0` expands the persistent journal into a market-observation dataset. The current signal
logic remains unchanged while every new closed-candle observation records richer evidence for
future setup-quality research.

For every configured MT5 symbol and M5 candle the runtime stores:

- BUY / SELL / WAIT, score, confidence and decision reasons;
- entry price and spread in points, price units, ATR units and percentage terms;
- signal-candle tick volume, previous-20 mean, relative volume and one-bar volume change;
- outcomes after 3, 6 and 12 closed candles;
- directional and net movement after configured spread cost;
- movement, MFE and MAE normalized by entry ATR;
- bars needed to reach maximum favorable and adverse excursion.

The data schema is documented in [`docs/DATA_SCHEMA_V1.md`](docs/DATA_SCHEMA_V1.md).
Existing journal rows are preserved. Fields that were not collected historically remain blank.

A broken or stale symbol does not block analysis of the remaining healthy symbols.

## Development model

- macOS: code, Git, tests and documentation
- Windows SER8: MetaTrader 5 gateway and 24/7 runtime
- The exporters are read-only and must be attached to separate charts
- No passwords, broker credentials or exchange API secrets are stored in Git

## Implemented modules

1. Configuration and logging
2. Mock market-data provider
3. EMA, RSI and ATR signal engine
4. BUY / SELL / WAIT scoring
5. MT5 CSV candle provider
6. Read-only MQL5 candle exporters
7. Hidden Windows live watchers and automatic startup
8. Persistent market-observation journal
9. Spread and relative tick-volume features
10. Forward progress and outcome evaluation
11. Non-overlapping performance statistics
12. Automated tests and GitHub Actions checks

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
TRADEMIND_SYMBOLS=XAUUSD \
TRADEMIND_TIMEFRAME=M5 \
TRADEMIND_JOURNAL_DIR=data/journal \
TRADEMIND_EVAL_HORIZONS=3,6,12 \
TRADEMIND_POINT_SIZES=XAUUSD=0.01 \
trademind
```

Show accumulated performance:

```bash
trademind-stats
trademind-stats --symbol XAUUSD --horizon 12 --non-overlap \
  --group-confidence --group-action
```

The live system remains read-only in v0.6.0. It records observations and evaluates outcomes;
no orders are sent to MetaTrader 5.
