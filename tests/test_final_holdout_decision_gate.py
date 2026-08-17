from __future__ import annotations

import io
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from trademind.discovery.hypothesis_registry import HypothesisRegistry
from trademind.discovery.manifest import (
    CriteriaMode,
    CriterionOperator,
    DatasetArtifactV2,
    EvaluationCriteriaV1,
    EvaluationCriterionV1,
    ExperimentManifestV2,
    ProposalIntakeProvenanceV1,
    TradingFrictionV1,
    build_experiment_manifest_v2,
    persist_experiment_manifest_v2,
)
from trademind.discovery.split_engine import SplitPlan, chronological_split
from trademind.experiment_evidence import ExperimentEvidenceBuilderV1
from trademind.experiment_execution_contract import (
    EvaluatorBinding,
    EvaluatorRegistry,
    ExecutionPhase,
    ExperimentExecutionContractError,
)
from trademind.experiment_execution_runtime import ExperimentExecutionRuntimeV1
from trademind.final_holdout_decision_gate import (
    FINAL_HOLDOUT_AUTHORIZATION_SEMANTIC_MARKERS,
    FinalHoldoutAuthorizationAlreadyGrantedError,
    FinalHoldoutAuthorizationConflictError,
    FinalHoldoutAuthorizationRejectedError,
    FinalHoldoutAuthorizationStore,
    FinalHoldoutDecisionGateError,
    FinalHoldoutDecisionGateV1,
    build_final_holdout_authorization_v1,
    load_final_holdout_authorization_v1,
    persist_final_holdout_authorization_v1,
    verify_final_holdout_authorization_v1,
)
from trademind.orchestrator.artifact_store import (
    ArtifactIntegrityError,
    ArtifactNotFoundError,
    ArtifactStore,
)
from trademind.signal_statistics_provenance import CodeProvenance
from trademind.validation_decision import ValidationDecisionBuilderV1

FAMILY = "gate-eval-v1"
EVALUATOR_ID = "fake-gate-eval-v1"
EVALUATOR_VERSION = "1"
SPLIT_ROLE = "market-data"
RESULT_REF = f"sha256:{'a' * 64}"
PACKET_REF = f"sha256:{'b' * 64}"
PACKET_SEMANTIC_HASH = f"sha256:{'c' * 64}"
REQUEST_HASH = "d" * 64
CANDIDATE_ID = f"ssc-v2-{'e' * 64}"
HYPOTHESIS_ID = f"rpi-v1:{RESULT_REF}:0"
CREATED_AT = "2026-08-14T10:00:00+00:00"
CREATED_BY = "operator:test"

OTHER_RESULT_REF = f"sha256:{'1' * 64}"
OTHER_HYPOTHESIS_ID = f"rpi-v1:{OTHER_RESULT_REF}:0"


class MeanValueEvaluator:
    evaluator_id = EVALUATOR_ID
    evaluator_version = EVALUATOR_VERSION

    def evaluate(self, rows, *, manifest, execution_phase):
        del manifest, execution_phase
        values = [float(row.fields["value"]) for row in rows]
        return {"mean_value": sum(values) / len(values)}


def _family(hypothesis_id: str = HYPOTHESIS_ID) -> dict[str, object]:
    return {"candidate_id": CANDIDATE_ID, "claim": f"gate effect for {hypothesis_id}"}


def _content(hypothesis_id: str = HYPOTHESIS_ID) -> dict[str, object]:
    return {
        "family_definition": _family(hypothesis_id),
        "proposal": {"falsifiable_claim": "gate effect remains positive"},
        "provenance": {"intake_id": hypothesis_id},
    }


def _code_provenance(*, git_commit: str = "f" * 40) -> CodeProvenance:
    return CodeProvenance(
        producer_name="trademind",
        producer_version="1.31.1",
        git_commit=git_commit,
        revision_source="git_worktree",
    )


def _register(registry: HypothesisRegistry, hypothesis_id: str = HYPOTHESIS_ID):
    return registry.register(
        hypothesis_id=hypothesis_id,
        family_definition=_family(hypothesis_id),
        content_definition=_content(hypothesis_id),
    )


def _transaction(registry: HypothesisRegistry) -> sqlite3.Connection:
    db = sqlite3.connect(registry.path, timeout=30.0)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("BEGIN IMMEDIATE")
    return db


def _split() -> SplitPlan:
    start = datetime(2026, 1, 1, 0, tzinfo=timezone.utc)
    return chronological_split([start + timedelta(hours=index) for index in range(12)])


def _public_rows(plan: SplitPlan) -> list[tuple[datetime, float]]:
    start = datetime.fromisoformat(plan.discovery_start)
    return [(start + timedelta(hours=index), float(index)) for index in range(plan.public_count)]


def _csv_bytes(rows: list[tuple[datetime, float]]) -> bytes:
    lines = ["time,value"]
    lines.extend(f"{time.isoformat()},{value}" for time, value in rows)
    return ("\n".join(lines) + "\n").encode("utf-8")


def _dataset(store: ArtifactStore, plan: SplitPlan) -> DatasetArtifactV2:
    payload = _csv_bytes(_public_rows(plan))
    artifact = store.import_snapshot(io.BytesIO(payload), media_type="text/csv")
    return DatasetArtifactV2(
        role=SPLIT_ROLE,
        artifact_hash_ref=artifact.hash_ref,
        media_type=artifact.media_type,
        size_bytes=artifact.size_bytes,
    )


def _criteria(*, threshold: float = -1.0) -> EvaluationCriteriaV1:
    return EvaluationCriteriaV1(
        mode=CriteriaMode.ALL,
        criteria=(
            EvaluationCriterionV1(
                metric="mean_value",
                operator=CriterionOperator.GREATER_THAN_OR_EQUAL,
                threshold=threshold,
            ),
        ),
    )


def _manifest(
    store: ArtifactStore,
    registry: HypothesisRegistry,
    *,
    hypothesis_id: str = HYPOTHESIS_ID,
    split_plan: SplitPlan | None = None,
    friction: TradingFrictionV1 | None = None,
    seed: int | None = 7,
    threshold: float = -1.0,
) -> ExperimentManifestV2:
    record = registry.get(hypothesis_id)
    plan = split_plan or _split()
    result_ref = hypothesis_id.removeprefix("rpi-v1:").rsplit(":", 1)[0]
    ds = _dataset(store, plan)
    return build_experiment_manifest_v2(
        artifact_store=store,
        hypothesis_id=hypothesis_id,
        hypothesis_family_id=record.hypothesis_family_id,
        bound_hypothesis_content_hash=record.content_hash,
        proposal_provenance=ProposalIntakeProvenanceV1(
            intake_id=hypothesis_id,
            execution_request_hash=REQUEST_HASH,
            authorization_id=1,
            task_id="gate-task",
            task_revision=1,
            packet_artifact_hash_ref=PACKET_REF,
            packet_semantic_hash=PACKET_SEMANTIC_HASH,
            result_artifact_hash_ref=result_ref,
            proposal_index=0,
            candidate_id=CANDIDATE_ID,
        ),
        datasets=(ds,),
        split_plan=plan,
        split_dataset_role=SPLIT_ROLE,
        test_family=FAMILY,
        primary_metric="mean_value",
        evaluation_criteria=_criteria(threshold=threshold),
        alpha=0.05,
        q=0.10,
        minimum_effect_size=0.05,
        max_hypotheses_tests=20,
        trading_friction=friction,
        deterministic_seed=seed,
        code_provenance=_code_provenance(),
        semantic_parameters={"horizon": 12},
        created_at=CREATED_AT,
        created_by=CREATED_BY,
    )


def _freeze(registry: HypothesisRegistry, store: ArtifactStore, manifest: ExperimentManifestV2) -> str:
    artifact = persist_experiment_manifest_v2(manifest, artifact_store=store)
    db = _transaction(registry)
    try:
        registry.freeze_manifest_v2_in_transaction(
            db,
            manifest_artifact_hash_ref=artifact.hash_ref,
            artifact_store=store,
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
    return artifact.hash_ref


def _binding(*, friction: tuple[tuple[str, str], ...] = ()) -> EvaluatorBinding:
    return EvaluatorBinding(
        test_family=FAMILY,
        evaluator_id=EVALUATOR_ID,
        evaluator_version=EVALUATOR_VERSION,
        supported_metrics=("mean_value",),
        supported_friction_models=friction,
        deterministic=True,
    )


def _evaluator_registry(**kwargs) -> EvaluatorRegistry:
    return EvaluatorRegistry({FAMILY: _binding(**kwargs)})


def _runtime(registry: HypothesisRegistry, store: ArtifactStore) -> ExperimentExecutionRuntimeV1:
    return ExperimentExecutionRuntimeV1(
        registry=registry,
        artifact_store=store,
        evaluator_registry=_evaluator_registry(),
        evaluator=MeanValueEvaluator(),
    )


def _evidence_builder(registry: HypothesisRegistry, store: ArtifactStore) -> ExperimentEvidenceBuilderV1:
    return ExperimentEvidenceBuilderV1(
        registry=registry,
        artifact_store=store,
        evaluator_registry=_evaluator_registry(),
    )


def _decision_builder(registry: HypothesisRegistry, store: ArtifactStore) -> ValidationDecisionBuilderV1:
    return ValidationDecisionBuilderV1(
        registry=registry,
        artifact_store=store,
        evaluator_registry=_evaluator_registry(),
    )


def _gate(registry: HypothesisRegistry, store: ArtifactStore) -> FinalHoldoutDecisionGateV1:
    return FinalHoldoutDecisionGateV1(
        registry=registry,
        artifact_store=store,
        evaluator_registry=_evaluator_registry(),
    )


def _case(tmp_path: Path, *, hypothesis_id: str = HYPOTHESIS_ID, **manifest_kwargs):
    registry = HypothesisRegistry(tmp_path / "registry.db")
    _register(registry, hypothesis_id)
    store = ArtifactStore(tmp_path / "artifacts")
    manifest = _manifest(store, registry, hypothesis_id=hypothesis_id, **manifest_kwargs)
    manifest_ref = _freeze(registry, store, manifest)
    return registry, store, manifest, manifest_ref


def _decision(registry, store, hypothesis_id=HYPOTHESIS_ID, phase=ExecutionPhase.VALIDATION):
    runtime = _runtime(registry, store)
    execution = runtime.execute(
        hypothesis_id,
        execution_phase=phase,
        execution_code_provenance=_code_provenance(),
        evaluator_friction=None,
        created_at=CREATED_AT,
        created_by=CREATED_BY,
    )
    evidence, evidence_artifact = _evidence_builder(registry, store).build(
        hypothesis_id, execution=execution, created_at=CREATED_AT, created_by=CREATED_BY
    )
    decision, decision_artifact = _decision_builder(registry, store).decide(
        hypothesis_id,
        evidence_artifact_hash_ref=evidence_artifact.hash_ref,
        result_artifact_hash_ref=execution.result_artifact.hash_ref,
        created_at=CREATED_AT,
        created_by=CREATED_BY,
    )
    return execution, evidence_artifact, decision, decision_artifact


# ---------------------------------------------------------------------------
# PASS authorization
# ---------------------------------------------------------------------------


def test_pass_authorization_granted(tmp_path: Path) -> None:
    registry, store, manifest, manifest_ref = _case(tmp_path, threshold=-1.0)
    execution, evidence_artifact, decision, decision_artifact = _decision(registry, store)
    authorization, artifact = _gate(registry, store).authorize(
        HYPOTHESIS_ID,
        validation_decision_artifact_hash_ref=decision_artifact.hash_ref,
        evidence_artifact_hash_ref=evidence_artifact.hash_ref,
        result_artifact_hash_ref=execution.result_artifact.hash_ref,
        created_at=CREATED_AT,
        created_by=CREATED_BY,
    )
    assert authorization.criteria_decision.passed is True
    assert authorization.manifest_artifact_hash_ref == manifest_ref
    assert authorization.validation_decision_artifact_hash_ref == decision_artifact.hash_ref
    assert authorization.evidence_artifact_hash_ref == evidence_artifact.hash_ref
    assert authorization.result_artifact_hash_ref == execution.result_artifact.hash_ref
    assert authorization.holdout_start == manifest.split_plan.holdout_start
    assert authorization.holdout_end == manifest.split_plan.holdout_end
    assert authorization.holdout_row_count == manifest.split_plan.holdout_count
    assert (
        authorization.semantic_projection()["authorization_semantic_markers"]
        == FINAL_HOLDOUT_AUTHORIZATION_SEMANTIC_MARKERS
    )
    assert artifact.hash_ref
    assert verify_final_holdout_authorization_v1(authorization.canonical_bytes()) == authorization


def test_authorization_succeeds_without_any_sealed_holdout(tmp_path: Path) -> None:
    """Proves zero coupling to the holdout-sealing subsystem: no seal, no key,
    no envelope ever needs to exist for this gate to grant authorization."""
    registry, store, manifest, manifest_ref = _case(tmp_path)
    execution, evidence_artifact, decision, decision_artifact = _decision(registry, store)
    # No FinalHoldoutSealer / HoldoutSealStore row was ever created for this
    # hypothesis, and none is imported anywhere in this test file's target
    # module. Authorization must still succeed.
    authorization, _ = _gate(registry, store).authorize(
        HYPOTHESIS_ID,
        validation_decision_artifact_hash_ref=decision_artifact.hash_ref,
        evidence_artifact_hash_ref=evidence_artifact.hash_ref,
        result_artifact_hash_ref=execution.result_artifact.hash_ref,
        created_at=CREATED_AT,
        created_by=CREATED_BY,
    )
    assert authorization.criteria_decision.passed is True


# ---------------------------------------------------------------------------
# FAIL rejection
# ---------------------------------------------------------------------------


def test_fail_decision_never_authorizes(tmp_path: Path) -> None:
    registry, store, manifest, manifest_ref = _case(tmp_path, threshold=999.0)
    execution, evidence_artifact, decision, decision_artifact = _decision(registry, store)
    assert decision.criteria_decision.passed is False
    with pytest.raises(FinalHoldoutAuthorizationRejectedError):
        _gate(registry, store).authorize(
            HYPOTHESIS_ID,
            validation_decision_artifact_hash_ref=decision_artifact.hash_ref,
            evidence_artifact_hash_ref=evidence_artifact.hash_ref,
            result_artifact_hash_ref=execution.result_artifact.hash_ref,
            created_at=CREATED_AT,
            created_by=CREATED_BY,
        )
    # A FAIL leaves no store entry and no discoverable "denied" artifact behind.
    store_obj = FinalHoldoutAuthorizationStore(registry)
    with pytest.raises(KeyError):
        store_obj.get(HYPOTHESIS_ID)


def test_discovery_phase_decision_cannot_authorize(tmp_path: Path) -> None:
    # A validation decision can only ever exist for VALIDATION-phase evidence
    # (enforced one layer down); confirm the whole chain up to this gate
    # never manages to produce something DISCOVERY-phase data could authorize.
    from trademind.validation_decision import ValidationDecisionPhaseError

    registry, store, manifest, manifest_ref = _case(tmp_path)
    with pytest.raises(ValidationDecisionPhaseError):
        _decision(registry, store, phase=ExecutionPhase.DISCOVERY)


# ---------------------------------------------------------------------------
# Missing / corrupt validation decision
# ---------------------------------------------------------------------------


def test_missing_validation_decision_ref_fails_closed(tmp_path: Path) -> None:
    registry, store, manifest, manifest_ref = _case(tmp_path)
    execution, evidence_artifact, decision, decision_artifact = _decision(registry, store)
    with pytest.raises(ArtifactNotFoundError):
        _gate(registry, store).authorize(
            HYPOTHESIS_ID,
            validation_decision_artifact_hash_ref=f"sha256:{'9' * 64}",
            evidence_artifact_hash_ref=evidence_artifact.hash_ref,
            result_artifact_hash_ref=execution.result_artifact.hash_ref,
            created_at=CREATED_AT,
            created_by=CREATED_BY,
        )


def test_corrupt_validation_decision_cas_fails_closed(tmp_path: Path) -> None:
    registry, store, manifest, manifest_ref = _case(tmp_path)
    execution, evidence_artifact, decision, decision_artifact = _decision(registry, store)
    resolved = store.resolve_verified(decision_artifact.hash_ref)
    Path(resolved.path).write_bytes(b"corrupted decision bytes")
    with pytest.raises(ArtifactIntegrityError):
        _gate(registry, store).authorize(
            HYPOTHESIS_ID,
            validation_decision_artifact_hash_ref=decision_artifact.hash_ref,
            evidence_artifact_hash_ref=evidence_artifact.hash_ref,
            result_artifact_hash_ref=execution.result_artifact.hash_ref,
            created_at=CREATED_AT,
            created_by=CREATED_BY,
        )


# ---------------------------------------------------------------------------
# Provenance substitution
# ---------------------------------------------------------------------------


def test_provenance_substitution_wrong_hypothesis_fails_closed(tmp_path: Path) -> None:
    registry, store, manifest_a, manifest_ref_a = _case(tmp_path, hypothesis_id=HYPOTHESIS_ID)
    _register(registry, OTHER_HYPOTHESIS_ID)
    manifest_b = _manifest(store, registry, hypothesis_id=OTHER_HYPOTHESIS_ID)
    _freeze(registry, store, manifest_b)

    execution_a, evidence_artifact_a, decision_a, decision_artifact_a = _decision(
        registry, store, hypothesis_id=HYPOTHESIS_ID
    )
    # Deliberately associate hypothesis B with hypothesis A's whole lineage.
    with pytest.raises(ExperimentExecutionContractError, match="manifest"):
        _gate(registry, store).authorize(
            OTHER_HYPOTHESIS_ID,
            validation_decision_artifact_hash_ref=decision_artifact_a.hash_ref,
            evidence_artifact_hash_ref=evidence_artifact_a.hash_ref,
            result_artifact_hash_ref=execution_a.result_artifact.hash_ref,
            created_at=CREATED_AT,
            created_by=CREATED_BY,
        )


# ---------------------------------------------------------------------------
# Manifest / evidence / execution mismatch
# ---------------------------------------------------------------------------


def test_manifest_mismatch_direct_build_rejected(tmp_path: Path) -> None:
    registry, store, manifest, manifest_ref = _case(tmp_path)
    execution, evidence_artifact, decision, decision_artifact = _decision(registry, store)
    _register(registry, OTHER_HYPOTHESIS_ID)
    other_manifest = _manifest(store, registry, hypothesis_id=OTHER_HYPOTHESIS_ID)
    with pytest.raises(FinalHoldoutDecisionGateError, match="manifest binding"):
        build_final_holdout_authorization_v1(
            manifest=other_manifest,
            validation_decision=decision,
            validation_decision_artifact_hash_ref=decision_artifact.hash_ref,
            created_at=CREATED_AT,
            created_by=CREATED_BY,
        )


def test_evidence_result_execution_mismatch_via_swapped_refs(tmp_path: Path) -> None:
    registry, store, manifest, manifest_ref = _case(tmp_path)
    execution, evidence_artifact, decision, decision_artifact = _decision(registry, store)
    # Swap in a result ref that never fed this evidence/decision at all.
    with pytest.raises((ArtifactNotFoundError, ArtifactIntegrityError, FinalHoldoutDecisionGateError)):
        _gate(registry, store).authorize(
            HYPOTHESIS_ID,
            validation_decision_artifact_hash_ref=decision_artifact.hash_ref,
            evidence_artifact_hash_ref=evidence_artifact.hash_ref,
            result_artifact_hash_ref=f"sha256:{'8' * 64}",
            created_at=CREATED_AT,
            created_by=CREATED_BY,
        )


# ---------------------------------------------------------------------------
# Duplicate retry / idempotency
# ---------------------------------------------------------------------------


def test_duplicate_retry_is_idempotent(tmp_path: Path) -> None:
    registry, store, manifest, manifest_ref = _case(tmp_path)
    execution, evidence_artifact, decision, decision_artifact = _decision(registry, store)
    gate = _gate(registry, store)
    first, artifact1 = gate.authorize(
        HYPOTHESIS_ID,
        validation_decision_artifact_hash_ref=decision_artifact.hash_ref,
        evidence_artifact_hash_ref=evidence_artifact.hash_ref,
        result_artifact_hash_ref=execution.result_artifact.hash_ref,
        created_at=CREATED_AT,
        created_by=CREATED_BY,
    )
    second, artifact2 = gate.authorize(
        HYPOTHESIS_ID,
        validation_decision_artifact_hash_ref=decision_artifact.hash_ref,
        evidence_artifact_hash_ref=evidence_artifact.hash_ref,
        result_artifact_hash_ref=execution.result_artifact.hash_ref,
        created_at=CREATED_AT,
        created_by=CREATED_BY,
    )
    assert first == second
    assert first.authorization_semantic_identity == second.authorization_semantic_identity
    assert artifact1.hash_ref == artifact2.hash_ref
    record = gate.store.get(HYPOTHESIS_ID)
    assert record.authorization_artifact_hash_ref == artifact1.hash_ref


def test_duplicate_retry_with_different_diagnostics_still_idempotent(tmp_path: Path) -> None:
    # Different created_at/created_by does not change semantic identity, so
    # the persistent store must treat this as the same authoritative grant.
    registry, store, manifest, manifest_ref = _case(tmp_path)
    execution, evidence_artifact, decision, decision_artifact = _decision(registry, store)
    gate = _gate(registry, store)
    first, artifact1 = gate.authorize(
        HYPOTHESIS_ID,
        validation_decision_artifact_hash_ref=decision_artifact.hash_ref,
        evidence_artifact_hash_ref=evidence_artifact.hash_ref,
        result_artifact_hash_ref=execution.result_artifact.hash_ref,
        created_at=CREATED_AT,
        created_by=CREATED_BY,
    )
    second, artifact2 = gate.authorize(
        HYPOTHESIS_ID,
        validation_decision_artifact_hash_ref=decision_artifact.hash_ref,
        evidence_artifact_hash_ref=evidence_artifact.hash_ref,
        result_artifact_hash_ref=execution.result_artifact.hash_ref,
        created_at="2027-01-01T00:00:00+00:00",
        created_by="operator:different",
    )
    assert first.authorization_semantic_identity == second.authorization_semantic_identity
    assert first.canonical_bytes() != second.canonical_bytes()  # exact CAS bytes differ.
    assert artifact1.hash_ref != artifact2.hash_ref  # two distinct CAS objects...
    record = gate.store.get(HYPOTHESIS_ID)
    assert record.authorization_artifact_hash_ref == artifact1.hash_ref  # ...but only the first is on file.


# ---------------------------------------------------------------------------
# Conflicting authorization
# ---------------------------------------------------------------------------


def test_conflicting_authorization_rejected(tmp_path: Path) -> None:
    # The persistent store must enforce exactly one authoritative grant per
    # hypothesis: a second, semantically-different authorization for the same
    # hypothesis_id (here: a different claimed validation_decision_semantic_identity,
    # as would happen if a stale or substituted lineage were re-submitted)
    # must be rejected even though it is itself a well-formed, individually
    # valid FinalHoldoutAuthorizationV1 object and persists fine under CAS.
    import dataclasses

    registry, store, manifest, manifest_ref = _case(tmp_path)
    execution, evidence_artifact, decision, decision_artifact = _decision(registry, store)
    gate = _gate(registry, store)
    first, first_artifact = gate.authorize(
        HYPOTHESIS_ID,
        validation_decision_artifact_hash_ref=decision_artifact.hash_ref,
        evidence_artifact_hash_ref=evidence_artifact.hash_ref,
        result_artifact_hash_ref=execution.result_artifact.hash_ref,
        created_at=CREATED_AT,
        created_by=CREATED_BY,
    )

    conflicting = dataclasses.replace(
        first, validation_decision_semantic_identity=f"sha256:{'2' * 64}"
    )
    assert conflicting.authorization_semantic_identity != first.authorization_semantic_identity
    conflicting_artifact = persist_final_holdout_authorization_v1(conflicting, artifact_store=store)

    with pytest.raises(FinalHoldoutAuthorizationAlreadyGrantedError):
        gate.store.register(
            conflicting, authorization_artifact_hash_ref=conflicting_artifact.hash_ref
        )
    # The original grant is untouched.
    record = gate.store.get(HYPOTHESIS_ID)
    assert record.authorization_artifact_hash_ref == first_artifact.hash_ref


def test_conflicting_criteria_decision_rejected(tmp_path: Path) -> None:
    import trademind.experiment_execution_contract as contract_module
    import trademind.validation_decision as decision_module

    registry, store, manifest, manifest_ref = _case(tmp_path, threshold=-1.0)
    execution, evidence_artifact, real_decision, decision_artifact = _decision(registry, store)

    # A self-consistent-but-untruthful validation decision: observed metrics
    # pass, yet outcome falsely claims PASS while the embedded criteria
    # decision disagrees with what recomputation would produce.
    lying_criteria_decision = contract_module.CriteriaDecisionV1(
        schema_version="experiment-execution-criteria-decision-v1",
        mode=real_decision.criteria_decision.mode,
        passed=True,
        primary_metric=real_decision.criteria_decision.primary_metric,
        primary_metric_value=real_decision.criteria_decision.primary_metric_value,
        evaluations=tuple(
            contract_module.CriterionEvaluation(
                metric=ev.metric,
                operator=ev.operator,
                threshold=999999.0,
                observed=ev.observed,
                passed=True,
            )
            for ev in real_decision.criteria_decision.evaluations
        ),
    )
    tampered_decision = decision_module.ValidationDecisionV1(
        schema_version=real_decision.schema_version,
        decision_kind=real_decision.decision_kind,
        outcome=real_decision.outcome,
        execution_identity=real_decision.execution_identity,
        execution_phase=real_decision.execution_phase,
        hypothesis_id=real_decision.hypothesis_id,
        hypothesis_family_id=real_decision.hypothesis_family_id,
        hypothesis_content_hash=real_decision.hypothesis_content_hash,
        manifest_semantic_hash=real_decision.manifest_semantic_hash,
        manifest_artifact_hash_ref=real_decision.manifest_artifact_hash_ref,
        split_plan_semantic_hash=real_decision.split_plan_semantic_hash,
        evaluator_id=real_decision.evaluator_id,
        evaluator_version=real_decision.evaluator_version,
        dataset_artifact_hash_refs=real_decision.dataset_artifact_hash_refs,
        evidence_artifact_hash_ref=real_decision.evidence_artifact_hash_ref,
        evidence_semantic_identity=real_decision.evidence_semantic_identity,
        result_artifact_hash_ref=real_decision.result_artifact_hash_ref,
        result_semantic_identity=real_decision.result_semantic_identity,
        deterministic_seed=real_decision.deterministic_seed,
        code_provenance=real_decision.code_provenance,
        friction_absent=real_decision.friction_absent,
        friction_model_id=real_decision.friction_model_id,
        friction_unit=real_decision.friction_unit,
        observed_metrics=real_decision.observed_metrics,
        criteria_decision=lying_criteria_decision,
        diagnostics=real_decision.diagnostics,
    )
    with pytest.raises(FinalHoldoutAuthorizationConflictError):
        build_final_holdout_authorization_v1(
            manifest=manifest,
            validation_decision=tampered_decision,
            validation_decision_artifact_hash_ref=decision_artifact.hash_ref,
            created_at=CREATED_AT,
            created_by=CREATED_BY,
        )


# ---------------------------------------------------------------------------
# Restart / reload
# ---------------------------------------------------------------------------


def test_restart_reload_reproduces_identical_authorization(tmp_path: Path) -> None:
    registry_path = tmp_path / "registry.db"
    store_root = tmp_path / "artifacts"
    registry = HypothesisRegistry(registry_path)
    _register(registry)
    store = ArtifactStore(store_root)
    manifest = _manifest(store, registry)
    _freeze(registry, store, manifest)
    execution, evidence_artifact, decision, decision_artifact = _decision(registry, store)

    authorization, artifact = _gate(registry, store).authorize(
        HYPOTHESIS_ID,
        validation_decision_artifact_hash_ref=decision_artifact.hash_ref,
        evidence_artifact_hash_ref=evidence_artifact.hash_ref,
        result_artifact_hash_ref=execution.result_artifact.hash_ref,
        created_at=CREATED_AT,
        created_by=CREATED_BY,
    )

    restarted_registry = HypothesisRegistry(registry_path)
    restarted_store = ArtifactStore(store_root)
    reloaded = _gate(restarted_registry, restarted_store).load(
        HYPOTHESIS_ID,
        authorization_artifact_hash_ref=artifact.hash_ref,
        validation_decision_artifact_hash_ref=decision_artifact.hash_ref,
        evidence_artifact_hash_ref=evidence_artifact.hash_ref,
        result_artifact_hash_ref=execution.result_artifact.hash_ref,
    )
    assert reloaded == authorization


def test_persist_load_verified_cas_round_trip(tmp_path: Path) -> None:
    registry, store, manifest, manifest_ref = _case(tmp_path)
    execution, evidence_artifact, decision, decision_artifact = _decision(registry, store)
    authorization = build_final_holdout_authorization_v1(
        manifest=manifest,
        validation_decision=decision,
        validation_decision_artifact_hash_ref=decision_artifact.hash_ref,
        created_at=CREATED_AT,
        created_by=CREATED_BY,
    )
    artifact = persist_final_holdout_authorization_v1(authorization, artifact_store=store)
    loaded = load_final_holdout_authorization_v1(
        artifact.hash_ref,
        artifact_store=store,
        manifest=manifest,
        validation_decision=decision,
        validation_decision_artifact_hash_ref=decision_artifact.hash_ref,
    )
    assert loaded == authorization


# ---------------------------------------------------------------------------
# Proof that holdout content cannot be read through this gate
# ---------------------------------------------------------------------------


def test_holdout_phase_structurally_impossible() -> None:
    for member in ExecutionPhase:
        assert member.value not in ("HOLDOUT", "FINAL_HOLDOUT")
    with pytest.raises(ExperimentExecutionContractError):
        ExecutionPhase.from_value("FINAL_HOLDOUT")


def test_module_never_imports_holdout_secrecy_machinery() -> None:
    import ast
    import inspect

    import trademind.final_holdout_decision_gate as module

    source = inspect.getsource(module)
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    forbidden = {
        "requests",
        "urllib",
        "socket",
        "http",
        "bybit",
        "paper_gate",
        "live_signal_runtime",
        "broker",
        "trademind.market.provider",
        "trademind.orchestrator.engine",
        "trademind.discovery.holdout_crypto",
        "trademind.discovery.holdout_keys",
        "trademind.discovery.holdout_sealer",
        "trademind.discovery.holdout_runner",
        "trademind.discovery.holdout_store",
    }
    assert not (imported & forbidden), imported & forbidden
    # No dotted module name anywhere in the import set may even contain "holdout"
    # except the closed split-boundary machinery this layer legitimately reuses.
    allowed_holdout_substrings = {"trademind.discovery.split_engine"}
    leaked = {
        name
        for name in imported
        if "holdout" in name.lower() and name not in allowed_holdout_substrings
    }
    assert not leaked, leaked


def test_module_never_mutates_hypothesis_lifecycle() -> None:
    import inspect

    import trademind.final_holdout_decision_gate as module

    source = inspect.getsource(module)
    assert not hasattr(module, "transition")
    for forbidden_call in (
        ".transition(",
        ".freeze(",
        ".freeze_manifest_v2_in_transaction(",
        "HOLDOUT_CONSUMED",
        "mark_isolated",
        "seal_file",
        "decrypt_bytes",
        "verify_envelope",
        "seal_bytes",
    ):
        assert forbidden_call not in source, forbidden_call


def test_authorization_never_contains_holdout_content_only_boundaries(tmp_path: Path) -> None:
    import json

    registry, store, manifest, manifest_ref = _case(tmp_path)
    execution, evidence_artifact, decision, decision_artifact = _decision(registry, store)
    authorization, _ = _gate(registry, store).authorize(
        HYPOTHESIS_ID,
        validation_decision_artifact_hash_ref=decision_artifact.hash_ref,
        evidence_artifact_hash_ref=evidence_artifact.hash_ref,
        result_artifact_hash_ref=execution.result_artifact.hash_ref,
        created_at=CREATED_AT,
        created_by=CREATED_BY,
    )
    payload = json.loads(authorization.canonical_bytes())

    # Only the boundary/count fields and the fixed-False marker are allowed to
    # mention "holdout"; nothing else (no rows, no metric values, no plaintext).
    allowed_holdout_paths = {
        ("holdout_window",),
        ("holdout_window", "holdout_start"),
        ("holdout_window", "holdout_end"),
        ("holdout_window", "holdout_row_count"),
        ("authorization_semantic_markers", "final_holdout_consumed"),
        ("authorization_semantic_markers", "final_holdout_content_read"),
    }

    def walk(value: object, path: tuple[str, ...]) -> None:
        if isinstance(value, dict):
            for key, nested in value.items():
                full_path = path + (str(key),)
                if "holdout" in str(key).lower() and full_path not in allowed_holdout_paths:
                    raise AssertionError(f"unexpected holdout-related field: {'.'.join(full_path)}")
                walk(nested, full_path)
        elif isinstance(value, list):
            for index, nested in enumerate(value):
                walk(nested, path + (str(index),))

    walk(payload, ())
    markers = authorization.semantic_projection()["authorization_semantic_markers"]
    assert markers["final_holdout_content_read"] is False
    assert markers["final_holdout_consumed"] is False
    assert markers["scientifically_validated"] is False
    assert markers["trading_authorized"] is False
    assert markers["hypothesis_registry_mutated"] is False

    # And the object model itself has no way to carry plaintext: the exact,
    # complete field set is fixed and none of it is row/observation data.
    known_fields = {
        "schema_version",
        "authorization_kind",
        "execution_identity",
        "execution_phase",
        "hypothesis_id",
        "hypothesis_family_id",
        "hypothesis_content_hash",
        "manifest_semantic_hash",
        "manifest_artifact_hash_ref",
        "split_plan_semantic_hash",
        "holdout_start",
        "holdout_end",
        "holdout_row_count",
        "evaluator_id",
        "evaluator_version",
        "dataset_artifact_hash_refs",
        "validation_decision_artifact_hash_ref",
        "validation_decision_semantic_identity",
        "evidence_artifact_hash_ref",
        "evidence_semantic_identity",
        "result_artifact_hash_ref",
        "result_semantic_identity",
        "deterministic_seed",
        "code_provenance",
        "friction_absent",
        "friction_model_id",
        "friction_unit",
        "criteria_decision",
        "diagnostics",
        "authorization_semantic_identity",
    }
    assert set(authorization.__dataclass_fields__) == known_fields


def test_authorization_semantic_markers_are_module_constant_not_a_field(tmp_path: Path) -> None:
    registry, store, manifest, manifest_ref = _case(tmp_path)
    execution, evidence_artifact, decision, decision_artifact = _decision(registry, store)
    authorization, _ = _gate(registry, store).authorize(
        HYPOTHESIS_ID,
        validation_decision_artifact_hash_ref=decision_artifact.hash_ref,
        evidence_artifact_hash_ref=evidence_artifact.hash_ref,
        result_artifact_hash_ref=execution.result_artifact.hash_ref,
        created_at=CREATED_AT,
        created_by=CREATED_BY,
    )
    assert not hasattr(authorization, "authorization_semantic_markers")
    assert not hasattr(authorization, "authorized")
    assert (
        authorization.to_payload()["authorization_semantic_markers"]
        == FINAL_HOLDOUT_AUTHORIZATION_SEMANTIC_MARKERS
    )
