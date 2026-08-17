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
from trademind.orchestrator.artifact_store import (
    ArtifactIntegrityError,
    ArtifactNotFoundError,
    ArtifactStore,
)
from trademind.signal_statistics_provenance import CodeProvenance
from trademind.validation_decision import (
    VALIDATION_DECISION_SEMANTIC_MARKERS,
    ValidationDecisionBuilderV1,
    ValidationDecisionConflictError,
    ValidationDecisionError,
    ValidationDecisionPhaseError,
    ValidationOutcome,
    build_validation_decision_v1,
    load_validation_decision_v1,
    persist_validation_decision_v1,
    verify_validation_decision_v1,
)

FAMILY = "decision-eval-v1"
EVALUATOR_ID = "fake-decision-eval-v1"
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


class MeanWinEvaluator:
    """Evaluator producing two metrics so ALL/ANY criteria can be exercised."""

    evaluator_id = EVALUATOR_ID
    evaluator_version = EVALUATOR_VERSION

    def evaluate(self, rows, *, manifest, execution_phase):
        del manifest, execution_phase
        values = [float(row.fields["value"]) for row in rows]
        mean_value = sum(values) / len(values)
        win_rate = sum(1 for v in values if v >= mean_value) / len(values)
        return {"mean_value": mean_value, "win_rate": win_rate}


def _family(hypothesis_id: str = HYPOTHESIS_ID) -> dict[str, object]:
    return {"candidate_id": CANDIDATE_ID, "claim": f"decision effect for {hypothesis_id}"}


def _content(hypothesis_id: str = HYPOTHESIS_ID) -> dict[str, object]:
    return {
        "family_definition": _family(hypothesis_id),
        "proposal": {"falsifiable_claim": "decision effect remains positive"},
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


def _criteria(
    *,
    mode: CriteriaMode = CriteriaMode.ALL,
    mean_threshold: float = -1.0,
    win_threshold: float = -1.0,
) -> EvaluationCriteriaV1:
    return EvaluationCriteriaV1(
        mode=mode,
        criteria=(
            EvaluationCriterionV1(
                metric="mean_value",
                operator=CriterionOperator.GREATER_THAN_OR_EQUAL,
                threshold=mean_threshold,
            ),
            EvaluationCriterionV1(
                metric="win_rate",
                operator=CriterionOperator.GREATER_THAN_OR_EQUAL,
                threshold=win_threshold,
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
    criteria: EvaluationCriteriaV1 | None = None,
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
            task_id="decision-task",
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
        evaluation_criteria=criteria or _criteria(),
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
        supported_metrics=("mean_value", "win_rate"),
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
        evaluator=MeanWinEvaluator(),
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


def _case(tmp_path: Path, *, hypothesis_id: str = HYPOTHESIS_ID, **manifest_kwargs):
    registry = HypothesisRegistry(tmp_path / "registry.db")
    _register(registry, hypothesis_id)
    store = ArtifactStore(tmp_path / "artifacts")
    manifest = _manifest(store, registry, hypothesis_id=hypothesis_id, **manifest_kwargs)
    manifest_ref = _freeze(registry, store, manifest)
    return registry, store, manifest, manifest_ref


def _execute(registry, store, hypothesis_id=HYPOTHESIS_ID, phase=ExecutionPhase.VALIDATION):
    runtime = _runtime(registry, store)
    return runtime.execute(
        hypothesis_id,
        execution_phase=phase,
        execution_code_provenance=_code_provenance(),
        evaluator_friction=None,
        created_at=CREATED_AT,
        created_by=CREATED_BY,
    )


def _evidence(registry, store, hypothesis_id=HYPOTHESIS_ID, phase=ExecutionPhase.VALIDATION):
    execution = _execute(registry, store, hypothesis_id=hypothesis_id, phase=phase)
    evidence, artifact = _evidence_builder(registry, store).build(
        hypothesis_id, execution=execution, created_at=CREATED_AT, created_by=CREATED_BY
    )
    return execution, evidence, artifact


# ---------------------------------------------------------------------------
# PASS / FAIL
# ---------------------------------------------------------------------------


def test_pass_outcome_recorded(tmp_path: Path) -> None:
    registry, store, manifest, manifest_ref = _case(tmp_path, criteria=_criteria(mean_threshold=-1.0))
    execution, evidence, evidence_artifact = _evidence(registry, store)
    decision, artifact = _decision_builder(registry, store).decide(
        HYPOTHESIS_ID,
        evidence_artifact_hash_ref=evidence_artifact.hash_ref,
        result_artifact_hash_ref=execution.result_artifact.hash_ref,
        created_at=CREATED_AT,
        created_by=CREATED_BY,
    )
    assert decision.outcome is ValidationOutcome.PASS
    assert decision.criteria_decision.passed is True
    assert decision.execution_phase is ExecutionPhase.VALIDATION
    assert decision.evidence_artifact_hash_ref == evidence_artifact.hash_ref
    assert decision.result_artifact_hash_ref == execution.result_artifact.hash_ref
    assert decision.manifest_artifact_hash_ref == manifest_ref
    assert decision.split_plan_semantic_hash == manifest.split_plan.semantic_hash
    assert decision.semantic_projection()["decision_semantic_markers"] == VALIDATION_DECISION_SEMANTIC_MARKERS
    assert artifact.hash_ref
    assert verify_validation_decision_v1(decision.canonical_bytes()) == decision


def test_fail_outcome_recorded(tmp_path: Path) -> None:
    registry, store, manifest, manifest_ref = _case(
        tmp_path, criteria=_criteria(mean_threshold=999.0)
    )
    execution, evidence, evidence_artifact = _evidence(registry, store)
    decision, artifact = _decision_builder(registry, store).decide(
        HYPOTHESIS_ID,
        evidence_artifact_hash_ref=evidence_artifact.hash_ref,
        result_artifact_hash_ref=execution.result_artifact.hash_ref,
        created_at=CREATED_AT,
        created_by=CREATED_BY,
    )
    assert decision.outcome is ValidationOutcome.FAIL
    assert decision.criteria_decision.passed is False
    # A FAIL is still faithfully recorded evidence of what happened.
    assert decision.semantic_projection()["decision_semantic_markers"]["scientifically_validated"] is False


# ---------------------------------------------------------------------------
# ALL / ANY semantics
# ---------------------------------------------------------------------------


def test_all_mode_requires_every_criterion(tmp_path: Path) -> None:
    # mean_value passes trivially; win_rate cannot reach 2.0 (rates are <= 1).
    criteria = _criteria(mode=CriteriaMode.ALL, mean_threshold=-1.0, win_threshold=2.0)
    registry, store, manifest, manifest_ref = _case(tmp_path, criteria=criteria)
    execution, evidence, evidence_artifact = _evidence(registry, store)
    decision, _ = _decision_builder(registry, store).decide(
        HYPOTHESIS_ID,
        evidence_artifact_hash_ref=evidence_artifact.hash_ref,
        result_artifact_hash_ref=execution.result_artifact.hash_ref,
        created_at=CREATED_AT,
        created_by=CREATED_BY,
    )
    assert decision.criteria_decision.mode is CriteriaMode.ALL
    assert decision.outcome is ValidationOutcome.FAIL


def test_any_mode_passes_with_one_satisfied_criterion(tmp_path: Path) -> None:
    # win_rate cannot reach 2.0, but mean_value >= -1.0 always holds.
    criteria = _criteria(mode=CriteriaMode.ANY, mean_threshold=-1.0, win_threshold=2.0)
    registry, store, manifest, manifest_ref = _case(tmp_path, criteria=criteria)
    execution, evidence, evidence_artifact = _evidence(registry, store)
    decision, _ = _decision_builder(registry, store).decide(
        HYPOTHESIS_ID,
        evidence_artifact_hash_ref=evidence_artifact.hash_ref,
        result_artifact_hash_ref=execution.result_artifact.hash_ref,
        created_at=CREATED_AT,
        created_by=CREATED_BY,
    )
    assert decision.criteria_decision.mode is CriteriaMode.ANY
    assert decision.outcome is ValidationOutcome.PASS
    # Exact per-criterion reasons are preserved.
    reasons = {ev.metric: ev.passed for ev in decision.criteria_decision.evaluations}
    assert reasons["mean_value"] is True
    assert reasons["win_rate"] is False


# ---------------------------------------------------------------------------
# Missing / invalid evidence
# ---------------------------------------------------------------------------


def test_discovery_phase_evidence_rejected(tmp_path: Path) -> None:
    registry, store, manifest, manifest_ref = _case(tmp_path)
    execution, evidence, evidence_artifact = _evidence(
        registry, store, phase=ExecutionPhase.DISCOVERY
    )
    with pytest.raises(ValidationDecisionPhaseError):
        _decision_builder(registry, store).decide(
            HYPOTHESIS_ID,
            evidence_artifact_hash_ref=evidence_artifact.hash_ref,
            result_artifact_hash_ref=execution.result_artifact.hash_ref,
            created_at=CREATED_AT,
            created_by=CREATED_BY,
        )


def test_missing_evidence_ref_fails_closed(tmp_path: Path) -> None:
    registry, store, manifest, manifest_ref = _case(tmp_path)
    execution, evidence, evidence_artifact = _evidence(registry, store)
    with pytest.raises(ArtifactNotFoundError):
        _decision_builder(registry, store).decide(
            HYPOTHESIS_ID,
            evidence_artifact_hash_ref=f"sha256:{'9' * 64}",
            result_artifact_hash_ref=execution.result_artifact.hash_ref,
            created_at=CREATED_AT,
            created_by=CREATED_BY,
        )


def test_invalid_manifest_type_rejected(tmp_path: Path) -> None:
    registry, store, manifest, manifest_ref = _case(tmp_path)
    execution, evidence, evidence_artifact = _evidence(registry, store)
    with pytest.raises(ValidationDecisionError):
        build_validation_decision_v1(
            manifest=object(),
            evidence=evidence,
            evidence_artifact_hash_ref=evidence_artifact.hash_ref,
            created_at=CREATED_AT,
            created_by=CREATED_BY,
        )


# ---------------------------------------------------------------------------
# Provenance mismatch
# ---------------------------------------------------------------------------


def test_provenance_mismatch_wrong_hypothesis_fails_closed(tmp_path: Path) -> None:
    registry, store, manifest_a, manifest_ref_a = _case(tmp_path, hypothesis_id=HYPOTHESIS_ID)
    _register(registry, OTHER_HYPOTHESIS_ID)
    manifest_b = _manifest(store, registry, hypothesis_id=OTHER_HYPOTHESIS_ID)
    _freeze(registry, store, manifest_b)

    execution_a, evidence_a, evidence_artifact_a = _evidence(registry, store, hypothesis_id=HYPOTHESIS_ID)
    with pytest.raises(ExperimentExecutionContractError, match="manifest"):
        _decision_builder(registry, store).decide(
            OTHER_HYPOTHESIS_ID,
            evidence_artifact_hash_ref=evidence_artifact_a.hash_ref,
            result_artifact_hash_ref=execution_a.result_artifact.hash_ref,
            created_at=CREATED_AT,
            created_by=CREATED_BY,
        )


# ---------------------------------------------------------------------------
# CAS corruption
# ---------------------------------------------------------------------------


def test_corrupt_manifest_cas_fails_closed(tmp_path: Path) -> None:
    registry, store, manifest, manifest_ref = _case(tmp_path)
    execution, evidence, evidence_artifact = _evidence(registry, store)
    artifact = store.resolve_verified(manifest_ref)
    Path(artifact.path).write_bytes(b"corrupted manifest bytes")
    with pytest.raises(ArtifactIntegrityError):
        _decision_builder(registry, store).decide(
            HYPOTHESIS_ID,
            evidence_artifact_hash_ref=evidence_artifact.hash_ref,
            result_artifact_hash_ref=execution.result_artifact.hash_ref,
            created_at=CREATED_AT,
            created_by=CREATED_BY,
        )


def test_corrupt_evidence_cas_fails_closed(tmp_path: Path) -> None:
    registry, store, manifest, manifest_ref = _case(tmp_path)
    execution, evidence, evidence_artifact = _evidence(registry, store)
    resolved = store.resolve_verified(evidence_artifact.hash_ref)
    Path(resolved.path).write_bytes(b"corrupted evidence bytes")
    with pytest.raises(ArtifactIntegrityError):
        _decision_builder(registry, store).decide(
            HYPOTHESIS_ID,
            evidence_artifact_hash_ref=evidence_artifact.hash_ref,
            result_artifact_hash_ref=execution.result_artifact.hash_ref,
            created_at=CREATED_AT,
            created_by=CREATED_BY,
        )


def test_missing_decision_cas_fails_closed_on_reload(tmp_path: Path) -> None:
    registry, store, manifest, manifest_ref = _case(tmp_path)
    execution, evidence, evidence_artifact = _evidence(registry, store)
    decision, artifact = _decision_builder(registry, store).decide(
        HYPOTHESIS_ID,
        evidence_artifact_hash_ref=evidence_artifact.hash_ref,
        result_artifact_hash_ref=execution.result_artifact.hash_ref,
        created_at=CREATED_AT,
        created_by=CREATED_BY,
    )
    resolved = store.resolve_verified(artifact.hash_ref)
    Path(resolved.path).unlink()
    with pytest.raises(ArtifactNotFoundError):
        _decision_builder(registry, store).load(
            HYPOTHESIS_ID,
            decision_artifact_hash_ref=artifact.hash_ref,
            evidence_artifact_hash_ref=evidence_artifact.hash_ref,
            result_artifact_hash_ref=execution.result_artifact.hash_ref,
        )


# ---------------------------------------------------------------------------
# Holdout leakage rejection
# ---------------------------------------------------------------------------


def test_holdout_phase_structurally_impossible() -> None:
    for member in ExecutionPhase:
        assert member.value not in ("HOLDOUT", "FINAL_HOLDOUT")
    with pytest.raises(ExperimentExecutionContractError):
        ExecutionPhase.from_value("FINAL_HOLDOUT")


def test_decision_never_contains_holdout_fields_or_conclusions(tmp_path: Path) -> None:
    import json

    registry, store, manifest, manifest_ref = _case(tmp_path)
    execution, evidence, evidence_artifact = _evidence(registry, store)
    decision, _ = _decision_builder(registry, store).decide(
        HYPOTHESIS_ID,
        evidence_artifact_hash_ref=evidence_artifact.hash_ref,
        result_artifact_hash_ref=execution.result_artifact.hash_ref,
        created_at=CREATED_AT,
        created_by=CREATED_BY,
    )

    allowed_holdout_keys = {"final_holdout_consumed"}

    def has_holdout(value: object) -> bool:
        if isinstance(value, dict):
            return any(
                "holdout" in str(k).lower() and k not in allowed_holdout_keys for k in value
            ) or any(has_holdout(v) for v in value.values())
        if isinstance(value, list):
            return any(has_holdout(v) for v in value)
        return False

    assert not has_holdout(json.loads(decision.canonical_bytes()))
    markers = decision.semantic_projection()["decision_semantic_markers"]
    assert markers["final_holdout_consumed"] is False
    assert markers["scientifically_validated"] is False
    assert markers["trading_authorized"] is False
    assert markers["hypothesis_registry_mutated"] is False


# ---------------------------------------------------------------------------
# Deterministic retry / reload
# ---------------------------------------------------------------------------


def test_deterministic_retry_is_idempotent(tmp_path: Path) -> None:
    registry, store, manifest, manifest_ref = _case(tmp_path)
    execution, evidence, evidence_artifact = _evidence(registry, store)
    builder = _decision_builder(registry, store)
    first, artifact1 = builder.decide(
        HYPOTHESIS_ID,
        evidence_artifact_hash_ref=evidence_artifact.hash_ref,
        result_artifact_hash_ref=execution.result_artifact.hash_ref,
        created_at=CREATED_AT,
        created_by=CREATED_BY,
    )
    second, artifact2 = builder.decide(
        HYPOTHESIS_ID,
        evidence_artifact_hash_ref=evidence_artifact.hash_ref,
        result_artifact_hash_ref=execution.result_artifact.hash_ref,
        created_at=CREATED_AT,
        created_by=CREATED_BY,
    )
    assert first == second
    assert first.decision_semantic_identity == second.decision_semantic_identity
    assert artifact1.hash_ref == artifact2.hash_ref


def test_restart_reload_reproduces_identical_decision(tmp_path: Path) -> None:
    registry_path = tmp_path / "registry.db"
    store_root = tmp_path / "artifacts"
    registry = HypothesisRegistry(registry_path)
    _register(registry)
    store = ArtifactStore(store_root)
    manifest = _manifest(store, registry)
    _freeze(registry, store, manifest)
    execution, evidence, evidence_artifact = _evidence(registry, store)
    decision, artifact = _decision_builder(registry, store).decide(
        HYPOTHESIS_ID,
        evidence_artifact_hash_ref=evidence_artifact.hash_ref,
        result_artifact_hash_ref=execution.result_artifact.hash_ref,
        created_at=CREATED_AT,
        created_by=CREATED_BY,
    )

    restarted_registry = HypothesisRegistry(registry_path)
    restarted_store = ArtifactStore(store_root)
    reloaded = _decision_builder(restarted_registry, restarted_store).load(
        HYPOTHESIS_ID,
        decision_artifact_hash_ref=artifact.hash_ref,
        evidence_artifact_hash_ref=evidence_artifact.hash_ref,
        result_artifact_hash_ref=execution.result_artifact.hash_ref,
    )
    assert reloaded == decision


# ---------------------------------------------------------------------------
# Conflict
# ---------------------------------------------------------------------------


def test_conflicting_criteria_decision_rejected(tmp_path: Path) -> None:
    import trademind.experiment_evidence as evidence_module

    registry, store, manifest, manifest_ref = _case(tmp_path, criteria=_criteria(mean_threshold=-1.0))
    execution, real_evidence, evidence_artifact = _evidence(registry, store)

    # A self-consistent-but-untruthful evidence record: observed metrics pass
    # (mean_value >= -1.0), yet the embedded decision falsely claims failure.
    from trademind.experiment_execution_contract import CriteriaDecisionV1, CriterionEvaluation

    lying_observed = real_evidence.observed_metrics
    lying_decision = CriteriaDecisionV1(
        schema_version="experiment-execution-criteria-decision-v1",
        mode=real_evidence.criteria_decision.mode,
        passed=False,
        primary_metric=real_evidence.criteria_decision.primary_metric,
        primary_metric_value=real_evidence.criteria_decision.primary_metric_value,
        evaluations=tuple(
            CriterionEvaluation(
                metric=ev.metric,
                operator=ev.operator,
                threshold=ev.threshold,
                observed=ev.observed,
                passed=False,
            )
            for ev in real_evidence.criteria_decision.evaluations
        ),
    )
    tampered_evidence = evidence_module.ExperimentEvidenceV1(
        schema_version=real_evidence.schema_version,
        evidence_kind=real_evidence.evidence_kind,
        execution_identity=real_evidence.execution_identity,
        execution_phase=real_evidence.execution_phase,
        hypothesis_id=real_evidence.hypothesis_id,
        hypothesis_family_id=real_evidence.hypothesis_family_id,
        hypothesis_content_hash=real_evidence.hypothesis_content_hash,
        manifest_semantic_hash=real_evidence.manifest_semantic_hash,
        manifest_artifact_hash_ref=real_evidence.manifest_artifact_hash_ref,
        split_plan_semantic_hash=real_evidence.split_plan_semantic_hash,
        evaluator_id=real_evidence.evaluator_id,
        evaluator_version=real_evidence.evaluator_version,
        dataset_artifact_hash_refs=real_evidence.dataset_artifact_hash_refs,
        result_artifact_hash_ref=real_evidence.result_artifact_hash_ref,
        result_semantic_identity=real_evidence.result_semantic_identity,
        deterministic_seed=real_evidence.deterministic_seed,
        code_provenance=real_evidence.code_provenance,
        friction_absent=real_evidence.friction_absent,
        friction_model_id=real_evidence.friction_model_id,
        friction_unit=real_evidence.friction_unit,
        observed_metrics=lying_observed,
        criteria_decision=lying_decision,
        diagnostics=real_evidence.diagnostics,
    )
    with pytest.raises(ValidationDecisionConflictError):
        build_validation_decision_v1(
            manifest=manifest,
            evidence=tampered_evidence,
            evidence_artifact_hash_ref=evidence_artifact.hash_ref,
            created_at=CREATED_AT,
            created_by=CREATED_BY,
        )


def test_wrong_manifest_semantic_hash_is_conflict(tmp_path: Path) -> None:
    registry, store, manifest, manifest_ref = _case(tmp_path)
    execution, evidence, evidence_artifact = _evidence(registry, store)
    _register(registry, OTHER_HYPOTHESIS_ID)
    other_manifest = _manifest(store, registry, hypothesis_id=OTHER_HYPOTHESIS_ID)
    with pytest.raises(ValidationDecisionError, match="manifest binding"):
        build_validation_decision_v1(
            manifest=other_manifest,
            evidence=evidence,
            evidence_artifact_hash_ref=evidence_artifact.hash_ref,
            created_at=CREATED_AT,
            created_by=CREATED_BY,
        )


# ---------------------------------------------------------------------------
# Architectural boundaries
# ---------------------------------------------------------------------------


def test_module_never_calls_provider_network_or_broker() -> None:
    import ast
    import inspect

    import trademind.validation_decision as module

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
    }
    assert not (imported & forbidden), imported & forbidden


def test_module_never_mutates_hypothesis_lifecycle() -> None:
    import inspect

    import trademind.validation_decision as module

    source = inspect.getsource(module)
    assert not hasattr(module, "transition")
    for forbidden_call in (
        ".transition(",
        ".freeze(",
        ".freeze_manifest_v2_in_transaction(",
        "HOLDOUT_CONSUMED",
        "mark_isolated",
        "seal_file",
    ):
        assert forbidden_call not in source, forbidden_call


def test_decision_semantic_markers_are_module_constant_not_a_field(tmp_path: Path) -> None:
    registry, store, manifest, manifest_ref = _case(tmp_path)
    execution, evidence, evidence_artifact = _evidence(registry, store)
    decision, _ = _decision_builder(registry, store).decide(
        HYPOTHESIS_ID,
        evidence_artifact_hash_ref=evidence_artifact.hash_ref,
        result_artifact_hash_ref=execution.result_artifact.hash_ref,
        created_at=CREATED_AT,
        created_by=CREATED_BY,
    )
    assert not hasattr(decision, "decision_semantic_markers")
    assert decision.to_payload()["decision_semantic_markers"] == VALIDATION_DECISION_SEMANTIC_MARKERS


def test_persist_load_verified_cas_round_trip(tmp_path: Path) -> None:
    registry, store, manifest, manifest_ref = _case(tmp_path)
    execution, evidence, evidence_artifact = _evidence(registry, store)
    decision = build_validation_decision_v1(
        manifest=manifest,
        evidence=evidence,
        evidence_artifact_hash_ref=evidence_artifact.hash_ref,
        created_at=CREATED_AT,
        created_by=CREATED_BY,
    )
    artifact = persist_validation_decision_v1(decision, artifact_store=store)
    loaded = load_validation_decision_v1(
        artifact.hash_ref,
        artifact_store=store,
        manifest=manifest,
        evidence=evidence,
        evidence_artifact_hash_ref=evidence_artifact.hash_ref,
    )
    assert loaded == decision
