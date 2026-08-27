# TradeMindAI — PROJECT STATUS

Replaceable current snapshot. Overwrite the changed parts after every completed
stage; do not let old status accumulate here. Stable truth lives in
[`MASTER_CONTEXT.md`](MASTER_CONTEXT.md); history lives in
[`PROJECT_MEMORY.md`](PROJECT_MEMORY.md).

_Last updated: 2026-08-27 — Persistent context correction; broker-capable DEMO
recorded as already enabled. Documentation only._

## CURRENT PHASE

SER8 CORE_8 **broker-capable DEMO execution is ALREADY ENABLED** and running on
the Windows SER8 runtime. The persistent project-context system (this file plus
`MASTER_CONTEXT.md`, `PROJECT_MEMORY.md`, `AGENTS.md`, `CLAUDE.md`) is installed
so future sessions continue from the repository, not chat history. No
application code, runtime, MT5, scheduler, config, or trading state was touched
by this stage.

## BRANCH / CHECKPOINT

- Branch: `codex/ser8-multisymbol-historical-research-data-v1`
- HEAD at start of this stage: `ebe16a3`
- Most recent checkpoint: `checkpoint/core8-market-only-demo-execution-v1`
  (commit `da8c5fa`, "checkpoint: CORE8 operational direct-connect ready")

## RUNTIME — OPERATIONAL NOW

- **Broker-capable DEMO execution: ENABLED.** CORE_8, natural SMC/OTE signals
  only, no forced or manufactured signal.
- **One consolidated authoritative scheduled task:**
  `TradeMindAI-SER8-Autonomous-Demo-Execution`, interval `PT3M`, DryRun
  `FALSE`. It owns the full loop: **producer -> execution -> reconciliation**.
- **Legacy scheduled tasks are DISABLED** (do not re-enable):
  `TradeMindAI-SER8-MT5-Reconciliation`,
  `TradeMindAI-v1.21-LiveSignalRuntime`,
  `TradeMindAI-v1.32-ECN-LiveSignalRuntime`.
- **Accounts:** execution `67206924` (DEMO only); market-data `77053345`
  (read-only, never executes).
- **Unified executor:** `TradeMind_Demo_Order_Executor_v1`, deployed
  implementation **v1.7** — the only component that sends an order.
- **CORE_8 status:** broker-capable DEMO enabled; 8/8 symbol routing verified
  (CHFJPY, EURJPY, EURNZD, GBPAUD, GBPNZD, NZDCAD, NZDCHF, USDJPY);
  non-positive-equity blocker resolved; scheduler healthy.
- **Reconciliation:** `unknown_legs_seen=0`, `ambiguous=0`.
- Risk gate: `config/risk_profiles/ser8_supervised_demo_v1.json`
  (`SER8_SUPERVISED_DEMO_V1`, `allowed_signal_states = ["APPROVED_MANUAL"]`).
- SMC/OTE signal generation (`ote_engine.build_ote_signals`) — live FX and
  historical replay. No EMA/RSI anywhere.
- Historical inventory: 28 `HISTORICAL_DATA_READY` FX symbols; acquisition layer
  frozen.
- Checkpoint create / verify tooling.

## COMPLETED (recent stages)

- Remove EMA/RSI, promote SMC/OTE V1 — `9547efb`, checkpoint
  `remove-ema-rsi-promote-smc-ote-v1`.
- Operationalize CORE_8 SMC/OTE MARKET_ONLY demo execution v1 — `30bbc14`,
  checkpoint `core8-market-only-demo-execution-v1`.
- Purge non-canonical MT5 accounts — `451b075`, checkpoint
  `purge-noncanonical-mt5-accounts-v1`.
- SER8 UNKNOWN-leg operator-confirmed-absent resolution CLI — `ebe16a3`.
- CORE_8 broker-capable DEMO brought online: consolidated scheduler, executor
  deployed at v1.7, 8/8 routing verified, non-positive-equity blocker resolved.
- Former EURJPY UNKNOWN leg `EAC-67206924-e5cedfbc6bf3af61` authoritatively
  resolved: `REJECTED` / `NO_FILL_TERMINAL`, active EURJPY plan cleared, no
  resend occurred.
- Full test gate at the CORE_8 checkpoint: 2555 passed, 0 failed.

## ACTIVE WORK

Persistent context correction (this stage) — documentation and agent
instructions only. Then: watching the live runtime for the first natural
broker-capable CORE_8 DEMO execution.

## CURRENT BLOCKERS

None.

## NEXT_ACTION

Observe and verify the **first natural broker-capable CORE_8 DEMO execution** —
do not manufacture a signal. When a fresh natural ALLOW candidate occurs, verify
the full chain end to end:

`signal -> RiskDecision -> ExecutionAuthorization -> Claim -> ExecutionPlan ->
DemoAccountSafetyGate -> unified MT5 executor -> broker result -> reconciliation`

then record the outcome in this file and in `PROJECT_MEMORY.md`.

## SAFETY STATE

- DEMO only. Execution account `67206924`. No live account is or may be
  connected. Market-data account `77053345` is read-only.
- Risk Manager, ExecutionAuthorization, one-shot Claim, and DemoAccountSafetyGate
  mandatory on every execution path; no bypass, override, or force flag exists.
- Natural signals only — no forced or manufactured signal.
- Protected final holdout sealed and unconsumed. The accepted EURUSD hypothesis
  `rpi-v1:sha256:205b5260...:0` is a protected historical / research artifact,
  immutable and outside the CORE_8 execution universe.
- No `git push` performed in this stage.

## LEGACY / SUPERSEDED DOCUMENTS (linked for history only)

- [`docs/SER8_AUTONOMOUS_DEMO_EXECUTION_V1.md`](docs/SER8_AUTONOMOUS_DEMO_EXECUTION_V1.md)
  — describes the original three-task split; the current layout is the single
  consolidated scheduler above. Read it for each phase's responsibilities only.
- [`docs/SER8_MT5_DEMO_ORDER_EXECUTOR_V1.md`](docs/SER8_MT5_DEMO_ORDER_EXECUTOR_V1.md)
  — documents executor v1.1–v1.5; deployed implementation is v1.7.
- [`docs/TRADEMIND_PROJECT_PROGRESS.md`](docs/TRADEMIND_PROJECT_PROGRESS.md)
  — SER8 research-layer running log; its trailing "NEXT ACTION" predates CORE_8
  operationalization. This file is authoritative for the current next action.
- The 28-symbol multi-symbol screening and 28x4 execution-geometry A/B result
  layers remain SUPERSEDED / INVALID (EMA/RSI-era candidate populations).
