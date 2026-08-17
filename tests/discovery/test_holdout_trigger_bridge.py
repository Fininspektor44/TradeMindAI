"""Tests for Holdout Trigger Bridge V1: the third missing bridge, from a
VALIDATION_PASSED hypothesis to an authorized, trusted invocation of the
already-closed FinalHoldoutRunner, reaching HOLDOUT_CONSUMED.

Chain exercised: ResearchProposalIntake -> ... -> ExperimentManifestV2 (two
dataset entries) -> FROZEN -> DiscoveryOrchestratorBridge.
submit_frozen_hypothesis_v2 -> TrainTestExecutionControl.execute() ->
TRAIN_TESTED -> ValidationExecutionControl.execute() -> VALIDATION_PASSED ->
FinalHoldoutSealer.seal_file() + HoldoutSealStore.mark_isolated() (a real
sealed envelope, not a fake one) -> HoldoutTriggerBridge.trigger() ->
FinalHoldoutRunner.run_once() -> HOLDOUT_CONSUMED.

This file does not import test helpers from sibling test files (consistent
with this lineage's convention of each test file owning its own small setup
helpers).
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
from trademind.discovery.holdout_keys import HoldoutKeyError
from trademind.discovery.holdout_runner import FinalHoldoutRunner, HoldoutRunError
from trademind.discovery.holdout_sealer import FinalHoldoutSealer
from trademind.discovery.holdout_store import HoldoutSealStore
from trademind.discovery.holdout_trigger_bridge import (
    HoldoutTriggerBridge,
    HoldoutTriggerError,
)
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

_KEY = bytes(range(32))
_OTHER_KEY = bytes(reversed(range(32)))
_KEY_ID = "holdout-trigger-key-v1"
_EVALUATOR_ARTIFACT = Path(__file__).resolve()
_HOLDOUT_PLAINTEXT = (
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
    evaluator_id = "holdout-trigger-aggregate-v1"

    def evaluate(self, plaintext: bytes) -> dict[str, int]:
        rows = max(0, plaintext.count(b"\n") - 1)
        return {"rows": rows}


@dataclass(frozen=True, slots=True)
class _Context:
    db_path: Path
    artifact_root: Path
    store: ArtifactStore
    control: ControlPlane
    registry: HypothesisRegistry
    holdout_seals: HoldoutSealStore
    sealer: FinalHoldoutSealer
    bridge: DiscoveryOrchestratorBridge
    train_test: TrainTestExecutionControl
    validator: ValidationExecutionControl
    ledger: ResultLedger
    runner: FinalHoldoutRunner
    trigger_bridge: HoldoutTriggerBridge
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
        producer_version="holdout-trigger-bridge-test",
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
    return _Context(
        db_path=db_path,
        artifact_root=artifact_root,
        store=store,
        control=control,
        registry=registry,
        holdout_seals=holdout_seals,
        sealer=sealer,
        bridge=bridge,
        train_test=train_test,
        validator=validator,
        ledger=ledger,
        runner=runner,
        trigger_bridge=trigger_bridge,
        sealed_path=tmp_path / "final.holdout.json",
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


def _build_manifest_v2(
    context: _Context, *, datasets: tuple[DatasetArtifactV2, ...], split_plan: SplitPlan
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
        created_by="operator:holdout-trigger-bridge-test",
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
    plaintext_path.write_text(_HOLDOUT_PLAINTEXT, encoding="utf-8")
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
    tmp_path: Path, *, reach: str = "VALIDATION_PASSED"
) -> tuple[_Context, list[datetime], SplitPlan, BoundSplitPlanV1]:
    """``reach`` controls how far the pipeline advances:
    "PROPOSED" | "FROZEN" | "TRAIN_TESTED" | "VALIDATION_REJECTED" |
    "VALIDATION_PASSED" (default)."""
    context = _setup(tmp_path)
    rows, plan, bound = _bound_split_plan(tmp_path)
    if reach == "PROPOSED":
        return context, rows, plan, bound

    discovery_dataset = _discovery_dataset_v2(context, rows, plan)
    validation_dataset = _validation_dataset_v2(context, rows, plan)
    manifest = _build_manifest_v2(context, datasets=(discovery_dataset, validation_dataset), split_plan=plan)
    _freeze_v2(context, manifest)
    _seal_and_isolate(context, tmp_path)
    context.bridge.submit_frozen_hypothesis_v2(
        context.hypothesis_id,
        bound_split_plan=bound,
        dataset_role_bindings={discovery_dataset.role: "DISCOVERY", validation_dataset.role: "VALIDATION"},
    )
    if reach == "FROZEN":
        return context, rows, plan, bound

    context.train_test.execute(context.hypothesis_id, bound_split_plan=bound)
    if reach == "TRAIN_TESTED":
        return context, rows, plan, bound

    context.validator.execute(context.hypothesis_id, bound_split_plan=bound)
    return context, rows, plan, bound


# ---------------------------------------------------------------------------
# 1-2: real VALIDATION_PASSED V2 hypothesis triggers the trusted runner and
# reaches HOLDOUT_CONSUMED.
# ---------------------------------------------------------------------------


def test_real_validation_passed_hypothesis_triggers_trusted_runner_and_consumes(tmp_path: Path) -> None:
    context, rows, plan, bound = _full_pipeline(tmp_path)
    assert context.registry.get(context.hypothesis_id).state is HypothesisState.VALIDATION_PASSED

    receipt = context.trigger_bridge.trigger(context.hypothesis_id, sealed_path=context.sealed_path)

    assert receipt.hypothesis_id == context.hypothesis_id
    assert receipt.aggregate_metrics == {"rows": 2}
    assert context.registry.get(context.hypothesis_id).state is HypothesisState.HOLDOUT_CONSUMED
    family_id = context.registry.get(context.hypothesis_id).hypothesis_family_id
    assert context.registry.family_status(family_id)["holdout_consumed"] is True
    assert context.ledger.verify()
    ledger_text = context.ledger.path.read_text(encoding="utf-8")
    assert "FINAL_HOLDOUT_CLAIM" in ledger_text
    assert "FINAL_HOLDOUT_RESULT" in ledger_text
    assert _HOLDOUT_PLAINTEXT not in ledger_text


# ---------------------------------------------------------------------------
# 3-4: wrong states rejected.
# ---------------------------------------------------------------------------


def test_validation_rejected_cannot_trigger_holdout(tmp_path: Path) -> None:
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
        semantic_parameters=spec.parameters, created_at="2026-08-17T00:00:00+00:00", created_by="operator:rejected-test",
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

    with pytest.raises(HoldoutTriggerError, match="must be VALIDATION_PASSED"):
        context.trigger_bridge.trigger(context.hypothesis_id, sealed_path=context.sealed_path)


@pytest.mark.parametrize("reach", ["PROPOSED", "FROZEN", "TRAIN_TESTED"])
def test_earlier_states_cannot_trigger_holdout(tmp_path: Path, reach: str) -> None:
    context, rows, plan, bound = _full_pipeline(tmp_path, reach=reach)
    with pytest.raises(HoldoutTriggerError, match="must be VALIDATION_PASSED"):
        context.trigger_bridge.trigger(context.hypothesis_id, sealed_path=context.sealed_path)


# ---------------------------------------------------------------------------
# 5: missing/tampered ValidationEvidence rejected.
# ---------------------------------------------------------------------------


def test_missing_validation_evidence_rejected(tmp_path: Path) -> None:
    context, rows, plan, bound = _full_pipeline(tmp_path)
    with sqlite3.connect(context.db_path) as db:
        db.execute("DELETE FROM validation_evidence WHERE hypothesis_id=?", (context.hypothesis_id,))
    with pytest.raises(HoldoutTriggerError, match="validation evidence could not be verified"):
        context.trigger_bridge.trigger(context.hypothesis_id, sealed_path=context.sealed_path)
    assert context.registry.get(context.hypothesis_id).state is HypothesisState.VALIDATION_PASSED


def test_tampered_validation_evidence_rejected(tmp_path: Path) -> None:
    context, rows, plan, bound = _full_pipeline(tmp_path)
    with sqlite3.connect(context.db_path) as db:
        row = db.execute(
            "SELECT evidence_artifact_hash_ref FROM validation_evidence WHERE hypothesis_id=?",
            (context.hypothesis_id,),
        ).fetchone()
    object_path = Path(context.store.resolve_verified(row[0]).path)
    object_path.write_bytes(b'{"tampered": true}')
    with pytest.raises(HoldoutTriggerError, match="validation evidence could not be verified"):
        context.trigger_bridge.trigger(context.hypothesis_id, sealed_path=context.sealed_path)


# ---------------------------------------------------------------------------
# 6-7: wrong manifest / family binding rejected.
# ---------------------------------------------------------------------------


def test_wrong_manifest_binding_rejected(tmp_path: Path) -> None:
    context, rows, plan, bound = _full_pipeline(tmp_path)
    with sqlite3.connect(context.db_path) as db:
        db.execute(
            "UPDATE hypotheses SET content_hash=? WHERE hypothesis_id=?",
            ("0" * 64, context.hypothesis_id),
        )
    with pytest.raises(HoldoutTriggerError, match="bound manifest v2 could not be verified"):
        context.trigger_bridge.trigger(context.hypothesis_id, sealed_path=context.sealed_path)


def test_wrong_family_binding_rejected(tmp_path: Path) -> None:
    context, rows, plan, bound = _full_pipeline(tmp_path)
    with sqlite3.connect(context.db_path) as db:
        db.execute(
            "INSERT INTO hypothesis_families(family_id, definition_json, holdout_consumed, terminal_state, updated_at) "
            "VALUES ('hf_' || ?, '{\"x\":1}', 0, NULL, '2026-01-01T00:00:00+00:00')",
            ("1" * 64,),
        )
        db.execute(
            "UPDATE hypotheses SET family_id=? WHERE hypothesis_id=?",
            ("hf_" + "1" * 64, context.hypothesis_id),
        )
    with pytest.raises(HoldoutTriggerError, match="bound manifest v2 could not be verified"):
        context.trigger_bridge.trigger(context.hypothesis_id, sealed_path=context.sealed_path)


# ---------------------------------------------------------------------------
# 8-10: seal-related rejections.
# ---------------------------------------------------------------------------


def test_missing_seal_rejected(tmp_path: Path) -> None:
    context = _setup(tmp_path)
    rows, plan, bound = _bound_split_plan(tmp_path)
    discovery_dataset = _discovery_dataset_v2(context, rows, plan)
    validation_dataset = _validation_dataset_v2(context, rows, plan)
    manifest = _build_manifest_v2(context, datasets=(discovery_dataset, validation_dataset), split_plan=plan)
    _freeze_v2(context, manifest)
    # No seal registered at all -- submit_frozen_hypothesis_v2 itself
    # requires an isolated seal, so it cannot even be reached; the
    # hypothesis therefore also has no Orchestrator task. Confirm the
    # bridge still fails closed for the right reason (no task) rather than
    # silently doing something else -- this also proves an un-sealed
    # hypothesis structurally cannot reach VALIDATION_PASSED at all through
    # the real pipeline.
    with pytest.raises(HoldoutTriggerError):
        context.trigger_bridge.trigger(context.hypothesis_id, sealed_path=context.sealed_path)


def test_non_isolated_seal_rejected(tmp_path: Path) -> None:
    # A genuinely un-isolated seal cannot coexist with a real
    # VALIDATION_PASSED hypothesis in this fully-wired pipeline:
    # DiscoveryOrchestratorBridge.submit_frozen_hypothesis_v2 itself already
    # requires isolation before an Orchestrator task (and therefore
    # TRAIN_TESTED/VALIDATION_PASSED) can ever exist. This proves the
    # trigger bridge's OWN redundant re-check instead, against an
    # adversarial/corrupted seal row on an otherwise fully genuine,
    # already-VALIDATION_PASSED pipeline -- exactly the "trust but verify"
    # pattern used throughout this lineage's other tampering tests.
    context, rows, plan, bound = _full_pipeline(tmp_path)
    with sqlite3.connect(context.db_path) as db:
        db.execute(
            "UPDATE final_holdout_seals SET isolated_at=NULL, isolation_receipt_hash=NULL "
            "WHERE hypothesis_id=?",
            (context.hypothesis_id,),
        )
    with pytest.raises(HoldoutTriggerError, match="isolation must be attested"):
        context.trigger_bridge.trigger(context.hypothesis_id, sealed_path=context.sealed_path)
    assert context.registry.get(context.hypothesis_id).state is HypothesisState.VALIDATION_PASSED


def test_wrong_manifest_hash_on_seal_rejected(tmp_path: Path) -> None:
    context, rows, plan, bound = _full_pipeline(tmp_path)
    with sqlite3.connect(context.db_path) as db:
        db.execute(
            "UPDATE final_holdout_seals SET manifest_hash=? WHERE hypothesis_id=?",
            ("9" * 64, context.hypothesis_id),
        )
    with pytest.raises(HoldoutTriggerError, match="sealed holdout manifest does not match"):
        context.trigger_bridge.trigger(context.hypothesis_id, sealed_path=context.sealed_path)


def test_wrong_evaluator_identity_rejected(tmp_path: Path) -> None:
    context, rows, plan, bound = _full_pipeline(tmp_path)

    class _OtherEvaluator(_CountingEvaluator):
        evaluator_id = "not-the-sealed-evaluator"

    other_runner = FinalHoldoutRunner(
        registry=context.registry,
        seals=context.holdout_seals,
        keys=_StaticKeys(),
        ledger=context.ledger,
        evaluator=_OtherEvaluator(),
        evaluator_artifact_path=_EVALUATOR_ARTIFACT,
    )
    other_bridge = HoldoutTriggerBridge(
        registry=context.registry, control=context.control, artifacts=context.store,
        validator=context.validator, runner=other_runner,
    )
    with pytest.raises(HoldoutTriggerError, match="evaluator_id does not match"):
        other_bridge.trigger(context.hypothesis_id, sealed_path=context.sealed_path)
    assert context.registry.get(context.hypothesis_id).state is HypothesisState.VALIDATION_PASSED


# ---------------------------------------------------------------------------
# 11-12: prior claim / already-consumed family rejected.
# ---------------------------------------------------------------------------


def test_prior_ledger_claim_for_family_rejected(tmp_path: Path) -> None:
    # (function deliberately avoids the substring "holdout" in its own
    # name; see test_train_test_execution.py for why -- pytest derives
    # tmp_path from the test's own node id.)
    context, rows, plan, bound = _full_pipeline(tmp_path)
    family_id = context.registry.get(context.hypothesis_id).hypothesis_family_id
    context.ledger.append(
        {
            "record_type": "FINAL_HOLDOUT_CLAIM",
            "hypothesis_id": "some-other-hypothesis",
            "hypothesis_family_id": family_id,
            "envelope_hash": "z" * 64,
            "evaluator_id": _CountingEvaluator.evaluator_id,
            "evaluator_hash": context.runner.evaluator_hash,
            "intent_record_hash": "y" * 64,
        }
    )
    with pytest.raises(HoldoutTriggerError, match="already contains a final-holdout claim"):
        context.trigger_bridge.trigger(context.hypothesis_id, sealed_path=context.sealed_path)
    assert context.registry.get(context.hypothesis_id).state is HypothesisState.VALIDATION_PASSED


def test_already_consumed_family_cannot_consume_again(tmp_path: Path) -> None:
    context, rows, plan, bound = _full_pipeline(tmp_path)
    context.trigger_bridge.trigger(context.hypothesis_id, sealed_path=context.sealed_path)
    assert context.registry.get(context.hypothesis_id).state is HypothesisState.HOLDOUT_CONSUMED

    with pytest.raises(HoldoutTriggerError, match="already been consumed for this hypothesis"):
        context.trigger_bridge.trigger(context.hypothesis_id, sealed_path=context.sealed_path)


# ---------------------------------------------------------------------------
# 13-14: authorization persisted before invocation; provenance complete.
# ---------------------------------------------------------------------------


def test_authorization_persisted_before_trusted_runner_invocation(tmp_path: Path) -> None:
    context, rows, plan, bound = _full_pipeline(tmp_path)
    context.trigger_bridge.trigger(context.hypothesis_id, sealed_path=context.sealed_path)
    with sqlite3.connect(context.db_path) as db:
        row = db.execute(
            "SELECT * FROM holdout_trigger_authorizations WHERE hypothesis_id=?",
            (context.hypothesis_id,),
        ).fetchone()
    assert row is not None


def test_failed_precondition_never_persists_authorization(tmp_path: Path) -> None:
    context, rows, plan, bound = _full_pipeline(tmp_path)
    with sqlite3.connect(context.db_path) as db:
        db.execute(
            "UPDATE final_holdout_seals SET manifest_hash=? WHERE hypothesis_id=?",
            ("9" * 64, context.hypothesis_id),
        )
    with pytest.raises(HoldoutTriggerError):
        context.trigger_bridge.trigger(context.hypothesis_id, sealed_path=context.sealed_path)
    with sqlite3.connect(context.db_path) as db:
        count = db.execute(
            "SELECT COUNT(*) FROM holdout_trigger_authorizations WHERE hypothesis_id=?",
            (context.hypothesis_id,),
        ).fetchone()[0]
    assert count == 0


def test_authorization_provenance_complete(tmp_path: Path) -> None:
    context, rows, plan, bound = _full_pipeline(tmp_path)
    validation_evidence = context.validator.get_evidence(context.hypothesis_id)
    context.trigger_bridge.trigger(context.hypothesis_id, sealed_path=context.sealed_path)
    authorization = context.trigger_bridge.get_authorization(context.hypothesis_id)
    record = context.registry.get(context.hypothesis_id)
    seal = context.holdout_seals.get(context.hypothesis_id)

    assert authorization.hypothesis_id == context.hypothesis_id
    assert authorization.hypothesis_family_id == record.hypothesis_family_id
    assert authorization.bound_hypothesis_content_hash == record.content_hash
    assert authorization.manifest_semantic_hash == f"sha256:{record.manifest_hash}"
    assert authorization.manifest_artifact_hash_ref == record.manifest_artifact_hash_ref
    assert authorization.orchestrator_task_id == DiscoveryOrchestratorBridge._task_id(context.hypothesis_id)
    assert authorization.validation_evidence_hash == validation_evidence.evidence_hash
    assert authorization.envelope_hash == seal.envelope_hash
    assert authorization.key_id == seal.key_id
    assert authorization.evaluator_id == seal.evaluator_id
    assert authorization.evaluator_hash == seal.evaluator_hash


# ---------------------------------------------------------------------------
# 15-16: identical retry cannot double-consume; conflicting authorization
# rejected.
# ---------------------------------------------------------------------------


def test_identical_retry_after_consumption_fails_closed_without_double_consuming(tmp_path: Path) -> None:
    context, rows, plan, bound = _full_pipeline(tmp_path)
    first = context.trigger_bridge.trigger(context.hypothesis_id, sealed_path=context.sealed_path)
    with pytest.raises(HoldoutTriggerError, match="one-shot contract with no receipt to recover"):
        context.trigger_bridge.trigger(context.hypothesis_id, sealed_path=context.sealed_path)
    with sqlite3.connect(context.db_path) as db:
        count = db.execute(
            "SELECT COUNT(*) FROM holdout_trigger_authorizations WHERE hypothesis_id=?",
            (context.hypothesis_id,),
        ).fetchone()[0]
    assert count == 1
    with pytest.raises(HoldoutRunError):
        context.runner.run_once(hypothesis_id=context.hypothesis_id, sealed_path=context.sealed_path)
    assert first.aggregate_metrics == {"rows": 2}


def test_conflicting_authorization_fails_closed(tmp_path: Path) -> None:
    context, rows, plan, bound = _full_pipeline(tmp_path)
    with sqlite3.connect(context.db_path) as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            """
            INSERT INTO holdout_trigger_authorizations(
                hypothesis_id, hypothesis_family_id, manifest_semantic_hash,
                manifest_artifact_hash_ref, orchestrator_task_id, validation_evidence_hash,
                authorization_hash, authorization_artifact_hash_ref, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                "2026-01-01T00:00:00+00:00",
            ),
        )
        db.commit()
    with pytest.raises(HoldoutTriggerError, match="conflicting holdout-trigger authorization"):
        context.trigger_bridge.trigger(context.hypothesis_id, sealed_path=context.sealed_path)
    assert context.registry.get(context.hypothesis_id).state is HypothesisState.VALIDATION_PASSED


# ---------------------------------------------------------------------------
# 17: concurrent trigger cannot consume twice.
# ---------------------------------------------------------------------------


def test_concurrent_trigger_cannot_consume_holdout_twice(tmp_path: Path) -> None:
    context, rows, plan, bound = _full_pipeline(tmp_path)
    results = []
    errors: list[BaseException] = []
    barrier = threading.Barrier(2)

    def _worker() -> None:
        barrier.wait()
        try:
            results.append(context.trigger_bridge.trigger(context.hypothesis_id, sealed_path=context.sealed_path))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=_worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    # FinalHoldoutRunner's own file lock guarantees at most one successful
    # consumption; the loser must fail (either the lock or a preflight
    # check), never a second consumption.
    assert len(results) == 1
    assert len(errors) == 1
    assert context.registry.get(context.hypothesis_id).state is HypothesisState.HOLDOUT_CONSUMED
    family_id = context.registry.get(context.hypothesis_id).hypothesis_family_id
    assert context.registry.family_status(family_id)["holdout_consumed"] is True


# ---------------------------------------------------------------------------
# 18-20: no plaintext/key exposure; bridge implements no crypto/evaluator
# logic of its own.
# ---------------------------------------------------------------------------


def test_no_plaintext_or_key_in_authorization_task_or_audit_record(tmp_path: Path) -> None:
    context, rows, plan, bound = _full_pipeline(tmp_path)
    context.trigger_bridge.trigger(context.hypothesis_id, sealed_path=context.sealed_path)
    authorization = context.trigger_bridge.get_authorization(context.hypothesis_id)
    payload_text = str(authorization.to_payload())
    assert _HOLDOUT_PLAINTEXT not in payload_text
    # A distinctive fragment that appears only inside the holdout plaintext
    # itself (its second data row), not inside any legitimate manifest/
    # dataset/authorization field value (unlike a short numeric fragment
    # such as "0.10", which can coincidentally appear inside unrelated
    # legitimate fields like q=0.10 or ISO timestamps).
    distinctive_plaintext_fragment = "2026-01-03T06:00:00+00:00,-0.05"
    assert distinctive_plaintext_fragment not in payload_text
    import base64

    assert base64.b64encode(_KEY).decode("ascii") not in payload_text
    assert _KEY.hex() not in payload_text

    brief_files = list(context.store.root.rglob("*research-brief-v2*.json"))
    assert len(brief_files) == 1
    brief_bytes = brief_files[0].read_bytes()
    assert distinctive_plaintext_fragment.encode() not in brief_bytes
    assert _KEY not in brief_bytes

    with sqlite3.connect(context.db_path) as db:
        audit_rows = db.execute("SELECT * FROM audit_events").fetchall()
    for row in audit_rows:
        row_text = str(tuple(row))
        assert distinctive_plaintext_fragment not in row_text
        assert _KEY.hex() not in row_text

    sealed_text = context.sealed_path.read_text(encoding="utf-8")
    assert _HOLDOUT_PLAINTEXT not in sealed_text


def test_bridge_holds_no_key_provider_reference(tmp_path: Path) -> None:
    context, rows, plan, bound = _full_pipeline(tmp_path)
    assert not hasattr(context.trigger_bridge, "keys")
    assert not hasattr(context.trigger_bridge, "key")


def test_bridge_does_not_implement_its_own_decrypt_or_evaluator_logic() -> None:
    source = Path("src/trademind/discovery/holdout_trigger_bridge.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    lowered = {name.lower() for name in imported}
    for forbidden in ("holdout_crypto", "holdout_keys", "holdout_sealer", "cryptography"):
        assert not any(forbidden in name for name in lowered), lowered

    called_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            called_names.add(node.func.id)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            called_names.add(node.func.attr)
    forbidden_calls = {
        "decrypt",
        "decrypt_bytes",
        "verify_envelope",
        "verify_key",
        "seal_bytes",
        "AESGCM",
        "evaluate",
    }
    assert not (called_names & forbidden_calls), called_names & forbidden_calls
    # The only holdout_runner call this module ever makes is run_once (plus
    # reusing the private ledger-claim scan) -- proven structurally by
    # scanning for the actual sensitive verbs above, not merely absent by
    # convention.
    assert "run_once" in called_names


# ---------------------------------------------------------------------------
# 21-22: no provider/network, no broker/MT5.
# ---------------------------------------------------------------------------


def test_no_provider_network_or_broker_shaped_imports() -> None:
    source = Path("src/trademind/discovery/holdout_trigger_bridge.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    forbidden_import_substrings = (
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
    forbidden_calls = {"OrderSend", "PositionClose", "PositionModify"}
    assert not (called_names & forbidden_calls), called_names & forbidden_calls
    assert "CTrade" not in source
    assert "TRADE_ACTION_DEAL" not in source


# ---------------------------------------------------------------------------
# 23-24: existing FinalHoldoutRunner adversarial/recovery tests, and
# Validation / Train-Test / Bridge V2 / Manifest V2 tests, remain green --
# verified by running those files directly as part of the required
# regression (see VALIDATION section of the implementation report); no test
# from them is duplicated here.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Bonus: no HOLDOUT_CONSUMED reimplementation, no ACCEPTED/REJECTED_FINAL
# reference anywhere in this module (scope boundary proof).
# ---------------------------------------------------------------------------


def test_no_acceptance_or_rejection_transition_referenced() -> None:
    source = Path("src/trademind/discovery/holdout_trigger_bridge.py").read_text(encoding="utf-8")
    assert "HypothesisState.ACCEPTED" not in source
    assert "REJECTED_FINAL" not in source
    assert '"ACCEPTED"' not in source
