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
   the worker **never** re-evaluates, re-authorizes, re-claims, or
   re-sends — it only observes the plan's already-persisted leg states.
   This is the one-shot anchor that makes the whole loop restart-safe;
   see `run_ser8_autonomous_demo_execution.py`'s own module docstring for
   the full design rationale (`RiskDecision.decision_id` is sensitive to
   live MT5 account/positions drift and is never persisted anywhere
   reloadable, so a candidate with no plan yet always gets one single,
   uninterrupted risk-eval → authorize → claim → send call chain, never
   split across a restart boundary).
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
   authorization_id=... claim_id=... execution_plan_id=... legs_total=...
   legs_newly_submitted=... legs_existing=... pending=... filled=...
   terminal_failures=... outcomes_ingested=... broker_sends_this_cycle=...
   cycle_status=...`).

`--dry-run` runs candidate selection, eligibility, and Risk Manager
evaluation exactly like a real cycle and prints what WOULD be sent, but
calls no authorization, claim, or send at all — zero broker sends, and
the candidate is never consumed, so a real cycle immediately afterward is
unaffected.

**This worker never itself sends an order to the broker.** It only ever
produces an authorized, claimed, `SER8DemoOrderSendControl.send()` call —
the same production primitive that has always existed — which round-trips
through the file bridge to the terminal.

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
  — proven by `get_plan_claim_id_for_candidate` gating every fresh
  evaluation attempt, and independently backstopped by
  `SER8DemoOrderSendControl.send`'s own pre-existing per-leg
  `_reserve_leg_attempt` uniqueness guard.
- `--account`/`--demo-account-allowlist` are both required, explicit, and
  cross-checked at startup — the worker never silently defaults to a
  live account, and fails closed immediately if `--account` is not a
  member of `--demo-account-allowlist`.
