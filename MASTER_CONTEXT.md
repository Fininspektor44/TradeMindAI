# TradeMindAI — MASTER CONTEXT

Stable project truth. Compact by design. This file changes only when stable
architecture, safety rules, account roles, or protected decisions change.
For "what is true right now" read [`PROJECT_STATUS.md`](PROJECT_STATUS.md).
For history and rationale read [`PROJECT_MEMORY.md`](PROJECT_MEMORY.md).

## PROJECT IDENTITY

TradeMindAI is an explainable market-screening and trader-analytics platform
that has grown into a safety-gated pipeline running from FX research to
supervised DEMO order execution on MetaTrader 5.

It exists to answer one question with recorded evidence, not opinion: does a
specific, frozen trading hypothesis hold up out of sample, and can it be
executed under strict, auditable risk control without a human placing every
order by hand. Every layer is additive, fail-closed, and traceable.

## GOALS

- Generate FX directional signals from one auditable model (SMC/OTE), never a
  black box and never an ensemble of undocumented indicators.
- Screen and rank symbols from real broker/runtime metadata, never a hand list.
- Freeze hypotheses, validate them out of sample, and protect a final holdout
  from any premature or repeated access.
- Execute only researched, accepted setups on a DEMO account, sized solely by
  the Risk Manager, through a single order-sending component.
- Keep the repository itself the authoritative memory of the project so any new
  AI or human session can continue without prior chat history.

## HIGH-LEVEL ARCHITECTURE

```
market data (MT5 account 77053345, read-only)
   -> ote_engine.build_ote_signals            SMC/OTE BUY/SELL (sole FX direction)
   -> SignalCandidate (journaled: candidates.jsonl)
   -> manual approval gate (APPROVED_MANUAL)
   -> Risk Manager (sole sizing authority; account-global risk)
   -> ExecutionAuthorization -> one-shot claim
   -> DEMO account gate (account 67206924 only)
   -> SER8DemoOrderSendControl.send / .resume_plan
   -> FileBridgeDemoOrderTransport (CSV bridge)
   -> mt5/TradeMind_Demo_Order_Executor_v1 (deployed v1.7)   THE ONLY ORDER SENDER
   -> real MT5 DEMO order -> immutable receipt + outcome capture
```

Supporting layers: historical acquisition + inventory (frozen), multi-symbol
research readiness, checkpoint create/verify tooling, final-holdout isolation,
Orchestrator v1 control-plane spec.

Runtime is **one consolidated authoritative Windows scheduled task**,
`TradeMindAI-SER8-Autonomous-Demo-Execution` (interval `PT3M`, DryRun `FALSE`),
which owns the full loop in order: **producer -> execution -> reconciliation**.
One candidate yields at most one execution plan, ever; one active plan per
account+symbol; broker truth reconciles leg state; no order is ever re-sent.

The earlier separate tasks are **DISABLED** and must not be re-enabled:
`TradeMindAI-SER8-MT5-Reconciliation`, `TradeMindAI-v1.21-LiveSignalRuntime`,
`TradeMindAI-v1.32-ECN-LiveSignalRuntime`. The design doc
[`docs/SER8_AUTONOMOUS_DEMO_EXECUTION_V1.md`](docs/SER8_AUTONOMOUS_DEMO_EXECUTION_V1.md)
still describes the original three-task split — read it as history for the
responsibilities of each phase, not the current task layout.

## ACCOUNT ROLES

| Account | Role | Rules |
|---|---|---|
| `77053345` | Active ECN **market-data** account | Authenticated MT5 Python attaches here for candles / history. Research evidence only. Never an execution target. |
| `67206924` | **DEMO execution** account, magic number `990244` | The only account the executor EA may touch. Requires explicit `--account` + `--demo-account-allowlist`, cross-checked at startup. |
| any live account | — | Never connected to the executor. No override, force, or bypass exists. |

Non-canonical MT5 accounts were purged (checkpoint
`purge-noncanonical-mt5-accounts-v1`). Only the two accounts above are canonical.

## IMMUTABLE / PROTECTED DECISIONS

- **SMC/OTE is the only FX directional source.** `ote_engine.build_ote_signals`.
  The legacy `SignalEngine` and all EMA/RSI-derived action/confidence/score
  fields are deleted and must not be reintroduced.
- **ATR has zero directional authority.** It is deterministic volatility / risk
  normalization only (`trademind.volatility`).
- **Risk Manager is the sole sizing authority.** `config/risk_profiles/standard_v1.json`
  and the lot-sizing logic are never modified. `maximum_signal_age_seconds` is
  never weakened above 900. MQL5 never computes lot size.
- **CORE_8 execution universe.** Broker-capable DEMO execution is limited to
  eight researched FX symbols — CHFJPY, EURJPY, EURNZD, GBPAUD, GBPNZD, NZDCAD,
  NZDCHF, USDJPY — executed as a single MARKET entry, on **natural SMC/OTE
  signals only** (never a forced or manufactured signal). Non-CORE_8 symbols and
  the legacy MARKET+LIMIT+LIMIT basket fail closed before any risk evaluation.
  Source of truth: `src/trademind/ser8_core8_market_only_policy.py` (pure
  functions, no override/force/bypass).
- **One order sender.** Only the unified `TradeMind_Demo_Order_Executor_v1`
  EA (deployed implementation **v1.7**) ever sends an order. No script or other
  EA calls `OrderSend`/`CTrade`.
- **Accepted EURUSD hypothesis is a protected historical / research artifact.**
  `rpi-v1:sha256:205b5260711f7578a59cef2feea59550b777b3df0956ffd192076b37c4e5866d:0`
  — immutable and unconsumed; never mutate, widen, recreate, or consume it. It
  is **not** part of the CORE_8 execution universe and has no bearing on
  CORE_8 broker execution.
- **Protected final holdout is sealed and off-limits.** AES-256-GCM sealed and
  quarantined outside the research root. `READ_PROTECTED_FINAL_HOLDOUT` is a
  forbidden Orchestrator action. Dual one-shot boundary (registry
  `HOLDOUT_CONSUMED` + tamper-evident ledger `FINAL_HOLDOUT_CLAIM`).
- **Checkpoint tags are immutable.** Never force-update, replace, or delete a
  `checkpoint/*` tag.

## SAFETY RULES

- DEMO / PAPER only. Execution account `67206924` only; market-data account
  `77053345` is read-only and never executes. No live-money path exists or is
  authorized.
- The `SER8_SUPERVISED_DEMO_V1` risk profile still gates on the
  `APPROVED_MANUAL` signal state (`config/risk_profiles/ser8_supervised_demo_v1.json`).
  The consolidated scheduler runs unattended (`PT3M`, DryRun `FALSE`) and will
  act on the first natural candidate that clears every gate.
- Risk Manager, authorization, and one-shot claim are mandatory on every
  execution path. No bypass, override, or force flag exists in the loop.
- Fail closed everywhere: missing evidence, stale snapshot, identity mismatch,
  or an `UNKNOWN` order leg stops the chain; nothing is inferred or retried
  silently.
- No secrets in Git: no broker passwords, API keys, or account credentials.
- No MT5 historical reacquisition without explicit operator authorization.
- Safety-critical layers close only through the checkpoint system below.

## CLOSED LAYERS — DO NOT CASUALLY REOPEN

1. **Historical acquisition + dataset identity** — frozen. 90 broker symbols,
   83 accepted datasets, 28 `HISTORICAL_DATA_READY` FX. Inventory:
   `data/ser8_historical_market_data/historical_inventory.json`. No layer may
   change chunk acquisition, coverage discovery, or dataset identity.
2. **EMA/RSI + legacy `SignalEngine`** — deleted. Do not reintroduce.
3. **28-symbol multi-symbol screening and the 28x4 execution-geometry A/B
   experiment** — SUPERSEDED / INVALID: their candidate populations came from
   the removed EMA/RSI architecture. Do not reuse their candidates, outcomes,
   rankings, or conclusions. Replay manifests must carry
   `SMC_OTE_BUILD_OTE_SIGNALS_V1`.
4. **Risk Manager lot-sizing + `standard_v1.json`** — immutable.
5. **Accepted EURUSD hypothesis + protected final holdout** — immutable /
   unconsumed protected research artifacts; outside the CORE_8 execution
   universe.
6. **Canonical account identities** — market-data `77053345` (read-only),
   demo execution `67206924` only.
7. **Consolidated scheduler** — the earlier separate producer / live-runtime /
   reconciliation scheduled tasks are disabled; do not re-enable them.

Reopening any of these needs an explicit, recorded operator decision — not an
agent's judgement call.

## AGENT NETWORK AND RESPONSIBILITIES

- **Human operator (repository owner).** Sole authority for: connecting any MT5
  terminal, MetaEditor compilation / deployment of the executor EA, scheduler
  enable/disable, observing the first natural broker-capable DEMO execution,
  authorizing a checkpoint push, `git push`, and reopening any closed layer.
- **AI coding sessions (Claude / ChatGPT / Codex).** Implement bounded, additive
  layers with tests; keep [`PROJECT_STATUS.md`](PROJECT_STATUS.md),
  [`PROJECT_MEMORY.md`](PROJECT_MEMORY.md), and this file current. Must not
  invent project state, weaken a safety rule, touch a protected layer, or run
  the full pytest suite / push unless explicitly asked. See
  [`AGENTS.md`](AGENTS.md).
- **Orchestrator v1 roles** (spec: [`docs/ORCHESTRATOR_V1_SPEC.md`](docs/ORCHESTRATOR_V1_SPEC.md)) —
  ARCHITECT (design review, no self-approval), DEVELOPER (bounded diff + tests,
  no self-promotion), AUDITOR (adversarial review + regression tests), OPERATOR
  (runs deterministic tools and tasks). Separation of duties: the author of a
  change never approves it. Orchestrator v1 cannot place or execute orders.
- **Deterministic runtime components** (not AI): the consolidated
  producer/execution/reconciliation scheduler, Risk Manager, authorization /
  claim controls, DemoAccountSafetyGate, and the unified executor EA. These own
  execution. AI may propose, review, and explain — never decide a trade.

## PROJECT CHECKPOINT SYSTEM

A safety-critical engineering layer is operationally finalized only when all
three are true: `FINAL STATUS = PASS`, `LAYER STATUS = CLOSED`,
`CHECKPOINT STATUS = CREATED + VERIFIED`. An ordinary commit or a green test run
never creates a checkpoint. Create with `scripts/create_trademind_checkpoint.py`
**only when the operator explicitly authorizes the remote push**; otherwise
return the exact command for the operator. Verify / list with
`scripts/verify_trademind_checkpoint.py`. Full rules:
[`docs/TRADEMIND_PROJECT_CHECKPOINT_SYSTEM_V1.md`](docs/TRADEMIND_PROJECT_CHECKPOINT_SYSTEM_V1.md)
and [`.cursor/rules/project-checkpoint-close.mdc`](.cursor/rules/project-checkpoint-close.mdc).

## SPECIALIZED DOCUMENTS

| Topic | Document |
|---|---|
| Agent operating rules (read this if you are an agent) | [`AGENTS.md`](AGENTS.md) |
| Current snapshot + single NEXT_ACTION | [`PROJECT_STATUS.md`](PROJECT_STATUS.md) |
| Append-only decision / milestone log | [`PROJECT_MEMORY.md`](PROJECT_MEMORY.md) |
| SER8 research-layer running detail log | [`docs/TRADEMIND_PROJECT_PROGRESS.md`](docs/TRADEMIND_PROJECT_PROGRESS.md) |
| Checkpoint system | [`docs/TRADEMIND_PROJECT_CHECKPOINT_SYSTEM_V1.md`](docs/TRADEMIND_PROJECT_CHECKPOINT_SYSTEM_V1.md) |
| Autonomous demo runtime — phase responsibilities (LEGACY three-task split; superseded by the consolidated scheduler) | [`docs/SER8_AUTONOMOUS_DEMO_EXECUTION_V1.md`](docs/SER8_AUTONOMOUS_DEMO_EXECUTION_V1.md) |
| Unified DEMO order executor EA (doc covers v1.1–v1.5; **deployed v1.7**) | [`docs/SER8_MT5_DEMO_ORDER_EXECUTOR_V1.md`](docs/SER8_MT5_DEMO_ORDER_EXECUTOR_V1.md) |
| Historical data acquisition + inventory | [`docs/SER8_MULTISYMBOL_HISTORICAL_RESEARCH_DATA_V1.md`](docs/SER8_MULTISYMBOL_HISTORICAL_RESEARCH_DATA_V1.md) |
| Symbol universe + research ranking | [`docs/SER8_FULL_SYMBOL_UNIVERSE_AND_RESEARCH_RANKING_V1.md`](docs/SER8_FULL_SYMBOL_UNIVERSE_AND_RESEARCH_RANKING_V1.md) |
| Final holdout isolation | [`docs/FINAL_HOLDOUT_ISOLATION_V0.md`](docs/FINAL_HOLDOUT_ISOLATION_V0.md) |
| Orchestrator v1 control-plane spec | [`docs/ORCHESTRATOR_V1_SPEC.md`](docs/ORCHESTRATOR_V1_SPEC.md) |
| Risk Manager core | [`docs/RISK_MANAGER_CORE_V1_0.md`](docs/RISK_MANAGER_CORE_V1_0.md) |
| SMC observation spec | [`docs/SMC_OBSERVATION_SPEC.md`](docs/SMC_OBSERVATION_SPEC.md) |
| Data schema | [`docs/DATA_SCHEMA_V1.md`](docs/DATA_SCHEMA_V1.md) |

The `docs/` directory also holds the versioned `v1.x` module histories. Open
only the documents relevant to the current task.

## HOW TO USE PROJECT DOCUMENTS

1. Read `MASTER_CONTEXT.md` first.
2. Then read `PROJECT_STATUS.md`.
3. Then open only the documents relevant to the current task.
4. Check goals, architecture, rules, the agent network and roles, and Project
   Memory before acting.
5. If context is insufficient, clarify with the operator instead of guessing.
6. Record decisions briefly and unambiguously.
7. No stage is complete until project context is updated (see `AGENTS.md`).
