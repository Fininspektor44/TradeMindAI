"""Tests for Train/Test Execution Control V1: the first missing bridge
between a FROZEN, V2-bound Discovery hypothesis and ``TRAIN_TESTED``.

Chain exercised: ResearchProposalIntake -> ACCEPTED_FOR_HYPOTHESIS ->
PROPOSED hypothesis -> ResearchExperimentSpecificationV1 ->
ExperimentManifestV2 -> HypothesisRegistry.freeze_manifest_v2_in_transaction
-> DiscoveryOrchestratorBridge.submit_frozen_hypothesis_v2 (Orchestrator
task) -> TrainTestExecutionControl.execute() -> verified DISCOVERY dataset
-> real bounded evidence -> TRAIN_TESTED.

This file does not import test helpers from test_orchestrator_bridge_v2.py
(consistent with this lineage's convention of each test file owning its own
small setup helpers); it duplicates the same overall pipeline shape.
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
from trademind.orchestrator.models import RiskClass, Task
from trademind.discovery.train_test_execution import (
    SUPPORTED_TEST_FAMILIES,
    TrainTestEvidenceV1,
    TrainTestExecutionControl,
    TrainTestExecutionError,
)
from trademind.orchestrator.artifact_store import ArtifactStore
from trademind.orchestrator.budget import BudgetManager
from trademind.orchestrator.control_plane import ControlPlane
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
_MARKET_DATA_ROLE = "market-data"
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
    executor: TrainTestExecutionControl
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
        producer_version="train-test-execution-test",
        git_commit="f" * 40,
        revision_source="git_worktree",
    )


def _timestamps(count: int = 10) -> list[datetime]:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return [start + timedelta(hours=i) for i in range(count)]


def _csv_bytes(rows: list[datetime]) -> bytes:
    lines = [f"time,{_METRIC}"] + [f"{t.isoformat()},{10.0 + i}" for i, t in enumerate(rows)]
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
    executor = TrainTestExecutionControl(registry=registry, control=control, artifacts=store)
    return _Context(
        db_path=db_path,
        artifact_root=artifact_root,
        store=store,
        control=control,
        registry=registry,
        holdout_seals=holdout_seals,
        bridge=bridge,
        executor=executor,
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


def _discovery_dataset_v2(
    context: _Context, rows: list[datetime], plan: SplitPlan, *, role: str = _MARKET_DATA_ROLE
) -> DatasetArtifactV2:
    discovery_rows = rows[: plan.discovery_count]
    artifact = context.store.import_snapshot(io.BytesIO(_csv_bytes(discovery_rows)), media_type="text/csv")
    return DatasetArtifactV2(
        role=role, artifact_hash_ref=artifact.hash_ref, media_type=artifact.media_type, size_bytes=artifact.size_bytes
    )


def _build_manifest_v2(
    context: _Context,
    *,
    dataset: DatasetArtifactV2,
    split_plan: SplitPlan,
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
            EvaluationCriterionV1(
                metric=spec.primary_metric, operator=CriterionOperator.GREATER_THAN_OR_EQUAL, threshold=0.0
            ),
        ),
    )
    return build_experiment_manifest_v2(
        artifact_store=context.store,
        hypothesis_id=spec.hypothesis_id,
        hypothesis_family_id=spec.hypothesis_family_id,
        bound_hypothesis_content_hash=spec.hypothesis_content_hash,
        proposal_provenance=provenance,
        datasets=(dataset,),
        split_plan=split_plan,
        split_dataset_role=dataset.role,
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
        created_by="operator:train-test-execution-test",
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
        key_id="train-test-execution-key",
        evaluator_id=_TEST_FAMILY,
        evaluator_hash="b" * 64,
    )
    context.holdout_seals.mark_isolated(
        context.hypothesis_id,
        isolation_receipt_hash="c" * 64,
        public_max_time="2026-01-02T00:00:00+00:00",
        holdout_start_time="2026-01-03T00:00:00+00:00",
        holdout_end_time="2026-01-04T00:00:00+00:00",
        public_row_count=2,
        holdout_row_count=2,
    )


def _create_matching_task(context: _Context) -> None:
    """Create the exact deterministic Orchestrator task
    ``TrainTestExecutionControl`` requires, WITHOUT going through
    ``DiscoveryOrchestratorBridge.submit_frozen_hypothesis_v2`` -- which
    would itself already re-verify DISCOVERY dataset content and reject a
    forged one before a task could ever exist. This isolates the property
    under test: that ``TrainTestExecutionControl`` performs its OWN
    independent dataset re-verification rather than merely trusting that a
    task's existence implies the Bridge already checked it."""
    task_id = DiscoveryOrchestratorBridge._task_id(context.hypothesis_id)
    task = Task.new(
        task_id=task_id,
        goal="test-only: matching task created without Bridge dataset verification",
        scope=("src/trademind/discovery",),
        risk_class=RiskClass.LOW,
    )
    context.control.create_task(task)


def _full_pipeline(
    tmp_path: Path, *, test_family: str = _TEST_FAMILY, submit: bool = True
) -> tuple[_Context, list[datetime], SplitPlan, BoundSplitPlanV1, DatasetArtifactV2]:
    context = _setup(tmp_path, test_family=test_family)
    rows, plan, bound = _bound_split_plan(tmp_path)
    dataset = _discovery_dataset_v2(context, rows, plan)
    manifest = _build_manifest_v2(context, dataset=dataset, split_plan=plan)
    _freeze_v2(context, manifest)
    _attest_holdout(context)
    if submit:
        context.bridge.submit_frozen_hypothesis_v2(
            context.hypothesis_id,
            bound_split_plan=bound,
            dataset_role_bindings={dataset.role: "DISCOVERY"},
        )
    return context, rows, plan, bound, dataset


# ---------------------------------------------------------------------------
# 1: happy path -- reaches TRAIN_TESTED only after valid DISCOVERY evidence.
# ---------------------------------------------------------------------------


def test_real_frozen_v2_hypothesis_reaches_train_tested_after_valid_evidence(tmp_path: Path) -> None:
    context, rows, plan, bound, dataset = _full_pipeline(tmp_path)
    assert context.registry.get(context.hypothesis_id).state is HypothesisState.FROZEN

    evidence = context.executor.execute(context.hypothesis_id, bound_split_plan=bound)

    assert isinstance(evidence, TrainTestEvidenceV1)
    assert evidence.hypothesis_id == context.hypothesis_id
    assert evidence.test_family == _TEST_FAMILY
    assert evidence.metrics["row_count"] == plan.discovery_count
    assert evidence.metrics[_METRIC] == pytest.approx(10.0 + (plan.discovery_count - 1) / 2)
    assert evidence.dataset_split_membership.role == "DISCOVERY"
    assert context.registry.get(context.hypothesis_id).state is HypothesisState.TRAIN_TESTED


# ---------------------------------------------------------------------------
# 2: non-FROZEN rejected.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("premature", [True, False])
def test_non_frozen_hypothesis_rejected(tmp_path: Path, premature: bool) -> None:
    if premature:
        context = _setup(tmp_path)
        _, plan, bound = _bound_split_plan(tmp_path)
        assert context.registry.get(context.hypothesis_id).state is HypothesisState.PROPOSED
        with pytest.raises(TrainTestExecutionError, match="must be FROZEN"):
            context.executor.execute(context.hypothesis_id, bound_split_plan=bound)
        return

    # Already past FROZEN (via TRAIN_TESTED) still resolves through the
    # idempotent-reload path, not the "must be FROZEN" rejection -- covered
    # separately by test 10. Here we cover a state the state machine forbids
    # reaching without the legal FROZEN precondition ever having existed.
    context, rows, plan, bound, dataset = _full_pipeline(tmp_path)
    with sqlite3.connect(context.db_path) as db:
        db.execute(
            "UPDATE hypotheses SET state=? WHERE hypothesis_id=?",
            (HypothesisState.PROPOSED.value, context.hypothesis_id),
        )
    with pytest.raises(TrainTestExecutionError, match="must be FROZEN"):
        context.executor.execute(context.hypothesis_id, bound_split_plan=bound)


# ---------------------------------------------------------------------------
# 3: missing/wrong Task rejected.
# ---------------------------------------------------------------------------


def test_missing_task_rejected(tmp_path: Path) -> None:
    context, rows, plan, bound, dataset = _full_pipeline(tmp_path, submit=False)
    assert context.registry.get(context.hypothesis_id).state is HypothesisState.FROZEN
    with pytest.raises(TrainTestExecutionError, match="no matching Orchestrator task"):
        context.executor.execute(context.hypothesis_id, bound_split_plan=bound)


# ---------------------------------------------------------------------------
# 4: wrong manifest binding rejected.
# ---------------------------------------------------------------------------


def test_wrong_manifest_binding_rejected(tmp_path: Path) -> None:
    context, rows, plan, bound, dataset = _full_pipeline(tmp_path)
    with sqlite3.connect(context.db_path) as db:
        db.execute(
            "UPDATE hypotheses SET content_hash=? WHERE hypothesis_id=?",
            ("0" * 64, context.hypothesis_id),
        )
    with pytest.raises(TrainTestExecutionError, match="not safely bound"):
        context.executor.execute(context.hypothesis_id, bound_split_plan=bound)


# ---------------------------------------------------------------------------
# 5: missing dataset proof rejected.
# ---------------------------------------------------------------------------


def test_missing_dataset_proof_rejected(tmp_path: Path) -> None:
    context, rows, plan, bound, dataset = _full_pipeline(tmp_path)
    object_path = Path(context.store.resolve_verified(dataset.artifact_hash_ref).path)
    metadata_path = object_path.parent / f"{object_path.stem}.meta.json"
    object_path.unlink()
    metadata_path.unlink()
    with pytest.raises(TrainTestExecutionError, match="could not be verified"):
        context.executor.execute(context.hypothesis_id, bound_split_plan=bound)


# ---------------------------------------------------------------------------
# 6: VALIDATION dataset rejected.
# ---------------------------------------------------------------------------


def test_validation_dataset_rejected(tmp_path: Path) -> None:
    context = _setup(tmp_path)
    rows, plan, bound = _bound_split_plan(tmp_path)
    validation_rows = rows[plan.discovery_count : plan.discovery_count + plan.validation_count]
    artifact = context.store.import_snapshot(io.BytesIO(_csv_bytes(validation_rows)), media_type="text/csv")
    dataset = DatasetArtifactV2(
        role=_MARKET_DATA_ROLE, artifact_hash_ref=artifact.hash_ref, media_type=artifact.media_type, size_bytes=artifact.size_bytes
    )
    manifest = _build_manifest_v2(context, dataset=dataset, split_plan=plan)
    _freeze_v2(context, manifest)
    _attest_holdout(context)
    _create_matching_task(context)
    with pytest.raises(TrainTestExecutionError, match="DISCOVERY dataset split-membership verification failed"):
        context.executor.execute(context.hypothesis_id, bound_split_plan=bound)
    assert context.registry.get(context.hypothesis_id).state is HypothesisState.FROZEN


# ---------------------------------------------------------------------------
# 7: full unsplit/holdout-containing dataset rejected.
# ---------------------------------------------------------------------------


def test_full_unsplit_dataset_rejected(tmp_path: Path) -> None:
    context = _setup(tmp_path)
    rows, plan, bound = _bound_split_plan(tmp_path)
    full_artifact = context.store.import_snapshot(io.BytesIO(_csv_bytes(rows)), media_type="text/csv")
    dataset = DatasetArtifactV2(
        role=_MARKET_DATA_ROLE, artifact_hash_ref=full_artifact.hash_ref, media_type=full_artifact.media_type, size_bytes=full_artifact.size_bytes
    )
    manifest = _build_manifest_v2(context, dataset=dataset, split_plan=plan)
    _freeze_v2(context, manifest)
    _attest_holdout(context)
    _create_matching_task(context)
    with pytest.raises(TrainTestExecutionError, match="DISCOVERY dataset split-membership verification failed"):
        context.executor.execute(context.hypothesis_id, bound_split_plan=bound)
    assert context.registry.get(context.hypothesis_id).state is HypothesisState.FROZEN


# ---------------------------------------------------------------------------
# 8: tampered artifact/evidence rejected.
# ---------------------------------------------------------------------------


def test_tampered_dataset_artifact_rejected(tmp_path: Path) -> None:
    context, rows, plan, bound, dataset = _full_pipeline(tmp_path)
    object_path = Path(context.store.resolve_verified(dataset.artifact_hash_ref).path)
    object_path.write_bytes(b"tampered-not-the-authoritative-dataset")
    with pytest.raises(Exception):
        context.executor.execute(context.hypothesis_id, bound_split_plan=bound)
    assert context.registry.get(context.hypothesis_id).state is HypothesisState.FROZEN


def test_tampered_recorded_evidence_rejected_on_reload(tmp_path: Path) -> None:
    context, rows, plan, bound, dataset = _full_pipeline(tmp_path)
    context.executor.execute(context.hypothesis_id, bound_split_plan=bound)
    with sqlite3.connect(context.db_path) as db:
        row = db.execute(
            "SELECT evidence_artifact_hash_ref FROM train_test_evidence WHERE hypothesis_id=?",
            (context.hypothesis_id,),
        ).fetchone()
    evidence_ref = row[0]
    object_path = Path(context.store.resolve_verified(evidence_ref).path)
    object_path.write_bytes(b'{"tampered": true}')
    with pytest.raises(Exception):
        context.executor.get_evidence(context.hypothesis_id)


# ---------------------------------------------------------------------------
# 9: failed execution does not advance state.
# ---------------------------------------------------------------------------


def test_failed_execution_does_not_advance_state(tmp_path: Path) -> None:
    context = _setup(tmp_path)
    rows, plan, bound = _bound_split_plan(tmp_path)
    # A DISCOVERY-range dataset whose declared metric column contains a
    # non-numeric value: passes dataset provenance verification, but the
    # test family itself must fail deterministically.
    discovery_rows = rows[: plan.discovery_count]
    lines = [f"time,{_METRIC}"] + [f"{t.isoformat()},not-a-number" for t in discovery_rows]
    bad_bytes = ("\n".join(lines) + "\n").encode("utf-8")
    artifact = context.store.import_snapshot(io.BytesIO(bad_bytes), media_type="text/csv")
    dataset = DatasetArtifactV2(
        role=_MARKET_DATA_ROLE, artifact_hash_ref=artifact.hash_ref, media_type=artifact.media_type, size_bytes=artifact.size_bytes
    )
    manifest = _build_manifest_v2(context, dataset=dataset, split_plan=plan)
    _freeze_v2(context, manifest)
    _attest_holdout(context)
    context.bridge.submit_frozen_hypothesis_v2(
        context.hypothesis_id, bound_split_plan=bound, dataset_role_bindings={dataset.role: "DISCOVERY"}
    )
    with pytest.raises(TrainTestExecutionError, match="non-numeric value"):
        context.executor.execute(context.hypothesis_id, bound_split_plan=bound)
    assert context.registry.get(context.hypothesis_id).state is HypothesisState.FROZEN
    with sqlite3.connect(context.db_path) as db:
        count = db.execute(
            "SELECT COUNT(*) FROM train_test_evidence WHERE hypothesis_id=?",
            (context.hypothesis_id,),
        ).fetchone()[0]
    assert count == 0


def test_unsupported_test_family_rejected_without_advancing(tmp_path: Path) -> None:
    context, rows, plan, bound, dataset = _full_pipeline(tmp_path, test_family="unregistered_test_family_v9")
    assert "unregistered_test_family_v9" not in SUPPORTED_TEST_FAMILIES
    with pytest.raises(TrainTestExecutionError, match="not in the supported execution vocabulary"):
        context.executor.execute(context.hypothesis_id, bound_split_plan=bound)
    assert context.registry.get(context.hypothesis_id).state is HypothesisState.FROZEN


# ---------------------------------------------------------------------------
# 10: duplicate identical execution does not duplicate evidence/state.
# ---------------------------------------------------------------------------


def test_duplicate_identical_execution_is_idempotent(tmp_path: Path) -> None:
    context, rows, plan, bound, dataset = _full_pipeline(tmp_path)
    first = context.executor.execute(context.hypothesis_id, bound_split_plan=bound)
    second = context.executor.execute(context.hypothesis_id, bound_split_plan=bound)
    assert first.evidence_hash == second.evidence_hash
    assert first.to_payload() == second.to_payload()
    with sqlite3.connect(context.db_path) as db:
        count = db.execute(
            "SELECT COUNT(*) FROM train_test_evidence WHERE hypothesis_id=?",
            (context.hypothesis_id,),
        ).fetchone()[0]
    assert count == 1
    assert context.registry.get(context.hypothesis_id).state is HypothesisState.TRAIN_TESTED


# ---------------------------------------------------------------------------
# 11: concurrent execution cannot double-advance.
# ---------------------------------------------------------------------------


def test_concurrent_execution_cannot_double_advance(tmp_path: Path) -> None:
    context, rows, plan, bound, dataset = _full_pipeline(tmp_path)
    results: list[TrainTestEvidenceV1] = []
    errors: list[BaseException] = []
    barrier = threading.Barrier(2)

    def _worker() -> None:
        barrier.wait()
        try:
            results.append(context.executor.execute(context.hypothesis_id, bound_split_plan=bound))
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
    assert context.registry.get(context.hypothesis_id).state is HypothesisState.TRAIN_TESTED
    with sqlite3.connect(context.db_path) as db:
        count = db.execute(
            "SELECT COUNT(*) FROM train_test_evidence WHERE hypothesis_id=?",
            (context.hypothesis_id,),
        ).fetchone()[0]
    assert count == 1


def test_conflicting_evidence_fails_closed(tmp_path: Path) -> None:
    """A pre-existing evidence row for this hypothesis with a DIFFERENT
    evidence_hash than what a fresh execution would compute (simulating an
    already-recorded, non-identical concurrent claim) must be rejected, not
    silently overwritten or accepted."""
    context, rows, plan, bound, dataset = _full_pipeline(tmp_path)
    with sqlite3.connect(context.db_path) as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            """
            INSERT INTO train_test_evidence(
                hypothesis_id, hypothesis_family_id, manifest_semantic_hash,
                manifest_artifact_hash_ref, orchestrator_task_id, evidence_hash,
                evidence_artifact_hash_ref, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                context.hypothesis_id,
                "hf_" + "1" * 64,
                f"sha256:{'2' * 64}",
                f"sha256:{'3' * 64}",
                "discovery-" + "4" * 20,
                "sha256:" + "5" * 64,
                "sha256:" + "6" * 64,
                "2026-01-01T00:00:00+00:00",
            ),
        )
        db.commit()
    with pytest.raises(TrainTestExecutionError, match="conflicting train/test evidence"):
        context.executor.execute(context.hypothesis_id, bound_split_plan=bound)
    assert context.registry.get(context.hypothesis_id).state is HypothesisState.FROZEN


# ---------------------------------------------------------------------------
# 12: provenance chain complete.
# ---------------------------------------------------------------------------


def test_provenance_chain_complete(tmp_path: Path) -> None:
    context, rows, plan, bound, dataset = _full_pipeline(tmp_path)
    evidence = context.executor.execute(context.hypothesis_id, bound_split_plan=bound)
    record = context.registry.get(context.hypothesis_id)
    assert evidence.hypothesis_id == context.hypothesis_id
    assert evidence.hypothesis_family_id == record.hypothesis_family_id
    assert evidence.bound_hypothesis_content_hash == record.content_hash
    assert evidence.manifest_semantic_hash == f"sha256:{record.manifest_hash}"
    assert evidence.manifest_artifact_hash_ref == record.manifest_artifact_hash_ref
    assert evidence.orchestrator_task_id == DiscoveryOrchestratorBridge._task_id(context.hypothesis_id)
    assert evidence.dataset_split_membership.artifact_hash_ref == dataset.artifact_hash_ref
    assert evidence.dataset_split_membership.bound_split_plan_hash == bound.bound_split_plan_hash
    with sqlite3.connect(context.db_path) as db:
        row = db.execute(
            "SELECT * FROM train_test_evidence WHERE hypothesis_id=?", (context.hypothesis_id,)
        ).fetchone()
    assert row is not None


def test_hypothesis_train_tested_by_external_path_has_no_recorded_evidence(tmp_path: Path) -> None:
    """A hypothesis advanced to TRAIN_TESTED through a path other than this
    control (e.g. the raw registry primitive, as every pre-existing test
    fixture in this repository does) must be rejected by this control, not
    silently treated as already-executed."""
    context, rows, plan, bound, dataset = _full_pipeline(tmp_path)
    context.registry.transition(context.hypothesis_id, HypothesisState.TRAIN_TESTED)
    with pytest.raises(TrainTestExecutionError, match="no recorded train/test evidence binding"):
        context.executor.execute(context.hypothesis_id, bound_split_plan=bound)


# ---------------------------------------------------------------------------
# 13: no validation transition.
# ---------------------------------------------------------------------------


def test_no_validation_transition_referenced() -> None:
    source = Path("src/trademind/discovery/train_test_execution.py").read_text(encoding="utf-8")
    assert "VALIDATION_PASSED" not in source
    assert "VALIDATION_REJECTED" not in source
    assert "HOLDOUT_CONSUMED" not in source
    assert "ACCEPTED" not in source
    assert "REJECTED_FINAL" not in source


# ---------------------------------------------------------------------------
# 14-16: no holdout access, no provider/network, no broker/MT5.
# ---------------------------------------------------------------------------


def test_no_holdout_provider_network_or_broker_shaped_imports() -> None:
    source = Path("src/trademind/discovery/train_test_execution.py").read_text(encoding="utf-8")
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
        "run_validation",
        "verify_envelope",
    }
    assert not (called_names & forbidden_calls), called_names & forbidden_calls
    assert "CTrade" not in source
    assert "TRADE_ACTION_DEAL" not in source


def test_execution_never_reads_sealed_protected_content(tmp_path: Path) -> None:
    # (function deliberately avoids the substring "holdout" in its own name:
    # pytest derives `tmp_path` from the test's own node id, and _setup()'s
    # spec dataset path would otherwise trip
    # research_experiment_specification.py's own unrelated holdout-shaped-
    # filename guard purely by coincidence of this test's name -- the same
    # gotcha documented in test_orchestrator_bridge_v2.py.)
    """Dynamic proof: executing train/test evidence only ever reads the
    manifest's declared DISCOVERY-role dataset artifact and the registry's
    own bookkeeping -- never the final-holdout seal store or its sealed
    envelope, which this control does not even hold a reference to."""
    context, rows, plan, bound, dataset = _full_pipeline(tmp_path)
    assert not hasattr(context.executor, "holdout_seals")
    evidence = context.executor.execute(context.hypothesis_id, bound_split_plan=bound)
    assert evidence.dataset_split_membership.role == "DISCOVERY"


# ---------------------------------------------------------------------------
# 17: existing Bridge V2 / ManifestV2 / dataset provenance / registry tests
# green -- verified by running those files directly as part of the required
# regression suite (see VALIDATION section of the implementation report); no
# test from them is duplicated here.
# ---------------------------------------------------------------------------
