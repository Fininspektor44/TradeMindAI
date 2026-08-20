# SER8 Autonomous Continuous Demo Execution V1 — three-task runtime

After this layer is installed, Aleksei does not manually run
`run_ser8_real_demo_pipeline.py` for each candidate. Three independently
scheduled Windows tasks together perform the full loop; each has exactly
one responsibility, and they are never merged:

## 1. CANDIDATE GENERATOR

**Task**: `TradeMindAI-v1.32-ECN-LiveSignalRuntime`
**Script**: `run_v121_live_signal_watch.ps1` / `run_v121_live_signal_runtime.ps1`
**Owns**: market data → live candidate generation, journaled to the
canonical live runtime (`.\data\live_signal_runtime_ecN_<login>\candidates.jsonl`).

This task is unmodified by SER8 AUTONOMOUS CONTINUOUS DEMO EXECUTION V1.
It never places, sizes, or authorizes an order.

## 2. EXECUTION WORKER

**Task**: `TradeMindAI-SER8-Autonomous-Demo-Execution`
**Scripts**: `run_ser8_autonomous_demo_execution.ps1` (wrapper) →
`run_ser8_autonomous_demo_execution.py` (worker, one bounded `--once`
cycle per tick, default every 1 minute)
**Owns**: candidate → eligibility/freshness → Risk Manager →
authorization → claim → multi-entry execution request → outcome capture.

Each cycle:

1. Selects the newest eligible candidate for the accepted hypothesis from
   the canonical live runtime (`select_candidate`, reused unmodified from
   `run_ser8_real_demo_pipeline.py`).
2. If this candidate already has a persisted execution plan (a `plan_id`
   under `ser8_mt5_demo_order_plans`, keyed by `candidate_signal_id`),
   the worker **never** re-evaluates, re-authorizes, or re-claims — this
   is the one-shot anchor that makes the whole loop restart-safe; see
   `run_ser8_autonomous_demo_execution.py`'s own module docstring for the
   full design rationale (`RiskDecision.decision_id` is sensitive to live
   MT5 account/positions drift and is never persisted anywhere
   reloadable, so a candidate with no plan yet always gets one single,
   uninterrupted risk-eval → authorize → claim → send call chain, never
   split across a restart boundary). An existing plan is **not**
   automatically "fully processed", though: if every leg already has a
   send attempt (in any state, including UNKNOWN), the cycle reports
   `ALREADY_PROCESSED`; if any leg genuinely has no send attempt yet (a
   process crash strictly between two legs), the worker calls
   `SER8DemoOrderSendControl.resume_plan` — the ONLY authoritative way to
   continue an existing plan, using EXCLUSIVELY the plan's own frozen leg
   data, never a freshly re-evaluated `RiskDecision` — which re-verifies
   the demo account gate and attempts only the genuinely unattempted
   legs, reloading the SAME already-persisted claim (never re-claims). An
   UNKNOWN leg still blocks every leg after it, exactly like `send`
   itself. `--dry-run` never reaches `resume_plan` (a real send-capable
   operation) — it reports `DRY_RUN_WOULD_RESUME` instead, with zero
   broker sends.

   **DURABLE PARTIAL PLAN RESUME (SER8 DURABLE PARTIAL PLAN RESUME
   CONTRACT V1)**: `resume_plan` deliberately does NOT re-check the
   claim's own initial 60-second freshness bound — that bound governs
   ONLY the moment a plan is first created. Reusing an old claim to
   resume an already-persisted plan is not "the claim presented as
   freshly valid again"; it is proof that a durably-authorized plan
   already exists. Instead, every plan persists its own bounded
   `resume_until` deadline at creation time — the minimum of the
   ORIGINAL authorization's own `expires_at` (never extended beyond it)
   and the standing, never-weakened 900-second signal-freshness ceiling
   measured from the candidate's own `created_at`. A restart *within*
   that window resumes cleanly, however many scheduler ticks the machine
   was unavailable for. A restart *after* that window reports
   `RESUME_WINDOW_EXPIRED` — fail closed, zero sends, permanently, until
   a human reviews it; the window is never silently renewed by a retry.
   A plan built without an authorization supplied to `send` (only
   possible via the legacy single-shot pipeline, never this worker)
   carries no durable resume authority at all and can never be resumed.

   **Tamper-evident (SER8 DURABLE RESUME AUTHORITY INTEGRITY V1)**:
   `resume_until` is authorization-critical — it alone decides whether an
   unattempted leg may still be submitted after a restart — so it is
   bound into a separate, independently-persisted `resume_authority_hash`
   covering `execution_plan_id`/`candidate_signal_id`/`hypothesis_id`/
   `account_id`/`authorization_id`/`claim_id`/`decision_id`/the plan's
   own `plan_hash`/`plan_created_at`. Every reconstruction of a plan
   (not only inside `resume_plan` — also this worker's own `ALREADY_
   PROCESSED` observation path) recomputes this hash fresh and compares
   it against the separately-stored original — any of those fields
   altered in persisted storage without also recomputing the stored hash
   to match fails closed immediately, before `resume_until` is ever read
   for anything. `execution_plan_id` itself stays fully deterministic
   and independent of wall-clock creation time; only the separate
   `resume_authority_hash`/`plan_created_at` fields vary with `now`, so
   repeated scheduler ticks for the same candidate never produce a
   second plan merely because time passed.
3. Otherwise runs `evaluate_ser8_research_risk_gate` fresh. A BLOCK is
   reported and produces no execution. An ALLOW proceeds to
   `SER8ExecutionAuthorizationControl.authorize` →
   `SER8ExecutionAuthorizationClaimControl.claim` →
   `verify_demo_account_authorization` →
   `SER8DemoOrderSendControl.send` — the exact same production chain
   `run_ser8_real_demo_pipeline.py --execute` already uses, called
   through the same functions/classes, never duplicated.
4. An `SER8ExecutionAuthorizationConflict` (an active authorization
   already exists for this hypothesis/account with a different approved
   risk decision — e.g. because the account snapshot moved between two
   evaluation attempts) is **never bypassed**. The cycle fails closed,
   reports it, and tries again next cycle; the existing authorization TTL
   (`DEFAULT_AUTHORIZATION_TTL_SECONDS` = 300s) clears it on its own —
   no new authorization-cleanup mechanism was needed or added.
5. Every cycle also runs the outcome-capture bridge (see below) for every
   currently-FILLED leg on the account.
6. Prints exactly one structured summary line
   (`SER8 AUTONOMOUS DEMO EXECUTION CYCLE -- account=... candidate_seen=...
   candidate_id=... candidate_status=... risk_state=... risk_block_reason=...
   authorization_id=... claim_id=... execution_plan_id=... resume_until=...
   legs_total=... legs_newly_submitted=... legs_existing=... pending=...
   filled=... terminal_failures=... unattempted_legs=...
   outcomes_ingested=... broker_sends_this_cycle=... cycle_status=...`).
   `cycle_status` distinguishes `EXECUTION_COMPLETE` (a brand-new plan,
   freshly sent) from `EXECUTION_RESUMED` (an existing plan's remaining
   legs, continued) from `RESUME_WINDOW_EXPIRED` (a resumable plan whose
   durable window has passed — fail closed, zero sends) — never folded
   together.

`--dry-run` runs candidate selection, eligibility, and Risk Manager
evaluation exactly like a real cycle and prints what WOULD be sent, but
calls no authorization, claim, send, or resume at all — zero broker
sends, and zero rows written to any execution-side table (no
`ExecutionAuthorizationV1`, no `ExecutionAuthorizationClaimV1`, no
execution plan, no leg attempt, no outcome row). This is deliberately
stronger than "the candidate is not consumed": even if the live MT5
account/positions snapshot drifts between the dry-run and a real run
immediately afterward — the real Windows incident that motivated this —
the dry-run cannot possibly have created a conflicting authorization,
because it never reaches any control's write path in the first place.

**This worker never itself sends an order to the broker.** It only ever
produces an authorized, claimed, `SER8DemoOrderSendControl.send()` (or,
resuming an existing plan, `.resume_plan()`) call — the same production
primitives that have always existed — which round-trip through the file
bridge to the terminal.

## 3. RECONCILIATION

**Task**: `TradeMindAI-SER8-MT5-Reconciliation`
**Scripts**: `run_ser8_mt5_reconciliation.ps1` → `reconcile_ser8_mt5_execution.py`
**Owns**: broker truth → SER8 execution state (PENDING → FILLED /
CANCELLED / EXPIRED / REJECTED for the leg's own entry order).

Runs completely independently of the execution worker, on its own
schedule, and never sends an order (a bare `FakeDemoOrderTransport()`
with no `result_factory` is always wired in). It reconciles ENTRY fills
only — see OUTCOME CAPTURE below for CLOSE (exit) evidence.

## The only order-sender

Across all three tasks, **the ONLY component that ever sends a real order
to the broker is the unified MT5 executor EA**
(`mt5/TradeMind_Demo_Order_Executor_v1.mq5`) — and only after the Python
execution worker (task 2) has produced an authorized, claimed,
`SER8DemoOrderSendControl.send()` request that round-trips through the
file bridge (`FileBridgeDemoOrderTransport`) to that EA. No other EA, no
scheduled task, and no script ever calls `OrderSend`/`CTrade` directly.

## Outcome capture (the narrow bridge, not the future Analytics Core)

`trademind.ser8_demo_trade_outcome_capture.SER8DemoTradeOutcomeControl`
is a small, additive bridge the execution worker calls every cycle for
every currently-FILLED leg on the account
(`SER8DemoOrderSendControl.list_filled_leg_ids_for_account`). For each
one, it looks for `DEAL_ENTRY_OUT` (close) evidence on that leg's own
`position_ticket` in the SAME `mt5_risk_deals_utc_<login>.csv` export
reconciliation already reads (as of the unified executor v1.5, this
export also carries `DEAL_PROFIT` as a `profit` column). If close
evidence exists, it persists one `ser8_demo_trade_outcomes` row with the
lineage, entry, and exit fields the future Analytics Core will need
(`hypothesis_id`, `candidate_signal_id`, `authorization_id`, `claim_id`,
`execution_plan_id`, `leg_id`, `symbol`, `side`, `order_type`,
`requested_volume`, entry price/timestamp/tickets, SL/TP, exit
price/timestamp/deal tickets, `realized_pl` when the broker evidence
itself supplied a `profit` value, and `terminal_reason`). A position with
no close evidence yet is simply still open — nothing is inferred from
candles, time passing, or a snapshot's absence. This task does **not**
redesign the analytics schema; it persists only the narrowest
authoritative record needed.

## Safety invariants preserved by all three tasks together

- The research lifecycle, the accepted hypothesis, and the protected
  final holdout are never touched by the execution worker or
  reconciliation.
- `standard_v1.json` and the Risk Manager's own lot-sizing logic are
  never modified; `maximum_signal_age_seconds` is never weakened above
  900 seconds.
- Risk Manager, authorization, and claim remain mandatory on every
  execution path — no bypass, override, or force flag exists anywhere in
  this loop.
- One candidate produces at most one authoritative execution plan, ever
  — proven by `get_plan_for_candidate` gating every fresh risk/authorize/
  claim attempt, and independently backstopped by
  `SER8DemoOrderSendControl.send`/`.resume_plan`'s own shared, pre-existing
  per-leg `_reserve_leg_attempt` uniqueness guard (an already-attempted
  leg, in any state including UNKNOWN, is never resent by either entry
  point).
- A plan's unattempted legs (a genuine crash strictly between two legs)
  are resumable ONLY through `resume_plan`, which never creates a second
  plan and never creates a second claim for the same plan — proven by
  dedicated tests asserting the plans/claims table row counts are
  unchanged by a resume, bounded by the plan's own persisted
  `resume_until` — proven never to exceed the ORIGINAL authorization's
  own `expires_at` (never extended).
- `--account`/`--demo-account-allowlist` are both required, explicit, and
  cross-checked at startup — the worker never silently defaults to a
  live account, and fails closed immediately if `--account` is not a
  member of `--demo-account-allowlist`.
