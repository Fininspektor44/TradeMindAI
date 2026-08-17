"""Tests for the Research -> SER8 Eligibility Boundary V1: the minimal,
fail-closed seam proving an ACCEPTED research hypothesis may be presented
as an eligible research artifact, without ever authorizing a trade.

Chain exercised: the full closed research lifecycle through
FinalVerdictAcceptanceControl.finalize() -> ACCEPTED ->
present_eligible_artifact().
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
from trademind.discovery.final_verdict_control import FinalVerdictAcceptanceControl
from trademind.discovery.holdout_keys import HoldoutKeyError
from trademind.discovery.holdout_runner import FinalHoldoutRunner
from trademind.discovery.holdout_sealer import FinalHoldoutSealer
from trademind.discovery.holdout_store import HoldoutSealStore
from trademind.discovery.holdout_trigger_bridge import HoldoutTriggerBridge
from trademind.discovery.hypothesis_registry import HypothesisRegistry, HypothesisState
from trademind.discovery.manifest import (
    CriteriaMode,
    CriterionOperator,
    DatasetArtifactV2,
    EvaluationCriteriaV1,
    EvaluationCriterionV1,
    ExperimentManifestV2,
    FinalHoldoutCriteriaV1,
    ProposalIntakeProvenanceV1,
    build_experiment_manifest_v2,
    persist_experiment_manifest_v2,
)
from trademind.discovery.orchestrator_bridge import DiscoveryOrchestratorBridge
from trademind.discovery.research_eligibility_boundary import (
    ResearchEligibilityArtifactV1,
    ResearchEligibilityError,
    present_eligible_artifact,
)
from trademind.discovery.result_ledger import ResultLedger
from trademind.discovery.split_engine import SplitPlan, chronological_split
from trademind.discovery.train_test_execution import TrainTestExecutionControl
from trademind.discovery.validation_execution import ValidationExecutionControl
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
_DISCOVERY_ROLE = "market-data"
_VALIDATION_ROLE = "market-data-validation"
_TEST_FAMILY = "deterministic_aggregate_v1"
_METRIC = "avg_net_atr"
_FINAL_METRIC = "rows"

_KEY = bytes(range(32))
_KEY_ID = "eligibility-key-v1"
_EVALUATOR_ARTIFACT = Path(__file__).resolve()
_FINAL_PLAINTEXT = (
    "time,return\n"
    "2026-01-03T00:00:00+00:00,0.10\n"
    "2026-01-03T06:00:00+00:00,-0.05\n"
)


class _StaticKeys:
    def __init__(self, key: bytes = _KEY, key_id: str = _KEY_ID) -> None:
        self.key = key
        self.key_id = key_id

    def load_key(self, key_id: str) -> bytes:
        if key_id != self.key_id:
            raise HoldoutKeyError("unknown key")
        return self.key


class _CountingEvaluator:
    evaluator_id = "eligibility-boundary-aggregate-v1"

    def evaluate(self, plaintext: bytes) -> dict[str, int]:
        rows = max(0, plaintext.count(b"\n") - 1)
        return {_FINAL_METRIC: rows}


@dataclass(frozen=True, slots=True)
class _Context:
    db_path: Path
    store: ArtifactStore
    control: ControlPlane
    registry: HypothesisRegistry
    holdout_seals: HoldoutSealStore
    sealer: FinalHoldoutSealer
    bridge: DiscoveryOrchestratorBridge
    train_test: TrainTestExecutionControl
    validator: ValidationExecutionControl
    trigger_bridge: HoldoutTriggerBridge
    final_verdict: FinalVerdictAcceptanceControl
    sealed_path: Path
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
        producer_version="eligibility-boundary-test",
        git_commit="f" * 40,
        revision_source="git_worktree",
    )


def _timestamps(count: int = 12) -> list[datetime]:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return [start + timedelta(hours=i) for i in range(count)]


def _csv_bytes(rows: list[datetime], *, base: float = 10.0) -> bytes:
    lines = [f"time,{_METRIC}"] + [f"{t.isoformat()},{base + i}" for i, t in enumerate(rows)]
    return ("\n".join(lines) + "\n").encode("utf-8")


def _setup(tmp_path: Path, *, symbol: str = "XAUUSD") -> _Context:
    db_path = tmp_path / "orchestrator.db"
    store = ArtifactStore(tmp_path / "artifacts")
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
        test_family=_TEST_FAMILY,
        primary_metric=_METRIC,
        alpha=0.05,
        q=0.10,
        minimum_effect_size=0.0,
        max_hypotheses_tests=1,
        datasets=(v1_dataset,),
        parameters={"horizon": 12},
    )
    holdout_seals = HoldoutSealStore(registry)
    sealer = FinalHoldoutSealer(registry=registry, seals=holdout_seals, keys=_StaticKeys())
    bridge = DiscoveryOrchestratorBridge(
        registry=registry, holdout_seals=holdout_seals, control=control, artifacts=store
    )
    train_test = TrainTestExecutionControl(registry=registry, control=control, artifacts=store)
    validator = ValidationExecutionControl(
        registry=registry, control=control, artifacts=store, train_test=train_test
    )
    ledger = ResultLedger(tmp_path / "results.jsonl")
    runner = FinalHoldoutRunner(
        registry=registry,
        seals=holdout_seals,
        keys=_StaticKeys(),
        ledger=ledger,
        evaluator=_CountingEvaluator(),
        evaluator_artifact_path=_EVALUATOR_ARTIFACT,
    )
    trigger_bridge = HoldoutTriggerBridge(
        registry=registry, control=control, artifacts=store, validator=validator, runner=runner
    )
    final_verdict = FinalVerdictAcceptanceControl(
        registry=registry,
        control=control,
        artifacts=store,
        validator=validator,
        trigger_bridge=trigger_bridge,
    )
    return _Context(
        db_path=db_path,
        store=store,
        control=control,
        registry=registry,
        holdout_seals=holdout_seals,
        sealer=sealer,
        bridge=bridge,
        train_test=train_test,
        validator=validator,
        trigger_bridge=trigger_bridge,
        final_verdict=final_verdict,
        sealed_path=tmp_path / "final.sealed.json",
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


def _validation_dataset_v2(context: _Context, rows: list[datetime], plan: SplitPlan) -> DatasetArtifactV2:
    validation_rows = rows[plan.discovery_count : plan.discovery_count + plan.validation_count]
    artifact = context.store.import_snapshot(io.BytesIO(_csv_bytes(validation_rows)), media_type="text/csv")
    return DatasetArtifactV2(
        role=_VALIDATION_ROLE, artifact_hash_ref=artifact.hash_ref, media_type=artifact.media_type, size_bytes=artifact.size_bytes
    )


def _final_holdout_criteria(*, threshold: int = 1) -> FinalHoldoutCriteriaV1:
    return FinalHoldoutCriteriaV1(
        mode=CriteriaMode.ALL,
        criteria=(
            EvaluationCriterionV1(
                metric=_FINAL_METRIC, operator=CriterionOperator.GREATER_THAN_OR_EQUAL, threshold=threshold
            ),
        ),
    )


def _build_manifest_v2(
    context: _Context, *, datasets: tuple[DatasetArtifactV2, ...], split_plan: SplitPlan, threshold: int
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
        created_by="operator:eligibility-boundary-test",
        final_holdout_criteria=_final_holdout_criteria(threshold=threshold),
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


def _seal_and_isolate(context: _Context, tmp_path: Path) -> None:
    plaintext_path = tmp_path / "final-plaintext.csv"
    plaintext_path.write_text(_FINAL_PLAINTEXT, encoding="utf-8")
    context.sealer.seal_file(
        hypothesis_id=context.hypothesis_id,
        plaintext_path=plaintext_path,
        destination_path=context.sealed_path,
        key_id=_KEY_ID,
        evaluator_id=_CountingEvaluator.evaluator_id,
        evaluator_artifact_path=_EVALUATOR_ARTIFACT,
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


def _full_pipeline(
    tmp_path: Path, *, threshold: int = 1, reach: str = "ACCEPTED"
) -> _Context:
    """``reach``: "VALIDATION_PASSED" | "HOLDOUT_CONSUMED" | "ACCEPTED"
    (default) | "REJECTED_FINAL"."""
    context = _setup(tmp_path)
    rows, plan, bound = _bound_split_plan(tmp_path)
    discovery_dataset = _discovery_dataset_v2(context, rows, plan)
    validation_dataset = _validation_dataset_v2(context, rows, plan)
    effective_threshold = 1_000_000 if reach == "REJECTED_FINAL" else threshold
    manifest = _build_manifest_v2(
        context, datasets=(discovery_dataset, validation_dataset), split_plan=plan, threshold=effective_threshold
    )
    _freeze_v2(context, manifest)
    _seal_and_isolate(context, tmp_path)
    context.bridge.submit_frozen_hypothesis_v2(
        context.hypothesis_id,
        bound_split_plan=bound,
        dataset_role_bindings={discovery_dataset.role: "DISCOVERY", validation_dataset.role: "VALIDATION"},
    )
    context.train_test.execute(context.hypothesis_id, bound_split_plan=bound)
    context.validator.execute(context.hypothesis_id, bound_split_plan=bound)
    if reach == "VALIDATION_PASSED":
        return context
    context.trigger_bridge.trigger(context.hypothesis_id, sealed_path=context.sealed_path)
    if reach == "HOLDOUT_CONSUMED":
        return context
    context.final_verdict.finalize(context.hypothesis_id)
    return context


# ---------------------------------------------------------------------------
# Real ACCEPTED chain presents an eligible artifact.
# ---------------------------------------------------------------------------


def test_real_accepted_hypothesis_is_presented_as_eligible(tmp_path: Path) -> None:
    context = _full_pipeline(tmp_path, threshold=1)
    assert context.registry.get(context.hypothesis_id).state is HypothesisState.ACCEPTED

    artifact = present_eligible_artifact(
        context.hypothesis_id, registry=context.registry, final_verdict=context.final_verdict
    )

    assert isinstance(artifact, ResearchEligibilityArtifactV1)
    assert artifact.hypothesis_id == context.hypothesis_id
    assert artifact.verdict == HypothesisState.ACCEPTED.value


# ---------------------------------------------------------------------------
# Hard safety boundary: REJECTED_FINAL and every intermediate state rejected.
# ---------------------------------------------------------------------------


def test_rejected_final_cannot_reach_eligibility(tmp_path: Path) -> None:
    context = _full_pipeline(tmp_path, reach="REJECTED_FINAL")
    assert context.registry.get(context.hypothesis_id).state is HypothesisState.REJECTED_FINAL
    with pytest.raises(ResearchEligibilityError, match="not eligible"):
        present_eligible_artifact(
            context.hypothesis_id, registry=context.registry, final_verdict=context.final_verdict
        )


@pytest.mark.parametrize("reach", ["VALIDATION_PASSED", "HOLDOUT_CONSUMED"])
def test_intermediate_states_cannot_reach_eligibility(tmp_path: Path, reach: str) -> None:
    context = _full_pipeline(tmp_path, reach=reach)
    with pytest.raises(ResearchEligibilityError, match="not eligible"):
        present_eligible_artifact(
            context.hypothesis_id, registry=context.registry, final_verdict=context.final_verdict
        )


def test_proposed_and_frozen_states_cannot_reach_eligibility(tmp_path: Path) -> None:
    context = _setup(tmp_path)
    assert context.registry.get(context.hypothesis_id).state is HypothesisState.PROPOSED
    with pytest.raises(ResearchEligibilityError, match="not eligible"):
        present_eligible_artifact(
            context.hypothesis_id, registry=context.registry, final_verdict=context.final_verdict
        )

    rows, plan, bound = _bound_split_plan(tmp_path)
    discovery_dataset = _discovery_dataset_v2(context, rows, plan)
    validation_dataset = _validation_dataset_v2(context, rows, plan)
    manifest = _build_manifest_v2(
        context, datasets=(discovery_dataset, validation_dataset), split_plan=plan, threshold=1
    )
    _freeze_v2(context, manifest)
    assert context.registry.get(context.hypothesis_id).state is HypothesisState.FROZEN
    with pytest.raises(ResearchEligibilityError, match="not eligible"):
        present_eligible_artifact(
            context.hypothesis_id, registry=context.registry, final_verdict=context.final_verdict
        )


def test_train_tested_state_cannot_reach_eligibility(tmp_path: Path) -> None:
    context = _setup(tmp_path)
    rows, plan, bound = _bound_split_plan(tmp_path)
    discovery_dataset = _discovery_dataset_v2(context, rows, plan)
    validation_dataset = _validation_dataset_v2(context, rows, plan)
    manifest = _build_manifest_v2(
        context, datasets=(discovery_dataset, validation_dataset), split_plan=plan, threshold=1
    )
    _freeze_v2(context, manifest)
    _seal_and_isolate(context, tmp_path)
    context.bridge.submit_frozen_hypothesis_v2(
        context.hypothesis_id,
        bound_split_plan=bound,
        dataset_role_bindings={discovery_dataset.role: "DISCOVERY", validation_dataset.role: "VALIDATION"},
    )
    context.train_test.execute(context.hypothesis_id, bound_split_plan=bound)
    assert context.registry.get(context.hypothesis_id).state is HypothesisState.TRAIN_TESTED
    with pytest.raises(ResearchEligibilityError, match="not eligible"):
        present_eligible_artifact(
            context.hypothesis_id, registry=context.registry, final_verdict=context.final_verdict
        )


def test_validation_rejected_state_cannot_reach_eligibility(tmp_path: Path) -> None:
    context = _setup(tmp_path)
    rows, plan, bound = _bound_split_plan(tmp_path)
    discovery_dataset = _discovery_dataset_v2(context, rows, plan)
    validation_dataset = _validation_dataset_v2(context, rows, plan)
    spec = context.spec
    provenance = ProposalIntakeProvenanceV1(
        intake_id=spec.intake_id, execution_request_hash=spec.request_hash, authorization_id=spec.authorization_id,
        task_id=spec.task_id, task_revision=spec.task_revision, packet_artifact_hash_ref=spec.packet_artifact_hash_ref,
        packet_semantic_hash=spec.packet_semantic_hash, result_artifact_hash_ref=spec.result_artifact_hash_ref,
        proposal_index=spec.proposal_index, candidate_id=spec.candidate_id,
    )
    criteria = EvaluationCriteriaV1(
        mode=CriteriaMode.ALL,
        criteria=(EvaluationCriterionV1(metric=spec.primary_metric, operator=CriterionOperator.GREATER_THAN_OR_EQUAL, threshold=1_000_000.0),),
    )
    manifest = build_experiment_manifest_v2(
        artifact_store=context.store, hypothesis_id=spec.hypothesis_id, hypothesis_family_id=spec.hypothesis_family_id,
        bound_hypothesis_content_hash=spec.hypothesis_content_hash, proposal_provenance=provenance,
        datasets=(discovery_dataset, validation_dataset), split_plan=plan, split_dataset_role=discovery_dataset.role,
        test_family=spec.test_family, primary_metric=spec.primary_metric, evaluation_criteria=criteria,
        alpha=spec.alpha, q=spec.q, minimum_effect_size=spec.minimum_effect_size, max_hypotheses_tests=spec.max_hypotheses_tests,
        trading_friction=None, deterministic_seed=None, code_provenance=_code_provenance(),
        semantic_parameters=spec.parameters, created_at="2026-08-17T00:00:00+00:00", created_by="operator:rejected-validation-test",
        final_holdout_criteria=_final_holdout_criteria(threshold=1),
    )
    _freeze_v2(context, manifest)
    _seal_and_isolate(context, tmp_path)
    context.bridge.submit_frozen_hypothesis_v2(
        context.hypothesis_id, bound_split_plan=bound,
        dataset_role_bindings={discovery_dataset.role: "DISCOVERY", validation_dataset.role: "VALIDATION"},
    )
    context.train_test.execute(context.hypothesis_id, bound_split_plan=bound)
    context.validator.execute(context.hypothesis_id, bound_split_plan=bound)
    assert context.registry.get(context.hypothesis_id).state is HypothesisState.VALIDATION_REJECTED
    with pytest.raises(ResearchEligibilityError, match="not eligible"):
        present_eligible_artifact(
            context.hypothesis_id, registry=context.registry, final_verdict=context.final_verdict
        )


def test_nonexistent_hypothesis_rejected(tmp_path: Path) -> None:
    context = _setup(tmp_path)
    with pytest.raises(ResearchEligibilityError, match="does not exist"):
        present_eligible_artifact(
            "rpi-v1:sha256:" + "0" * 64 + ":0", registry=context.registry, final_verdict=context.final_verdict
        )


# ---------------------------------------------------------------------------
# ACCEPTED artifact identity/provenance remains intact.
# ---------------------------------------------------------------------------


def test_artifact_provenance_matches_final_verdict_evidence(tmp_path: Path) -> None:
    context = _full_pipeline(tmp_path, threshold=1)
    evidence = context.final_verdict.get_evidence(context.hypothesis_id)
    artifact = present_eligible_artifact(
        context.hypothesis_id, registry=context.registry, final_verdict=context.final_verdict
    )
    assert artifact.hypothesis_id == evidence.hypothesis_id
    assert artifact.hypothesis_family_id == evidence.hypothesis_family_id
    assert artifact.bound_hypothesis_content_hash == evidence.bound_hypothesis_content_hash
    assert artifact.manifest_semantic_hash == evidence.manifest_semantic_hash
    assert artifact.manifest_artifact_hash_ref == evidence.manifest_artifact_hash_ref
    assert artifact.final_verdict_evidence_hash == evidence.evidence_hash


def test_tampered_final_verdict_evidence_rejected(tmp_path: Path) -> None:
    context = _full_pipeline(tmp_path, threshold=1)
    with sqlite3.connect(context.db_path) as db:
        row = db.execute(
            "SELECT evidence_artifact_hash_ref FROM final_verdict_evidence WHERE hypothesis_id=?",
            (context.hypothesis_id,),
        ).fetchone()
    object_path = Path(context.store.resolve_verified(row[0]).path)
    object_path.write_bytes(b'{"tampered": true}')
    with pytest.raises(ResearchEligibilityError, match="could not be verified"):
        present_eligible_artifact(
            context.hypothesis_id, registry=context.registry, final_verdict=context.final_verdict
        )


# ---------------------------------------------------------------------------
# Idempotent, deterministic identical presentation.
# ---------------------------------------------------------------------------


def test_identical_presentation_is_idempotent(tmp_path: Path) -> None:
    context = _full_pipeline(tmp_path, threshold=1)
    first = present_eligible_artifact(
        context.hypothesis_id, registry=context.registry, final_verdict=context.final_verdict
    )
    second = present_eligible_artifact(
        context.hypothesis_id, registry=context.registry, final_verdict=context.final_verdict
    )
    assert first.artifact_hash == second.artifact_hash
    assert first.semantic_projection() == second.semantic_projection()


def test_concurrent_presentation_produces_consistent_artifacts(tmp_path: Path) -> None:
    context = _full_pipeline(tmp_path, threshold=1)
    results: list[ResearchEligibilityArtifactV1] = []
    errors: list[BaseException] = []
    barrier = threading.Barrier(4)

    def _worker() -> None:
        barrier.wait()
        try:
            results.append(
                present_eligible_artifact(
                    context.hypothesis_id, registry=context.registry, final_verdict=context.final_verdict
                )
            )
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=_worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert not errors, errors
    assert len(results) == 4
    assert len({item.artifact_hash for item in results}) == 1


# ---------------------------------------------------------------------------
# No trading authorization content; no order path; no plaintext/key.
# ---------------------------------------------------------------------------


def test_artifact_carries_no_trading_or_order_fields(tmp_path: Path) -> None:
    import dataclasses

    field_names = {item.name for item in dataclasses.fields(ResearchEligibilityArtifactV1)}
    forbidden = {
        "symbol", "side", "size", "price", "volume", "order", "broker",
        "risk_limit", "authorized", "authorization", "eligible_for_live", "live",
    }
    assert field_names.isdisjoint(forbidden), field_names & forbidden


def test_no_holdout_plaintext_key_or_broker_call_in_boundary() -> None:
    source = Path("src/trademind/discovery/research_eligibility_boundary.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    lowered = {name.lower() for name in imported}
    forbidden_import_substrings = (
        "holdout_crypto", "holdout_keys", "holdout_sealer", "holdout_runner",
        "requests", "httpx", "urllib", "socket",
        "openai", "anthropic", "claude", "ollama",
        "metatrader5", "mt5",
    )
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
        "decrypt", "decrypt_bytes", "run_once", "evaluate",
        "OrderSend", "PositionClose", "PositionModify",
    }
    assert not (called_names & forbidden_calls), called_names & forbidden_calls
    assert "CTrade" not in source
    assert "TRADE_ACTION_DEAL" not in source
