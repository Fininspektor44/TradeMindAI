# TradeMindAI — PROJECT MEMORY

Append-only. One entry per material completed stage: what was done, what
changed, the important decision and why, what remains, next step. No raw logs.
Newest entries at the bottom. Stable truth lives in
[`MASTER_CONTEXT.md`](MASTER_CONTEXT.md); the live snapshot is
[`PROJECT_STATUS.md`](PROJECT_STATUS.md).

The entries below were reconstructed on 2026-08-27 from Git history and the
authoritative `docs/` set when this memory system was created; they are
milestones, not a full commit log.

---

## 2026-08-22 — SER8 historical acquisition layer CLOSED / FROZEN

- **Done:** Full Windows verification of multi-symbol historical M5 acquisition
  from the ECN market-data account `77053345`, plus pre-history sentinel
  classification (`fb1295e`).
- **Changed:** Authoritative inventory
  `data/ser8_historical_market_data/historical_inventory.json` — 90 broker
  symbols, 83 accepted datasets, 28 `HISTORICAL_DATA_READY` FX,
  55 `RISK_MODEL_UNSUPPORTED`.
- **Decision & why:** Acquisition code and dataset identity are frozen so no
  later layer can silently change chunk acquisition, coverage discovery, or
  dataset identity — research evidence must be stable to be trustworthy.
- **Remains:** None for acquisition. No MT5 reacquisition without operator
  authorization.
- **Next:** Use the frozen datasets for replay / research readiness only.
- Ref: [`docs/SER8_MULTISYMBOL_HISTORICAL_RESEARCH_DATA_V1.md`](docs/SER8_MULTISYMBOL_HISTORICAL_RESEARCH_DATA_V1.md),
  checkpoint `ser8-full-symbol-universe-real-inventory-v1`.

---

## 2026-08-23 to 08-24 — Multi-symbol screening + execution-geometry A/B: SUPERSEDED / INVALID

- **Done:** Added additive multi-symbol historical screening (`79f6c03`), fixed
  its direction-count reporting defect (`270904f`, direction now from
  `plan.action`), added and hardened the 28x4 execution-geometry A/B experiment
  (`661ee12`, `aded85a`).
- **Changed:** New research modules + CLIs; report-serialization budget
  (`EXECUTION_GEOMETRY_REPORT_JSON_BUDGET`) and per-symbol resume checkpoints.
- **Decision & why:** All screening / geometry **result** layers are marked
  SUPERSEDED / INVALID because their candidate populations came from the
  EMA/RSI architecture removed on 08-25. Their candidates, outcomes, rankings,
  and conclusions must not be reused. The real Windows geometry run failed at
  final report serialization, so no geometry verdict exists from it either.
- **Remains:** A corrected replay population must be built through
  `build_ote_signals` before any screening / geometry result can be trusted.
- **Next:** Superseded by the CORE_8 operationalization below, which adopted the
  already-published `MARKET_ONLY_SAME_TARGET` variant shape directly.
- Ref: [`docs/TRADEMIND_PROJECT_PROGRESS.md`](docs/TRADEMIND_PROJECT_PROGRESS.md).

---

## 2026-08-25 — Remove EMA/RSI, promote SMC/OTE V1

- **Done:** Deleted the legacy `SignalEngine` container and its models
  (`9547efb`).
- **Changed:** The sole authoritative FX directional source is now
  `trademind.ote_engine.build_ote_signals`. ATR moved to the neutral
  `trademind.volatility` utility with zero directional authority.
- **Decision & why:** The retired `SignalEngine` supplied EMA/RSI-derived
  action, confidence, and source-score into both historical replay and the live
  FX candidate path — contaminating direction. One auditable model is a project
  goal; an undocumented indicator blend is not acceptable.
- **Remains:** Historical result layers built on the old populations stay
  invalid (see previous entry).
- **Next:** Operationalize execution on the researched SMC/OTE path.
- Ref: checkpoint `remove-ema-rsi-promote-smc-ote-v1`.

---

## 2026-08-25 — Operationalize CORE_8 SMC/OTE MARKET_ONLY demo execution v1

- **Done:** Narrowed supervised demo execution to eight researched FX symbols
  (CHFJPY, EURJPY, EURNZD, GBPAUD, GBPNZD, NZDCAD, NZDCHF, USDJPY), single
  MARKET entry only (`30bbc14`).
- **Changed:** New `src/trademind/ser8_core8_market_only_policy.py` — the single
  fail-closed source of truth (pure functions; no sizing, broker, network,
  filesystem, override, force, or bypass). `fx_signal_adapter._build_plan`
  applies MARKET_ONLY for CORE_8 symbols before `signal_id` is derived, so a
  CORE_8 candidate is born market-only with one traceable identity. The worker
  enforces the policy at three points: selected candidate, sized
  `RiskDecision.orders` before the real transport is built, and a persisted
  plan's frozen legs before resume.
- **Decision & why:** The MARKET+LIMIT+LIMIT basket showed an asymmetric-fill
  pattern (PF < 1 on every screened symbol). CORE_8 MARKET-only reproduces the
  already-published `MARKET_ONLY_SAME_TARGET` variant shape byte-for-byte
  (stop and primary target unchanged), asserted by a binding equivalence test.
  Non-CORE_8 symbols and legacy basket geometry now fail closed before any risk
  evaluation.
- **Remains:** Nothing. CORE_8 executes natural SMC/OTE signals on the eight
  symbols directly; there is no per-symbol accepted-hypothesis requirement for
  CORE_8 execution. The accepted EURUSD hypothesis is a separate protected
  historical / research artifact and is not part of the CORE_8 execution
  universe.
- **Next:** Bring CORE_8 broker-capable DEMO online (see 2026-08-27 entry).
- Ref: checkpoint `core8-market-only-demo-execution-v1`; full suite 2555 passed.

---

## 2026-08-25 — Purge non-canonical MT5 accounts

- **Done:** Removed non-canonical MT5 account references (`451b075`).
- **Changed:** Canonical accounts are now exactly two — market-data `77053345`
  and DEMO execution `67206924` (magic `990244`).
- **Decision & why:** Ambiguous account identity is a safety hazard on an
  execution path; the set must be explicit and minimal.
- **Remains:** None.
- **Next:** —
- Ref: checkpoint `purge-noncanonical-mt5-accounts-v1`.

---

## 2026-08-27 — SER8 UNKNOWN-leg operator-confirmed-absent resolution CLI

- **Done:** Added a guarded operator entrypoint (`ebe16a3`) for the one case the
  automatic reconciler will not resolve: a persisted `UNKNOWN` demo-order leg
  whose send never reached the broker, after an operator has confirmed via
  manual broker History inspection that no order/deal/position of that identity
  exists.
- **Changed:** Transitions only the named leg to terminal `REJECTED`
  (no ticket, no fill, no P/L manufactured) and finalizes the plan
  (`NO_FILL_TERMINAL`). Refuses unless the leg is `UNKNOWN` and its persisted
  request identity matches the caller args; re-scans the risk orders/deals CSVs
  and aborts on any match; never builds authorization/claim, never calls a
  transport, never writes a request file; `--dry-run` previews; idempotent.
- **Decision & why:** Real broker truth always wins; a stuck `UNKNOWN` leg must
  be resolvable without ever inventing a fill or re-sending an order.
- **Remains:** Nothing. Used to authoritatively resolve EURJPY leg
  `EAC-67206924-e5cedfbc6bf3af61` (plan `EOP-ccf676f2f990eac0`) to `REJECTED` /
  `NO_FILL_TERMINAL`. The active EURJPY plan was cleared; no resend occurred;
  reconciliation now reports `unknown_legs_seen=0`, `ambiguous=0`.
- **Next:** —

---

## 2026-08-27 — CORE_8 broker-capable DEMO enabled; scheduler consolidated

- **Done:** Brought CORE_8 broker-capable DEMO execution online on the Windows
  SER8 runtime. Consolidated the runtime into one authoritative scheduled task,
  `TradeMindAI-SER8-Autonomous-Demo-Execution` (interval `PT3M`, DryRun
  `FALSE`), owning producer -> execution -> reconciliation. Deployed the unified
  executor `TradeMind_Demo_Order_Executor_v1` at implementation **v1.7**.
- **Changed:** Legacy scheduled tasks disabled —
  `TradeMindAI-SER8-MT5-Reconciliation`, `TradeMindAI-v1.21-LiveSignalRuntime`,
  `TradeMindAI-v1.32-ECN-LiveSignalRuntime`. 8/8 CORE_8 symbol routing verified.
  Non-positive-equity blocker resolved. Scheduler healthy.
- **Decision & why:** One scheduler with a single ordered loop removes cross-task
  race conditions and gives one place to reason about producer / execution /
  reconciliation ordering. Broker-capable DEMO runs on natural SMC/OTE signals
  only — never a forced or manufactured signal.
- **Remains:** Observe the first natural broker-capable CORE_8 DEMO execution
  end to end and record its outcome.
- **Next:** See `PROJECT_STATUS.md` NEXT_ACTION.

---

## 2026-08-27 — Persistent Project Context System V1 (+ correction pass)

- **Done:** Created `MASTER_CONTEXT.md`, `PROJECT_STATUS.md`,
  `PROJECT_MEMORY.md`, `AGENTS.md`, and `CLAUDE.md` at the repository root so a
  new AI or human session can continue the project from the repository alone.
  A correction pass then aligned all five with authoritative current runtime
  facts: consolidated scheduler (not three live tasks), executor v1.7 (not
  v1.5), broker-capable DEMO already enabled, EURJPY UNKNOWN leg resolved, and
  removed the false CORE_8/EURUSD "open contradiction".
- **Changed:** Documentation and agent instructions only. No application code,
  runtime, MT5, scheduler, config, or trading state was modified. No full
  pytest run, no push.
- **Decision & why:** Chat history is not durable project memory. From now on a
  stage is not complete until `PROJECT_STATUS.md` reflects current state,
  material decisions are appended here, and `MASTER_CONTEXT.md` is updated if
  stable truth changed (the stage-closure rule in `AGENTS.md`). Stale
  design docs stay linked with an explicit legacy / superseded note rather than
  being rewritten.
- **Remains:** Nothing for this stage.
- **Next:** See `PROJECT_STATUS.md` NEXT_ACTION.
