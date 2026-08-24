# TradeMind SER8 Project Progress

Concise, running log of the SER8 historical-data/research pipeline's
authoritative state. Not a replacement for the detailed design docs in
this directory -- links only, no raw logs duplicated here.

## Historical acquisition layer: CLOSED

Full C3 Windows verification passed on the authoritative source commit.

- Total broker symbols: 90
- Accepted datasets: 83
- `HISTORICAL_DATA_READY` (research-ready FX): 28
- `RISK_MODEL_UNSUPPORTED`: 55
- `BROKER_SYMBOL_DISABLED`: 2, `BROKER_SYMBOL_UNAVAILABLE`: 5
- Coverage: `COMPLETE` 50, `TRUNCATED_GENUINE_BOUNDARY` 33
- Authoritative inventory: `data/ser8_historical_market_data/historical_inventory.json`

The 28 `HISTORICAL_DATA_READY` FX symbols: AUDCAD, AUDCHF, AUDJPY, AUDNZD,
AUDUSD, CADCHF, CADJPY, CHFJPY, EURAUD, EURCAD, EURCHF, EURGBP, EURJPY,
EURNZD, EURUSD, GBPAUD, GBPCAD, GBPCHF, GBPJPY, GBPNZD, GBPUSD, NZDCAD,
NZDCHF, NZDJPY, NZDUSD, USDCAD, USDCHF, USDJPY.

Historical acquisition code/semantics are frozen; no further layer may
modify chunk acquisition, coverage discovery, or dataset identity. See
`docs/SER8_MULTISYMBOL_HISTORICAL_RESEARCH_DATA_V1.md`.

- `fb1295e` -- Classify SER8 MT5 Pre-History Sentinel: source-evidence-gated
  (MT5-only, verified `last_error()` Success) pre-history sentinel
  classification, closing the acquisition layer. Pushed.

## Multi-symbol historical screening (SCREENING ONLY)

Additive aggregation/ranking layer over the existing, unmodified replay
engine (`create_replay` / `build_research_readiness_inventory`). Never a
hypothesis-acceptance verdict -- see
`docs/SER8_MULTISYMBOL_HISTORICAL_RESEARCH_DATA_V1.md` section G.

- `79f6c03` -- Add SER8 Multi-Symbol Historical Screening: new module
  `ser8_historical_multisymbol_screening.py` + CLI
  `run_ser8_historical_multisymbol_screening.py`. Pushed.

### Real Windows screening result (post-79f6c03, pre-direction-fix)

All 28 `HISTORICAL_DATA_READY` symbols screened: 0 positive expectancy,
28 negative expectancy. `BUY`/`SELL` counts were reported as 0/0 for every
symbol -- a reporting defect (see below), not evidence about the trades
themselves; the win/loss/expectancy/profit-factor/drawdown numbers behind
that run were not affected by it.

### Direction-count reporting defect found and fixed

Root cause: real candidates carry no top-level `candidate["action"]`; the
screening code read `row.get("action")`, which is always absent, so every
trade silently resolved to an empty direction and BUY/SELL counts were
always 0/0. The authoritative field is `plan.action`
(`trademind.signal_intelligence.TradePlan.action`), the only
structurally-validated direction field, confirmed via real NZDCAD candidate
evidence.

- `270904fc` -- Fix SER8 Screening Direction Counts: candidate direction
  resolved from `plan.action`, defensive consistency checks, fail-closed
  behavior; no PnL/ranking/acquisition changes. Pushed. Full project gate:
  2439 passed, 0 failed.

## NEXT ACTION

Windows pull the final pushed state and rerun the 28-symbol screening from
existing replay artifacts only; no historical reacquisition / MT5 calls.
