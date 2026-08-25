# TradeMind SER8 Project Progress

Concise, running log of the SER8 historical-data/research pipeline's
authoritative state. Not a replacement for the detailed design docs in
this directory -- links only, no raw logs duplicated here.

## SMC/OTE signal architecture: ACTIVE

The Claude repository audit confirmed the root cause: the retired
`SignalEngine` supplied both primary FX direction and secondary candidate
scoring inputs through EMA/RSI-derived action, confidence, and source-score
fields. That contaminated both historical replay and the live FX candidate
path.

The legacy container and its models are retired and deleted. The sole
authoritative FX directional source is now
`trademind.ote_engine.build_ote_signals`. Its confirmed-pivot close-break,
minimum-impulse, OTE geometry, confirmation, structure/FVG, and liquidity
semantics are unchanged. A bar population with no valid OTE signal emits no
observation and no candidate. Market structure and ATR cannot manufacture a
direction.

Verified architecture chains:

- Historical M5 bars -> `build_ote_signals` -> SMC/OTE BUY/SELL -> candidate
  -> conservative replay.
- Live closed FX M5 bars -> `build_ote_signals` -> candidate -> existing
  evidence, passport, risk, and execution-authorization chain.

ATR remains deterministic volatility/risk normalization with zero independent
directional authority. Its implementation lives in the neutral
`trademind.volatility` utility.

The previous 28-symbol screening and 28x4 execution-geometry result layers are
**SUPERSEDED / INVALID FOR THE TARGET SIGNAL ARCHITECTURE** because their
candidate populations came from the removed EMA/RSI architecture. Their
candidates, outcomes, rankings, and conclusions must not be reused. Replay
manifests now carry `SMC_OTE_BUILD_OTE_SIGNALS_V1`; verification rejects old
manifests as superseded. Geometry evaluation fails closed when a corrected
candidate/outcome population is absent.

Raw broker historical datasets remain preserved and the acquisition layer
remains closed/frozen. No MT5 reacquisition is authorized. The protected
holdout was not accessed. The accepted EURUSD hypothesis
`rpi-v1:sha256:205b5260711f7578a59cef2feea59550b777b3df0956ffd192076b37c4e5866d:0`
remains unchanged, unconsumed, and outside this cleanup.

Historical documentation exception: this section is the sole current
documentation location that names EMA/RSI, only to record removal and
supersession of the legacy architecture.

Cleanup verification on macOS: focused architecture tests 29 passed; related
runtime/replay/UI tests 144 passed; full project gate 2481 passed, 0 failed.

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

## Multi-symbol historical screening: SUPERSEDED / INVALID

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

### Real Windows screening result (post-270904fc)

28/28 screened, 0 positive expectancy, 28 negative expectancy. Typical
profile: win rate ~53-58%, average winner ~+0.46R to +0.52R, average loser
~-0.78R to -0.84R, payoff ~0.55-0.63, PF < 1 on every symbol -- consistent
with an asymmetric-fill pattern (winners often exit on the initial MARKET
allocation alone; losers more often have one or both LIMIT add-ons filled
before hitting stop).

## Execution geometry A/B experiment: SUPERSEDED / INVALID

Additive, read-only counterfactual layer testing whether the existing
MARKET+LIMIT+LIMIT basket geometry is the structural cause of the negative
expectancy above. Re-evaluates the SAME already-published replay candidates
against the SAME already-published bars under four variants
(`CONTROL_BASKET`, `MARKET_ONLY_SAME_TARGET`, `MARKET_ONLY_1_5R`,
`MARKET_ONLY_2_0R`) using the existing, unmodified
`trademind.signal_shadow.evaluate_shadow_candidate` and the existing
`compute_symbol_replay_metrics` aggregation -- no signals regenerated, no
historical reacquisition, no evaluator semantics changed.
`CONTROL_BASKET` must exactly reproduce the already-published replay
outcomes before any variant is interpreted for a symbol; if it cannot, that
symbol fails closed and is reported, never dropped.

- `661ee12` -- Add SER8 Execution Geometry Experiment: new module
  `ser8_execution_geometry_experiment.py` + CLI
  `run_ser8_execution_geometry_experiment.py`. Pushed. Full project gate:
  2457 passed, 0 failed. Implementation/tests only (Mac has no real
  historical datasets); real 28-symbol run pending on Windows.

### Real Windows run: failed at final report serialization, not a strategy verdict

The real 28-symbol x 4-variant computation was launched on Windows against
the existing 28 `HISTORICAL_DATA_READY` FX historical datasets. The worker
process was observed active and consuming CPU/RAM for the duration of the
run -- i.e. every symbol's four-variant evaluation genuinely executed. The
run then terminated at the FINAL report-serialization step (after every
symbol had already been fully evaluated), with:

```
{"error": "payload exceeds maximum canonical JSON bytes 262144",
 "error_code": "EXPERIMENT_FAILED",
 "status": "FAILED"}
```

No `experiment_report.json` was written. **No experiment result -- positive
or negative -- may be interpreted from that failed run**; nothing about the
execution-geometry variants' effect on expectancy is known from it, because
the report that would have carried those numbers never reached disk.

Root cause (confirmed from code, see
`trademind.ser8_execution_geometry_checkpoint` and
`trademind.ser8_execution_geometry_experiment`): the final report hash was
computed via `canonical_json_bytes(payload)` with no explicit `budget=`,
which defaulted to `DEFAULT_JSON_SAFETY_BUDGET`'s module-wide
`max_canonical_bytes = 262_144` -- a ceiling sized for much smaller
artifacts, not a 28-symbol x 4-variant experiment report. This is a
**report-artifact-capacity failure**, not evidence that execution-geometry
evaluation itself failed, and not a strategy verdict of any kind.

This commit ("Harden SER8 Geometry Experiment Reporting") fixes it: a new
named, finite, artifact-specific `EXECUTION_GEOMETRY_REPORT_JSON_BUDGET`
(never changes `DEFAULT_JSON_SAFETY_BUDGET`) is used consistently for report
serialization, hash creation, hash verification, writing, and loading; and
new per-symbol resume checkpoints
(`trademind.ser8_execution_geometry_checkpoint`) are atomically written
after one symbol's four variants are fully evaluated AND its CONTROL
reproduction gate is resolved, reused only on an exact identity match
(schema/version, symbol, dataset/replay/candidate/outcome evidence
identity, shadow parameters, `stability_window_count`, and variant
definitions) and otherwise recomputed -- never a silent partial trust.
Checkpointing does not change the final report content or hash: a fresh
run and a resumed run over identical evidence produce the identical
`experiment_report_sha256`. No trading logic, signal generation,
candidate population, shadow evaluator semantics, CONTROL reproduction
semantics, cost model, or ranking/metrics were touched --
`build_symbol_geometry_experiment` itself is unmodified. Full project
gate: 2479 passed, 0 failed.

## NEXT ACTION

Run a corrected 28-symbol historical replay from the preserved raw broker M5
datasets through `ote_engine.build_ote_signals`, then build a new screening
candidate/outcome population. Do not reacquire data, call MT5, access the
protected holdout, consume the accepted EURUSD hypothesis, or reuse any
candidate/outcome artifact from the superseded result layers. A new execution-
geometry experiment may be considered only after that corrected replay exists.
