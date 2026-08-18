#!/usr/bin/env python3
"""SER8 Real Runtime Bootstrap + Supervised Demo Trade Runner V1.

ONE operational entry point connecting the already-closed SER8/discovery
production chain to the already-built MT5 demo order sender, using ONLY
existing, unmodified, public production APIs and ONLY the single
authoritative research lifecycle established in this repository:
``TrainTestExecutionControl`` -> ``ValidationExecutionControl`` ->
``HoldoutTriggerBridge`` -> ``FinalVerdictAcceptanceControl``. The retired,
non-authoritative ``FinalHoldoutEvaluationControlV1.evaluate`` path (and the
old ``scripts/run_real_discovery_pilot_v1.py``, which depended on it for its
terminal state transitions) is never imported or reachable from this script.

REQUIRED FLOW (each step fails closed on the FIRST genuinely missing or
invalid precondition; every genuinely missing runtime ARTIFACT -- a file,
not a state-machine precondition -- is discovered and reported together in
ONE consolidated message, never one file at a time):

    genuine persisted research/live state
      -> ACCEPTED eligibility            (present_eligible_artifact)
      -> tradeable scope match           (bind_hypothesis_tradeable_scope +
                                           verify_live_candidate_matches_scope)
      -> RiskDecision(ALLOW)             (evaluate_ser8_research_risk_gate,
                                           itself composing mt5_risk_adapter +
                                           risk_manager.evaluate_risk, unmodified)
      -> ExecutionAuthorizationV1        (SER8ExecutionAuthorizationControl)
      -> ExecutionAuthorizationClaimV1   (SER8ExecutionAuthorizationClaimControl)
      -> DemoAccountAuthorizationV1      (verify_demo_account_authorization)
      -> PREVIEW
      -> explicit human --execute + --confirm-demo-login
      -> SER8DemoOrderSendControl.send(...)

AUTO-DISCOVERED REAL RUNTIME INPUTS (never fabricated, never a test fixture):

  <data-root>/signal_intelligence_v1_16/candidates.jsonl
      One genuine SignalCandidate JSON payload per line (the exact shape
      ``trademind.signal_intelligence.candidate_from_dict`` already expects
      and every existing production consumer -- ``live_signal_runtime.py``,
      ``signal_passport_factory.py``, ``fx_signal_adapter.py`` -- already
      reads). This script never constructs a ``SignalCandidate`` itself; it
      only loads one that a real, already-running signal adapter journaled.
  <data-root>/mt5/mt5_risk_account_<login>.csv
  <data-root>/mt5/mt5_risk_positions_<login>.csv
  <data-root>/mt5/mt5_risk_symbols_<login>.csv
      The exact filename convention documented in
      ``docs/MT5_READONLY_RISK_ADAPTER_V1_18.md`` and already produced by the
      existing MT5 risk exporter, keyed by the account/login this run is for.
  <db>  (default: <data-root>/ser8_registry.db)
      The real ``HypothesisRegistry`` SQLite file. Created if absent (an
      empty registry is not fake state -- it is simply empty; every
      hypothesis-specific check below still fails closed against it).

RESEARCH-STATE ADVANCEMENT -- DELIBERATE, DOCUMENTED SCOPE BOUNDARY:

This script CAN genuinely advance an EXISTING hypothesis through the sole
authoritative lifecycle (FROZEN -> TRAIN_TESTED -> VALIDATION_PASSED ->
HOLDOUT_CONSUMED -> ACCEPTED/REJECTED_FINAL), calling nothing but
``TrainTestExecutionControl.execute``, ``ValidationExecutionControl.execute``,
``HoldoutTriggerBridge.trigger``, and ``FinalVerdictAcceptanceControl.
finalize`` -- unmodified, with no bypass -- using ONLY parameters already
committed to that hypothesis's OWN frozen manifest (never a value this
script invents). It requires the operator to supply real, already-existing
infrastructure for the steps that need it: the ORIGINAL full source dataset
used at manifest-freeze time (``--research-source-csv``, for split-plan
re-binding), a real AES-256 holdout key reachable through an environment
variable (``--holdout-key-env`` / ``--holdout-key-id``), and the path to an
ALREADY sealed-and-isolated holdout ciphertext file (``--sealed-holdout-path``,
produced out of band by the existing, unmodified ``FinalHoldoutSealer`` /
``HoldoutSealStore.mark_isolated`` -- this script never seals or isolates a
holdout itself, exactly as every other module built on this lineage assumes).

This script deliberately does NOT create a brand-new hypothesis or freeze a
brand-new manifest from raw signal data. Doing so would require this script
to choose a primary_metric/evaluation criterion for real trading research on
its own -- exactly the kind of invented trading/statistical parameter this
repository's standing rule forbids. Bootstrapping the FIRST hypothesis for a
new research question remains a separate, explicit, human-reviewed action
(structurally identical in spirit to the surviving, still-legitimate first
half of the old ``run_real_discovery_pilot_v1.py`` -- manifest freezing --
which this script does not need to re-implement or touch).
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from trademind.discovery.dataset_split_provenance import (  # noqa: E402
    DatasetProvenanceError,
    bind_split_plan_to_source,
)
from trademind.discovery.final_verdict_control import (  # noqa: E402
    FinalVerdictAcceptanceControl,
    FinalVerdictError,
)
from trademind.discovery.holdout_keys import EnvironmentKeyProvider  # noqa: E402
from trademind.discovery.holdout_runner import FinalHoldoutRunner, HoldoutRunError  # noqa: E402
from trademind.discovery.holdout_store import HoldoutSealStore  # noqa: E402
from trademind.discovery.holdout_trigger_bridge import (  # noqa: E402
    HoldoutTriggerBridge,
    HoldoutTriggerError,
)
from trademind.discovery.hypothesis_registry import (  # noqa: E402
    HypothesisRegistry,
    HypothesisState,
    RegistryError,
)
from trademind.discovery.hypothesis_tradeable_scope import (  # noqa: E402
    HypothesisTradeableScopeError,
    bind_hypothesis_tradeable_scope,
)
from trademind.discovery.orchestrator_bridge import DiscoveryOrchestratorBridge  # noqa: E402
from trademind.discovery.research_eligibility_boundary import (  # noqa: E402
    ResearchEligibilityError,
    present_eligible_artifact,
)
from trademind.discovery.result_ledger import ResultLedger  # noqa: E402
from trademind.discovery.train_test_execution import (  # noqa: E402
    SUPPORTED_TEST_FAMILIES,
    TrainTestExecutionControl,
    TrainTestExecutionError,
)
from trademind.discovery.validation_execution import (  # noqa: E402
    ValidationExecutionControl,
    ValidationExecutionError,
)
from trademind.hypothesis_live_candidate_matching import (  # noqa: E402
    verify_live_candidate_matches_scope,
)
from trademind.orchestrator.artifact_store import ArtifactStore  # noqa: E402
from trademind.risk_manager import RiskProfile, profile_from_dict  # noqa: E402
from trademind.ser8_demo_account_safety_gate import (  # noqa: E402
    DemoAccountAllowlistV1,
    DemoAccountSafetyGateError,
    verify_demo_account_authorization,
)
from trademind.ser8_execution_authorization import (  # noqa: E402
    SER8ExecutionAuthorizationControl,
    SER8ExecutionAuthorizationError,
)
from trademind.ser8_execution_authorization_claim import (  # noqa: E402
    SER8ExecutionAuthorizationClaimControl,
    SER8ExecutionAuthorizationClaimError,
)
from trademind.ser8_mt5_demo_order_send import (  # noqa: E402
    DemoOrderTransport,
    FileBridgeDemoOrderTransport,
    SER8DemoOrderSendControl,
    SER8DemoOrderSendError,
)
from trademind.ser8_research_risk_gate import (  # noqa: E402
    SER8ResearchRiskGateError,
    SER8ResearchRiskGateResult,
    evaluate_ser8_research_risk_gate,
)
from trademind.signal_intelligence import SignalCandidate, candidate_from_dict  # noqa: E402

SCHEMA_VERSION = "ser8-real-demo-pipeline-v1"
DEFAULT_HOLDOUT_KEY_ID = "ser8-holdout-key-v1"
DEFAULT_HOLDOUT_KEY_ENV = "SER8_HOLDOUT_KEY"


class PipelineGapError(RuntimeError):
    """Raised once, with every missing/invalid runtime artifact listed
    together, instead of forcing the operator through repeated
    one-file-at-a-time diagnostics."""

    def __init__(self, problems: Sequence[str]) -> None:
        self.problems = tuple(problems)
        message = "genuine runtime inputs are insufficient:\n" + "\n".join(
            f"  - {problem}" for problem in self.problems
        )
        super().__init__(message)


# ---------------------------------------------------------------------------
# Phase 0: real runtime input discovery (never fabricated, never a fixture).
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DiscoveredInputs:
    data_root: Path
    db_path: Path
    candidates_path: Path
    account_csv: Path
    positions_csv: Path
    symbols_csv: Path
    risk_profile_path: Path
    correlations_path: Path | None


def discover_inputs(
    *,
    data_root: Path,
    db_path: Path | None,
    candidates_path: Path | None,
    mt5_export_dir: Path | None,
    login: str,
    risk_profile_path: Path | None,
    correlations_path: Path | None,
    repo_root: Path,
) -> DiscoveredInputs:
    problems: list[str] = []

    resolved_db = (db_path or (data_root / "ser8_registry.db")).expanduser()

    resolved_candidates = (
        candidates_path or (data_root / "signal_intelligence_v1_16" / "candidates.jsonl")
    ).expanduser()
    if not resolved_candidates.is_file():
        problems.append(
            f"candidate journal not found: {resolved_candidates} "
            "(expected the real signal_intelligence_v1_16/candidates.jsonl journal; "
            "override with --candidates)"
        )

    mt5_dir = (mt5_export_dir or (data_root / "mt5")).expanduser()
    account_csv = mt5_dir / f"mt5_risk_account_{login}.csv"
    positions_csv = mt5_dir / f"mt5_risk_positions_{login}.csv"
    symbols_csv = mt5_dir / f"mt5_risk_symbols_{login}.csv"
    for path, label in (
        (account_csv, "MT5 account export"),
        (positions_csv, "MT5 positions export"),
        (symbols_csv, "MT5 symbols export"),
    ):
        if not path.is_file():
            problems.append(
                f"{label} not found: {path} "
                "(expected the real MT5 risk exporter output for this account/login; "
                "override with --mt5-export-dir)"
            )

    resolved_profile = (
        risk_profile_path or (repo_root / "config" / "risk_profiles" / "standard_v1.json")
    ).expanduser()
    if not resolved_profile.is_file():
        problems.append(
            f"risk profile config not found: {resolved_profile} "
            "(override with --risk-profile)"
        )

    resolved_correlations: Path | None
    if correlations_path is not None:
        resolved_correlations = correlations_path.expanduser()
        if not resolved_correlations.is_file():
            problems.append(f"correlations config not found: {resolved_correlations}")
    else:
        default_correlations = repo_root / "config" / "mt5" / "correlation_groups_v1.json"
        resolved_correlations = default_correlations if default_correlations.is_file() else None

    if problems:
        raise PipelineGapError(problems)

    return DiscoveredInputs(
        data_root=data_root,
        db_path=resolved_db,
        candidates_path=resolved_candidates,
        account_csv=account_csv,
        positions_csv=positions_csv,
        symbols_csv=symbols_csv,
        risk_profile_path=resolved_profile,
        correlations_path=resolved_correlations,
    )


def load_candidates(path: Path) -> dict[str, SignalCandidate]:
    """Load every genuine, already-journaled SignalCandidate from a real
    candidates.jsonl file. Never constructs a candidate itself -- every
    value is exactly what ``candidate_from_dict`` (the same public loader
    every production consumer of this journal already uses) returns."""
    candidates: dict[str, SignalCandidate] = {}
    text = path.read_text(encoding="utf-8-sig")
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise PipelineGapError(
                [f"{path}:{line_number}: candidate journal line is not valid JSON: {exc}"]
            ) from exc
        if not isinstance(payload, Mapping):
            raise PipelineGapError([f"{path}:{line_number}: candidate journal line must be an object"])
        candidate = candidate_from_dict(payload)
        candidates[candidate.signal_id] = candidate
    return candidates


def select_candidate(
    candidates: Mapping[str, SignalCandidate],
    *,
    scope,
    signal_id: str | None,
) -> SignalCandidate:
    if signal_id is not None:
        candidate = candidates.get(signal_id)
        if candidate is None:
            raise PipelineGapError([f"no candidate with signal_id={signal_id!r} in the candidate journal"])
        return candidate

    matching = [
        candidate
        for candidate in candidates.values()
        if verify_live_candidate_matches_scope(scope, candidate)
    ]
    if not matching:
        raise PipelineGapError(
            [
                "no journaled SignalCandidate matches this hypothesis's tradeable scope "
                f"(symbol={scope.symbol!r} timeframe={scope.timeframe!r} "
                f"setup_family={scope.setup_family!r}); pass --signal-id explicitly once one exists"
            ]
        )
    if len(matching) > 1:
        matching.sort(key=lambda item: item.observed_at, reverse=True)
    return matching[0]


# ---------------------------------------------------------------------------
# Phase 1: authoritative-lifecycle-only research pipeline object graph.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ResearchPipeline:
    registry: HypothesisRegistry
    control: Any
    artifacts: ArtifactStore
    train_test: TrainTestExecutionControl
    validator: ValidationExecutionControl
    holdout_seals: HoldoutSealStore
    ledger: ResultLedger
    runner: FinalHoldoutRunner
    trigger_bridge: HoldoutTriggerBridge
    final_verdict: FinalVerdictAcceptanceControl
    bridge: DiscoveryOrchestratorBridge


class _DeterministicAggregateHoldoutEvaluator:
    """Reuses the SAME already-vetted, predeclared
    ``train_test_execution.SUPPORTED_TEST_FAMILIES["deterministic_aggregate_v1"]``
    aggregation at the holdout phase. No new statistical methodology is
    introduced here -- this is the identical, already-authoritative
    computation, applied to the holdout plaintext CSV instead of the
    discovery/validation CSV, imported from the public
    ``SUPPORTED_TEST_FAMILIES`` mapping rather than reimplemented."""

    evaluator_id = "deterministic-aggregate-v1-holdout"

    def __init__(self, *, primary_metric: str, parameters: Mapping[str, Any]) -> None:
        self._primary_metric = primary_metric
        self._parameters = dict(parameters)

    def evaluate(self, plaintext: bytes) -> Mapping[str, int | float | bool | None]:
        text = plaintext.decode("utf-8")
        rows = [dict(row) for row in csv.DictReader(io.StringIO(text))]
        if not rows:
            raise HoldoutRunError("holdout plaintext CSV has no rows")
        runner = SUPPORTED_TEST_FAMILIES["deterministic_aggregate_v1"]
        return runner(rows, primary_metric=self._primary_metric, parameters=self._parameters)


def build_research_pipeline(
    *,
    db_path: Path,
    orchestrator_db_path: Path,
    artifact_root: Path,
    holdout_key_env: str,
    holdout_key_id: str,
    holdout_primary_metric: str,
    holdout_parameters: Mapping[str, Any],
) -> ResearchPipeline:
    """Construct the full, unmodified authoritative research-lifecycle
    object graph. Never imports or references the retired
    ``FinalHoldoutEvaluationControlV1`` lineage. Cheap and safe to construct
    even when no holdout key is currently configured -- the key is read
    only if a holdout is genuinely triggered."""
    from trademind.orchestrator.control_plane import ControlPlane

    registry = HypothesisRegistry(db_path)
    control = ControlPlane(orchestrator_db_path)
    artifacts = ArtifactStore(artifact_root)
    train_test = TrainTestExecutionControl(registry=registry, control=control, artifacts=artifacts)
    validator = ValidationExecutionControl(
        registry=registry, control=control, artifacts=artifacts, train_test=train_test
    )
    holdout_seals = HoldoutSealStore(registry)
    ledger = ResultLedger(orchestrator_db_path.with_name(orchestrator_db_path.stem + "_holdout_ledger.jsonl"))
    keys = EnvironmentKeyProvider(key_id=holdout_key_id, environment_variable=holdout_key_env)
    evaluator = _DeterministicAggregateHoldoutEvaluator(
        primary_metric=holdout_primary_metric, parameters=holdout_parameters
    )
    runner = FinalHoldoutRunner(
        registry=registry,
        seals=holdout_seals,
        keys=keys,
        ledger=ledger,
        evaluator=evaluator,
        evaluator_artifact_path=Path(__file__).resolve(),
    )
    trigger_bridge = HoldoutTriggerBridge(
        registry=registry, control=control, artifacts=artifacts, validator=validator, runner=runner
    )
    final_verdict = FinalVerdictAcceptanceControl(
        registry=registry,
        control=control,
        artifacts=artifacts,
        validator=validator,
        trigger_bridge=trigger_bridge,
    )
    bridge = DiscoveryOrchestratorBridge(
        registry=registry, holdout_seals=holdout_seals, control=control, artifacts=artifacts
    )
    return ResearchPipeline(
        registry=registry,
        control=control,
        artifacts=artifacts,
        train_test=train_test,
        validator=validator,
        holdout_seals=holdout_seals,
        ledger=ledger,
        runner=runner,
        trigger_bridge=trigger_bridge,
        final_verdict=final_verdict,
        bridge=bridge,
    )


def advance_research_state(
    pipeline: ResearchPipeline,
    hypothesis_id: str,
    *,
    research_source_csv: Path | None,
    sealed_holdout_path: Path | None,
) -> None:
    """Advance ``hypothesis_id`` through the sole authoritative lifecycle as
    far as its CURRENT state and its OWN already-frozen manifest allow.
    Calls nothing but the real, unmodified authoritative control methods,
    using only parameters already committed to the manifest. Fails closed,
    with one clear, single message, on the first missing precondition --
    never invents a manifest, a dataset, or a metric."""
    record = pipeline.registry.get(hypothesis_id)
    state = record.state

    if state in (HypothesisState.ACCEPTED, HypothesisState.REJECTED_FINAL):
        return
    if state is HypothesisState.PROPOSED:
        raise PipelineGapError(
            [
                f"hypothesis {hypothesis_id} has no frozen manifest yet (state=PROPOSED); "
                "this runner does not create or freeze a new manifest -- that remains a "
                "separate, explicit, human-reviewed research action"
            ]
        )
    if state is HypothesisState.VALIDATION_REJECTED:
        raise PipelineGapError([f"hypothesis {hypothesis_id} is VALIDATION_REJECTED; not eligible"])

    if state in (HypothesisState.FROZEN, HypothesisState.TRAIN_TESTED):
        if research_source_csv is None or not research_source_csv.is_file():
            raise PipelineGapError(
                [
                    "original research source CSV not found (required to re-derive the "
                    "provenance-bound split plan for TRAIN_TESTED/VALIDATION execution); "
                    "pass --research-source-csv pointing at the exact file used when this "
                    f"hypothesis's manifest was frozen (current value: {research_source_csv})"
                ]
            )
        manifest = pipeline.registry.load_bound_manifest_v2(
            hypothesis_id, artifact_store=pipeline.artifacts
        )
        try:
            bound_split_plan = bind_split_plan_to_source(
                str(research_source_csv), split_plan=manifest.split_plan
            )
        except DatasetProvenanceError as exc:
            raise PipelineGapError([f"research source CSV does not match the frozen manifest: {exc}"]) from exc

        task_id = DiscoveryOrchestratorBridge._task_id(hypothesis_id)
        dataset_role_bindings = {
            dataset.role: ("DISCOVERY" if dataset.role == manifest.split_dataset_role else "VALIDATION")
            for dataset in manifest.datasets
        }
        if pipeline.control.task_store.get(task_id) is None:
            pipeline.bridge.submit_frozen_hypothesis_v2(
                hypothesis_id,
                bound_split_plan=bound_split_plan,
                dataset_role_bindings=dataset_role_bindings,
            )

        if state is HypothesisState.FROZEN:
            pipeline.train_test.execute(hypothesis_id, bound_split_plan=bound_split_plan)
        pipeline.validator.execute(hypothesis_id, bound_split_plan=bound_split_plan)
        record = pipeline.registry.get(hypothesis_id)
        state = record.state

    if state is HypothesisState.VALIDATION_REJECTED:
        raise PipelineGapError([f"hypothesis {hypothesis_id} is VALIDATION_REJECTED; not eligible"])

    if state is HypothesisState.VALIDATION_PASSED:
        if sealed_holdout_path is None or not sealed_holdout_path.is_file():
            raise PipelineGapError(
                [
                    "sealed final-holdout artifact not found; pass --sealed-holdout-path "
                    "pointing at the already sealed-and-isolated holdout file produced out "
                    "of band by FinalHoldoutSealer/HoldoutSealStore.mark_isolated "
                    f"(current value: {sealed_holdout_path})"
                ]
            )
        pipeline.trigger_bridge.trigger(hypothesis_id, sealed_path=sealed_holdout_path)
        record = pipeline.registry.get(hypothesis_id)
        state = record.state

    if state is HypothesisState.HOLDOUT_CONSUMED:
        pipeline.final_verdict.finalize(hypothesis_id)


# ---------------------------------------------------------------------------
# Phase 2: demo-trade preview/execute runner.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Preview:
    hypothesis_id: str
    candidate_signal_id: str
    symbol: str
    action: str
    volume: float
    stop_price: float
    target_price: float
    risk_decision_state: str
    authorization_id: str
    claim_id: str
    demo_account_id: str
    order_type: str
    price: float


def _print_preview(preview: Preview) -> None:
    print("SER8 REAL DEMO PIPELINE -- PREVIEW (NO ORDER SENT)")
    print(f"  hypothesis_id        = {preview.hypothesis_id}")
    print(f"  candidate signal_id  = {preview.candidate_signal_id}")
    print(f"  symbol / action      = {preview.symbol} / {preview.action}")
    print(f"  volume               = {preview.volume}")
    print(f"  SL / TP              = {preview.stop_price} / {preview.target_price}")
    print(f"  RiskDecision.state   = {preview.risk_decision_state}")
    print(f"  authorization_id     = {preview.authorization_id}")
    print(f"  claim_id             = {preview.claim_id}")
    print(f"  demo account         = {preview.demo_account_id}")
    print(f"  MT5 request preview  = {preview.order_type} {preview.action} {preview.volume} {preview.symbol}"
          f" @ {preview.price if preview.order_type != 'MARKET' else 'MARKET'}")


def run_pipeline(args: argparse.Namespace) -> int:
    repo_root = Path(__file__).resolve().parent.parent
    data_root = Path(args.data_root).expanduser() if args.data_root else repo_root / "data"

    try:
        inputs = discover_inputs(
            data_root=data_root,
            db_path=Path(args.db).expanduser() if args.db else None,
            candidates_path=Path(args.candidates).expanduser() if args.candidates else None,
            mt5_export_dir=Path(args.mt5_export_dir).expanduser() if args.mt5_export_dir else None,
            login=args.account,
            risk_profile_path=Path(args.risk_profile).expanduser() if args.risk_profile else None,
            correlations_path=Path(args.correlations).expanduser() if args.correlations else None,
            repo_root=repo_root,
        )
    except PipelineGapError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    orchestrator_db = (
        Path(args.orchestrator_db).expanduser()
        if args.orchestrator_db
        else inputs.data_root / "ser8_orchestrator.db"
    )
    artifact_root = (
        Path(args.artifact_root).expanduser()
        if args.artifact_root
        else inputs.data_root / "ser8_artifacts"
    )

    try:
        pipeline = build_research_pipeline(
            db_path=inputs.db_path,
            orchestrator_db_path=orchestrator_db,
            artifact_root=artifact_root,
            holdout_key_env=args.holdout_key_env or DEFAULT_HOLDOUT_KEY_ENV,
            holdout_key_id=args.holdout_key_id or DEFAULT_HOLDOUT_KEY_ID,
            holdout_primary_metric=args.holdout_primary_metric or "",
            holdout_parameters={},
        )

        try:
            advance_research_state(
                pipeline,
                args.hypothesis_id,
                research_source_csv=(
                    Path(args.research_source_csv).expanduser() if args.research_source_csv else None
                ),
                sealed_holdout_path=(
                    Path(args.sealed_holdout_path).expanduser() if args.sealed_holdout_path else None
                ),
            )
        except KeyError as exc:
            raise PipelineGapError([f"hypothesis does not exist in the registry: {exc}"]) from exc
        except (
            RegistryError,
            TrainTestExecutionError,
            ValidationExecutionError,
            HoldoutTriggerError,
            FinalVerdictError,
        ) as exc:
            raise PipelineGapError([f"authoritative research-state advancement failed: {exc}"]) from exc

        eligibility = present_eligible_artifact(
            args.hypothesis_id, registry=pipeline.registry, final_verdict=pipeline.final_verdict
        )
        scope = bind_hypothesis_tradeable_scope(
            args.hypothesis_id, registry=pipeline.registry, artifact_store=pipeline.artifacts
        )

        candidates = load_candidates(inputs.candidates_path)
        candidate = select_candidate(candidates, scope=scope, signal_id=args.signal_id)

        profile: RiskProfile | None = None
        if inputs.risk_profile_path.is_file():
            profile = profile_from_dict(json.loads(inputs.risk_profile_path.read_text(encoding="utf-8")))

        result: SER8ResearchRiskGateResult = evaluate_ser8_research_risk_gate(
            eligibility,
            scope,
            candidate,
            registry=pipeline.registry,
            final_verdict=pipeline.final_verdict,
            login=args.account,
            account_csv=inputs.account_csv,
            positions_csv=inputs.positions_csv,
            symbols_csv=inputs.symbols_csv,
            profile=profile,
            correlations=inputs.correlations_path,
            requested_risk_pct=args.requested_risk_pct,
        )

        if result.decision.state != "ALLOW":
            print(
                f"RiskDecision.state={result.decision.state!r}; not ALLOW, refusing to authorize execution.",
                file=sys.stderr,
            )
            return 3

        authorization_control = SER8ExecutionAuthorizationControl(
            registry=pipeline.registry, final_verdict=pipeline.final_verdict
        )
        authorization = authorization_control.authorize(eligibility, scope, candidate, result)

        claim_control = SER8ExecutionAuthorizationClaimControl(registry=pipeline.registry)
        claim = claim_control.claim(authorization, claimant_id=args.claimant_id)

        allowlist = DemoAccountAllowlistV1(account_ids=tuple(args.demo_account_allowlist))
        demo_authorization = verify_demo_account_authorization(claim, allowlist=allowlist)

        order = result.decision.orders[0]
        preview = Preview(
            hypothesis_id=args.hypothesis_id,
            candidate_signal_id=candidate.signal_id,
            symbol=candidate.symbol,
            action=candidate.plan.action,
            volume=order.volume,
            stop_price=candidate.plan.stop_price,
            target_price=candidate.plan.targets[0],
            risk_decision_state=result.decision.state,
            authorization_id=authorization.authorization_id,
            claim_id=claim.claim_id,
            demo_account_id=demo_authorization.account_id,
            order_type=order.order_type,
            price=order.planned_price,
        )
        _print_preview(preview)

        if not args.execute:
            return 0

        if args.confirm_demo_login != args.account:
            print(
                "refusing to execute: --confirm-demo-login must exactly match --account "
                f"({args.confirm_demo_login!r} != {args.account!r})",
                file=sys.stderr,
            )
            return 4

        common_files_dir = (
            Path(args.common_files_dir).expanduser()
            if args.common_files_dir
            else inputs.data_root / "mt5_common"
        )
        transport: DemoOrderTransport = FileBridgeDemoOrderTransport(
            common_files_dir=common_files_dir,
            login=args.account,
            poll_interval_seconds=args.transport_poll_seconds,
            timeout_seconds=args.transport_timeout_seconds,
        )
        send_control = SER8DemoOrderSendControl(registry=pipeline.registry, transport=transport)
        receipt = send_control.send(claim, result.decision, candidate, allowlist=allowlist)
        print("SER8 REAL DEMO PIPELINE -- ORDER SENT")
        print(f"  result_state = {receipt.result_state}")
        print(f"  order_ticket = {receipt.order_ticket}")
        print(f"  deal_ticket  = {receipt.deal_ticket}")
        print(f"  filled       = {receipt.filled_volume} @ {receipt.filled_price}")
        return 0
    except PipelineGapError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except (
        ResearchEligibilityError,
        HypothesisTradeableScopeError,
        SER8ResearchRiskGateError,
        SER8ExecutionAuthorizationError,
        SER8ExecutionAuthorizationClaimError,
        DemoAccountSafetyGateError,
        SER8DemoOrderSendError,
    ) as exc:
        print(f"pipeline denied: {exc}", file=sys.stderr)
        return 5


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, default=None, help="Real runtime data root (default: repo data/)")
    parser.add_argument("--db", type=Path, default=None, help="HypothesisRegistry SQLite path")
    parser.add_argument("--orchestrator-db", type=Path, default=None)
    parser.add_argument("--artifact-root", type=Path, default=None)
    parser.add_argument("--hypothesis-id", required=True)
    parser.add_argument("--account", required=True, help="MT5 demo login/account id")
    parser.add_argument("--candidates", type=Path, default=None)
    parser.add_argument("--signal-id", default=None)
    parser.add_argument("--mt5-export-dir", type=Path, default=None)
    parser.add_argument("--risk-profile", type=Path, default=None)
    parser.add_argument("--correlations", type=Path, default=None)
    parser.add_argument("--requested-risk-pct", type=float, default=None)
    parser.add_argument("--claimant-id", default="operator:run-ser8-real-demo-pipeline")
    parser.add_argument(
        "--demo-account-allowlist",
        nargs="+",
        required=True,
        help="Explicit list of approved demo/paper account ids (no live account).",
    )
    parser.add_argument("--research-source-csv", type=Path, default=None)
    parser.add_argument("--sealed-holdout-path", type=Path, default=None)
    parser.add_argument("--holdout-key-env", default=None)
    parser.add_argument("--holdout-key-id", default=None)
    parser.add_argument("--holdout-primary-metric", default=None)
    parser.add_argument("--common-files-dir", type=Path, default=None)
    parser.add_argument("--transport-poll-seconds", type=float, default=1.0)
    parser.add_argument("--transport-timeout-seconds", type=float, default=60.0)
    parser.add_argument("--execute", action="store_true", help="Actually send the order. Default is PREVIEW ONLY.")
    parser.add_argument("--confirm-demo-login", default=None, help="Must exactly equal --account to allow --execute.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    return run_pipeline(args)


if __name__ == "__main__":
    raise SystemExit(main())
