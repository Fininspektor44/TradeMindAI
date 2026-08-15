from __future__ import annotations

import io
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from trademind.discovery.hypothesis_registry import HypothesisRegistry, HypothesisState
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
from trademind.experiment_execution_contract import (
    EvaluatorBinding,
    EvaluatorRegistry,
    ExecutionPhase,
    ExperimentExecutionContractError,
    load_result_v1,
)
from trademind.experiment_execution_runtime import (
    ExperimentExecutionRuntimeError,
    ExperimentExecutionRuntimeV1,
    HoldoutLeakageError,
    SplitViolationError,
    select_public_phase_rows,
)
from trademind.orchestrator.artifact_store import (
    ArtifactIntegrityError,
    ArtifactNotFoundError,
    ArtifactStore,
)
from trademind.signal_statistics_provenance import CodeProvenance

FAMILY = "runtime-eval-v1"
EVALUATOR_ID = "fake-runtime-eval-v1"
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


class MeanValueEvaluator:
    """Pure deterministic evaluator: mean of the CSV ``value`` column."""

    evaluator_id = EVALUATOR_ID
    evaluator_version = EVALUATOR_VERSION

    def evaluate(self, rows, *, manifest, execution_phase):
        del manifest, execution_phase
        values = [float(row.fields["value"]) for row in rows]
        return {"mean_value": sum(values) / len(values)}


class BoolMetricEvaluator:
    evaluator_id = EVALUATOR_ID
    evaluator_version = EVALUATOR_VERSION

    def evaluate(self, rows, *, manifest, execution_phase):
        del rows, manifest, execution_phase
        return {"mean_value": True}


class FlakyEvaluator:
    """Fails once, then succeeds; used to exercise fail-closed rollback."""

    evaluator_id = EVALUATOR_ID
    evaluator_version = EVALUATOR_VERSION

    def __init__(self) -> None:
        self.calls = 0

    def evaluate(self, rows, *, manifest, execution_phase):
        del manifest, execution_phase
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("simulated transient evaluator failure")
        values = [float(row.fields["value"]) for row in rows]
        return {"mean_value": sum(values) / len(values)}


def _family() -> dict[str, object]:
    return {"candidate_id": CANDIDATE_ID, "claim": "runtime effect remains positive"}


def _content() -> dict[str, object]:
    return {
        "family_definition": _family(),
        "proposal": {"falsifiable_claim": "runtime effect remains positive"},
        "provenance": {"intake_id": HYPOTHESIS_ID},
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


def _register(registry: HypothesisRegistry):
    return registry.register(
        hypothesis_id=HYPOTHESIS_ID,
        family_definition=_family(),
        content_definition=_content(),
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


def _dataset(
    store: ArtifactStore,
    plan: SplitPlan,
    *,
    rows: list[tuple[datetime, float]] | None = None,
    media_type: str = "text/csv",
) -> DatasetArtifactV2:
    payload = _csv_bytes(rows if rows is not None else _public_rows(plan))
    artifact = store.import_snapshot(io.BytesIO(payload), media_type=media_type)
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
    split_plan: SplitPlan | None = None,
    dataset: DatasetArtifactV2 | None = None,
    friction: TradingFrictionV1 | None = None,
    seed: int | None = 7,
    primary_metric: str = "mean_value",
) -> ExperimentManifestV2:
    record = registry.get(HYPOTHESIS_ID)
    plan = split_plan or _split()
    result_ref = HYPOTHESIS_ID.removeprefix("rpi-v1:").rsplit(":", 1)[0]
    ds = dataset if dataset is not None else _dataset(store, plan)
    return build_experiment_manifest_v2(
        artifact_store=store,
        hypothesis_id=HYPOTHESIS_ID,
        hypothesis_family_id=record.hypothesis_family_id,
        bound_hypothesis_content_hash=record.content_hash,
        proposal_provenance=ProposalIntakeProvenanceV1(
            intake_id=HYPOTHESIS_ID,
            execution_request_hash=REQUEST_HASH,
            authorization_id=1,
            task_id="runtime-task",
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
        evaluation_criteria=_criteria(primary=primary_metric),
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


def _runtime(
    registry: HypothesisRegistry,
    store: ArtifactStore,
    *,
    evaluator=None,
    evaluator_registry: EvaluatorRegistry | None = None,
) -> ExperimentExecutionRuntimeV1:
    return ExperimentExecutionRuntimeV1(
        registry=registry,
        artifact_store=store,
        evaluator_registry=evaluator_registry or _evaluator_registry(),
        evaluator=evaluator or MeanValueEvaluator(),
    )


def _case(tmp_path: Path, **manifest_kwargs):
    registry = HypothesisRegistry(tmp_path / "registry.db")
    _register(registry)
    store = ArtifactStore(tmp_path / "artifacts")
    manifest = _manifest(store, registry, **manifest_kwargs)
    manifest_ref = _freeze(registry, store, manifest)
    return registry, store, manifest, manifest_ref


def _execute(runtime, *, phase=ExecutionPhase.DISCOVERY, provenance=None, friction=None):
    return runtime.execute(
        HYPOTHESIS_ID,
        execution_phase=phase,
        execution_code_provenance=provenance or _code_provenance(),
        evaluator_friction=friction,
        created_at=CREATED_AT,
        created_by=CREATED_BY,
    )


def _cas_object_files(store: ArtifactStore) -> set[str]:
    objects_dir = store.root / ".verified-cas-v1" / "objects"
    if not objects_dir.exists():
        return set()
    return {str(path.relative_to(store.root)) for path in objects_dir.rglob("*") if path.is_file()}


# ---------------------------------------------------------------------------
# Success
# ---------------------------------------------------------------------------


def test_discovery_execution_happy_path(tmp_path: Path) -> None:
    registry, store, manifest, manifest_ref = _case(tmp_path)
    runtime = _runtime(registry, store)
    outcome = _execute(runtime, phase=ExecutionPhase.DISCOVERY)
    assert outcome.result.execution_phase is ExecutionPhase.DISCOVERY
    assert outcome.public_rows_used == manifest.split_plan.discovery_count
    assert outcome.manifest_artifact_hash_ref == manifest_ref
    assert outcome.result.criteria_decision.passed is True
    assert outcome.result.observed_metrics.values["mean_value"] == pytest.approx(3.0)
    reloaded = load_result_v1(
        outcome.result_artifact.hash_ref,
        artifact_store=store,
        manifest=manifest,
        manifest_artifact_hash_ref=manifest_ref,
        registry=_evaluator_registry(),
    )
    assert reloaded == outcome.result


def test_validation_execution_happy_path(tmp_path: Path) -> None:
    registry, store, manifest, manifest_ref = _case(tmp_path)
    runtime = _runtime(registry, store)
    outcome = _execute(runtime, phase=ExecutionPhase.VALIDATION)
    assert outcome.result.execution_phase is ExecutionPhase.VALIDATION
    assert outcome.public_rows_used == manifest.split_plan.validation_count
    assert outcome.result.observed_metrics.values["mean_value"] == pytest.approx(7.5)


def test_execution_never_advances_hypothesis_state(tmp_path: Path) -> None:
    registry, store, manifest, manifest_ref = _case(tmp_path)
    runtime = _runtime(registry, store)
    _execute(runtime, phase=ExecutionPhase.DISCOVERY)
    assert registry.get(HYPOTHESIS_ID).state is HypothesisState.FROZEN
    _execute(runtime, phase=ExecutionPhase.VALIDATION)
    assert registry.get(HYPOTHESIS_ID).state is HypothesisState.FROZEN


# ---------------------------------------------------------------------------
# Deterministic retry / idempotency
# ---------------------------------------------------------------------------


def test_deterministic_retry_is_idempotent(tmp_path: Path) -> None:
    registry, store, manifest, manifest_ref = _case(tmp_path)
    runtime = _runtime(registry, store)
    first = _execute(runtime, phase=ExecutionPhase.DISCOVERY)
    second = _execute(runtime, phase=ExecutionPhase.DISCOVERY)
    assert first.result == second.result
    assert first.result.execution_identity == second.result.execution_identity
    assert first.result_artifact.hash_ref == second.result_artifact.hash_ref
    assert first.public_rows_used == second.public_rows_used


def test_restart_reload_reproduces_identical_result(tmp_path: Path) -> None:
    registry_path = tmp_path / "registry.db"
    store_root = tmp_path / "artifacts"
    registry = HypothesisRegistry(registry_path)
    _register(registry)
    store = ArtifactStore(store_root)
    manifest = _manifest(store, registry)
    manifest_ref = _freeze(registry, store, manifest)

    first = _execute(_runtime(registry, store), phase=ExecutionPhase.DISCOVERY)

    # Simulate a process restart: fresh registry/store objects over the same paths.
    restarted_registry = HypothesisRegistry(registry_path)
    restarted_store = ArtifactStore(store_root)
    second = _execute(_runtime(restarted_registry, restarted_store), phase=ExecutionPhase.DISCOVERY)

    assert second.result == first.result
    assert second.result_artifact.hash_ref == first.result_artifact.hash_ref

    reloaded = load_result_v1(
        second.result_artifact.hash_ref,
        artifact_store=restarted_store,
        manifest=manifest,
        manifest_artifact_hash_ref=manifest_ref,
        registry=_evaluator_registry(),
    )
    assert reloaded == first.result


# ---------------------------------------------------------------------------
# Frozen-manifest authority / unknown hypothesis
# ---------------------------------------------------------------------------


def test_execution_requires_frozen_hypothesis(tmp_path: Path) -> None:
    registry = HypothesisRegistry(tmp_path / "registry.db")
    _register(registry)  # left PROPOSED; never frozen.
    store = ArtifactStore(tmp_path / "artifacts")
    runtime = _runtime(registry, store)
    with pytest.raises(ExperimentExecutionRuntimeError, match="FROZEN"):
        _execute(runtime)


def test_execution_unknown_hypothesis_fails_closed(tmp_path: Path) -> None:
    registry = HypothesisRegistry(tmp_path / "registry.db")
    store = ArtifactStore(tmp_path / "artifacts")
    runtime = _runtime(registry, store)
    with pytest.raises(ExperimentExecutionRuntimeError):
        _execute(runtime)


def test_evaluator_must_implement_protocol(tmp_path: Path) -> None:
    registry, store, manifest, manifest_ref = _case(tmp_path)
    with pytest.raises(TypeError):
        ExperimentExecutionRuntimeV1(
            registry=registry,
            artifact_store=store,
            evaluator_registry=_evaluator_registry(),
            evaluator=object(),
        )


# ---------------------------------------------------------------------------
# Provenance mismatch
# ---------------------------------------------------------------------------


def test_unknown_test_family_binding_fails_closed(tmp_path: Path) -> None:
    registry, store, manifest, manifest_ref = _case(tmp_path)
    other_registry = EvaluatorRegistry({"other-family-v1": _binding()})
    runtime = _runtime(registry, store, evaluator_registry=other_registry)
    with pytest.raises(ExperimentExecutionContractError, match="unknown"):
        _execute(runtime)


def test_code_provenance_mismatch_fails_closed(tmp_path: Path) -> None:
    registry, store, manifest, manifest_ref = _case(tmp_path)
    runtime = _runtime(registry, store)
    other = _code_provenance(git_commit="0" * 40)
    with pytest.raises(ExperimentExecutionContractError, match="code provenance"):
        _execute(runtime, provenance=other)


def test_friction_unsupported_by_evaluator_fails_closed(tmp_path: Path) -> None:
    registry, store, manifest, manifest_ref = _case(tmp_path, friction=_friction())
    runtime = _runtime(registry, store)  # default binding declares zero friction models.
    with pytest.raises(ExperimentExecutionContractError, match="friction"):
        _execute(runtime, friction=("fixed-bps-v1", "bps-per-round-trip"))


# ---------------------------------------------------------------------------
# Split violation / holdout leakage
# ---------------------------------------------------------------------------


def test_dataset_row_count_mismatch_is_split_violation(tmp_path: Path) -> None:
    plan = _split()
    registry = HypothesisRegistry(tmp_path / "registry.db")
    _register(registry)
    store = ArtifactStore(tmp_path / "artifacts")
    short_rows = _public_rows(plan)[:-1]  # one row short of the frozen public_count.
    dataset = _dataset(store, plan, rows=short_rows)
    manifest = _manifest(store, registry, split_plan=plan, dataset=dataset)
    _freeze(registry, store, manifest)
    runtime = _runtime(registry, store)
    with pytest.raises(SplitViolationError, match="row count"):
        _execute(runtime)


def test_dataset_boundary_mismatch_is_split_violation(tmp_path: Path) -> None:
    plan = _split()
    registry = HypothesisRegistry(tmp_path / "registry.db")
    _register(registry)
    store = ArtifactStore(tmp_path / "artifacts")
    # Same count, well inside the public window, but shifted off the frozen boundaries.
    shifted_start = datetime(2026, 1, 1, 0, 5, tzinfo=timezone.utc)
    shifted_rows = [
        (shifted_start + timedelta(minutes=10 * index), float(index))
        for index in range(plan.public_count)
    ]
    dataset = _dataset(store, plan, rows=shifted_rows)
    manifest = _manifest(store, registry, split_plan=plan, dataset=dataset)
    _freeze(registry, store, manifest)
    runtime = _runtime(registry, store)
    with pytest.raises(SplitViolationError, match="boundaries"):
        _execute(runtime)


def test_dataset_unsorted_rows_is_split_violation(tmp_path: Path) -> None:
    plan = _split()
    registry = HypothesisRegistry(tmp_path / "registry.db")
    _register(registry)
    store = ArtifactStore(tmp_path / "artifacts")
    rows = _public_rows(plan)
    rows[3], rows[4] = rows[4], rows[3]  # break chronological order.
    dataset = _dataset(store, plan, rows=rows)
    manifest = _manifest(store, registry, split_plan=plan, dataset=dataset)
    _freeze(registry, store, manifest)
    runtime = _runtime(registry, store)
    with pytest.raises(SplitViolationError, match="non-decreasing"):
        _execute(runtime)


def test_holdout_row_triggers_leakage_error(tmp_path: Path) -> None:
    plan = _split()
    registry = HypothesisRegistry(tmp_path / "registry.db")
    _register(registry)
    store = ArtifactStore(tmp_path / "artifacts")
    start = datetime.fromisoformat(plan.discovery_start)
    # Same public row count, but the last row is swapped for the first holdout hour.
    rows = [(start + timedelta(hours=index), float(index)) for index in range(plan.public_count - 1)]
    rows.append((datetime.fromisoformat(plan.holdout_start), 999.0))
    dataset = _dataset(store, plan, rows=rows)
    manifest = _manifest(store, registry, split_plan=plan, dataset=dataset)
    _freeze(registry, store, manifest)
    runtime = _runtime(registry, store)
    with pytest.raises(HoldoutLeakageError):
        _execute(runtime)


def test_non_csv_split_dataset_media_type_rejected(tmp_path: Path) -> None:
    plan = _split()
    registry = HypothesisRegistry(tmp_path / "registry.db")
    _register(registry)
    store = ArtifactStore(tmp_path / "artifacts")
    dataset = _dataset(store, plan, media_type="application/json")
    manifest = _manifest(store, registry, split_plan=plan, dataset=dataset)
    _freeze(registry, store, manifest)
    runtime = _runtime(registry, store)
    with pytest.raises(SplitViolationError, match="media_type"):
        _execute(runtime)


def test_select_public_phase_rows_rejects_wrong_types() -> None:
    plan = _split()
    with pytest.raises(ExperimentExecutionRuntimeError):
        select_public_phase_rows((), plan="not-a-plan", execution_phase=ExecutionPhase.DISCOVERY)
    with pytest.raises(ExperimentExecutionRuntimeError):
        select_public_phase_rows((), plan=plan, execution_phase="DISCOVERY")


# ---------------------------------------------------------------------------
# Corrupt / missing CAS
# ---------------------------------------------------------------------------


def test_corrupt_manifest_cas_fails_closed(tmp_path: Path) -> None:
    registry, store, manifest, manifest_ref = _case(tmp_path)
    artifact = store.resolve_verified(manifest_ref)
    Path(artifact.path).write_bytes(b"corrupted manifest bytes")
    runtime = _runtime(registry, store)
    with pytest.raises(ArtifactIntegrityError):
        _execute(runtime)


def test_missing_manifest_cas_fails_closed(tmp_path: Path) -> None:
    registry, store, manifest, manifest_ref = _case(tmp_path)
    artifact = store.resolve_verified(manifest_ref)
    Path(artifact.path).unlink()
    runtime = _runtime(registry, store)
    with pytest.raises(ArtifactNotFoundError):
        _execute(runtime)


def test_corrupt_dataset_cas_fails_closed(tmp_path: Path) -> None:
    registry, store, manifest, manifest_ref = _case(tmp_path)
    dataset = store.resolve_verified(manifest.datasets[0].artifact_hash_ref)
    Path(dataset.path).write_bytes(b"corrupted dataset bytes")
    runtime = _runtime(registry, store)
    with pytest.raises(ArtifactIntegrityError):
        _execute(runtime)


def test_missing_dataset_cas_fails_closed(tmp_path: Path) -> None:
    registry, store, manifest, manifest_ref = _case(tmp_path)
    dataset = store.resolve_verified(manifest.datasets[0].artifact_hash_ref)
    Path(dataset.path).unlink()
    runtime = _runtime(registry, store)
    with pytest.raises(ArtifactNotFoundError):
        _execute(runtime)


# ---------------------------------------------------------------------------
# Evaluator-side failures and fail-closed rollback
# ---------------------------------------------------------------------------


def test_evaluator_non_finite_or_bool_metric_rejected(tmp_path: Path) -> None:
    registry, store, manifest, manifest_ref = _case(tmp_path)
    runtime = _runtime(registry, store, evaluator=BoolMetricEvaluator())
    with pytest.raises(ExperimentExecutionRuntimeError, match="observed metrics"):
        _execute(runtime)


def test_evaluator_failure_rolls_back_no_partial_cas_and_retry_succeeds(tmp_path: Path) -> None:
    registry, store, manifest, manifest_ref = _case(tmp_path)
    evaluator = FlakyEvaluator()
    runtime = _runtime(registry, store, evaluator=evaluator)

    before = _cas_object_files(store)
    with pytest.raises(ExperimentExecutionRuntimeError, match="failed during execution"):
        _execute(runtime)
    after_failure = _cas_object_files(store)
    assert after_failure == before
    assert registry.get(HYPOTHESIS_ID).state is HypothesisState.FROZEN

    outcome = _execute(runtime)
    assert outcome.result.criteria_decision.passed is True
    after_success = _cas_object_files(store)
    assert after_success != before  # exactly the new result blob + metadata were added.
    assert after_success - before


# ---------------------------------------------------------------------------
# Architectural boundaries
# ---------------------------------------------------------------------------


def test_module_never_calls_provider_network_or_broker() -> None:
    import ast
    import inspect

    import trademind.experiment_execution_runtime as module

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

    import trademind.experiment_execution_runtime as module

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
