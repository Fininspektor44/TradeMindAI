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
from trademind.experiment_evidence import (
    EVIDENCE_SEMANTIC_MARKERS,
    ExperimentEvidenceBuilderV1,
    ExperimentEvidenceConflictError,
    build_evidence_v1,
    load_evidence_v1,
    persist_evidence_v1,
    verify_experiment_evidence_v1,
)
from trademind.experiment_execution_contract import (
    CriteriaDecisionV1,
    CriterionEvaluation,
    EvaluatorBinding,
    EvaluatorRegistry,
    ExecutionPhase,
    ExperimentExecutionContractError,
    ExperimentResultDiagnostics,
    ExperimentResultV1,
    ObservedMetricsV1,
    load_result_v1,
)
from trademind.experiment_execution_runtime import ExperimentExecutionRuntimeV1
from trademind.orchestrator.artifact_store import (
    ArtifactIntegrityError,
    ArtifactNotFoundError,
    ArtifactStore,
)
from trademind.signal_statistics_provenance import CodeProvenance

FAMILY = "evidence-eval-v1"
EVALUATOR_ID = "fake-evidence-eval-v1"
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
    return {"candidate_id": CANDIDATE_ID, "claim": f"evidence effect for {hypothesis_id}"}


def _content(hypothesis_id: str = HYPOTHESIS_ID) -> dict[str, object]:
    return {
        "family_definition": _family(hypothesis_id),
        "proposal": {"falsifiable_claim": "evidence effect remains positive"},
        "provenance": {"intake_id": hypothesis_id},
    }


def _code_provenance(*, git_commit: str = "f" * 40) -> CodeProvenance:
    return CodeProvenance(
        producer_name="trademind",
        producer_version="1.31.1",
        git_commit=git_commit,
        revision_source="git_worktree",
    )


def _friction() -> TradingFrictionV1:
    return TradingFrictionV1(
        model_id="fixed-bps-v1",
        unit="bps-per-round-trip",
        spread=1.0,
        commission=0.5,
        slippage=0.5,
        fees=0.2,
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


def _criteria(*, primary: str = "mean_value", threshold: float = -1.0) -> EvaluationCriteriaV1:
    return EvaluationCriteriaV1(
        mode=CriteriaMode.ALL,
        criteria=(
            EvaluationCriterionV1(
                metric=primary,
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
    primary_metric: str = "mean_value",
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
            task_id="evidence-task",
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
        primary_metric=primary_metric,
        evaluation_criteria=_criteria(primary=primary_metric, threshold=threshold),
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


def _builder(registry: HypothesisRegistry, store: ArtifactStore) -> ExperimentEvidenceBuilderV1:
    return ExperimentEvidenceBuilderV1(
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


def _execute(registry, store, hypothesis_id=HYPOTHESIS_ID, phase=ExecutionPhase.DISCOVERY):
    runtime = _runtime(registry, store)
    return runtime.execute(
        hypothesis_id,
        execution_phase=phase,
        execution_code_provenance=_code_provenance(),
        evaluator_friction=None,
        created_at=CREATED_AT,
        created_by=CREATED_BY,
    )


# ---------------------------------------------------------------------------
# Happy path / pass-fail criteria
# ---------------------------------------------------------------------------


def test_happy_path_discovery_evidence(tmp_path: Path) -> None:
    registry, store, manifest, manifest_ref = _case(tmp_path)
    execution = _execute(registry, store)
    builder = _builder(registry, store)
    evidence, artifact = builder.build(
        HYPOTHESIS_ID,
        execution=execution,
        created_at=CREATED_AT,
        created_by=CREATED_BY,
    )
    assert evidence.execution_phase is ExecutionPhase.DISCOVERY
    assert evidence.execution_identity == execution.result.execution_identity
    assert evidence.manifest_artifact_hash_ref == manifest_ref
    assert evidence.split_plan_semantic_hash == manifest.split_plan.semantic_hash
    assert evidence.result_artifact_hash_ref == execution.result_artifact.hash_ref
    assert evidence.result_semantic_identity == execution.result.result_semantic_identity
    assert evidence.observed_metrics == execution.result.observed_metrics
    assert evidence.criteria_decision.passed is True
    assert evidence.semantic_projection()["evidence_semantic_markers"] == EVIDENCE_SEMANTIC_MARKERS
    assert artifact.hash_ref  # persisted under Verified CAS.
    # Round-trip through the wire contract.
    assert verify_experiment_evidence_v1(evidence.canonical_bytes()) == evidence


def test_pass_fail_criteria_recorded_both_ways(tmp_path: Path) -> None:
    # Passing case: threshold well below the observed mean.
    registry, store, manifest, manifest_ref = _case(tmp_path, threshold=-1.0)
    execution = _execute(registry, store)
    evidence, _ = _builder(registry, store).build(
        HYPOTHESIS_ID, execution=execution, created_at=CREATED_AT, created_by=CREATED_BY
    )
    assert evidence.criteria_decision.passed is True

    # Failing case: threshold above the observed discovery mean (3.0).
    registry2, store2, manifest2, manifest_ref2 = _case(
        tmp_path / "fail-case", hypothesis_id=HYPOTHESIS_ID, threshold=999.0
    )
    execution2 = _execute(registry2, store2)
    evidence2, _ = _builder(registry2, store2).build(
        HYPOTHESIS_ID, execution=execution2, created_at=CREATED_AT, created_by=CREATED_BY
    )
    assert evidence2.criteria_decision.passed is False
    # Evidence still records what happened even though the criteria failed.
    assert evidence2.semantic_projection()["evidence_semantic_markers"]["scientifically_validated"] is False


# ---------------------------------------------------------------------------
# Missing metric / conflicting evidence
# ---------------------------------------------------------------------------


def test_missing_metric_in_result_fails_closed(tmp_path: Path) -> None:
    registry, store, manifest, manifest_ref = _case(
        tmp_path,
        threshold=-1.0,
    )
    # Manifest predeclares a second criterion metric the tampered result will lack.
    extra_criteria = EvaluationCriteriaV1(
        mode=CriteriaMode.ALL,
        criteria=(
            EvaluationCriterionV1(
                metric="mean_value", operator=CriterionOperator.GREATER_THAN_OR_EQUAL, threshold=-1.0
            ),
            EvaluationCriterionV1(
                metric="win_rate", operator=CriterionOperator.GREATER_THAN_OR_EQUAL, threshold=0.5
            ),
        ),
    )
    manifest = ExperimentManifestV2(
        hypothesis_id=manifest.hypothesis_id,
        hypothesis_family_id=manifest.hypothesis_family_id,
        bound_hypothesis_content_hash=manifest.bound_hypothesis_content_hash,
        proposal_provenance=manifest.proposal_provenance,
        datasets=manifest.datasets,
        split_plan=manifest.split_plan,
        split_dataset_role=manifest.split_dataset_role,
        test_family=manifest.test_family,
        primary_metric=manifest.primary_metric,
        evaluation_criteria=extra_criteria,
        alpha=manifest.alpha,
        q=manifest.q,
        minimum_effect_size=manifest.minimum_effect_size,
        max_hypotheses_tests=manifest.max_hypotheses_tests,
        trading_friction=manifest.trading_friction,
        deterministic_seed=manifest.deterministic_seed,
        code_provenance=manifest.code_provenance,
        semantic_parameters=manifest.semantic_parameters,
        created_at=manifest.created_at,
        created_by=manifest.created_by,
    )
    # A result that only reports the primary metric (built directly, bypassing
    # build_result_v1's own coverage guard, to reach the wire-loadable state
    # this layer must still catch).
    observed = ObservedMetricsV1(primary_metric="mean_value", values={"mean_value": 3.0})
    decision = CriteriaDecisionV1(
        schema_version="experiment-execution-criteria-decision-v1",
        mode=CriteriaMode.ALL,
        passed=True,
        primary_metric="mean_value",
        primary_metric_value=3.0,
        evaluations=(
            CriterionEvaluation(
                metric="mean_value",
                operator=CriterionOperator.GREATER_THAN_OR_EQUAL,
                threshold=-1.0,
                observed=3.0,
                passed=True,
            ),
            CriterionEvaluation(
                metric="win_rate",
                operator=CriterionOperator.GREATER_THAN_OR_EQUAL,
                threshold=0.5,
                observed=0.9,
                passed=True,
            ),
        ),
    )
    result = ExperimentResultV1(
        schema_version="experiment-result-v1",
        result_kind="EXPERIMENT_RESULT_V1",
        execution_identity="deadbeef",
        execution_phase=ExecutionPhase.DISCOVERY,
        hypothesis_id=manifest.hypothesis_id,
        hypothesis_family_id=manifest.hypothesis_family_id,
        hypothesis_content_hash=manifest.bound_hypothesis_content_hash,
        manifest_semantic_hash=manifest.manifest_semantic_hash,
        manifest_artifact_hash_ref=manifest_ref,
        evaluator_id=EVALUATOR_ID,
        evaluator_version=EVALUATOR_VERSION,
        dataset_artifact_hash_refs=tuple(d.artifact_hash_ref for d in manifest.datasets),
        deterministic_seed=manifest.deterministic_seed,
        code_provenance=manifest.code_provenance,
        friction_absent=True,
        friction_model_id=None,
        friction_unit=None,
        observed_metrics=observed,
        criteria_decision=decision,
        diagnostics=ExperimentResultDiagnostics(created_at=CREATED_AT, created_by=CREATED_BY),
    )
    with pytest.raises(ExperimentExecutionContractError, match="missing"):
        build_evidence_v1(
            manifest=manifest,
            manifest_artifact_hash_ref=manifest_ref,
            result=result,
            result_artifact_hash_ref=RESULT_REF,
            created_at=CREATED_AT,
            created_by=CREATED_BY,
        )


def test_conflicting_evidence_rejected(tmp_path: Path) -> None:
    registry, store, manifest, manifest_ref = _case(tmp_path, threshold=-1.0)
    # A self-consistent-but-untruthful result: observed passes the criterion
    # (3.0 >= -1.0) yet the embedded decision falsely claims failure.
    observed = ObservedMetricsV1(primary_metric="mean_value", values={"mean_value": 3.0})
    lying_decision = CriteriaDecisionV1(
        schema_version="experiment-execution-criteria-decision-v1",
        mode=CriteriaMode.ALL,
        passed=False,
        primary_metric="mean_value",
        primary_metric_value=3.0,
        evaluations=(
            CriterionEvaluation(
                metric="mean_value",
                operator=CriterionOperator.GREATER_THAN_OR_EQUAL,
                threshold=-1.0,
                observed=3.0,
                passed=False,
            ),
        ),
    )
    result = ExperimentResultV1(
        schema_version="experiment-result-v1",
        result_kind="EXPERIMENT_RESULT_V1",
        execution_identity="deadbeef",
        execution_phase=ExecutionPhase.DISCOVERY,
        hypothesis_id=manifest.hypothesis_id,
        hypothesis_family_id=manifest.hypothesis_family_id,
        hypothesis_content_hash=manifest.bound_hypothesis_content_hash,
        manifest_semantic_hash=manifest.manifest_semantic_hash,
        manifest_artifact_hash_ref=manifest_ref,
        evaluator_id=EVALUATOR_ID,
        evaluator_version=EVALUATOR_VERSION,
        dataset_artifact_hash_refs=tuple(d.artifact_hash_ref for d in manifest.datasets),
        deterministic_seed=manifest.deterministic_seed,
        code_provenance=manifest.code_provenance,
        friction_absent=True,
        friction_model_id=None,
        friction_unit=None,
        observed_metrics=observed,
        criteria_decision=lying_decision,
        diagnostics=ExperimentResultDiagnostics(created_at=CREATED_AT, created_by=CREATED_BY),
    )
    with pytest.raises(ExperimentEvidenceConflictError):
        build_evidence_v1(
            manifest=manifest,
            manifest_artifact_hash_ref=manifest_ref,
            result=result,
            result_artifact_hash_ref=RESULT_REF,
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

    execution_a = _execute(registry, store, hypothesis_id=HYPOTHESIS_ID)
    builder = _builder(registry, store)
    # Deliberately associate hypothesis B with hypothesis A's execution result.
    with pytest.raises(ExperimentExecutionContractError, match="manifest"):
        builder.build(
            OTHER_HYPOTHESIS_ID,
            execution=execution_a,
            created_at=CREATED_AT,
            created_by=CREATED_BY,
        )


# ---------------------------------------------------------------------------
# Corrupt / missing CAS
# ---------------------------------------------------------------------------


def test_corrupt_manifest_cas_fails_closed(tmp_path: Path) -> None:
    registry, store, manifest, manifest_ref = _case(tmp_path)
    execution = _execute(registry, store)
    artifact = store.resolve_verified(manifest_ref)
    Path(artifact.path).write_bytes(b"corrupted manifest bytes")
    with pytest.raises(ArtifactIntegrityError):
        _builder(registry, store).build(
            HYPOTHESIS_ID, execution=execution, created_at=CREATED_AT, created_by=CREATED_BY
        )


def test_missing_result_cas_fails_closed(tmp_path: Path) -> None:
    registry, store, manifest, manifest_ref = _case(tmp_path)
    execution = _execute(registry, store)
    artifact = store.resolve_verified(execution.result_artifact.hash_ref)
    Path(artifact.path).unlink()
    with pytest.raises(ArtifactNotFoundError):
        _builder(registry, store).build(
            HYPOTHESIS_ID, execution=execution, created_at=CREATED_AT, created_by=CREATED_BY
        )


def test_corrupt_evidence_cas_fails_closed_on_reload(tmp_path: Path) -> None:
    registry, store, manifest, manifest_ref = _case(tmp_path)
    execution = _execute(registry, store)
    builder = _builder(registry, store)
    evidence, artifact = builder.build(
        HYPOTHESIS_ID, execution=execution, created_at=CREATED_AT, created_by=CREATED_BY
    )
    resolved = store.resolve_verified(artifact.hash_ref)
    Path(resolved.path).write_bytes(b"corrupted evidence bytes")
    with pytest.raises(ArtifactIntegrityError):
        builder.load(
            HYPOTHESIS_ID,
            evidence_artifact_hash_ref=artifact.hash_ref,
            result_artifact_hash_ref=execution.result_artifact.hash_ref,
            manifest_artifact_hash_ref=manifest_ref,
        )


def test_missing_evidence_cas_fails_closed_on_reload(tmp_path: Path) -> None:
    registry, store, manifest, manifest_ref = _case(tmp_path)
    execution = _execute(registry, store)
    builder = _builder(registry, store)
    evidence, artifact = builder.build(
        HYPOTHESIS_ID, execution=execution, created_at=CREATED_AT, created_by=CREATED_BY
    )
    resolved = store.resolve_verified(artifact.hash_ref)
    Path(resolved.path).unlink()
    with pytest.raises(ArtifactNotFoundError):
        builder.load(
            HYPOTHESIS_ID,
            evidence_artifact_hash_ref=artifact.hash_ref,
            result_artifact_hash_ref=execution.result_artifact.hash_ref,
            manifest_artifact_hash_ref=manifest_ref,
        )


# ---------------------------------------------------------------------------
# Holdout leakage rejection
# ---------------------------------------------------------------------------


def test_holdout_phase_structurally_impossible_in_evidence() -> None:
    for member in ExecutionPhase:
        assert member.value not in ("HOLDOUT", "FINAL_HOLDOUT")
    with pytest.raises(ExperimentExecutionContractError):
        ExecutionPhase.from_value("FINAL_HOLDOUT")


def test_evidence_never_contains_holdout_fields_or_conclusions(tmp_path: Path) -> None:
    import json

    registry, store, manifest, manifest_ref = _case(tmp_path)
    execution = _execute(registry, store)
    evidence, _ = _builder(registry, store).build(
        HYPOTHESIS_ID, execution=execution, created_at=CREATED_AT, created_by=CREATED_BY
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

    assert not has_holdout(json.loads(evidence.canonical_bytes()))
    assert evidence.semantic_projection()["evidence_semantic_markers"]["final_holdout_consumed"] is False


def test_evidence_never_claims_validation_or_trading_authority(tmp_path: Path) -> None:
    registry, store, manifest, manifest_ref = _case(tmp_path)
    execution = _execute(registry, store)
    evidence, _ = _builder(registry, store).build(
        HYPOTHESIS_ID, execution=execution, created_at=CREATED_AT, created_by=CREATED_BY
    )
    markers = evidence.semantic_projection()["evidence_semantic_markers"]
    assert markers["scientifically_validated"] is False
    assert markers["trading_authorized"] is False
    assert markers["ad_hoc_criteria_used"] is False


# ---------------------------------------------------------------------------
# Deterministic retry / reload
# ---------------------------------------------------------------------------


def test_deterministic_retry_is_idempotent(tmp_path: Path) -> None:
    registry, store, manifest, manifest_ref = _case(tmp_path)
    execution = _execute(registry, store)
    builder = _builder(registry, store)
    first, artifact1 = builder.build(
        HYPOTHESIS_ID, execution=execution, created_at=CREATED_AT, created_by=CREATED_BY
    )
    second, artifact2 = builder.build(
        HYPOTHESIS_ID, execution=execution, created_at=CREATED_AT, created_by=CREATED_BY
    )
    assert first == second
    assert first.evidence_semantic_identity == second.evidence_semantic_identity
    assert artifact1.hash_ref == artifact2.hash_ref


def test_restart_reload_reproduces_identical_evidence(tmp_path: Path) -> None:
    registry_path = tmp_path / "registry.db"
    store_root = tmp_path / "artifacts"
    registry = HypothesisRegistry(registry_path)
    _register(registry)
    store = ArtifactStore(store_root)
    manifest = _manifest(store, registry)
    manifest_ref = _freeze(registry, store, manifest)
    execution = _execute(registry, store)
    evidence, artifact = _builder(registry, store).build(
        HYPOTHESIS_ID, execution=execution, created_at=CREATED_AT, created_by=CREATED_BY
    )

    restarted_registry = HypothesisRegistry(registry_path)
    restarted_store = ArtifactStore(store_root)
    reloaded = _builder(restarted_registry, restarted_store).load(
        HYPOTHESIS_ID,
        evidence_artifact_hash_ref=artifact.hash_ref,
        result_artifact_hash_ref=execution.result_artifact.hash_ref,
        manifest_artifact_hash_ref=manifest_ref,
    )
    assert reloaded == evidence


def test_persist_load_verified_cas_round_trip(tmp_path: Path) -> None:
    registry, store, manifest, manifest_ref = _case(tmp_path)
    execution = _execute(registry, store)
    result = load_result_v1(
        execution.result_artifact.hash_ref,
        artifact_store=store,
        manifest=manifest,
        manifest_artifact_hash_ref=manifest_ref,
        registry=_evaluator_registry(),
    )
    evidence = build_evidence_v1(
        manifest=manifest,
        manifest_artifact_hash_ref=manifest_ref,
        result=result,
        result_artifact_hash_ref=execution.result_artifact.hash_ref,
        created_at=CREATED_AT,
        created_by=CREATED_BY,
    )
    artifact = persist_evidence_v1(evidence, artifact_store=store)
    loaded = load_evidence_v1(
        artifact.hash_ref,
        artifact_store=store,
        manifest=manifest,
        manifest_artifact_hash_ref=manifest_ref,
        result=result,
        result_artifact_hash_ref=execution.result_artifact.hash_ref,
    )
    assert loaded == evidence


# ---------------------------------------------------------------------------
# Architectural boundaries
# ---------------------------------------------------------------------------


def test_module_never_calls_provider_network_or_broker() -> None:
    import ast
    import inspect

    import trademind.experiment_evidence as module

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

    import trademind.experiment_evidence as module

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


def test_evidence_semantic_markers_are_module_constant_not_a_field(tmp_path: Path) -> None:
    # Mirrors the closed Result's own pattern: markers are always the fixed
    # constant in the wire payload, never threaded into a settable field.
    registry, store, manifest, manifest_ref = _case(tmp_path)
    execution = _execute(registry, store)
    evidence, _ = _builder(registry, store).build(
        HYPOTHESIS_ID, execution=execution, created_at=CREATED_AT, created_by=CREATED_BY
    )
    assert not hasattr(evidence, "evidence_semantic_markers")
    assert evidence.to_payload()["evidence_semantic_markers"] == EVIDENCE_SEMANTIC_MARKERS
