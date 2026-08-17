"""Tests for Validation Execution Control V1: the second missing bridge,
from a TRAIN_TESTED hypothesis to VALIDATION_PASSED / VALIDATION_REJECTED.

Chain exercised: ResearchProposalIntake -> ACCEPTED_FOR_HYPOTHESIS ->
PROPOSED hypothesis -> ResearchExperimentSpecificationV1 ->
ExperimentManifestV2 (two dataset entries: one genuinely DISCOVERY-range,
one genuinely VALIDATION-range) -> HypothesisRegistry.
freeze_manifest_v2_in_transaction -> DiscoveryOrchestratorBridge.
submit_frozen_hypothesis_v2 (Orchestrator task) -> TrainTestExecutionControl.
execute() -> TRAIN_TESTED -> ValidationExecutionControl.execute() -> verified
VALIDATION dataset -> real bounded evidence -> VALIDATION_PASSED /
VALIDATION_REJECTED.

This file does not import test helpers from the sibling
test_orchestrator_bridge_v2.py / test_train_test_execution.py files
(consistent with this lineage's convention of each test file owning its own
small setup helpers).
"""

from __future__ import annotations

import ast
import io
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from trademind.discovery.dataset_split_provenance import (
    BoundSplitPlanV1,
    bind_split_plan_to_source,
)
from trademind.discovery.holdout_store import HoldoutSealStore
from trademind.discovery.hypothesis_registry import HypothesisRegistry, HypothesisState
from trademind.discovery.manifest import (
    CriteriaMode,
    CriterionOperator,
    DatasetArtifactV2,
    EvaluationCriteriaV1,
    EvaluationCriterionV1,
    ExperimentManifestV2,
    ProposalIntakeProvenanceV1,
    build_experiment_manifest_v2,
    persist_experiment_manifest_v2,
)
from trademind.discovery.orchestrator_bridge import DiscoveryOrchestratorBridge
from trademind.discovery.split_engine import SplitPlan, chronological_split
from trademind.discovery.train_test_execution import TrainTestExecutionControl
from trademind.discovery.validation_execution import (
    ValidationEvidenceV1,
    ValidationExecutionControl,
    ValidationExecutionError,
)
from trademind.orchestrator.artifact_store import ArtifactStore
from trademind.orchestrator.budget import BudgetManager
from trademind.orchestrator.control_plane import ControlPlane
from trademind.orchestrator.models import RiskClass, Task
from trademind.research_execution import ResearchExecutionControl
from trademind.research_experiment_specification import (
    ResearchExperimentSpecificationControl,
    ResearchExperimentSpecificationV1,
)
from trademind.research_proposal_intake import ResearchProposalIntakeControl
from trademind.research_proposal_response import (
    RESEARCH_PROPOSAL_RESPONSE_KIND,
    RESEARCH_PROPOSAL_RESPONSE_SCHEMA_VERSION,
    ResearchProposalResponseV1,
)
from trademind.signal_statistics_agent_packet import (
    SignalStatisticsPacketV2,
    build_packet_v2_from_artifact,
    persist_packet_v2,
)
from trademind.signal_statistics_orchestrator_bridge import register_verified_packet_v2_task
from trademind.signal_statistics_provenance import (
    CandidateContentV2,
    CandidateDefinitionV2,
    CodeProvenance,
)
from trademind.signal_statistics_report import build_report_v2, persist_report_v2

_POLICY_HASH = f"sha256:{'2' * 64}"
_SOURCE_HASH = f"sha256:{'3' * 64}"
_DISCOVERY_ROLE = "market-data"
_VALIDATION_ROLE = "market-data-validation"
_TEST_FAMILY = "deterministic_aggregate_v1"
_METRIC = "avg_net_atr"


@dataclass(frozen=True, slots=True)
class _Context:
    db_path: Path
    artifact_root: Path
    store: ArtifactStore
    control: ControlPlane
    registry: HypothesisRegistry
    holdout_seals: HoldoutSealStore
    bridge: DiscoveryOrchestratorBridge
    train_test: TrainTestExecutionControl
    validator: ValidationExecutionControl
    spec: ResearchExperimentSpecificationV1
    hypothesis_id: str


def _candidate(symbol: str = "XAUUSD") -> CandidateContentV2:
    return CandidateContentV2(
        candidate_definition=CandidateDefinitionV2(
            source_kind="signal_journal",
            source_namespace="trademind_signal_journal",
            symbol=symbol,
            timeframe="M5",
            feature="spread_pressure",
            horizon=3,
            action_scope="BUY_SELL_DIRECTIONAL",
            evaluation_method_version="signal-statistics-v2",
        ),
        evaluation_policy_hash=_POLICY_HASH,
        metrics={"trades": 24, "win_rate": 0.5},
        status="RESEARCH_CANDIDATE",
        reason_codes=("BELOW_RESEARCH_MINIMUM",),
    )


def _manager(path: Path) -> BudgetManager:
    return BudgetManager(
        path,
        daily_cost_ceiling=100.0,
        monthly_cost_ceiling=100.0,
        daily_token_ceiling=100_000,
        monthly_token_ceiling=100_000,
        per_task_call_limit=8,
        per_role_call_limit=32,
    )


def _response(packet: SignalStatisticsPacketV2) -> ResearchProposalResponseV1:
    candidate_id = packet.candidate_bindings[0]["candidate_id"]
    return ResearchProposalResponseV1.from_payload(
        {
            "schema_version": RESEARCH_PROPOSAL_RESPONSE_SCHEMA_VERSION,
            "response_kind": RESEARCH_PROPOSAL_RESPONSE_KIND,
            "proposals": [
                {
                    "candidate_id": candidate_id,
                    "title": "Regime-conditioned continuation",
                    "rationale": "The candidate may depend on volatility regime.",
                    "falsifiable_claim": "The effect remains positive in a predefined regime.",
                    "proposed_test": "Compare predefined public-data subsets for the regime.",
                    "rejection_condition": "Reject if the regime effect is non-positive.",
                    "confidence": "HIGH",
                }
            ],
        }
    )


def _code_provenance() -> CodeProvenance:
    return CodeProvenance(
        producer_name="trademind",
        producer_version="validation-execution-test",
        git_commit="f" * 40,
        revision_source="git_worktree",
    )


def _timestamps(count: int = 12) -> list[datetime]:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return [start + timedelta(hours=i) for i in range(count)]


def _csv_bytes(rows: list[datetime], *, base: float = 10.0) -> bytes:
    lines = [f"time,{_METRIC}"] + [f"{t.isoformat()},{base + i}" for i, t in enumerate(rows)]
    return ("\n".join(lines) + "\n").encode("utf-8")


def _setup(tmp_path: Path, *, symbol: str = "XAUUSD", test_family: str = _TEST_FAMILY) -> _Context:
    db_path = tmp_path / "orchestrator.db"
    artifact_root = tmp_path / "artifacts"
    store = ArtifactStore(artifact_root)
    control = ControlPlane(db_path)
    budget = _manager(db_path)
    report = build_report_v2(
        (_candidate(symbol),),
        source_snapshot_hash_ref=_SOURCE_HASH,
        source_schema_version="1.1",
        code_provenance=_code_provenance(),
        journal_rows=24,
        generated_at="2026-08-14T12:00:00+00:00",
    )
    report_ref = persist_report_v2(report, artifact_store=store)
    packet = build_packet_v2_from_artifact(report_ref.hash_ref, artifact_store=store)
    packet_ref = persist_packet_v2(packet, artifact_store=store)
    task = register_verified_packet_v2_task(
        packet_ref.hash_ref, control_plane=control, artifact_store=store
    )
    execution_control = ResearchExecutionControl(
        control_plane=control, budget_manager=budget, artifact_store=store
    )
    authorization = execution_control.create_authorization(
        task_id=task.task_id,
        task_revision=task.revision,
        reserved_cost=1.0,
        reserved_tokens=100,
        authorized_by="operator:execution",
    )
    execution = execution_control.claim_execution(authorization.authorization_id)
    execution = execution_control.mark_call_in_flight(execution.request_hash)
    execution = execution_control.finalize_success(
        execution.request_hash,
        response=_response(packet),
        actual_cost=0.5,
        actual_tokens=50,
    )
    registry = HypothesisRegistry(db_path)
    intake_control = ResearchProposalIntakeControl(
        execution_control=execution_control, hypothesis_registry=registry
    )
    spec_control = ResearchExperimentSpecificationControl(
        intake_control=intake_control, hypothesis_registry=registry
    )
    pending = intake_control.ingest_succeeded_research_execution_v1(execution.request_hash)[0]
    accepted, _hypothesis = intake_control.accept_for_hypothesis(
        pending.intake_id, reviewer_id="operator:reviewer"
    )
    from trademind.discovery.manifest import DatasetArtifact as DatasetArtifactV1

    dataset_file = tmp_path / "spec_dataset.csv"
    dataset_file.write_text("time,symbol,close\n1,XAUUSD,2000.0\n", encoding="utf-8")
    v1_dataset = DatasetArtifactV1.from_path(dataset_file)
    spec = spec_control.create_specification(
        accepted.intake_id,
        reviewer_id="operator:spec-reviewer",
        test_family=test_family,
        primary_metric=_METRIC,
        alpha=0.05,
        q=0.10,
        minimum_effect_size=0.0,
        max_hypotheses_tests=1,
        datasets=(v1_dataset,),
        parameters={"horizon": 12},
    )
    holdout_seals = HoldoutSealStore(registry)
    bridge = DiscoveryOrchestratorBridge(
        registry=registry, holdout_seals=holdout_seals, control=control, artifacts=store
    )
    train_test = TrainTestExecutionControl(registry=registry, control=control, artifacts=store)
    validator = ValidationExecutionControl(
        registry=registry, control=control, artifacts=store, train_test=train_test
    )
    return _Context(
        db_path=db_path,
        artifact_root=artifact_root,
        store=store,
        control=control,
        registry=registry,
        holdout_seals=holdout_seals,
        bridge=bridge,
        train_test=train_test,
        validator=validator,
        spec=spec,
        hypothesis_id=spec.hypothesis_id,
    )


def _bound_split_plan(
    tmp_path: Path, rows: list[datetime] | None = None
) -> tuple[list[datetime], SplitPlan, BoundSplitPlanV1]:
    rows = rows or _timestamps()
    plan = chronological_split(rows)
    source_path = tmp_path / "full_source.csv"
    source_path.write_bytes(_csv_bytes(rows))
    bound = bind_split_plan_to_source(str(source_path), split_plan=plan)
    return rows, plan, bound


def _discovery_dataset_v2(context: _Context, rows: list[datetime], plan: SplitPlan) -> DatasetArtifactV2:
    discovery_rows = rows[: plan.discovery_count]
    artifact = context.store.import_snapshot(io.BytesIO(_csv_bytes(discovery_rows)), media_type="text/csv")
    return DatasetArtifactV2(
        role=_DISCOVERY_ROLE, artifact_hash_ref=artifact.hash_ref, media_type=artifact.media_type, size_bytes=artifact.size_bytes
    )


def _validation_dataset_v2(context: _Context, rows: list[datetime], plan: SplitPlan, *, base: float = 10.0) -> DatasetArtifactV2:
    validation_rows = rows[plan.discovery_count : plan.discovery_count + plan.validation_count]
    artifact = context.store.import_snapshot(io.BytesIO(_csv_bytes(validation_rows, base=base)), media_type="text/csv")
    return DatasetArtifactV2(
        role=_VALIDATION_ROLE, artifact_hash_ref=artifact.hash_ref, media_type=artifact.media_type, size_bytes=artifact.size_bytes
    )


def _build_manifest_v2(
    context: _Context,
    *,
    datasets: tuple[DatasetArtifactV2, ...],
    split_plan: SplitPlan,
    threshold: float = 0.0,
    operator: CriterionOperator = CriterionOperator.GREATER_THAN_OR_EQUAL,
) -> ExperimentManifestV2:
    spec = context.spec
    provenance = ProposalIntakeProvenanceV1(
        intake_id=spec.intake_id,
        execution_request_hash=spec.request_hash,
        authorization_id=spec.authorization_id,
        task_id=spec.task_id,
        task_revision=spec.task_revision,
        packet_artifact_hash_ref=spec.packet_artifact_hash_ref,
        packet_semantic_hash=spec.packet_semantic_hash,
        result_artifact_hash_ref=spec.result_artifact_hash_ref,
        proposal_index=spec.proposal_index,
        candidate_id=spec.candidate_id,
    )
    criteria = EvaluationCriteriaV1(
        mode=CriteriaMode.ALL,
        criteria=(
            EvaluationCriterionV1(metric=spec.primary_metric, operator=operator, threshold=threshold),
        ),
    )
    return build_experiment_manifest_v2(
        artifact_store=context.store,
        hypothesis_id=spec.hypothesis_id,
        hypothesis_family_id=spec.hypothesis_family_id,
        bound_hypothesis_content_hash=spec.hypothesis_content_hash,
        proposal_provenance=provenance,
        datasets=datasets,
        split_plan=split_plan,
        split_dataset_role=datasets[0].role,
        test_family=spec.test_family,
        primary_metric=spec.primary_metric,
        evaluation_criteria=criteria,
        alpha=spec.alpha,
        q=spec.q,
        minimum_effect_size=spec.minimum_effect_size,
        max_hypotheses_tests=spec.max_hypotheses_tests,
        trading_friction=None,
        deterministic_seed=None,
        code_provenance=_code_provenance(),
        semantic_parameters=spec.parameters,
        created_at="2026-08-17T00:00:00+00:00",
        created_by="operator:validation-execution-test",
    )


def _freeze_v2(context: _Context, manifest: ExperimentManifestV2):
    artifact = persist_experiment_manifest_v2(manifest, artifact_store=context.store)
    db = sqlite3.connect(context.db_path, timeout=30.0)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    try:
        db.execute("BEGIN IMMEDIATE")
        result = context.registry.freeze_manifest_v2_in_transaction(
            db, manifest_artifact_hash_ref=artifact.hash_ref, artifact_store=context.store
        )
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _attest_holdout(context: _Context) -> None:
    context.holdout_seals.register(
        hypothesis_id=context.hypothesis_id,
        envelope_hash="a" * 64,
        key_id="validation-execution-key",
        evaluator_id=_TEST_FAMILY,
        evaluator_hash="b" * 64,
    )
    context.holdout_seals.mark_isolated(
        context.hypothesis_id,
        isolation_receipt_hash="c" * 64,
        public_max_time="2026-01-02T12:00:00+00:00",
        holdout_start_time="2026-01-03T00:00:00+00:00",
        holdout_end_time="2026-01-04T00:00:00+00:00",
        public_row_count=2,
        holdout_row_count=2,
    )


def _create_matching_task(context: _Context) -> None:
    """Create the exact deterministic Orchestrator task
    ``ValidationExecutionControl`` requires, WITHOUT going through
    ``DiscoveryOrchestratorBridge.submit_frozen_hypothesis_v2`` -- which
    would itself already re-verify every declared dataset's content and
    reject a forged one before a task could ever exist. This isolates the
    property under test: that the executor performs its OWN independent
    dataset re-verification rather than merely trusting that a task's
    existence implies the Bridge already checked it."""
    task_id = DiscoveryOrchestratorBridge._task_id(context.hypothesis_id)
    task = Task.new(
        task_id=task_id,
        goal="test-only: matching task created without Bridge dataset verification",
        scope=("src/trademind/discovery",),
        risk_class=RiskClass.LOW,
    )
    context.control.create_task(task)


def _full_pipeline(
    tmp_path: Path,
    *,
    test_family: str = _TEST_FAMILY,
    threshold: float = 0.0,
    operator: CriterionOperator = CriterionOperator.GREATER_THAN_OR_EQUAL,
    include_validation_dataset: bool = True,
    attest: bool = True,
    run_train_test: bool = True,
) -> tuple[_Context, list[datetime], SplitPlan, BoundSplitPlanV1, DatasetArtifactV2]:
    context = _setup(tmp_path, test_family=test_family)
    rows, plan, bound = _bound_split_plan(tmp_path)
    discovery_dataset = _discovery_dataset_v2(context, rows, plan)
    datasets = (discovery_dataset,)
    if include_validation_dataset:
        datasets = (discovery_dataset, _validation_dataset_v2(context, rows, plan))
    manifest = _build_manifest_v2(
        context, datasets=datasets, split_plan=plan, threshold=threshold, operator=operator
    )
    _freeze_v2(context, manifest)
    if attest:
        _attest_holdout(context)
    context.bridge.submit_frozen_hypothesis_v2(
        context.hypothesis_id,
        bound_split_plan=bound,
        dataset_role_bindings={
            dataset.role: ("DISCOVERY" if dataset.role == _DISCOVERY_ROLE else "VALIDATION")
            for dataset in datasets
        },
    )
    if run_train_test:
        context.train_test.execute(context.hypothesis_id, bound_split_plan=bound)
    return context, rows, plan, bound, discovery_dataset


# ---------------------------------------------------------------------------
# 1: real TRAIN_TESTED V2 hypothesis + valid VALIDATION data -> PASSED.
# ---------------------------------------------------------------------------


def test_real_train_tested_hypothesis_with_valid_validation_data_passes(tmp_path: Path) -> None:
    context, rows, plan, bound, dataset = _full_pipeline(tmp_path, threshold=0.0)
    assert context.registry.get(context.hypothesis_id).state is HypothesisState.TRAIN_TESTED

    evidence = context.validator.execute(context.hypothesis_id, bound_split_plan=bound)

    assert isinstance(evidence, ValidationEvidenceV1)
    assert evidence.verdict == HypothesisState.VALIDATION_PASSED.value
    assert evidence.dataset_split_membership.role == "VALIDATION"
    assert evidence.metrics["row_count"] == plan.validation_count
    assert evidence.criteria_results[0]["passed"] is True
    assert context.registry.get(context.hypothesis_id).state is HypothesisState.VALIDATION_PASSED


# ---------------------------------------------------------------------------
# 2: criteria failure -> VALIDATION_REJECTED.
# ---------------------------------------------------------------------------


def test_criteria_failure_rejects_validation(tmp_path: Path) -> None:
    # Validation rows carry avg_net_atr values around 10..N; an impossibly
    # high threshold guarantees the single ALL-mode criterion fails.
    context, rows, plan, bound, dataset = _full_pipeline(tmp_path, threshold=1_000_000.0)

    evidence = context.validator.execute(context.hypothesis_id, bound_split_plan=bound)

    assert evidence.verdict == HypothesisState.VALIDATION_REJECTED.value
    assert evidence.criteria_results[0]["passed"] is False
    assert context.registry.get(context.hypothesis_id).state is HypothesisState.VALIDATION_REJECTED
    # Terminal per existing registry rules: family is now terminal, and any
    # further transition attempt (even a legitimate-looking one) is refused.
    family = context.registry.family_status(context.registry.get(context.hypothesis_id).hypothesis_family_id)
    assert family["terminal_state"] == HypothesisState.VALIDATION_REJECTED.value


# ---------------------------------------------------------------------------
# 3: non-TRAIN_TESTED rejected.
# ---------------------------------------------------------------------------


def test_non_train_tested_hypothesis_rejected(tmp_path: Path) -> None:
    context, rows, plan, bound, dataset = _full_pipeline(tmp_path, run_train_test=False)
    assert context.registry.get(context.hypothesis_id).state is HypothesisState.FROZEN
    with pytest.raises(ValidationExecutionError, match="must be TRAIN_TESTED"):
        context.validator.execute(context.hypothesis_id, bound_split_plan=bound)


# ---------------------------------------------------------------------------
# 4: missing/tampered TrainTestEvidence rejected.
# ---------------------------------------------------------------------------


def test_missing_train_test_evidence_rejected(tmp_path: Path) -> None:
    context, rows, plan, bound, dataset = _full_pipeline(tmp_path)
    with sqlite3.connect(context.db_path) as db:
        db.execute(
            "DELETE FROM train_test_evidence WHERE hypothesis_id=?", (context.hypothesis_id,)
        )
    with pytest.raises(ValidationExecutionError, match="train/test evidence could not be verified"):
        context.validator.execute(context.hypothesis_id, bound_split_plan=bound)


def test_tampered_train_test_evidence_rejected(tmp_path: Path) -> None:
    context, rows, plan, bound, dataset = _full_pipeline(tmp_path)
    with sqlite3.connect(context.db_path) as db:
        row = db.execute(
            "SELECT evidence_artifact_hash_ref FROM train_test_evidence WHERE hypothesis_id=?",
            (context.hypothesis_id,),
        ).fetchone()
    evidence_ref = row[0]
    object_path = Path(context.store.resolve_verified(evidence_ref).path)
    object_path.write_bytes(b'{"tampered": true}')
    with pytest.raises(ValidationExecutionError, match="train/test evidence could not be verified"):
        context.validator.execute(context.hypothesis_id, bound_split_plan=bound)


# ---------------------------------------------------------------------------
# 5: wrong manifest binding rejected.
# ---------------------------------------------------------------------------


def test_wrong_manifest_binding_rejected(tmp_path: Path) -> None:
    context, rows, plan, bound, dataset = _full_pipeline(tmp_path)
    with sqlite3.connect(context.db_path) as db:
        db.execute(
            "UPDATE hypotheses SET content_hash=? WHERE hypothesis_id=?",
            ("0" * 64, context.hypothesis_id),
        )
    with pytest.raises(ValidationExecutionError, match="does not match registry identities"):
        context.validator.execute(context.hypothesis_id, bound_split_plan=bound)


# ---------------------------------------------------------------------------
# 6: DISCOVERY dataset rejected as validation input.
# ---------------------------------------------------------------------------


def test_discovery_only_manifest_rejected_as_validation_input(tmp_path: Path) -> None:
    context, rows, plan, bound, dataset = _full_pipeline(tmp_path, include_validation_dataset=False)
    with pytest.raises(ValidationExecutionError, match="no dataset declared by the manifest verifies"):
        context.validator.execute(context.hypothesis_id, bound_split_plan=bound)
    assert context.registry.get(context.hypothesis_id).state is HypothesisState.TRAIN_TESTED


# ---------------------------------------------------------------------------
# 7: full unsplit/holdout-containing dataset rejected.
# ---------------------------------------------------------------------------


def test_full_unsplit_dataset_rejected_as_validation_input(tmp_path: Path) -> None:
    context = _setup(tmp_path)
    rows, plan, bound = _bound_split_plan(tmp_path)
    discovery_dataset = _discovery_dataset_v2(context, rows, plan)
    full_artifact = context.store.import_snapshot(io.BytesIO(_csv_bytes(rows)), media_type="text/csv")
    full_dataset = DatasetArtifactV2(
        role=_VALIDATION_ROLE, artifact_hash_ref=full_artifact.hash_ref, media_type=full_artifact.media_type, size_bytes=full_artifact.size_bytes
    )
    manifest = _build_manifest_v2(context, datasets=(discovery_dataset, full_dataset), split_plan=plan)
    _freeze_v2(context, manifest)
    _attest_holdout(context)
    _create_matching_task(context)
    context.train_test.execute(context.hypothesis_id, bound_split_plan=bound)
    with pytest.raises(ValidationExecutionError, match="no dataset declared by the manifest verifies"):
        context.validator.execute(context.hypothesis_id, bound_split_plan=bound)
    assert context.registry.get(context.hypothesis_id).state is HypothesisState.TRAIN_TESTED


# ---------------------------------------------------------------------------
# 8: tampered VALIDATION artifact rejected.
# ---------------------------------------------------------------------------


def test_tampered_validation_artifact_rejected(tmp_path: Path) -> None:
    context, rows, plan, bound, dataset = _full_pipeline(tmp_path)
    # Locate the validation dataset artifact through the manifest itself.
    manifest = context.validator._load_verified_manifest_past_frozen(context.hypothesis_id)
    validation_entry = next(item for item in manifest.datasets if item.role == _VALIDATION_ROLE)
    object_path = Path(context.store.resolve_verified(validation_entry.artifact_hash_ref).path)
    object_path.write_bytes(b"tampered-not-the-authoritative-validation-dataset")
    # Tampering is caught as early as manifest-binding re-verification (which
    # re-verifies every declared dataset's CAS bytes, including this one) --
    # an equally valid, equally fail-closed outcome to catching it later at
    # the dedicated VALIDATION-content re-verification step.
    with pytest.raises(ValidationExecutionError, match="could not be verified"):
        context.validator.execute(context.hypothesis_id, bound_split_plan=bound)
    assert context.registry.get(context.hypothesis_id).state is HypothesisState.TRAIN_TESTED


# ---------------------------------------------------------------------------
# 9: missing metric rejected/fails closed.
# ---------------------------------------------------------------------------


def test_missing_metric_fails_closed(tmp_path: Path) -> None:
    context = _setup(tmp_path)
    rows, plan, bound = _bound_split_plan(tmp_path)
    discovery_dataset = _discovery_dataset_v2(context, rows, plan)
    # A criterion metric name that the deterministic_aggregate_v1 test family
    # will never produce (it only ever emits "row_count" and, conditionally,
    # the primary_metric column mean).
    validation_rows = rows[plan.discovery_count : plan.discovery_count + plan.validation_count]
    validation_bytes = ("time\n" + "\n".join(t.isoformat() for t in validation_rows) + "\n").encode()
    validation_artifact = context.store.import_snapshot(io.BytesIO(validation_bytes), media_type="text/csv")
    validation_dataset = DatasetArtifactV2(
        role=_VALIDATION_ROLE, artifact_hash_ref=validation_artifact.hash_ref, media_type=validation_artifact.media_type, size_bytes=validation_artifact.size_bytes
    )
    spec = context.spec
    provenance = ProposalIntakeProvenanceV1(
        intake_id=spec.intake_id, execution_request_hash=spec.request_hash, authorization_id=spec.authorization_id,
        task_id=spec.task_id, task_revision=spec.task_revision, packet_artifact_hash_ref=spec.packet_artifact_hash_ref,
        packet_semantic_hash=spec.packet_semantic_hash, result_artifact_hash_ref=spec.result_artifact_hash_ref,
        proposal_index=spec.proposal_index, candidate_id=spec.candidate_id,
    )
    # The manifest requires evaluation_criteria to predeclare primary_metric
    # itself (ExperimentManifestV2.__post_init__), so the criterion targets
    # primary_metric -- but the VALIDATION CSV below deliberately has no
    # column with that name, so deterministic_aggregate_v1 never produces it.
    criteria = EvaluationCriteriaV1(
        mode=CriteriaMode.ALL,
        criteria=(EvaluationCriterionV1(metric=spec.primary_metric, operator=CriterionOperator.GREATER_THAN_OR_EQUAL, threshold=0.0),),
    )
    manifest = build_experiment_manifest_v2(
        artifact_store=context.store, hypothesis_id=spec.hypothesis_id, hypothesis_family_id=spec.hypothesis_family_id,
        bound_hypothesis_content_hash=spec.hypothesis_content_hash, proposal_provenance=provenance,
        datasets=(discovery_dataset, validation_dataset), split_plan=plan, split_dataset_role=discovery_dataset.role,
        test_family=spec.test_family, primary_metric=spec.primary_metric, evaluation_criteria=criteria,
        alpha=spec.alpha, q=spec.q, minimum_effect_size=spec.minimum_effect_size, max_hypotheses_tests=spec.max_hypotheses_tests,
        trading_friction=None, deterministic_seed=None, code_provenance=_code_provenance(),
        semantic_parameters=spec.parameters, created_at="2026-08-17T00:00:00+00:00", created_by="operator:missing-metric-test",
    )
    _freeze_v2(context, manifest)
    _attest_holdout(context)
    context.bridge.submit_frozen_hypothesis_v2(
        context.hypothesis_id, bound_split_plan=bound,
        dataset_role_bindings={discovery_dataset.role: "DISCOVERY", validation_dataset.role: "VALIDATION"},
    )
    context.train_test.execute(context.hypothesis_id, bound_split_plan=bound)
    with pytest.raises(ValidationExecutionError, match="missing required metric"):
        context.validator.execute(context.hypothesis_id, bound_split_plan=bound)
    assert context.registry.get(context.hypothesis_id).state is HypothesisState.TRAIN_TESTED


# ---------------------------------------------------------------------------
# 10: NaN/Inf rejected.
# ---------------------------------------------------------------------------


def test_nan_metric_value_rejected(tmp_path: Path) -> None:
    context, rows, plan, bound, dataset = _full_pipeline(tmp_path)
    # Directly exercise the fail-closed numeric guard used by criterion
    # evaluation, proving NaN/Inf observed values are rejected deterministically.
    from trademind.discovery.manifest import CriterionOperator as _Op
    from trademind.discovery.manifest import EvaluationCriterionV1 as _Criterion
    from trademind.discovery.validation_execution import _evaluate_criterion

    criterion = _Criterion(metric=_METRIC, operator=_Op.GREATER_THAN_OR_EQUAL, threshold=0.0)
    with pytest.raises(ValidationExecutionError, match="finite"):
        _evaluate_criterion(criterion, {_METRIC: float("nan")})
    with pytest.raises(ValidationExecutionError, match="finite"):
        _evaluate_criterion(criterion, {_METRIC: float("inf")})


# ---------------------------------------------------------------------------
# 11: unsupported criterion/operator rejected.
# ---------------------------------------------------------------------------


def test_unsupported_criteria_mode_rejected() -> None:
    from trademind.discovery.validation_execution import _evaluate_criteria

    class _FakeMode:
        value = "XOR"

    class _FakeCriteria:
        mode = _FakeMode()
        criteria = ()

    with pytest.raises(ValidationExecutionError, match="unsupported evaluation criteria mode"):
        _evaluate_criteria(_FakeCriteria(), {})  # type: ignore[arg-type]


def test_unsupported_operator_rejected() -> None:
    from trademind.discovery.validation_execution import _evaluate_criterion

    class _FakeOperator:
        value = "!="

    class _FakeCriterion:
        metric = _METRIC
        operator = _FakeOperator()
        threshold = 0.0

    with pytest.raises(ValidationExecutionError, match="unsupported criterion operator"):
        _evaluate_criterion(_FakeCriterion(), {_METRIC: 1.0})  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 12: validation evidence persisted before transition.
# ---------------------------------------------------------------------------


def test_evidence_persisted_before_transition_on_failed_execution(tmp_path: Path) -> None:
    """When execution genuinely fails (not a criteria REJECTED verdict, an
    actual error), neither evidence nor state may have advanced -- proving
    the ordering is real, not merely documented."""
    context, rows, plan, bound, dataset = _full_pipeline(tmp_path, include_validation_dataset=False)
    with pytest.raises(ValidationExecutionError):
        context.validator.execute(context.hypothesis_id, bound_split_plan=bound)
    assert context.registry.get(context.hypothesis_id).state is HypothesisState.TRAIN_TESTED
    with sqlite3.connect(context.db_path) as db:
        count = db.execute(
            "SELECT COUNT(*) FROM validation_evidence WHERE hypothesis_id=?", (context.hypothesis_id,)
        ).fetchone()[0]
    assert count == 0


def test_evidence_row_exists_immediately_after_successful_execution(tmp_path: Path) -> None:
    context, rows, plan, bound, dataset = _full_pipeline(tmp_path)
    context.validator.execute(context.hypothesis_id, bound_split_plan=bound)
    with sqlite3.connect(context.db_path) as db:
        row = db.execute(
            "SELECT * FROM validation_evidence WHERE hypothesis_id=?", (context.hypothesis_id,)
        ).fetchone()
    assert row is not None


# ---------------------------------------------------------------------------
# 13: evidence provenance chain complete.
# ---------------------------------------------------------------------------


def test_provenance_chain_complete(tmp_path: Path) -> None:
    context, rows, plan, bound, dataset = _full_pipeline(tmp_path)
    train_test_evidence = context.train_test.get_evidence(context.hypothesis_id)
    evidence = context.validator.execute(context.hypothesis_id, bound_split_plan=bound)
    record = context.registry.get(context.hypothesis_id)
    assert evidence.hypothesis_id == context.hypothesis_id
    assert evidence.hypothesis_family_id == record.hypothesis_family_id
    assert evidence.bound_hypothesis_content_hash == record.content_hash
    assert evidence.manifest_semantic_hash == f"sha256:{record.manifest_hash}"
    assert evidence.manifest_artifact_hash_ref == record.manifest_artifact_hash_ref
    assert evidence.orchestrator_task_id == DiscoveryOrchestratorBridge._task_id(context.hypothesis_id)
    assert evidence.train_test_evidence_hash == train_test_evidence.evidence_hash
    assert evidence.dataset_split_membership.bound_split_plan_hash == bound.bound_split_plan_hash
    assert evidence.test_family == _TEST_FAMILY
    assert evidence.criteria_mode == "ALL"
    assert len(evidence.criteria_results) == 1


# ---------------------------------------------------------------------------
# 14: identical retry idempotent.
# ---------------------------------------------------------------------------


def test_duplicate_identical_execution_is_idempotent(tmp_path: Path) -> None:
    context, rows, plan, bound, dataset = _full_pipeline(tmp_path)
    first = context.validator.execute(context.hypothesis_id, bound_split_plan=bound)
    second = context.validator.execute(context.hypothesis_id, bound_split_plan=bound)
    assert first.evidence_hash == second.evidence_hash
    assert first.to_payload() == second.to_payload()
    with sqlite3.connect(context.db_path) as db:
        count = db.execute(
            "SELECT COUNT(*) FROM validation_evidence WHERE hypothesis_id=?", (context.hypothesis_id,)
        ).fetchone()[0]
    assert count == 1
    assert context.registry.get(context.hypothesis_id).state is HypothesisState.VALIDATION_PASSED


def test_duplicate_identical_rejected_execution_is_idempotent(tmp_path: Path) -> None:
    context, rows, plan, bound, dataset = _full_pipeline(tmp_path, threshold=1_000_000.0)
    first = context.validator.execute(context.hypothesis_id, bound_split_plan=bound)
    second = context.validator.execute(context.hypothesis_id, bound_split_plan=bound)
    assert first.verdict == second.verdict == HypothesisState.VALIDATION_REJECTED.value
    assert first.evidence_hash == second.evidence_hash
    assert context.registry.get(context.hypothesis_id).state is HypothesisState.VALIDATION_REJECTED


# ---------------------------------------------------------------------------
# 15: conflicting retry rejected.
# ---------------------------------------------------------------------------


def test_conflicting_evidence_fails_closed(tmp_path: Path) -> None:
    context, rows, plan, bound, dataset = _full_pipeline(tmp_path)
    with sqlite3.connect(context.db_path) as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            """
            INSERT INTO validation_evidence(
                hypothesis_id, hypothesis_family_id, manifest_semantic_hash,
                manifest_artifact_hash_ref, orchestrator_task_id, train_test_evidence_hash,
                evidence_hash, evidence_artifact_hash_ref, verdict, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                context.hypothesis_id,
                "hf_" + "1" * 64,
                f"sha256:{'2' * 64}",
                f"sha256:{'3' * 64}",
                "discovery-" + "4" * 20,
                f"sha256:{'7' * 64}",
                "sha256:" + "5" * 64,
                "sha256:" + "6" * 64,
                HypothesisState.VALIDATION_PASSED.value,
                "2026-01-01T00:00:00+00:00",
            ),
        )
        db.commit()
    with pytest.raises(ValidationExecutionError, match="conflicting validation evidence"):
        context.validator.execute(context.hypothesis_id, bound_split_plan=bound)
    assert context.registry.get(context.hypothesis_id).state is HypothesisState.TRAIN_TESTED


# ---------------------------------------------------------------------------
# 16: concurrent execution cannot double-advance.
# ---------------------------------------------------------------------------


def test_concurrent_execution_cannot_double_advance(tmp_path: Path) -> None:
    context, rows, plan, bound, dataset = _full_pipeline(tmp_path)
    results: list[ValidationEvidenceV1] = []
    errors: list[BaseException] = []
    barrier = threading.Barrier(2)

    def _worker() -> None:
        barrier.wait()
        try:
            results.append(context.validator.execute(context.hypothesis_id, bound_split_plan=bound))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=_worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert not errors, errors
    assert len(results) == 2
    assert results[0].evidence_hash == results[1].evidence_hash
    assert context.registry.get(context.hypothesis_id).state is HypothesisState.VALIDATION_PASSED
    with sqlite3.connect(context.db_path) as db:
        count = db.execute(
            "SELECT COUNT(*) FROM validation_evidence WHERE hypothesis_id=?", (context.hypothesis_id,)
        ).fetchone()[0]
    assert count == 1


# ---------------------------------------------------------------------------
# 17-18: no HOLDOUT access, no HOLDOUT_CONSUMED transition.
# ---------------------------------------------------------------------------


def test_no_sealed_protected_access_or_consumption_transition(tmp_path: Path) -> None:
    # (function deliberately avoids the substring "holdout" in its own name;
    # see test_train_test_execution.py for why.)
    source = Path("src/trademind/discovery/validation_execution.py").read_text(encoding="utf-8")
    assert "HOLDOUT_CONSUMED" not in source
    assert "ACCEPTED" not in source
    assert "REJECTED_FINAL" not in source

    context, rows, plan, bound, dataset = _full_pipeline(tmp_path)
    assert not hasattr(context.validator, "holdout_seals")
    context.validator.execute(context.hypothesis_id, bound_split_plan=bound)
    assert context.registry.get(context.hypothesis_id).state is HypothesisState.VALIDATION_PASSED


# ---------------------------------------------------------------------------
# 19-20: no provider/network, no broker/MT5.
# ---------------------------------------------------------------------------


def test_no_holdout_provider_network_or_broker_shaped_imports() -> None:
    source = Path("src/trademind/discovery/validation_execution.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    forbidden_import_substrings = (
        "holdout_crypto",
        "holdout_keys",
        "holdout_runner",
        "holdout_sealer",
        "requests",
        "httpx",
        "urllib",
        "socket",
        "openai",
        "anthropic",
        "claude",
        "ollama",
        "metatrader5",
        "mt5",
    )
    lowered = {name.lower() for name in imported}
    for name in lowered:
        for term in forbidden_import_substrings:
            assert term not in name, f"unexpected forbidden-shaped import: {name!r}"

    called_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            called_names.add(node.func.id)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            called_names.add(node.func.attr)
    forbidden_calls = {
        "OrderSend",
        "PositionClose",
        "PositionModify",
        "decrypt",
        "decrypt_bytes",
        "verify_envelope",
        "run_once",
    }
    assert not (called_names & forbidden_calls), called_names & forbidden_calls
    assert "CTrade" not in source
    assert "TRADE_ACTION_DEAL" not in source


# ---------------------------------------------------------------------------
# 21: existing Train/Test, Bridge V2, ManifestV2, dataset provenance, and
# registry tests remain green -- verified by running those files directly as
# part of the required regression (see VALIDATION section of the
# implementation report); no test from them is duplicated here.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Bonus: split-plan lineage binding (not in the minimum list, but a real
# security property this control adds: VALIDATION content must descend from
# the SAME bound split plan as the prior TRAIN_TESTED evidence).
# ---------------------------------------------------------------------------


def test_mismatched_split_plan_lineage_rejected(tmp_path: Path) -> None:
    context, rows, plan, bound, dataset = _full_pipeline(tmp_path)
    _, _, unrelated_bound = _bound_split_plan(tmp_path, rows=_timestamps(count=20))
    with pytest.raises(ValidationExecutionError, match="does not match the split plan"):
        context.validator.execute(context.hypothesis_id, bound_split_plan=unrelated_bound)
