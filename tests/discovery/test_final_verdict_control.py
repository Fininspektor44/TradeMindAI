"""Tests for Final Verdict / Acceptance Control V1: the terminal research
lifecycle layer, from a HOLDOUT_CONSUMED hypothesis to ACCEPTED or
REJECTED_FINAL.

Chain exercised: ResearchProposalIntake -> ... -> ExperimentManifestV2 (two
dataset entries + final_holdout_criteria) -> FROZEN -> Bridge V2 submission
-> TrainTestExecutionControl -> TRAIN_TESTED -> ValidationExecutionControl
-> VALIDATION_PASSED -> FinalHoldoutSealer.seal_file() + mark_isolated() (a
real sealed envelope) -> HoldoutTriggerBridge.trigger() -> real
FinalHoldoutRunner.run_once() -> HOLDOUT_CONSUMED ->
FinalVerdictAcceptanceControl.finalize() -> ACCEPTED / REJECTED_FINAL.

This file does not import test helpers from sibling test files (consistent
with this lineage's convention of each test file owning its own small setup
helpers).
"""

from __future__ import annotations

import ast
import io
import json
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
from trademind.discovery.final_verdict_control import (
    FinalVerdictAcceptanceControl,
    FinalVerdictError,
    FinalVerdictEvidenceV1,
)
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
_HOLDOUT_METRIC = "rows"

_KEY = bytes(range(32))
_KEY_ID = "final-verdict-key-v1"
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
    evaluator_id = "final-verdict-aggregate-v1"

    def evaluate(self, plaintext: bytes) -> dict[str, int]:
        rows = max(0, plaintext.count(b"\n") - 1)
        return {_HOLDOUT_METRIC: rows}


class _OtherCountingEvaluator(_CountingEvaluator):
    """Same behavior and same source file (so the same evaluator_hash) as
    _CountingEvaluator, but a different evaluator_id -- used only to prove
    finalize()'s cross-check catches a mismatched evaluator IDENTITY, not
    to exercise a different evaluator_hash (that would require a second
    source file, unnecessary for this specific invariant)."""

    evaluator_id = "final-verdict-other-evaluator-v1"


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
        producer_version="final-verdict-control-test",
        git_commit="f" * 40,
        revision_source="git_worktree",
    )


def _timestamps(count: int = 12) -> list[datetime]:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return [start + timedelta(hours=i) for i in range(count)]


def _csv_bytes(rows: list[datetime], *, base: float = 10.0) -> bytes:
    lines = [f"time,{_METRIC}"] + [f"{t.isoformat()},{base + i}" for i, t in enumerate(rows)]
    return ("\n".join(lines) + "\n").encode("utf-8")


def _setup(tmp_path: Path, *, symbol: str = "XAUUSD", evaluator: _CountingEvaluator | None = None) -> _Context:
    evaluator = evaluator if evaluator is not None else _CountingEvaluator()
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
        evaluator=evaluator,
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
        final_verdict=final_verdict,
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


def _final_holdout_criteria(*, threshold: int = 1) -> FinalHoldoutCriteriaV1:
    return FinalHoldoutCriteriaV1(
        mode=CriteriaMode.ALL,
        criteria=(
            EvaluationCriterionV1(
                metric=_HOLDOUT_METRIC, operator=CriterionOperator.GREATER_THAN_OR_EQUAL, threshold=threshold
            ),
        ),
    )


def _build_manifest_v2(
    context: _Context,
    *,
    datasets: tuple[DatasetArtifactV2, ...],
    split_plan: SplitPlan,
    final_holdout_criteria: FinalHoldoutCriteriaV1 | None,
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
        created_by="operator:final-verdict-control-test",
        final_holdout_criteria=final_holdout_criteria,
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


def _seal_and_isolate(context: _Context, tmp_path: Path, *, evaluator_id: str = _CountingEvaluator.evaluator_id) -> None:
    plaintext_path = tmp_path / "final-plaintext.csv"
    plaintext_path.write_text(_HOLDOUT_PLAINTEXT, encoding="utf-8")
    context.sealer.seal_file(
        hypothesis_id=context.hypothesis_id,
        plaintext_path=plaintext_path,
        destination_path=context.sealed_path,
        key_id=_KEY_ID,
        evaluator_id=evaluator_id,
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
    tmp_path: Path,
    *,
    threshold: int = 1,
    reach: str = "HOLDOUT_CONSUMED",
    include_final_holdout_criteria: bool = True,
    evaluator: _CountingEvaluator | None = None,
) -> tuple[_Context, list[datetime], SplitPlan, BoundSplitPlanV1]:
    """``reach`` controls how far the pipeline advances:
    "VALIDATION_PASSED" | "HOLDOUT_CONSUMED" (default)."""
    evaluator = evaluator if evaluator is not None else _CountingEvaluator()
    context = _setup(tmp_path, evaluator=evaluator)
    rows, plan, bound = _bound_split_plan(tmp_path)
    discovery_dataset = _discovery_dataset_v2(context, rows, plan)
    validation_dataset = _validation_dataset_v2(context, rows, plan)
    manifest = _build_manifest_v2(
        context,
        datasets=(discovery_dataset, validation_dataset),
        split_plan=plan,
        final_holdout_criteria=(
            _final_holdout_criteria(threshold=threshold) if include_final_holdout_criteria else None
        ),
    )
    _freeze_v2(context, manifest)
    _seal_and_isolate(context, tmp_path, evaluator_id=evaluator.evaluator_id)
    context.bridge.submit_frozen_hypothesis_v2(
        context.hypothesis_id,
        bound_split_plan=bound,
        dataset_role_bindings={discovery_dataset.role: "DISCOVERY", validation_dataset.role: "VALIDATION"},
    )
    context.train_test.execute(context.hypothesis_id, bound_split_plan=bound)
    context.validator.execute(context.hypothesis_id, bound_split_plan=bound)
    if reach == "VALIDATION_PASSED":
        return context, rows, plan, bound
    context.trigger_bridge.trigger(context.hypothesis_id, sealed_path=context.sealed_path)
    return context, rows, plan, bound


# ---------------------------------------------------------------------------
# 1-2: real HOLDOUT_CONSUMED -> ACCEPTED / REJECTED_FINAL.
# ---------------------------------------------------------------------------


def test_real_consumed_hypothesis_reaches_accepted_when_criteria_pass(tmp_path: Path) -> None:
    context, rows, plan, bound = _full_pipeline(tmp_path, threshold=1)
    assert context.registry.get(context.hypothesis_id).state is HypothesisState.HOLDOUT_CONSUMED

    evidence = context.final_verdict.finalize(context.hypothesis_id)

    assert isinstance(evidence, FinalVerdictEvidenceV1)
    assert evidence.verdict == HypothesisState.ACCEPTED.value
    assert evidence.aggregate_metrics[_HOLDOUT_METRIC] == 2
    assert evidence.criteria_results[0]["passed"] is True
    assert context.registry.get(context.hypothesis_id).state is HypothesisState.ACCEPTED


def test_real_consumed_hypothesis_reaches_rejected_final_when_criteria_fail(tmp_path: Path) -> None:
    context, rows, plan, bound = _full_pipeline(tmp_path, threshold=1_000_000)

    evidence = context.final_verdict.finalize(context.hypothesis_id)

    assert evidence.verdict == HypothesisState.REJECTED_FINAL.value
    assert evidence.criteria_results[0]["passed"] is False
    assert context.registry.get(context.hypothesis_id).state is HypothesisState.REJECTED_FINAL


# ---------------------------------------------------------------------------
# 3: non-HOLDOUT_CONSUMED rejected.
# ---------------------------------------------------------------------------


def test_non_consumed_hypothesis_rejected(tmp_path: Path) -> None:
    context, rows, plan, bound = _full_pipeline(tmp_path, reach="VALIDATION_PASSED")
    with pytest.raises(FinalVerdictError, match="must be HOLDOUT_CONSUMED"):
        context.final_verdict.finalize(context.hypothesis_id)


# ---------------------------------------------------------------------------
# 4: missing final_holdout_criteria rejected.
# ---------------------------------------------------------------------------


def test_missing_final_criteria_rejected(tmp_path: Path) -> None:
    context, rows, plan, bound = _full_pipeline(tmp_path, include_final_holdout_criteria=False)
    with pytest.raises(FinalVerdictError, match="declares no final_holdout_criteria"):
        context.final_verdict.finalize(context.hypothesis_id)
    assert context.registry.get(context.hypothesis_id).state is HypothesisState.HOLDOUT_CONSUMED


# ---------------------------------------------------------------------------
# 5: missing/tampered ValidationEvidence rejected.
# ---------------------------------------------------------------------------


def test_missing_validation_evidence_rejected(tmp_path: Path) -> None:
    context, rows, plan, bound = _full_pipeline(tmp_path)
    with sqlite3.connect(context.db_path) as db:
        db.execute("DELETE FROM validation_evidence WHERE hypothesis_id=?", (context.hypothesis_id,))
    with pytest.raises(FinalVerdictError, match="validation evidence could not be verified"):
        context.final_verdict.finalize(context.hypothesis_id)


def test_tampered_validation_evidence_rejected(tmp_path: Path) -> None:
    context, rows, plan, bound = _full_pipeline(tmp_path)
    with sqlite3.connect(context.db_path) as db:
        row = db.execute(
            "SELECT evidence_artifact_hash_ref FROM validation_evidence WHERE hypothesis_id=?",
            (context.hypothesis_id,),
        ).fetchone()
    object_path = Path(context.store.resolve_verified(row[0]).path)
    object_path.write_bytes(b'{"tampered": true}')
    with pytest.raises(FinalVerdictError, match="validation evidence could not be verified"):
        context.final_verdict.finalize(context.hypothesis_id)


# ---------------------------------------------------------------------------
# 6: missing/tampered Holdout authorization rejected.
# ---------------------------------------------------------------------------


def test_missing_trigger_authorization_rejected(tmp_path: Path) -> None:
    context, rows, plan, bound = _full_pipeline(tmp_path)
    with sqlite3.connect(context.db_path) as db:
        db.execute(
            "DELETE FROM holdout_trigger_authorizations WHERE hypothesis_id=?", (context.hypothesis_id,)
        )
    with pytest.raises(FinalVerdictError, match="holdout authorization could not be verified"):
        context.final_verdict.finalize(context.hypothesis_id)


def test_tampered_trigger_authorization_rejected(tmp_path: Path) -> None:
    context, rows, plan, bound = _full_pipeline(tmp_path)
    with sqlite3.connect(context.db_path) as db:
        row = db.execute(
            "SELECT authorization_artifact_hash_ref FROM holdout_trigger_authorizations WHERE hypothesis_id=?",
            (context.hypothesis_id,),
        ).fetchone()
    object_path = Path(context.store.resolve_verified(row[0]).path)
    object_path.write_bytes(b'{"tampered": true}')
    with pytest.raises(FinalVerdictError, match="holdout authorization could not be verified"):
        context.final_verdict.finalize(context.hypothesis_id)


# ---------------------------------------------------------------------------
# 7: missing/duplicate/conflicting FINAL_HOLDOUT_CLAIM rejected.
# ---------------------------------------------------------------------------


def test_missing_final_result_record_rejected(tmp_path: Path) -> None:
    """Simulates an evaluator that fails AFTER one-shot consumption (the
    real FinalHoldoutRunner records this as FINAL_HOLDOUT_CLAIM +
    FINAL_HOLDOUT_RUN_FAILED, never a FINAL_HOLDOUT_RESULT) using a real,
    properly hash-chained ledger -- not a broken/tampered one -- so this
    test exercises _find_holdout_receipt_records's own "no result" branch
    specifically, not the ledger's generic tamper-evidence."""
    context = _setup(tmp_path)
    rows, plan, bound = _bound_split_plan(tmp_path)
    discovery_dataset = _discovery_dataset_v2(context, rows, plan)
    validation_dataset = _validation_dataset_v2(context, rows, plan)
    manifest = _build_manifest_v2(
        context, datasets=(discovery_dataset, validation_dataset), split_plan=plan,
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

    class _FailingEvaluator(_CountingEvaluator):
        def evaluate(self, plaintext: bytes) -> dict[str, int]:
            raise RuntimeError("synthetic evaluator failure")

    failing_runner = FinalHoldoutRunner(
        registry=context.registry, seals=context.holdout_seals, keys=_StaticKeys(),
        ledger=context.ledger, evaluator=_FailingEvaluator(), evaluator_artifact_path=_EVALUATOR_ARTIFACT,
    )
    failing_bridge = HoldoutTriggerBridge(
        registry=context.registry, control=context.control, artifacts=context.store,
        validator=context.validator, runner=failing_runner,
    )
    from trademind.discovery.holdout_trigger_bridge import HoldoutTriggerError

    with pytest.raises(HoldoutTriggerError):
        failing_bridge.trigger(context.hypothesis_id, sealed_path=context.sealed_path)
    assert context.registry.get(context.hypothesis_id).state is HypothesisState.HOLDOUT_CONSUMED
    assert context.ledger.verify()
    ledger_text = context.ledger.path.read_text(encoding="utf-8")
    assert "FINAL_HOLDOUT_RUN_FAILED" in ledger_text
    assert "FINAL_HOLDOUT_RESULT" not in ledger_text

    final_verdict = FinalVerdictAcceptanceControl(
        registry=context.registry, control=context.control, artifacts=context.store,
        validator=context.validator, trigger_bridge=failing_bridge,
    )
    with pytest.raises(FinalVerdictError, match="no FINAL_HOLDOUT_RESULT record"):
        final_verdict.finalize(context.hypothesis_id)


def test_duplicate_final_result_record_rejected(tmp_path: Path) -> None:
    """A second, legitimately-appended (chain-preserving, not corrupted)
    FINAL_HOLDOUT_RESULT record for the same hypothesis_id must be
    rejected as an unresolvable conflict, not silently picked by
    first-match."""
    context, rows, plan, bound = _full_pipeline(tmp_path)
    family_id = context.registry.get(context.hypothesis_id).hypothesis_family_id
    context.ledger.append(
        {
            "record_type": "FINAL_HOLDOUT_RESULT",
            "hypothesis_id": context.hypothesis_id,
            "hypothesis_family_id": family_id,
            "envelope_hash": "z" * 64,
            "evaluator_id": _CountingEvaluator.evaluator_id,
            "evaluator_hash": context.runner.evaluator_hash,
            "claim_record_hash": "y" * 64,
            "aggregate_metrics": {_HOLDOUT_METRIC: 999},
            "holdout_consumed": True,
        }
    )
    assert context.ledger.verify()
    with pytest.raises(FinalVerdictError, match="more than one FINAL_HOLDOUT_RESULT"):
        context.final_verdict.finalize(context.hypothesis_id)


# ---------------------------------------------------------------------------
# 8: wrong hypothesis/family/manifest binding rejected.
# ---------------------------------------------------------------------------


def test_wrong_manifest_binding_rejected(tmp_path: Path) -> None:
    context, rows, plan, bound = _full_pipeline(tmp_path)
    with sqlite3.connect(context.db_path) as db:
        db.execute(
            "UPDATE hypotheses SET content_hash=? WHERE hypothesis_id=?",
            ("0" * 64, context.hypothesis_id),
        )
    with pytest.raises(FinalVerdictError, match="bound manifest v2 could not be verified"):
        context.final_verdict.finalize(context.hypothesis_id)


# ---------------------------------------------------------------------------
# 9: wrong evaluator identity rejected.
# ---------------------------------------------------------------------------


def test_wrong_evaluator_identity_rejected(tmp_path: Path) -> None:
    """A stored authorization record that is internally self-consistent
    (its own hash checks out) but whose evaluator_id/evaluator_hash do not
    match what the LEDGER's real claim/result records for this hypothesis
    actually used must be rejected by finalize()'s own cross-check -- not
    merely by the authorization's own hash-identity check (that is a
    separate, already-covered scenario). Constructed by combining two
    otherwise-independent, fully genuine pipelines: hypothesis A's real
    identity fields with hypothesis B's real (but different) evaluator
    identity."""
    from trademind.discovery.holdout_trigger_bridge import (
        HOLDOUT_TRIGGER_AUTHORIZATION_MEDIA_TYPE,
        HoldoutTriggerAuthorizationV1,
    )

    context_a, *_ = _full_pipeline(tmp_path / "a", threshold=1, evaluator=_CountingEvaluator())
    context_b, *_ = _full_pipeline(tmp_path / "b", threshold=1, evaluator=_OtherCountingEvaluator())
    real_authorization = context_a.trigger_bridge.get_authorization(context_a.hypothesis_id)
    other_authorization = context_b.trigger_bridge.get_authorization(context_b.hypothesis_id)
    assert other_authorization.evaluator_id != real_authorization.evaluator_id

    forged = HoldoutTriggerAuthorizationV1(
        hypothesis_id=real_authorization.hypothesis_id,
        hypothesis_family_id=real_authorization.hypothesis_family_id,
        bound_hypothesis_content_hash=real_authorization.bound_hypothesis_content_hash,
        manifest_semantic_hash=real_authorization.manifest_semantic_hash,
        manifest_artifact_hash_ref=real_authorization.manifest_artifact_hash_ref,
        orchestrator_task_id=real_authorization.orchestrator_task_id,
        validation_evidence_hash=real_authorization.validation_evidence_hash,
        envelope_hash=real_authorization.envelope_hash,
        key_id=real_authorization.key_id,
        evaluator_id=other_authorization.evaluator_id,
        evaluator_hash=other_authorization.evaluator_hash,
        authorized_at=real_authorization.authorized_at,
    )
    artifact = context_a.store.import_snapshot(
        io.BytesIO(forged.canonical_bytes()), media_type=HOLDOUT_TRIGGER_AUTHORIZATION_MEDIA_TYPE
    )
    with sqlite3.connect(context_a.db_path) as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            "UPDATE holdout_trigger_authorizations SET authorization_hash=?, "
            "authorization_artifact_hash_ref=? WHERE hypothesis_id=?",
            (forged.authorization_hash, artifact.hash_ref, context_a.hypothesis_id),
        )
        db.commit()

    with pytest.raises(FinalVerdictError, match="does not match the authorized"):
        context_a.final_verdict.finalize(context_a.hypothesis_id)


# ---------------------------------------------------------------------------
# 10-11: forged aggregate_metrics / missing metric fails closed.
# ---------------------------------------------------------------------------


def test_forged_aggregate_metrics_in_ledger_line_breaks_hash_chain(tmp_path: Path) -> None:
    context, rows, plan, bound = _full_pipeline(tmp_path)
    text = context.ledger.path.read_text(encoding="utf-8")
    forged = text.replace(f'"{_HOLDOUT_METRIC}":2', f'"{_HOLDOUT_METRIC}":999999')
    assert forged != text
    context.ledger.path.write_text(forged, encoding="utf-8")
    # A forged aggregate_metrics value inside an already-hashed ledger line
    # breaks that line's own record_hash under the closed, reused
    # ResultLedger.verify() contract -- caller-supplied aggregate_metrics
    # are never trusted independently of this tamper-evident anchor.
    with pytest.raises(FinalVerdictError, match="result ledger integrity verification failed"):
        context.final_verdict.finalize(context.hypothesis_id)


def test_missing_required_metric_fails_closed(tmp_path: Path) -> None:
    context = _setup(tmp_path)
    rows, plan, bound = _bound_split_plan(tmp_path)
    discovery_dataset = _discovery_dataset_v2(context, rows, plan)
    validation_dataset = _validation_dataset_v2(context, rows, plan)
    unsatisfiable = FinalHoldoutCriteriaV1(
        mode=CriteriaMode.ALL,
        criteria=(
            EvaluationCriterionV1(
                metric="a_metric_the_evaluator_never_produces",
                operator=CriterionOperator.GREATER_THAN_OR_EQUAL,
                threshold=0,
            ),
        ),
    )
    manifest = _build_manifest_v2(
        context, datasets=(discovery_dataset, validation_dataset), split_plan=plan,
        final_holdout_criteria=unsatisfiable,
    )
    _freeze_v2(context, manifest)
    _seal_and_isolate(context, tmp_path)
    context.bridge.submit_frozen_hypothesis_v2(
        context.hypothesis_id, bound_split_plan=bound,
        dataset_role_bindings={discovery_dataset.role: "DISCOVERY", validation_dataset.role: "VALIDATION"},
    )
    context.train_test.execute(context.hypothesis_id, bound_split_plan=bound)
    context.validator.execute(context.hypothesis_id, bound_split_plan=bound)
    context.trigger_bridge.trigger(context.hypothesis_id, sealed_path=context.sealed_path)

    with pytest.raises(FinalVerdictError, match="vocabulary check"):
        context.final_verdict.finalize(context.hypothesis_id)
    assert context.registry.get(context.hypothesis_id).state is HypothesisState.HOLDOUT_CONSUMED


# ---------------------------------------------------------------------------
# 12: NaN/Inf fails closed.
# ---------------------------------------------------------------------------


def test_nan_metric_value_fails_closed() -> None:
    from trademind.discovery.validation_execution import _evaluate_criterion

    criterion = EvaluationCriterionV1(
        metric=_HOLDOUT_METRIC, operator=CriterionOperator.GREATER_THAN_OR_EQUAL, threshold=0
    )
    with pytest.raises(Exception, match="finite"):
        _evaluate_criterion(criterion, {_HOLDOUT_METRIC: float("nan")})
    with pytest.raises(Exception, match="finite"):
        _evaluate_criterion(criterion, {_HOLDOUT_METRIC: float("inf")})


# ---------------------------------------------------------------------------
# 13: unsupported operator/mode fails closed.
# ---------------------------------------------------------------------------


def test_unsupported_operator_and_mode_fail_closed() -> None:
    from trademind.discovery.validation_execution import _evaluate_criteria, _evaluate_criterion

    class _FakeOperator:
        value = "!="

    class _FakeCriterion:
        metric = _HOLDOUT_METRIC
        operator = _FakeOperator()
        threshold = 0

    with pytest.raises(Exception, match="unsupported criterion operator"):
        _evaluate_criterion(_FakeCriterion(), {_HOLDOUT_METRIC: 1})  # type: ignore[arg-type]

    class _FakeMode:
        value = "MAJORITY"

    class _FakeCriteria:
        mode = _FakeMode()
        criteria = ()

    with pytest.raises(Exception, match="unsupported evaluation criteria mode"):
        _evaluate_criteria(_FakeCriteria(), {})  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 14-15: validation criteria NOT used; alpha/q/minimum_effect_size/
# max_hypotheses_tests NOT reused.
# ---------------------------------------------------------------------------


def test_validation_criteria_and_numeric_fields_not_reused() -> None:
    # Checks for actual CODE ACCESS patterns (attribute reads on a loaded
    # manifest object), not bare English words -- the module's own
    # docstring legitimately names these fields in prose to explain why
    # they are NOT reused, so a bare-substring ban would false-positive on
    # its own documentation.
    source = Path("src/trademind/discovery/final_verdict_control.py").read_text(encoding="utf-8")
    for forbidden_access in (
        "manifest.evaluation_criteria",
        "manifest.alpha",
        "manifest.q",
        "manifest.minimum_effect_size",
        "manifest.max_hypotheses_tests",
    ):
        assert forbidden_access not in source, forbidden_access


def test_manifest_evaluation_criteria_never_read_by_finalize(tmp_path: Path) -> None:
    context, rows, plan, bound = _full_pipeline(tmp_path, threshold=1_000_000)
    # A manifest whose VALIDATION evaluation_criteria would legitimately
    # pass (threshold 0.0, satisfied by any positive avg_net_atr mean) but
    # whose final_holdout_criteria is set to fail proves the final verdict
    # is decided purely by final_holdout_criteria.
    evidence = context.final_verdict.finalize(context.hypothesis_id)
    assert evidence.verdict == HypothesisState.REJECTED_FINAL.value


# ---------------------------------------------------------------------------
# 16-17: evidence persisted before transition; complete provenance chain.
# ---------------------------------------------------------------------------


def test_evidence_persisted_before_transition_on_failed_precondition(tmp_path: Path) -> None:
    context, rows, plan, bound = _full_pipeline(tmp_path, include_final_holdout_criteria=False)
    with pytest.raises(FinalVerdictError):
        context.final_verdict.finalize(context.hypothesis_id)
    assert context.registry.get(context.hypothesis_id).state is HypothesisState.HOLDOUT_CONSUMED
    with sqlite3.connect(context.db_path) as db:
        count = db.execute(
            "SELECT COUNT(*) FROM final_verdict_evidence WHERE hypothesis_id=?", (context.hypothesis_id,)
        ).fetchone()[0]
    assert count == 0


def test_provenance_chain_complete(tmp_path: Path) -> None:
    context, rows, plan, bound = _full_pipeline(tmp_path, threshold=1)
    # get_evidence() itself requires current state to still be
    # VALIDATION_PASSED/VALIDATION_REJECTED, which is no longer true once
    # HOLDOUT_CONSUMED has been reached -- reuse the same state-agnostic
    # private accessor finalize() itself uses.
    validation_evidence = context.validator._reload_existing_evidence(
        context.hypothesis_id, expected_state=HypothesisState.VALIDATION_PASSED
    )
    authorization = context.trigger_bridge.get_authorization(context.hypothesis_id)
    evidence = context.final_verdict.finalize(context.hypothesis_id)
    record = context.registry.get(context.hypothesis_id)

    assert evidence.hypothesis_id == context.hypothesis_id
    assert evidence.hypothesis_family_id == record.hypothesis_family_id
    assert evidence.bound_hypothesis_content_hash == record.content_hash
    assert evidence.manifest_semantic_hash == f"sha256:{record.manifest_hash}"
    assert evidence.manifest_artifact_hash_ref == record.manifest_artifact_hash_ref
    assert evidence.orchestrator_task_id == DiscoveryOrchestratorBridge._task_id(context.hypothesis_id)
    assert evidence.validation_evidence_hash == validation_evidence.evidence_hash
    assert evidence.holdout_authorization_hash == authorization.authorization_hash
    assert evidence.envelope_hash == authorization.envelope_hash
    assert evidence.evaluator_id == authorization.evaluator_id
    assert evidence.evaluator_hash == authorization.evaluator_hash
    with sqlite3.connect(context.db_path) as db:
        row = db.execute(
            "SELECT * FROM final_verdict_evidence WHERE hypothesis_id=?", (context.hypothesis_id,)
        ).fetchone()
    assert row is not None


# ---------------------------------------------------------------------------
# 18-19: identical retry idempotent; conflicting retry fails closed.
# ---------------------------------------------------------------------------


def test_duplicate_identical_finalization_is_idempotent(tmp_path: Path) -> None:
    context, rows, plan, bound = _full_pipeline(tmp_path, threshold=1)
    first = context.final_verdict.finalize(context.hypothesis_id)
    second = context.final_verdict.finalize(context.hypothesis_id)
    assert first.evidence_hash == second.evidence_hash
    assert first.to_payload() == second.to_payload()
    with sqlite3.connect(context.db_path) as db:
        count = db.execute(
            "SELECT COUNT(*) FROM final_verdict_evidence WHERE hypothesis_id=?", (context.hypothesis_id,)
        ).fetchone()[0]
    assert count == 1
    assert context.registry.get(context.hypothesis_id).state is HypothesisState.ACCEPTED


def test_conflicting_evidence_fails_closed(tmp_path: Path) -> None:
    context, rows, plan, bound = _full_pipeline(tmp_path)
    with sqlite3.connect(context.db_path) as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            """
            INSERT INTO final_verdict_evidence(
                hypothesis_id, hypothesis_family_id, manifest_semantic_hash,
                manifest_artifact_hash_ref, orchestrator_task_id, validation_evidence_hash,
                holdout_authorization_hash, evidence_hash, evidence_artifact_hash_ref, verdict,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                context.hypothesis_id,
                "hf_" + "1" * 64,
                f"sha256:{'2' * 64}",
                f"sha256:{'3' * 64}",
                "discovery-" + "4" * 20,
                f"sha256:{'7' * 64}",
                f"sha256:{'8' * 64}",
                "sha256:" + "5" * 64,
                "sha256:" + "6" * 64,
                HypothesisState.ACCEPTED.value,
                "2026-01-01T00:00:00+00:00",
            ),
        )
        db.commit()
    with pytest.raises(FinalVerdictError, match="conflicting final verdict evidence"):
        context.final_verdict.finalize(context.hypothesis_id)
    assert context.registry.get(context.hypothesis_id).state is HypothesisState.HOLDOUT_CONSUMED


# ---------------------------------------------------------------------------
# 20: concurrent finalization cannot double-transition.
# ---------------------------------------------------------------------------


def test_concurrent_finalization_cannot_double_transition(tmp_path: Path) -> None:
    context, rows, plan, bound = _full_pipeline(tmp_path, threshold=1)
    results: list[FinalVerdictEvidenceV1] = []
    errors: list[BaseException] = []
    barrier = threading.Barrier(2)

    def _worker() -> None:
        barrier.wait()
        try:
            results.append(context.final_verdict.finalize(context.hypothesis_id))
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
    assert context.registry.get(context.hypothesis_id).state is HypothesisState.ACCEPTED
    with sqlite3.connect(context.db_path) as db:
        count = db.execute(
            "SELECT COUNT(*) FROM final_verdict_evidence WHERE hypothesis_id=?", (context.hypothesis_id,)
        ).fetchone()[0]
    assert count == 1


# ---------------------------------------------------------------------------
# 21-23: ACCEPTED/REJECTED_FINAL family terminal; terminal family cannot
# re-enter research.
# ---------------------------------------------------------------------------


def _reused_family_definition(context: _Context, family_id: str) -> dict[str, object]:
    """Read back the family's own immutable definition_json exactly as
    HypothesisRegistry itself pinned it, so a fresh register() attempt
    derives the SAME family_id (derive_hypothesis_family_id is a pure
    function of this exact content) -- proving terminality against the
    real family, not a coincidentally-different one."""
    with sqlite3.connect(context.db_path) as db:
        row = db.execute(
            "SELECT definition_json FROM hypothesis_families WHERE family_id=?", (family_id,)
        ).fetchone()
    assert row is not None
    return json.loads(row[0])


def test_accepted_family_is_terminal_and_cannot_reenter_research(tmp_path: Path) -> None:
    context, rows, plan, bound = _full_pipeline(tmp_path, threshold=1)
    context.final_verdict.finalize(context.hypothesis_id)
    family_id = context.registry.get(context.hypothesis_id).hypothesis_family_id
    assert context.registry.family_status(family_id)["terminal_state"] == HypothesisState.ACCEPTED.value

    from trademind.discovery.hypothesis_registry import RegistryError

    with pytest.raises(RegistryError, match="already consumed final holdout|is terminal"):
        context.registry.register(
            hypothesis_id="a-different-hypothesis-id-for-the-same-family",
            family_definition=_reused_family_definition(context, family_id),
            content_definition={"new": "content"},
        )


def test_rejected_final_family_is_terminal(tmp_path: Path) -> None:
    context, rows, plan, bound = _full_pipeline(tmp_path, threshold=1_000_000)
    context.final_verdict.finalize(context.hypothesis_id)
    family_id = context.registry.get(context.hypothesis_id).hypothesis_family_id
    assert (
        context.registry.family_status(family_id)["terminal_state"]
        == HypothesisState.REJECTED_FINAL.value
    )
    # An identical retry of the SAME hypothesis is a benign idempotent
    # reload (proven separately by test_duplicate_identical_finalization_is_idempotent);
    # terminality is proven here the same way as the ACCEPTED case above --
    # a fresh hypothesis attempting to reuse the now-terminal family.
    from trademind.discovery.hypothesis_registry import RegistryError

    with pytest.raises(RegistryError, match="already consumed final holdout|is terminal"):
        context.registry.register(
            hypothesis_id="a-different-hypothesis-id-for-the-same-rejected-family",
            family_definition=_reused_family_definition(context, family_id),
            content_definition={"new": "content"},
        )


# ---------------------------------------------------------------------------
# 24-25: no holdout decrypt/rerun/evaluator call; no plaintext/key/raw path.
# ---------------------------------------------------------------------------


def test_no_sealed_protected_access_decrypt_rerun_or_evaluator_call() -> None:
    # (function deliberately avoids the substring "holdout" in its own
    # name; see test_train_test_execution.py for why.)
    source = Path("src/trademind/discovery/final_verdict_control.py").read_text(encoding="utf-8")
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
        "seal_file",
        "run_once",
        "evaluate",
        "AESGCM",
    }
    assert not (called_names & forbidden_calls), called_names & forbidden_calls


def test_no_plaintext_or_key_in_final_verdict_evidence(tmp_path: Path) -> None:
    context, rows, plan, bound = _full_pipeline(tmp_path, threshold=1)
    evidence = context.final_verdict.finalize(context.hypothesis_id)
    payload_text = json.dumps(evidence.to_payload())
    distinctive_plaintext_fragment = "2026-01-03T06:00:00+00:00,-0.05"
    assert distinctive_plaintext_fragment not in payload_text
    import base64

    assert base64.b64encode(_KEY).decode("ascii") not in payload_text
    assert _KEY.hex() not in payload_text


# ---------------------------------------------------------------------------
# 26-27: no provider/network; no broker/MT5/trading.
# ---------------------------------------------------------------------------


def test_no_provider_network_or_broker_shaped_imports() -> None:
    source = Path("src/trademind/discovery/final_verdict_control.py").read_text(encoding="utf-8")
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
# 28: all existing holdout/validation/train-test/bridge/manifest tests
# remain green -- verified by running those files directly as part of the
# required regression (see VALIDATION section of the implementation
# report); no test from them is duplicated here.
# ---------------------------------------------------------------------------
