# SER8 Full Symbol Universe + Research Ranking V1

## The central rule: FULL UNIVERSE != FULL EXECUTION

Discovering, classifying, and ranking a symbol never grants it execution
authority. A symbol can appear in the universe inventory — even as
`RESEARCH_READY` — without being executable in any sense. Broker
execution for a symbol still requires the full, unmodified chain this
repository already closed:

```
exact symbol → exact matching ACCEPTED hypothesis → exact accepted
tradeable scope → fresh candidate → Risk Manager ALLOW → authorization →
claim → DEMO account gate
```

No accepted hypothesis for a symbol means zero broker sends for that
symbol, regardless of how "ready" its research classification looks.
Nothing in this layer advances, bypasses, or shortcuts the authoritative
research lifecycle (Proposal → Experiment Specification → Manifest →
Dataset Provenance → Train/Test → Validation → Protected Holdout → Final
Verdict); it only *observes* a hypothesis's already-persisted state.

## What this layer adds

Three new, purely additive pieces sit alongside the already-closed SER8
chain — nothing in `risk_manager.py`, `hypothesis_registry.py`,
`ser8_execution_authorization.py`, `ser8_mt5_demo_order_send.py`, or the
EURUSD-proven single-hypothesis worker path was weakened or modified:

1. **`src/trademind/ser8_symbol_universe.py`** — symbol discovery,
   classification, ranking, and an additive `ser8_symbol_universe`
   SQLite table.
2. **`scripts/discover_ser8_symbol_universe.py`** — a read-only CLI that
   runs discovery against real broker/runtime metadata and prints the
   inventory + research queue.
3. **A generalized, symbol-agnostic execution router** inside
   `scripts/run_ser8_autonomous_demo_execution.py` — an explicitly
   configured *set* of ACCEPTED hypotheses, still one worker process per
   account.

## 1. Symbol universe discovery

`discover_symbol_universe()` builds the universe from real broker/runtime
metadata — never a handwritten symbol list:

- **Broker symbols**: read from the unified executor's own
  `mt5_risk_symbols_utc_<account>.csv` export — the SAME file
  `trademind.mt5_risk_adapter` already consumes for risk evaluation. The
  required-field list (`SYMBOL_REQUIRED_FIELDS`) and structural validity
  criteria (`trade_mode` not `DISABLED`/`CLOSEONLY`; positive
  `tick_size`/`volume_min`/`volume_max`/`volume_step`; at least one
  positive `tick_value*`) are the exact same ones `mt5_risk_adapter`
  already applies — mirrored, never re-invented, never imported (private
  cross-module helpers stay off-limits).
- **Live-runtime support**: derived from scanning real candidate
  journal(s) via the SAME `candidate_from_dict` parser the rest of the
  SER8 chain uses. A symbol needs at least
  `--minimum-live-signal-sample` (default 1) journaled candidates to
  count as live-runtime supported.
- **Historical data sufficiency**: supplied by the caller from a real
  dataset inventory (`historical_rows_by_symbol` / `--historical-data-csv`
  on the CLI). This module never fabricates a row count for a symbol it
  was not told about — such a symbol is conservatively classified
  `DATA_INSUFFICIENT`, never assumed ready.
- **Correlation coverage**: `config/mt5/correlation_groups_v1.json` is
  already symbol-agnostic by construction — an unlisted symbol falls
  back to its own isolated `SYMBOL:<symbol>` group (mirrors
  `mt5_risk_adapter._correlation_group`'s own fallback exactly), so
  `correlation_model_supported` is structurally `True` for every symbol.
  No code change to the risk adapter was needed for this.

### Asset-class boundary

`classify_asset_class()` is a narrow, documented, naming-convention
heuristic (6-letter currency-pair shapes → `FX`; known metal root
tickers → `METALS`; everything else → `UNKNOWN`) — used *only* for
research-priority routing and the asset-class safety gate below, never
consulted by risk/authorization/execution.

This repository's current risk/execution model is proven safe **only for
FX** (`_RISK_MODEL_PROVEN_ASSET_CLASSES = {FX}`). A structurally valid
metals/indices/crypto/commodities symbol is still classified
`RISK_MODEL_UNSUPPORTED` with an explicit `rejection_reason` — discovered
and reported, never silently enabled. Extending that set to a new asset
class is a separate, human-reviewed decision (new instrument-metadata
handling, new correlation semantics, new margin assumptions), never
inferred from mere symbol discovery.

### Status vocabulary

| Status | Meaning |
|---|---|
| `RISK_MODEL_UNSUPPORTED` | Invalid instrument metadata, or an asset class this repo isn't proven safe for yet. |
| `DATA_INSUFFICIENT` | Risk-model valid, but no real historical row count supplied. |
| `RESEARCH_READY` | Risk-model valid, proven asset class, sufficient historical data — genuinely ready to enter the research lifecycle. |
| `RESEARCHING` | A mapped hypothesis exists and is past `PROPOSED` but not yet at a terminal state. |
| `REJECTED` | A mapped hypothesis reached `VALIDATION_REJECTED` or `REJECTED_FINAL` (both real, terminal rejection states) and none is `ACCEPTED`. |
| `ACCEPTED` | A mapped hypothesis reached `ACCEPTED`. |

Execution status stays `NOT_EXECUTABLE` even for an `ACCEPTED` symbol
unless it is also present in the caller's own `demo_active_symbols` (the
autonomous worker's actually-configured `--hypothesis-id`/
`--hypothesis-ids`) — never inferred.

### Ranking (three separate, never-mixed concepts)

- **A. Research readiness** (`rank_research_readiness`) — deterministic,
  uses only pre-holdout/operational properties (risk-model validity,
  data availability, historical/live sample sizes). No fabricated
  profitability score.
- **B. Research verdict** — `ACCEPTED` / `REJECTED`, observed from the
  real registry, never computed here.
- **C. Forward demo performance** (`aggregate_forward_demo_performance`)
  — aggregates real, already-captured `ser8_demo_trade_outcomes` rows
  (see `ser8_demo_trade_outcome_capture.py`, Task R). A hard minimum
  sample-size safeguard (`sufficient_sample`) means every derived metric
  (`total_realized_pl`, `win_rate`, `average_realized_pl`) stays `None`
  below the threshold — a symbol is never declared "best" from a tiny
  sample.

### `SER8SymbolUniverseControl`

A new, additive SQLite table (`ser8_symbol_universe`, one row per
symbol, upserted on each discovery run) in the SAME database file as
`HypothesisRegistry`. Never modifies the registry's own schema or state.

## 2. `discover_ser8_symbol_universe.py`

The real-Windows-runnable CLI. Read-only: zero broker interaction, zero
research-lifecycle mutation (no proposal, no manifest freeze, no
train/test, no validation, no holdout, no final verdict). It only writes
to the additive `ser8_symbol_universe` table, and only with `--persist`.

```
python scripts/discover_ser8_symbol_universe.py \
  --mt5-export-dir .\data\mt5 --account 77053345 \
  --historical-data-csv .\data\research_inventory.csv \
  --db .\data\ser8_registry.db --hypothesis-map .\data\symbol_hypotheses.json \
  --demo-active EURUSD --persist
```

Prints the exact inventory fields this task's own spec requires
(`TOTAL BROKER SYMBOLS`, `LIVE-RUNTIME SUPPORTED`, `RESEARCH-DATA
AVAILABLE`, `RESEARCH-READY`, `CURRENTLY ACCEPTED`, `CURRENTLY
EXECUTABLE`) followed by the ranked research queue. `--json` emits the
same data machine-readably.

`--hypothesis-map` is a plain JSON file `{"SYMBOL": ["hypothesis_id",
...]}` the operator maintains — this script never guesses which
hypothesis belongs to which symbol.

## 3. The generalized execution router

`scripts/run_ser8_autonomous_demo_execution.py` gained a second,
optional entrypoint alongside the original, unchanged, EURUSD-proven
single-hypothesis path.

### The refactor that made this safe

The original `run_one_cycle(args, *, now=None)` body was split into:

- **`_prepare_cycle_inputs(args)`** — the account-level state (MT5
  export discovery, deals CSV path, the research pipeline object graph)
  built exactly once per cycle.
- **`_run_cycle_for_hypothesis(args, hypothesis_id, *, pipeline, inputs,
  deals_csv, now)`** — the exact, byte-identical original per-hypothesis
  body (eligibility → scope → candidate → risk → dry-run/authorize →
  claim → send → outcome capture), with `args.hypothesis_id` replaced by
  an explicit parameter.

`run_one_cycle` is now a thin wrapper: `_prepare_cycle_inputs` +
`_run_cycle_for_hypothesis(args, args.hypothesis_id, ...)` — proven
behavior-preserving by the full existing test suite passing unmodified
against this refactor.

### `run_one_cycle_for_hypotheses(args, hypothesis_ids, *, now=None)`

One cycle spanning an explicitly configured **set** of hypotheses, still
one worker process, one lock file, one account:

- **0 matching hypotheses** for a candidate — that hypothesis's own
  `_run_cycle_for_hypothesis` call reports its own
  `NO_ELIGIBLE_CANDIDATE`; zero sends.
- **Exactly 1 matching hypothesis** — routed through the same,
  unmodified Risk → authorize → claim → execute chain, independently of
  every other configured hypothesis.
- **More than 1 hypothesis with a genuinely overlapping tradeable
  scope** — every one of them is reported
  `FAIL_CLOSED_AMBIGUOUS_SCOPE`, zero sends, before candidate selection
  is ever reached for any of them.

A not-yet-`ACCEPTED` or scope-unresolvable configured hypothesis is
reported `FAIL_CLOSED_NOT_ACCEPTED` / `FAIL_CLOSED_SCOPE_UNAVAILABLE` on
its own line and never blocks any other configured hypothesis in the
same cycle.

### True ambiguity detection

`_group_ambiguous_hypotheses` groups by exact `(symbol, timeframe,
setup_family)` **and** an overlapping `allowed_action_scope`
(`_action_scopes_overlap`, mirroring
`verify_live_candidate_matches_scope`'s own action-consistency rule: any
`BOTH` overlaps anything; otherwise only an exact match overlaps). Two
hypotheses on the identical symbol/timeframe/setup_family with disjoint
single action scopes — one `BUY`-only, one `SELL`-only — are correctly
**not** flagged: a real candidate only ever carries one action, so it can
only ever match one of them.

### Account-global risk, preserved for free

Every non-ambiguous hypothesis still calls the same, unmodified
`evaluate_ser8_research_risk_gate`, which always reads the account's
real, current, shared MT5 positions/account snapshot — reflecting every
open position regardless of which hypothesis opened it. Hypotheses are
processed sequentially within one cycle, in the order given, so a
position opened for one hypothesis earlier in the cycle is already
visible to every other hypothesis's risk evaluation later in that same
cycle. No change to `risk_manager.py` was needed.

### CLI

`--hypothesis-id` (single-hypothesis, unchanged) and `--hypothesis-ids`
(new, multi-hypothesis) are mutually exclusive; `main()` fails closed
with exit code 2 if neither or both are supplied. The PowerShell wrapper
(`run_ser8_autonomous_demo_execution.ps1`) and installer
(`install_ser8_autonomous_demo_execution.ps1`) gained a matching, empty-
by-default `-HypothesisIds` array parameter — the proven single-symbol
deployment is completely unaffected unless an operator explicitly
supplies it. Still one Scheduled Task, one EA, one worker binary —
never one per symbol.

## Explicitly out of scope for this layer

- No Analytics Core (deferred, per the task spec).
- No automatic promotion of any symbol to `ACCEPTED` — that only ever
  happens through the real research lifecycle.
- No per-symbol risk budget — Risk Manager remains account-global by
  design.
- No silent enablement of a non-FX asset class.
