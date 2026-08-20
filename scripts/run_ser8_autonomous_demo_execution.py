#!/usr/bin/env python3
"""SER8 Autonomous Continuous Demo Execution V1.

ONE production entrypoint that continuously (or once, via ``--once``)
drives the ALREADY-CLOSED SER8 chain end to end for the canonical live
candidate runtime, so Aleksei no longer has to manually invoke
``run_ser8_real_demo_pipeline.py`` for each candidate:

    canonical live candidate -> eligibility/freshness -> Risk Manager ->
    authorization -> claim -> multi-entry execution -> unified MT5 EA ->
    (automatic reconciliation runs independently) -> outcome capture ->
    next candidate

ARCHITECTURAL PRINCIPLE -- CONNECT, NEVER DUPLICATE: this script imports
and calls the SAME research-pipeline glue ``run_ser8_real_demo_pipeline``
already uses (``discover_inputs``, ``load_candidates``, ``select_candidate``,
``build_research_pipeline``, ``Preview``/``LegPreview``/``_print_preview``,
``_print_block_diagnostics``) -- exactly the way ``tests/
test_run_ser8_real_demo_pipeline.py`` already imports that script as a
module -- and the SAME production control classes
(``evaluate_ser8_research_risk_gate``, ``SER8ExecutionAuthorizationControl``,
``SER8ExecutionAuthorizationClaimControl``, ``SER8DemoOrderSendControl``,
``DemoAccountAllowlistV1``/``verify_demo_account_authorization``,
``SER8DemoTradeOutcomeControl``). There remains exactly ONE authoritative
demo execution path; this script adds a cycle/scheduling loop around it,
nothing else. It never advances the research lifecycle (no
``advance_research_state`` call -- the hypothesis must already be
ACCEPTED before this worker runs), never recreates a hypothesis, never
opens or weakens the protected holdout, and never touches
``standard_v1.json``.

RESTART/ONE-SHOT SAFETY -- the central design decision (see this task's
own commit message for the full audit trail): ``RiskDecision.decision_id``
is sensitive to the live MT5 account/positions snapshot content, which
drifts over time as the unified executor's own risk-refresh timer
rewrites those CSVs -- so re-evaluating risk fresh for the "same"
candidate at two different moments can legitimately produce a DIFFERENT
``decision_id``, and ``SER8DemoOrderSendControl.send`` requires the
EXACT ``RiskDecision`` object whose ``decision_id`` matches what
``claim.risk_decision_id`` recorded. ``RiskDecision`` is never persisted
anywhere reloadable, so it cannot be safely reconstructed after a
process restart. This worker resolves that tension the ONLY way that is
provably safe, without weakening or duplicating anything in the already-
closed authorization/claim/send layers:

  * Before ANY fresh risk evaluation, it checks
    ``SER8DemoOrderSendControl.get_plan_claim_id_for_candidate`` -- a new,
    narrow, read-only accessor. If an execution PLAN already exists for
    this candidate, the worker NEVER re-evaluates risk, NEVER re-
    authorizes, NEVER re-claims, and NEVER calls ``send`` again for it --
    it only OBSERVES the already-persisted leg states. This is the ONE-
    SHOT anchor: a plan is persisted (by ``send`` itself) BEFORE any leg
    is ever attempted, so "does a plan exist" is a stable, restart-safe,
    durable fact.
  * Only a candidate with NO persisted plan gets a fresh, single,
    uninterrupted risk-eval -> authorize -> claim -> send call chain,
    entirely within one Python call (never split across a restart
    boundary) -- so the ``RiskDecision`` object ``send`` receives is
    always the SAME one ``authorize``/``claim`` were just computed
    against, and ``send``'s own crash-resume logic (skip already-
    attempted legs) is always operating on a plan keyed by a
    ``decision_id`` this exact process just produced.
  * If a process crashes strictly BETWEEN ``authorize`` succeeding and
    ``send`` ever being called (so no plan was ever persisted), the OLD
    authorization simply sits there until its own TTL
    (``DEFAULT_AUTHORIZATION_TTL_SECONDS`` = 300s) expires. A fresh
    attempt next cycle either idempotently reuses it (if the freshly
    recomputed decision happens to hash identically) or fails closed
    with ``SER8ExecutionAuthorizationConflict`` -- reported, never
    bypassed -- until the TTL naturally clears it. This directly answers
    the task's own audit question: NO new authorization-cleanup
    mechanism is needed; the existing TTL is sufficient, and the worker
    only needs to fail closed on conflict rather than invent a bypass.
  * If a process crashes AFTER ``send`` has already persisted a plan and
    attempted at least one leg, the plan-existence check above means
    this candidate is NEVER touched again by this worker -- an
    intentionally conservative, explicitly-safe outcome (a partially-
    sent multi-leg basket can stall short of its remaining legs after a
    crash in that narrow window) that is always visible via this
    script's own observability line (``legs_existing`` vs ``pending``/
    ``filled``) for human follow-up, rather than risk ANY code path that
    could double-send a leg under a changed ``decision_id``.

DRY-RUN: performs candidate selection, eligibility, and risk evaluation
exactly like a real cycle, and previews the plan Risk Manager WOULD
produce -- but calls no authorization, claim, or send at all (not even to
persist an idempotent no-op), so it can never consume the candidate,
never conflicts with a concurrent/later real cycle, and never creates
misleading terminal execution state.

OUTCOME CAPTURE: every cycle also calls
``trademind.ser8_demo_trade_outcome_capture.SER8DemoTradeOutcomeControl.
capture_outcome_for_leg`` for every currently-FILLED leg on this account
(discovered generically via ``list_filled_leg_ids_for_account`` -- never a
hard-coded leg/claim), using ONLY authoritative close-deal evidence from
the SAME ``mt5_risk_deals_utc_<account>.csv`` reconciliation already
reads. This is independent of, and never touches, PENDING-leg
reconciliation (a separate, independently-scheduled task).

SAFETY: this script never imports MetaTrader5. In ``--dry-run`` or the
observation-only ``ALREADY_PROCESSED`` path it wires a bare
``FakeDemoOrderTransport()`` (no ``result_factory``) into
``SER8DemoOrderSendControl`` -- a transport that raises immediately if
ever invoked -- so a real send is structurally impossible in those modes.
Only the real, non-dry-run execution path (reached only for a candidate
with NO existing plan, only after RiskDecision.state == ALLOW, only after
authorization + claim + demo-account-allowlist verification all succeed)
constructs a ``FileBridgeDemoOrderTransport``, exactly mirroring
``run_ser8_real_demo_pipeline.py --execute``.
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

pipeline_module = importlib.import_module("run_ser8_real_demo_pipeline")

from trademind.discovery.research_eligibility_boundary import (  # noqa: E402
    ResearchEligibilityError,
    present_eligible_artifact,
)
from trademind.discovery.hypothesis_tradeable_scope import (  # noqa: E402
    HypothesisTradeableScopeError,
    bind_hypothesis_tradeable_scope,
)
from trademind.risk_manager import RiskProfile, profile_from_dict  # noqa: E402
from trademind.ser8_demo_account_safety_gate import (  # noqa: E402
    DemoAccountAllowlistV1,
    DemoAccountSafetyGateError,
    verify_demo_account_authorization,
)
from trademind.ser8_demo_trade_outcome_capture import (  # noqa: E402
    SER8DemoTradeOutcomeControl,
    SER8DemoTradeOutcomeError,
)
from trademind.ser8_execution_authorization import (  # noqa: E402
    SER8ExecutionAuthorizationConflict,
    SER8ExecutionAuthorizationControl,
    SER8ExecutionAuthorizationError,
)
from trademind.ser8_execution_authorization_claim import (  # noqa: E402
    SER8ExecutionAuthorizationClaimConflict,
    SER8ExecutionAuthorizationClaimControl,
    SER8ExecutionAuthorizationClaimError,
)
from trademind.ser8_mt5_demo_order_send import (  # noqa: E402
    FakeDemoOrderTransport,
    FileBridgeDemoOrderTransport,
    SER8DemoOrderAlreadyAttemptedError,
    SER8DemoOrderPartialExecutionError,
    SER8DemoOrderPendingError,
    SER8DemoOrderReconciliationRequiredError,
    SER8DemoOrderRejectedError,
    SER8DemoOrderSendControl,
    SER8DemoOrderSendError,
    build_demo_order_execution_plan,
)
from trademind.ser8_research_risk_gate import (  # noqa: E402
    SER8ResearchRiskGateError,
    evaluate_ser8_research_risk_gate,
)

DEFAULT_POLL_INTERVAL_SECONDS = 60.0
DEFAULT_CLAIMANT_ID = "worker:ser8-autonomous-demo-execution"


class _AlreadyRunningError(RuntimeError):
    """Raised when this script's own lock file already exists -- fails
    closed rather than risking two autonomous workers racing on the same
    registry. Mirrors scripts/reconcile_ser8_mt5_execution.py's own
    ``_LockFile`` exactly."""


class _LockFile:
    """A simple, portable exclusive-create lock file -- see
    scripts/reconcile_ser8_mt5_execution.py's own ``_LockFile`` (the SAME
    proven pattern, deliberately duplicated rather than cross-imported,
    matching this repository's existing convention of standalone script
    entrypoints)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._acquired = False

    def acquire(self) -> None:
        try:
            handle = self.path.open("x", encoding="utf-8")
        except FileExistsError as exc:
            raise _AlreadyRunningError(
                f"lock file already exists: {self.path} -- another autonomous execution process may "
                "already be running; if you are certain none is, remove this file by hand and retry"
            ) from exc
        with handle:
            handle.write(f"pid={__import__('os').getpid()} started_at={datetime.now(timezone.utc).isoformat()}\n")
        self._acquired = True

    def release(self) -> None:
        if self._acquired and self.path.is_file():
            self.path.unlink()
        self._acquired = False

    def __enter__(self) -> "_LockFile":
        self.acquire()
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.release()


class CycleSummary:
    """Mutable accumulator for the ONE structured observability line this
    script prints per cycle -- never per-field spam."""

    def __init__(self, *, account: str) -> None:
        self.account = account
        self.candidate_seen = "NO"
        self.candidate_id = "-"
        self.candidate_status = "-"
        self.risk_state = "-"
        self.risk_block_reason = "-"
        self.authorization_id = "-"
        self.claim_id = "-"
        self.execution_plan_id = "-"
        self.legs_total = 0
        self.legs_newly_submitted = 0
        self.legs_existing = 0
        self.pending = 0
        self.filled = 0
        self.terminal_failures = 0
        self.outcomes_ingested = 0
        self.broker_sends_this_cycle = 0
        self.cycle_status = "NO_ACTION"

    def line(self) -> str:
        return (
            "SER8 AUTONOMOUS DEMO EXECUTION CYCLE -- "
            f"account={self.account} candidate_seen={self.candidate_seen} "
            f"candidate_id={self.candidate_id} candidate_status={self.candidate_status} "
            f"risk_state={self.risk_state} risk_block_reason={self.risk_block_reason} "
            f"authorization_id={self.authorization_id} claim_id={self.claim_id} "
            f"execution_plan_id={self.execution_plan_id} legs_total={self.legs_total} "
            f"legs_newly_submitted={self.legs_newly_submitted} legs_existing={self.legs_existing} "
            f"pending={self.pending} filled={self.filled} terminal_failures={self.terminal_failures} "
            f"outcomes_ingested={self.outcomes_ingested} broker_sends_this_cycle={self.broker_sends_this_cycle} "
            f"cycle_status={self.cycle_status}"
        )


def _observe_claim_legs(send_control: SER8DemoOrderSendControl, claim_id: str, summary: CycleSummary) -> None:
    leg_ids = send_control.list_leg_ids_for_claim(claim_id)
    summary.legs_total = len(leg_ids)
    summary.legs_existing = len(leg_ids)
    for leg_id in leg_ids:
        receipt = send_control.get_leg_receipt(leg_id)
        if receipt is None:
            continue
        if receipt.result_state == "FILLED":
            summary.filled += 1
        elif receipt.result_state == "PENDING":
            summary.pending += 1
        elif receipt.result_state != "UNKNOWN":
            summary.terminal_failures += 1


def _capture_outcomes(
    *,
    outcome_control: SER8DemoTradeOutcomeControl,
    send_control: SER8DemoOrderSendControl,
    authorization_control: SER8ExecutionAuthorizationControl,
    account: str,
    deals_csv: Path,
    now: datetime,
) -> int:
    """Runs the outcome-capture bridge for every currently-FILLED leg on
    this account. Returns how many outcomes were NEWLY captured this
    cycle (already-captured legs are untouched, idempotent no-ops).
    "Still open, no close evidence yet" is never an error. A
    structurally invalid deals export IS reported (via
    ``SER8DemoTradeOutcomeError``) -- but never here: outcome capture is
    an explicitly best-effort, narrow bridge, never a precondition of the
    core candidate/risk/authorization/claim/send safety chain, so a
    caller must decide how loudly to surface that failure without letting
    it mask an already-determined cycle_status (see ``run_one_cycle``'s
    own call sites)."""
    newly_captured = 0
    for leg_id in send_control.list_filled_leg_ids_for_account(account):
        already = outcome_control.get_outcome(leg_id)
        outcome = outcome_control.capture_outcome_for_leg(
            leg_id,
            send_control=send_control,
            authorization_control=authorization_control,
            deals_csv=deals_csv,
            now=now,
        )
        if already is None and outcome is not None:
            newly_captured += 1
    return newly_captured


def _capture_outcomes_report_only(
    *,
    outcome_control: SER8DemoTradeOutcomeControl,
    send_control: SER8DemoOrderSendControl,
    authorization_control: SER8ExecutionAuthorizationControl,
    account: str,
    deals_csv: Path,
    now: datetime,
) -> int:
    """Wraps :func:`_capture_outcomes` so a structurally invalid deals
    export is reported on its own diagnostic line, never allowed to mask
    or override an already-determined ``cycle_status`` for the SAFETY-
    CRITICAL part of this cycle (candidate/risk/authorization/claim/send)
    -- outcome capture is explicitly best-effort, and a formatting
    problem in an evidence file must never make a genuinely successful
    execution get reported as a failed cycle."""
    try:
        return _capture_outcomes(
            outcome_control=outcome_control, send_control=send_control,
            authorization_control=authorization_control, account=account, deals_csv=deals_csv, now=now,
        )
    except SER8DemoTradeOutcomeError as exc:
        print(f"  outcome capture failed this cycle (non-fatal, retried next cycle): {exc}", file=sys.stderr)
        return 0


def run_one_cycle(args: argparse.Namespace, *, now: datetime | None = None) -> CycleSummary:
    now = now or datetime.now(timezone.utc)
    repo_root = REPO_ROOT
    data_root = Path(args.data_root).expanduser() if args.data_root else repo_root / "data"
    summary = CycleSummary(account=args.account)

    inputs = pipeline_module.discover_inputs(
        data_root=data_root,
        runtime_root=Path(args.runtime_root).expanduser(),
        db_path=Path(args.db).expanduser(),
        candidates_path=Path(args.candidates).expanduser() if args.candidates else None,
        mt5_export_dir=Path(args.mt5_export_dir).expanduser(),
        login=args.account,
        risk_profile_path=Path(args.risk_profile).expanduser(),
        correlations_path=Path(args.correlations).expanduser() if args.correlations else None,
        repo_root=repo_root,
    )
    deals_csv = Path(args.mt5_export_dir).expanduser() / f"mt5_risk_deals_utc_{args.account}.csv"

    orchestrator_db = (
        Path(args.orchestrator_db).expanduser() if args.orchestrator_db else inputs.data_root / "ser8_orchestrator.db"
    )
    artifact_root = (
        Path(args.artifact_root).expanduser() if args.artifact_root else inputs.data_root / "ser8_artifacts"
    )
    pipeline = pipeline_module.build_research_pipeline(
        db_path=inputs.db_path,
        orchestrator_db_path=orchestrator_db,
        artifact_root=artifact_root,
        holdout_key_env=args.holdout_key_env or pipeline_module.DEFAULT_HOLDOUT_KEY_ENV,
        holdout_key_id=args.holdout_key_id or pipeline_module.DEFAULT_HOLDOUT_KEY_ID,
        holdout_primary_metric=args.holdout_primary_metric,
        holdout_parameters={},
    )

    # This worker NEVER advances the research lifecycle -- the hypothesis
    # must already be ACCEPTED (a prior, separate, human-reviewed action)
    # before autonomous execution runs. present_eligible_artifact fails
    # closed for anything else.
    eligibility = present_eligible_artifact(
        args.hypothesis_id, registry=pipeline.registry, final_verdict=pipeline.final_verdict
    )
    scope = bind_hypothesis_tradeable_scope(
        args.hypothesis_id, registry=pipeline.registry, artifact_store=pipeline.artifacts
    )

    candidates = pipeline_module.load_candidates(inputs.candidates_path)
    try:
        candidate = pipeline_module.select_candidate(candidates, scope=scope, signal_id=None)
    except pipeline_module.PipelineGapError:
        summary.cycle_status = "NO_ELIGIBLE_CANDIDATE"
        return summary

    summary.candidate_seen = "YES"
    summary.candidate_id = candidate.signal_id

    transport_for_observation = FakeDemoOrderTransport()  # never invoked in the observation-only path.
    send_control = SER8DemoOrderSendControl(registry=pipeline.registry, transport=transport_for_observation)
    authorization_control = SER8ExecutionAuthorizationControl(
        registry=pipeline.registry, final_verdict=pipeline.final_verdict
    )
    claim_control = SER8ExecutionAuthorizationClaimControl(registry=pipeline.registry)
    outcome_control = SER8DemoTradeOutcomeControl(registry=pipeline.registry)

    # ONE-SHOT ANCHOR: a candidate with an already-persisted execution
    # plan is NEVER re-evaluated, re-authorized, re-claimed, or re-sent --
    # see this script's own module docstring for the full restart-safety
    # rationale.
    existing_claim_id = send_control.get_plan_claim_id_for_candidate(candidate.signal_id)
    if existing_claim_id is not None:
        summary.candidate_status = "ALREADY_PROCESSED"
        summary.claim_id = existing_claim_id
        summary.cycle_status = "ALREADY_PROCESSED"
        _observe_claim_legs(send_control, existing_claim_id, summary)
        summary.outcomes_ingested = _capture_outcomes_report_only(
            outcome_control=outcome_control, send_control=send_control,
            authorization_control=authorization_control, account=args.account, deals_csv=deals_csv, now=now,
        )
        return summary

    profile: RiskProfile | None = None
    if inputs.risk_profile_path.is_file():
        profile = profile_from_dict(json.loads(inputs.risk_profile_path.read_text(encoding="utf-8")))

    result = evaluate_ser8_research_risk_gate(
        eligibility, scope, candidate,
        registry=pipeline.registry, final_verdict=pipeline.final_verdict, login=args.account,
        account_csv=inputs.account_csv, positions_csv=inputs.positions_csv, symbols_csv=inputs.symbols_csv,
        profile=profile, correlations=inputs.correlations_path, requested_risk_pct=args.requested_risk_pct,
        now=now,
    )
    summary.risk_state = result.decision.state

    if result.decision.state != "ALLOW":
        summary.candidate_status = "BLOCKED"
        summary.cycle_status = "RISK_BLOCK"
        reasons = [f"[{reason.code}] {reason.message}" for reason in result.decision.reasons]
        summary.risk_block_reason = "; ".join(reasons) if reasons else "no RiskDecision.reasons recorded"
        pipeline_module._print_block_diagnostics(result.decision, candidate=candidate, account_csv=inputs.account_csv)
        return summary

    if args.dry_run:
        # DRY-RUN: eligibility + scope + Risk Manager only. Zero
        # authorization, zero claim, zero send -- so the candidate is
        # never consumed and no terminal state is ever created.
        summary.candidate_status = "ELIGIBLE_ALLOW"
        summary.cycle_status = "DRY_RUN_WOULD_EXECUTE"
        preview = pipeline_module.Preview(
            hypothesis_id=args.hypothesis_id, candidate_signal_id=candidate.signal_id,
            symbol=candidate.symbol, action=candidate.plan.action, risk_decision_state=result.decision.state,
            authorization_id="(dry-run: not authorized)", claim_id="(dry-run: not claimed)",
            demo_account_id=args.account,
            legs=tuple(
                pipeline_module.LegPreview(
                    entry_index=order.entry_index, order_type=order.order_type, volume=order.volume,
                    price=order.planned_price,
                    stop_price=candidate.plan.stop_price, target_price=candidate.plan.targets[0],
                )
                for order in result.decision.orders
            ),
        )
        pipeline_module._print_preview(preview)
        summary.legs_total = len(result.decision.orders)
        return summary

    summary.candidate_status = "ELIGIBLE_ALLOW"

    try:
        authorization = authorization_control.authorize(eligibility, scope, candidate, result, now=now)
    except SER8ExecutionAuthorizationConflict as exc:
        summary.cycle_status = "FAIL_CLOSED_AUTHORIZATION_CONFLICT"
        summary.risk_block_reason = str(exc)
        return summary
    except SER8ExecutionAuthorizationError as exc:
        summary.cycle_status = "FAIL_CLOSED_AUTHORIZATION_DENIED"
        summary.risk_block_reason = str(exc)
        return summary
    summary.authorization_id = authorization.authorization_id

    try:
        claim = claim_control.claim(authorization, claimant_id=args.claimant_id, now=now)
    except SER8ExecutionAuthorizationClaimConflict as exc:
        summary.cycle_status = "FAIL_CLOSED_CLAIM_CONFLICT"
        summary.risk_block_reason = str(exc)
        return summary
    except SER8ExecutionAuthorizationClaimError as exc:
        summary.cycle_status = "FAIL_CLOSED_CLAIM_DENIED"
        summary.risk_block_reason = str(exc)
        return summary
    summary.claim_id = claim.claim_id

    allowlist = DemoAccountAllowlistV1(account_ids=tuple(args.demo_account_allowlist))
    try:
        demo_authorization = verify_demo_account_authorization(claim, allowlist=allowlist, now=now)
    except DemoAccountSafetyGateError as exc:
        summary.cycle_status = "FAIL_CLOSED_ACCOUNT_DENIED"
        summary.risk_block_reason = str(exc)
        return summary

    # Pure, side-effect-free -- the SAME deterministic builder ``send``
    # itself uses -- computed here only so ``execution_plan_id`` is
    # reported even if ``send`` below raises before returning a receipt
    # (the plan itself is still persisted by ``send`` before any leg is
    # attempted, so this id is authoritative either way).
    planned = build_demo_order_execution_plan(claim, result.decision, candidate, demo_authorization=demo_authorization)
    summary.execution_plan_id = planned.plan_id

    common_files_dir = Path(args.common_files_dir).expanduser()
    real_transport = FileBridgeDemoOrderTransport(
        common_files_dir=common_files_dir, login=args.account,
        poll_interval_seconds=args.transport_poll_seconds, timeout_seconds=args.transport_timeout_seconds,
    )
    real_send_control = SER8DemoOrderSendControl(registry=pipeline.registry, transport=real_transport)

    pre_leg_ids = set(real_send_control.list_leg_ids_for_claim(claim.claim_id))
    try:
        real_send_control.send(claim, result.decision, candidate, allowlist=allowlist, now=now)
        summary.cycle_status = "EXECUTION_COMPLETE"
    except SER8DemoOrderPendingError:
        summary.cycle_status = "EXECUTION_PENDING"
    except SER8DemoOrderReconciliationRequiredError as exc:
        summary.cycle_status = "EXECUTION_PENDING_RECONCILIATION"
        summary.risk_block_reason = str(exc)
    except SER8DemoOrderPartialExecutionError as exc:
        summary.cycle_status = "EXECUTION_PARTIAL"
        summary.risk_block_reason = str(exc)
    except SER8DemoOrderRejectedError as exc:
        summary.cycle_status = "EXECUTION_FAILED"
        summary.risk_block_reason = str(exc)
    except SER8DemoOrderAlreadyAttemptedError:
        summary.cycle_status = "ALREADY_PROCESSED"
    except SER8DemoOrderSendError as exc:
        summary.cycle_status = "FAIL_CLOSED_SEND_DENIED"
        summary.risk_block_reason = str(exc)
    post_leg_ids = set(real_send_control.list_leg_ids_for_claim(claim.claim_id))
    summary.broker_sends_this_cycle = len(post_leg_ids - pre_leg_ids)

    _observe_claim_legs(real_send_control, claim.claim_id, summary)
    summary.legs_newly_submitted = summary.broker_sends_this_cycle

    summary.outcomes_ingested = _capture_outcomes_report_only(
        outcome_control=outcome_control, send_control=real_send_control,
        authorization_control=authorization_control, account=args.account, deals_csv=deals_csv, now=now,
    )
    return summary


def _cycle_exit_code(cycle_status: str) -> int:
    if cycle_status in (
        "NO_ELIGIBLE_CANDIDATE", "RISK_BLOCK", "ALREADY_PROCESSED", "EXECUTION_COMPLETE",
        "EXECUTION_PENDING", "DRY_RUN_WOULD_EXECUTE",
    ):
        return 0
    return 1


def run_one_cycle_and_print(args: argparse.Namespace, *, now: datetime | None = None) -> CycleSummary:
    try:
        summary = run_one_cycle(args, now=now)
    except pipeline_module.PipelineGapError as exc:
        print(str(exc), file=sys.stderr)
        raise
    print(("DRY-RUN " if args.dry_run else "") + summary.line())
    return summary


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data-root", type=Path, default=None)
    parser.add_argument("--db", type=Path, required=True, help="HypothesisRegistry SQLite path")
    parser.add_argument("--orchestrator-db", type=Path, default=None)
    parser.add_argument("--artifact-root", type=Path, default=None)
    parser.add_argument("--hypothesis-id", required=True)
    parser.add_argument("--account", required=True, help="MT5 demo login/account id. Never defaults.")
    parser.add_argument(
        "--demo-account-allowlist", nargs="+", required=True,
        help="Explicit list of approved demo/paper account ids (no live account). --account must be a member.",
    )
    parser.add_argument("--runtime-root", type=Path, required=True, help="Canonical live candidate runtime root")
    parser.add_argument("--candidates", type=Path, default=None)
    parser.add_argument("--mt5-export-dir", type=Path, required=True)
    parser.add_argument("--sealed-holdout-path", type=Path, required=True)
    parser.add_argument("--holdout-primary-metric", required=True)
    parser.add_argument("--holdout-key-env", default=None)
    parser.add_argument("--holdout-key-id", default=None)
    parser.add_argument("--risk-profile", type=Path, required=True)
    parser.add_argument("--correlations", type=Path, default=None)
    parser.add_argument("--requested-risk-pct", type=float, default=None)
    parser.add_argument("--claimant-id", default=DEFAULT_CLAIMANT_ID)
    parser.add_argument("--common-files-dir", type=Path, required=True)
    parser.add_argument("--transport-poll-seconds", type=float, default=1.0)
    parser.add_argument("--transport-timeout-seconds", type=float, default=60.0)
    parser.add_argument("--dry-run", action="store_true", help="Preview only -- zero authorization/claim/send.")
    parser.add_argument("--once", action="store_true", help="Run exactly one cycle and exit")
    parser.add_argument("--poll-interval-seconds", type=float, default=DEFAULT_POLL_INTERVAL_SECONDS)
    parser.add_argument("--lock-file", type=Path, default=None)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    if args.poll_interval_seconds <= 0:
        print("--poll-interval-seconds must be positive", file=sys.stderr)
        return 2

    # Account fail-closed: the worker must NEVER silently default to a
    # live account, and must fail closed at startup -- every cycle,
    # including a NO_ELIGIBLE_CANDIDATE one -- if --account is not
    # explicitly present in --demo-account-allowlist.
    if args.account not in args.demo_account_allowlist:
        print(
            f"refusing to start: --account {args.account!r} is not present in "
            f"--demo-account-allowlist {list(args.demo_account_allowlist)!r}",
            file=sys.stderr,
        )
        return 2

    db_path = Path(args.db).expanduser()
    lock_path = Path(args.lock_file).expanduser() if args.lock_file else db_path.with_suffix(".autonomous.lock")

    try:
        with _LockFile(lock_path):
            if args.once:
                try:
                    summary = run_one_cycle_and_print(args)
                except (
                    pipeline_module.PipelineGapError, ResearchEligibilityError, HypothesisTradeableScopeError,
                    SER8ResearchRiskGateError, SER8DemoTradeOutcomeError,
                ) as exc:
                    print(f"cycle failed closed: {exc}", file=sys.stderr)
                    return 2
                return _cycle_exit_code(summary.cycle_status)

            print(
                f"SER8 autonomous demo execution starting -- account={args.account} "
                f"hypothesis_id={args.hypothesis_id} poll_interval_seconds={args.poll_interval_seconds} "
                f"dry_run={args.dry_run}"
            )
            while True:
                try:
                    run_one_cycle_and_print(args)
                except (
                    pipeline_module.PipelineGapError, ResearchEligibilityError, HypothesisTradeableScopeError,
                    SER8ResearchRiskGateError, SER8DemoTradeOutcomeError,
                ) as exc:
                    print(f"cycle failed closed: {exc}", file=sys.stderr)
                time.sleep(args.poll_interval_seconds)
    except _AlreadyRunningError as exc:
        print(str(exc), file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        print("SER8 autonomous demo execution stopped (KeyboardInterrupt)")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
