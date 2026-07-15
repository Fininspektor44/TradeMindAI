# TradeMind AI

TradeMind AI is an explainable market-screening and trader-analytics platform.

## Current milestone

`v0.7.0` adds an observation-only market-structure module to the existing ECN research
pipeline. It records transparent SMC-style features without changing BUY/SELL/WAIT decisions.

For every newly closed configured M5 candle the runtime stores:

- signal action, score, confidence and indicator context;
- spread cost and tick-volume features;
- forward progress and outcomes after 3, 6 and 12 candles;
- internal structure using a 4-bar reference window;
- swing structure using a 30-bar reference window;
- BOS and CHoCH observations;
- buy-side and sell-side liquidity sweeps with depth in price and ATR;
- bullish or bearish three-candle FVG with size in price and ATR.

All market-structure fields are experimental observations. They have zero score weight and do
not alter trade direction. Their value will be judged only from forward results after spread.

The data schema is documented in [`docs/DATA_SCHEMA_V1.md`](docs/DATA_SCHEMA_V1.md).
The structure definitions are documented in
[`docs/SMC_OBSERVATION_SPEC.md`](docs/SMC_OBSERVATION_SPEC.md).
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
12. Observation-only market structure: BOS, CHoCH, sweeps and FVG
13. Automated tests and GitHub Actions checks

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

The live system remains read-only in v0.7.0. It records observations and evaluates outcomes;
no orders are sent to MetaTrader 5.
