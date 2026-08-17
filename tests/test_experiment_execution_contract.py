from __future__ import annotations

import io
import json
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
from trademind.experiment_execution_contract import (
    EvaluatorBinding,
    EvaluatorRegistry,
    ExecutionPhase,
    ExperimentExecutionContractError,
    ObservedMetricsV1,
    build_result_v1,
    evaluate_criteria,
    load_result_v1,
    persist_result_v1,
    verify_experiment_result_v1,
)
from trademind.orchestrator.artifact_store import (
    ArtifactIntegrityError,
    ArtifactMediaTypeError,
    ArtifactStore,
)
from trademind.signal_statistics_provenance import (
    CodeProvenance,
    canonical_json_bytes,
)

FAMILY = "chronological-signal-evaluation-v1"
EVALUATOR_ID = "fake-signal-eval-v1"
EVALUATOR_VERSION = "1"
RESULT_REF = f"sha256:{'a' * 64}"
PACKET_REF = f"sha256:{'b' * 64}"
PACKET_SEMANTIC_HASH = f"sha256:{'c' * 64}"
REQUEST_HASH = "d" * 64
CANDIDATE_ID = f"ssc-v2-{'e' * 64}"
HYPOTHESIS_ID = f"rpi-v1:{RESULT_REF}:0"
CREATED_AT = "2026-08-14T10:00:00+00:00"


def _family() -> dict[str, object]:
    return {"candidate_id": CANDIDATE_ID, "claim": "effect remains positive"}


def _content() -> dict[str, object]:
    return {
        "family_definition": _family(),
        "proposal": {"falsifiable_claim": "effect remains positive"},
        "provenance": {"intake_id": HYPOTHESIS_ID},
    }


def _code_provenance() -> CodeProvenance:
    return CodeProvenance(
        producer_name="trademind",
        producer_version="1.31.1",
        git_commit="f" * 40,
        revision_source="git_worktree",
    )


def _split(*, start_hour: int = 0) -> SplitPlan:
    start = datetime(2026, 1, 1, start_hour, tzinfo=timezone.utc)
    return chronological_split([start + timedelta(hours=index) for index in range(12)])


def _criterion(
    metric: str, operator: CriterionOperator, threshold: int | float
) -> EvaluationCriterionV1:
    return EvaluationCriterionV1(metric=metric, operator=operator, threshold=threshold)


def _criteria(
    *,
    mode: CriteriaMode = CriteriaMode.ALL,
    metrics: tuple[str, ...] = ("mean_net_r",),
    threshold: int | float = 0.05,
) -> EvaluationCriteriaV1:
    if mode is CriteriaMode.ANY and len(metrics) > 1:
        criteria = tuple(
            _criterion(m, CriterionOperator.GREATER_THAN_OR_EQUAL, threshold) for m in metrics
        )
    else:
        criteria = tuple(
            _criterion(m, CriterionOperator.GREATER_THAN_OR_EQUAL, threshold) for m in metrics
        )
    return EvaluationCriteriaV1(mode=mode, criteria=criteria)


def _friction(*, spread: float = 1.0) -> TradingFrictionV1:
    return TradingFrictionV1(
        model_id="fixed-bps-v1",
        unit="bps-per-round-trip",
        spread=spread,
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


def _dataset(store: ArtifactStore, payload: bytes = b"timestamp,value\n2026-01-01,1\n"):
    artifact = store.import_snapshot(io.BytesIO(payload), media_type="text/csv")
    return DatasetArtifactV2(
        role="market-data",
        artifact_hash_ref=artifact.hash_ref,
        media_type=artifact.media_type,
        size_bytes=artifact.size_bytes,
    )


_UNSET_FRICTION = object()


def _manifest(
    store: ArtifactStore,
    registry: HypothesisRegistry,
    *,
    created_at: str = CREATED_AT,
    created_by: str = "operator:test",
    split_plan: SplitPlan | None = None,
    criteria: EvaluationCriteriaV1 | None = None,
    friction: TradingFrictionV1 | None | object = _UNSET_FRICTION,
    seed: int | None = 7,
    primary_metric: str = "mean_net_r",
) -> ExperimentManifestV2:
    record = registry.get(HYPOTHESIS_ID)
    result_ref = HYPOTHESIS_ID.removeprefix("rpi-v1:").rsplit(":", 1)[0]
    manifest = build_experiment_manifest_v2(
        artifact_store=store,
        hypothesis_id=HYPOTHESIS_ID,
        hypothesis_family_id=record.hypothesis_family_id,
        bound_hypothesis_content_hash=record.content_hash,
        proposal_provenance=ProposalIntakeProvenanceV1(
            intake_id=HYPOTHESIS_ID,
            execution_request_hash=REQUEST_HASH,
            authorization_id=1,
            task_id="signal-stats-v2-task",
            task_revision=1,
            packet_artifact_hash_ref=PACKET_REF,
            packet_semantic_hash=PACKET_SEMANTIC_HASH,
            result_artifact_hash_ref=result_ref,
            proposal_index=0,
            candidate_id=CANDIDATE_ID,
        ),
        datasets=(_dataset(store),),
        split_plan=split_plan or _split(),
        split_dataset_role="market-data",
        test_family=FAMILY,
        primary_metric=primary_metric,
        evaluation_criteria=criteria or _criteria(metrics=(primary_metric,)),
        alpha=0.05,
        q=0.10,
        minimum_effect_size=0.05,
        max_hypotheses_tests=20,
        trading_friction=_friction() if friction is _UNSET_FRICTION else friction,
        deterministic_seed=seed,
        code_provenance=_code_provenance(),
        semantic_parameters={"horizon": 12, "target": "forward_net_r"},
        created_at=created_at,
        created_by=created_by,
    )
    return manifest


def _binding(
    *,
    version: str = EVALUATOR_VERSION,
    metrics: tuple[str, ...] = ("mean_net_r", "win_rate"),
    friction: tuple[tuple[str, str], ...] = (("fixed-bps-v1", "bps-per-round-trip"),),
) -> EvaluatorBinding:
    return EvaluatorBinding(
        test_family=FAMILY,
        evaluator_id=EVALUATOR_ID,
        evaluator_version=version,
        supported_metrics=metrics,
        supported_friction_models=friction,
        deterministic=True,
    )


def _registry(**kwargs) -> EvaluatorRegistry:
    return EvaluatorRegistry({FAMILY: _binding(**kwargs)})


def _observed(
    *,
    primary: str = "mean_net_r",
    values: dict[str, object] | None = None,
) -> ObservedMetricsV1:
    vals = values if values is not None else {"mean_net_r": 0.12, "win_rate": 0.7}
    return ObservedMetricsV1(primary_metric=primary, values=vals)


def _persist(store: ArtifactStore, manifest: ExperimentManifestV2) -> str:
    return persist_experiment_manifest_v2(manifest, artifact_store=store).hash_ref


def _case(tmp_path: Path, **manifest_kwargs):
    registry = HypothesisRegistry(tmp_path / "registry.db")
    _register(registry)
    store = ArtifactStore(tmp_path / "artifacts")
    manifest = _manifest(store, registry, **manifest_kwargs)
    manifest_ref = _persist(store, manifest)
    return registry, store, manifest, manifest_ref


def _friction_probe() -> tuple[str, str]:
    return ("fixed-bps-v1", "bps-per-round-trip")


def _build_result(
    manifest: ExperimentManifestV2,
    manifest_ref: str,
    *,
    registry: EvaluatorRegistry | None = None,
    phase: ExecutionPhase = ExecutionPhase.DISCOVERY,
    version: str = EVALUATOR_VERSION,
    observed: ObservedMetricsV1 | None = None,
    evaluator_friction: tuple[str, str] | None = _friction_probe(),
    created_at: str = CREATED_AT,
    created_by: str = "operator:test",
    provenance: CodeProvenance | None = None,
):
    return build_result_v1(
        manifest=manifest,
        manifest_artifact_hash_ref=manifest_ref,
        execution_phase=phase,
        registry=registry or _registry(version=version),
        evaluator_id=EVALUATOR_ID,
        evaluator_version=version,
        execution_code_provenance=provenance or _code_provenance(),
        observed=observed or _observed(primary=manifest.primary_metric),
        evaluator_friction=evaluator_friction,
        created_at=created_at,
        created_by=created_by,
    )


def test_discovery_result_happy_path(tmp_path: Path) -> None:
    _, store, manifest, ref = _case(tmp_path)
    result = _build_result(manifest, ref)
    assert result.execution_phase is ExecutionPhase.DISCOVERY
    assert result.criteria_decision.passed is True
    assert result.friction_absent is False
    assert result.friction_model_id == "fixed-bps-v1"
    assert result.semantic_projection()["semantic_markers"]["trading_authorized"] is False
    assert result.semantic_projection()["semantic_markers"]["scientifically_validated"] is False
    # Round trip through the wire contract.
    assert verify_experiment_result_v1(result.canonical_bytes()) == result

    # Persist + load round trip.
    artifact = persist_result_v1(result, artifact_store=store)
    loaded = load_result_v1(
        artifact.hash_ref,
        artifact_store=store,
        manifest=manifest,
        manifest_artifact_hash_ref=ref,
        registry=_registry(),
    )
    assert loaded == result


def test_validation_result_happy_path(tmp_path: Path) -> None:
    _, store, manifest, ref = _case(tmp_path)
    result = _build_result(manifest, ref, phase=ExecutionPhase.VALIDATION)
    assert result.execution_phase is ExecutionPhase.VALIDATION
    assert result.criteria_decision.passed is True


def test_holdout_phase_is_impossible() -> None:
    for member in ExecutionPhase:
        assert member.value not in ("HOLDOUT", "FINAL_HOLDOUT")
    assert "HOLDOUT" not in {str(m) for m in ExecutionPhase}
    with pytest.raises(ExperimentExecutionContractError):
        ExecutionPhase.from_value("FINAL_HOLDOUT")


def test_unknown_test_family_fails_closed(tmp_path: Path) -> None:
    _, store, manifest, ref = _case(tmp_path)
    registry = EvaluatorRegistry({"other-family-v1": _binding()})
    with pytest.raises(ExperimentExecutionContractError, match="unknown"):
        build_result_v1(
            manifest=manifest,
            manifest_artifact_hash_ref=ref,
            execution_phase=ExecutionPhase.DISCOVERY,
            registry=registry,
            evaluator_id=EVALUATOR_ID,
            evaluator_version=EVALUATOR_VERSION,
            execution_code_provenance=_code_provenance(),
            observed=_observed(),
            evaluator_friction=_friction_probe(),
            created_at=CREATED_AT,
            created_by="operator:test",
        )


def test_unknown_evaluator_version_rejected(tmp_path: Path) -> None:
    _, store, manifest, ref = _case(tmp_path)
    with pytest.raises(ExperimentExecutionContractError, match="version"):
        build_result_v1(
            manifest=manifest,
            manifest_artifact_hash_ref=ref,
            execution_phase=ExecutionPhase.DISCOVERY,
            registry=_registry(version="1"),
            evaluator_id=EVALUATOR_ID,
            evaluator_version="999",
            execution_code_provenance=_code_provenance(),
            observed=_observed(),
            evaluator_friction=_friction_probe(),
            created_at=CREATED_AT,
            created_by="operator:test",
        )


def test_unsupported_metric_rejected(tmp_path: Path) -> None:
    _, store, manifest, ref = _case(tmp_path)
    observed = _observed(values={"mean_net_r": 0.12, "win_rate": 0.7, "out_of_vocab": 1.0})
    with pytest.raises(ExperimentExecutionContractError, match="vocabulary"):
        _build_result(manifest, ref, observed=observed)


def test_missing_primary_metric_rejected() -> None:
    with pytest.raises(ExperimentExecutionContractError, match="primary metric"):
        ObservedMetricsV1(primary_metric="mean_net_r", values={"win_rate": 0.7})


def test_missing_criterion_metric_rejected(tmp_path: Path) -> None:
    _, store, manifest, ref = _case(
        tmp_path, criteria=_criteria(metrics=("mean_net_r", "win_rate"))
    )
    observed = _observed(values={"mean_net_r": 0.12})
    with pytest.raises(ExperimentExecutionContractError, match="missing"):
        _build_result(manifest, ref, observed=observed)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf"), True])
def test_non_finite_and_bool_numeric_rejected(bad: object) -> None:
    with pytest.raises(ExperimentExecutionContractError):
        ObservedMetricsV1(primary_metric="mean_net_r", values={"mean_net_r": bad})


def _mixed_scale_criteria(mode: CriteriaMode) -> EvaluationCriteriaV1:
    # mean_net_r and win_rate live on different natural scales, so each needs its
    # own threshold to exercise a genuine mixed pass/fail scenario; a single
    # uniform threshold (as produced by `_criteria`) cannot express that.
    return EvaluationCriteriaV1(
        mode=mode,
        criteria=(
            _criterion("mean_net_r", CriterionOperator.GREATER_THAN_OR_EQUAL, 0.05),
            _criterion("win_rate", CriterionOperator.GREATER_THAN_OR_EQUAL, 0.5),
        ),
    )


def test_all_criteria_semantics(tmp_path: Path) -> None:
    criteria = _mixed_scale_criteria(CriteriaMode.ALL)
    fail = _observed(values={"mean_net_r": 0.12, "win_rate": 0.4})
    assert evaluate_criteria(criteria, fail).passed is False
    ok = _observed(values={"mean_net_r": 0.12, "win_rate": 0.8})
    assert evaluate_criteria(criteria, ok).passed is True


def test_any_criteria_semantics(tmp_path: Path) -> None:
    criteria = _mixed_scale_criteria(CriteriaMode.ANY)
    one_ok = _observed(values={"mean_net_r": 0.12, "win_rate": 0.4})
    assert evaluate_criteria(criteria, one_ok).passed is True
    both_fail = _observed(values={"mean_net_r": 0.001, "win_rate": 0.4})
    assert evaluate_criteria(criteria, both_fail).passed is False


@pytest.mark.parametrize(
    ("op", "observed", "threshold", "expected"),
    [
        (CriterionOperator.GREATER_THAN_OR_EQUAL, 0.05, 0.05, True),
        (CriterionOperator.GREATER_THAN_OR_EQUAL, 0.04, 0.05, False),
        (CriterionOperator.GREATER_THAN, 0.06, 0.05, True),
        (CriterionOperator.GREATER_THAN, 0.05, 0.05, False),
        (CriterionOperator.LESS_THAN_OR_EQUAL, 0.05, 0.05, True),
        (CriterionOperator.LESS_THAN_OR_EQUAL, 0.06, 0.05, False),
        (CriterionOperator.LESS_THAN, 0.04, 0.05, True),
        (CriterionOperator.LESS_THAN, 0.05, 0.05, False),
    ],
)
def test_all_four_comparison_operators(
    op: CriterionOperator, observed: float, threshold: float, expected: bool
) -> None:
    criteria = EvaluationCriteriaV1(
        mode=CriteriaMode.ALL,
        criteria=(_criterion("mean_net_r", op, threshold),),
    )
    metrics = ObservedMetricsV1(primary_metric="mean_net_r", values={"mean_net_r": observed})
    assert evaluate_criteria(criteria, metrics).passed is expected


def test_friction_exact_binding_accepted(tmp_path: Path) -> None:
    _, store, manifest, ref = _case(tmp_path)
    result = _build_result(manifest, ref, evaluator_friction=("fixed-bps-v1", "bps-per-round-trip"))
    assert result.friction_absent is False
    assert result.friction_model_id == "fixed-bps-v1"
    assert result.friction_unit == "bps-per-round-trip"


def test_friction_mismatch_rejected(tmp_path: Path) -> None:
    _, store, manifest, ref = _case(tmp_path)
    with pytest.raises(ExperimentExecutionContractError, match="friction"):
        _build_result(manifest, ref, evaluator_friction=("other-model", "other-unit"))


def test_friction_silent_omission_rejected(tmp_path: Path) -> None:
    _, store, manifest, ref = _case(tmp_path)
    with pytest.raises(ExperimentExecutionContractError, match="friction"):
        _build_result(manifest, ref, evaluator_friction=None)


def test_explicit_no_friction_supported(tmp_path: Path) -> None:
    # No-friction manifest: evaluator must not be required to declare a friction
    # model, and the result must explicitly mark no-friction.
    registry = HypothesisRegistry(tmp_path / "missing.db")
    _register(registry)
    store = ArtifactStore(tmp_path / "nf-artifacts")
    manifest = _manifest(store, registry, friction=None)
    ref = _persist(store, manifest)
    # Evaluator declares no friction models at all.
    reg = _registry(friction=())
    result = _build_result(
        manifest,
        ref,
        registry=reg,
        evaluator_friction=None,
        observed=_observed(values={"mean_net_r": 0.12, "win_rate": 0.7}),
    )
    assert result.friction_absent is True
    assert result.friction_model_id is None
    assert result.friction_unit is None


def test_code_provenance_exact_match_accepted(tmp_path: Path) -> None:
    _, store, manifest, ref = _case(tmp_path)
    result = _build_result(manifest, ref, provenance=_code_provenance())
    assert result.code_provenance == manifest.code_provenance


def test_code_provenance_mismatch_rejected(tmp_path: Path) -> None:
    _, store, manifest, ref = _case(tmp_path)
    other = CodeProvenance(
        producer_name="trademind",
        producer_version="9.9.9",
        git_commit="0" * 40,
        revision_source="git_worktree",
    )
    with pytest.raises(ExperimentExecutionContractError, match="code provenance"):
        _build_result(manifest, ref, provenance=other)


def test_same_manifest_phase_evaluator_same_execution_identity(tmp_path: Path) -> None:
    _, store, manifest, ref = _case(tmp_path)
    a = _build_result(manifest, ref)
    b = _build_result(manifest, ref)
    assert a.execution_identity == b.execution_identity


def test_different_phase_different_execution_identity(tmp_path: Path) -> None:
    _, store, manifest, ref = _case(tmp_path)
    d = _build_result(manifest, ref, phase=ExecutionPhase.DISCOVERY)
    v = _build_result(manifest, ref, phase=ExecutionPhase.VALIDATION)
    assert d.execution_identity != v.execution_identity


def test_different_evaluator_version_different_execution_identity(tmp_path: Path) -> None:
    _, store, manifest, ref = _case(tmp_path)
    v1 = _build_result(manifest, ref, registry=_registry(version="1"), version="1")
    v2 = _build_result(manifest, ref, registry=_registry(version="2"), version="2")
    assert v1.execution_identity != v2.execution_identity


def test_different_manifest_semantic_identity_different_execution_identity(tmp_path: Path) -> None:
    _, store_a, manifest_a, ref_a = _case(tmp_path, seed=7)
    _ = store_a
    registry_b = HypothesisRegistry(tmp_path / "b.db")
    _register(registry_b)
    store_b = ArtifactStore(tmp_path / "artifacts-b")
    manifest_b = _manifest(store_b, registry_b, seed=8)
    ref_b = _persist(store_b, manifest_b)
    assert manifest_a.manifest_semantic_hash != manifest_b.manifest_semantic_hash
    a = _build_result(manifest_a, ref_a)
    b = _build_result(manifest_b, ref_b)
    assert a.execution_identity != b.execution_identity


def test_semantic_result_hash_independent_of_diagnostics_timestamp(tmp_path: Path) -> None:
    _, store, manifest, ref = _case(tmp_path)
    a = _build_result(manifest, ref, created_at=CREATED_AT, created_by="operator:test")
    b = _build_result(
        manifest,
        ref,
        created_at="2027-01-01T00:00:00+00:00",
        created_by="operator:different",
    )
    assert a.execution_identity == b.execution_identity
    assert a.result_semantic_identity == b.result_semantic_identity
    assert a.canonical_bytes() != b.canonical_bytes()


def test_exact_cas_identity_changes_when_diagnostics_change(tmp_path: Path) -> None:
    _, store, manifest, ref = _case(tmp_path)
    a = _build_result(manifest, ref, created_at=CREATED_AT, created_by="operator:test")
    b = _build_result(
        manifest,
        ref,
        created_at="2027-01-01T00:00:00+00:00",
        created_by="operator:different",
    )
    ref_a = persist_result_v1(a, artifact_store=store)
    ref_b = persist_result_v1(b, artifact_store=store)
    assert ref_a.hash_ref != ref_b.hash_ref


def test_persist_load_verified_cas_round_trip(tmp_path: Path) -> None:
    registry, store, manifest, ref = _case(tmp_path)
    result = _build_result(manifest, ref)
    artifact = persist_result_v1(result, artifact_store=store)
    loaded = load_result_v1(
        artifact.hash_ref,
        artifact_store=store,
        manifest=manifest,
        manifest_artifact_hash_ref=ref,
        registry=_registry(),
    )
    assert loaded == result
    assert loaded.canonical_bytes() == result.canonical_bytes()


def test_wrong_media_type_rejected(tmp_path: Path) -> None:
    registry, store, manifest, ref = _case(tmp_path)
    result = _build_result(manifest, ref)
    wrong = store.import_snapshot(io.BytesIO(result.canonical_bytes()), media_type="text/plain")
    with pytest.raises(ArtifactMediaTypeError):
        load_result_v1(
            wrong.hash_ref,
            artifact_store=store,
            manifest=manifest,
            manifest_artifact_hash_ref=ref,
            registry=_registry(),
        )


def test_corrupt_cas_rejected(tmp_path: Path) -> None:
    registry, store, manifest, ref = _case(tmp_path)
    result = persist_result_v1(_build_result(manifest, ref), artifact_store=store)
    Path(result.path).write_bytes(b"corrupted result bytes")
    with pytest.raises(ArtifactIntegrityError):
        load_result_v1(
            result.hash_ref,
            artifact_store=store,
            manifest=manifest,
            manifest_artifact_hash_ref=ref,
            registry=_registry(),
        )


def test_manifest_cas_mismatch_rejected(tmp_path: Path) -> None:
    registry, store, manifest, ref = _case(tmp_path)
    artifact = persist_result_v1(_build_result(manifest, ref), artifact_store=store)
    with pytest.raises(ExperimentExecutionContractError, match="manifest"):
        load_result_v1(
            artifact.hash_ref,
            artifact_store=store,
            manifest=manifest,
            manifest_artifact_hash_ref=f"sha256:{'9' * 64}",
            registry=_registry(),
        )


def test_dataset_cas_corruption_rejected(tmp_path: Path) -> None:
    registry, store, manifest, ref = _case(tmp_path)
    artifact = persist_result_v1(_build_result(manifest, ref), artifact_store=store)
    dataset = store.resolve_verified(manifest.datasets[0].artifact_hash_ref)
    Path(dataset.path).write_bytes(b"corrupted dataset bytes")
    with pytest.raises(ArtifactIntegrityError):
        load_result_v1(
            artifact.hash_ref,
            artifact_store=store,
            manifest=manifest,
            manifest_artifact_hash_ref=ref,
            registry=_registry(),
        )


def test_hypothesis_identity_mismatch_rejected(tmp_path: Path) -> None:
    registry, store, manifest, ref = _case(tmp_path)
    result = _build_result(manifest, ref)
    payload = json.loads(result.canonical_bytes())
    payload["hypothesis"]["hypothesis_id"] = f"rpi-v1:sha256:{'1' * 64}:0"
    tampered = canonical_json_bytes(payload)
    with pytest.raises(ExperimentExecutionContractError, match="semantic identity"):
        verify_experiment_result_v1(tampered)


def test_result_cannot_contain_holdout_observations_or_fields(tmp_path: Path) -> None:
    registry, store, manifest, ref = _case(tmp_path)
    result = _build_result(manifest, ref)

    # "final_holdout_consumed" is the one permitted holdout-related key: a fixed,
    # always-False semantic marker that proves the absence of holdout data, not
    # holdout data itself. Every other "holdout" key would indicate a leak.
    allowed_holdout_keys = {"final_holdout_consumed"}

    def has_holdout(value: object) -> bool:
        if isinstance(value, dict):
            return any(
                "holdout" in str(k).lower() and k not in allowed_holdout_keys for k in value
            ) or any(has_holdout(v) for v in value.values())
        if isinstance(value, list):
            return any(has_holdout(v) for v in value)
        return False

    assert not has_holdout(json.loads(result.canonical_bytes()))
    assert result.semantic_projection()["semantic_markers"]["final_holdout_consumed"] is False


def test_result_never_claims_trading_authorization(tmp_path: Path) -> None:
    registry, store, manifest, ref = _case(tmp_path)
    result = _build_result(manifest, ref)
    markers = result.semantic_projection()["semantic_markers"]
    assert markers["trading_authorized"] is False
    assert "orders" not in result.semantic_projection()


def test_module_never_calls_provider_network_or_broker() -> None:
    import ast
    import inspect

    import trademind.experiment_execution_contract as module

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
    }
    assert not (imported & forbidden), imported & forbidden


def test_no_hypothesis_registry_lifecycle_mutation(tmp_path: Path) -> None:
    import trademind.experiment_execution_contract as module

    registry, store, manifest, ref = _case(tmp_path)
    # register leaves the hypothesis PROPOSED (no freeze/transition here).
    result = _build_result(manifest, ref)
    persist_result_v1(result, artifact_store=store)
    assert registry.get(HYPOTHESIS_ID).state.value == "PROPOSED"
    # The contract exposes no lifecycle transition API and no lifecycle phases.
    assert not hasattr(module, "transition")
    lifecycle = {"FROZEN", "TRAIN_TESTED", "VALIDATION_PASSED", "VALIDATION_REJECTED"}
    assert not (lifecycle & {str(m) for m in ExecutionPhase})


def _mutate(result, mutator: object) -> bytes:
    payload = json.loads(result.canonical_bytes())
    mutated = mutator(payload)
    return canonical_json_bytes(mutated)


def test_unknown_field_rejected(tmp_path: Path) -> None:
    registry, store, manifest, ref = _case(tmp_path)
    result = _build_result(manifest, ref)
    with pytest.raises(ExperimentExecutionContractError, match="unknown fields"):
        verify_experiment_result_v1(_mutate(result, lambda p: {**p, "extra_field": 1}))


def test_duplicate_json_key_rejected(tmp_path: Path) -> None:
    registry, store, manifest, ref = _case(tmp_path)
    result = _build_result(manifest, ref)
    text = result.canonical_bytes().decode()
    first_quote = text.index('"')
    dup = '{"schema_version":"experiment-result-v1",' + text[first_quote:]
    with pytest.raises(ExperimentExecutionContractError, match="duplicate"):
        verify_experiment_result_v1(dup.encode())


def test_noncanonical_json_rejected(tmp_path: Path) -> None:
    registry, store, manifest, ref = _case(tmp_path)
    result = _build_result(manifest, ref)
    payload = json.loads(result.canonical_bytes())
    noncanonical = json.dumps(
        payload, sort_keys=True, separators=(", ", ": "), ensure_ascii=True
    ).encode()
    assert noncanonical != result.canonical_bytes()
    with pytest.raises(ExperimentExecutionContractError, match="canonical"):
        verify_experiment_result_v1(noncanonical)


def test_malformed_sha256_ref_rejected(tmp_path: Path) -> None:
    registry, store, manifest, ref = _case(tmp_path)
    result = _build_result(manifest, ref)
    with pytest.raises(ExperimentExecutionContractError):
        verify_experiment_result_v1(
            _mutate(
                result,
                lambda p: {**p, "manifest": {**p["manifest"], "artifact_hash_ref": "sha256:xyz"}},
            )
        )


def test_oversized_identifier_rejected(tmp_path: Path) -> None:
    registry, store, manifest, ref = _case(tmp_path)
    result = _build_result(manifest, ref)
    huge = "m" * 300
    with pytest.raises(ExperimentExecutionContractError, match="identifier"):
        verify_experiment_result_v1(
            _mutate(
                result,
                lambda p: {
                    **p,
                    "observed_metrics": {
                        **p["observed_metrics"],
                        "values": {**p["observed_metrics"]["values"], huge: 1.0},
                    },
                },
            )
        )


def test_wrong_exact_python_types_rejected(tmp_path: Path) -> None:
    registry, store, manifest, ref = _case(tmp_path)
    result = _build_result(manifest, ref)
    with pytest.raises(ExperimentExecutionContractError):
        verify_experiment_result_v1(_mutate(result, lambda p: {**p, "deterministic_seed": "7"}))
    with pytest.raises(ExperimentExecutionContractError):
        verify_experiment_result_v1(
            _mutate(result, lambda p: {**p, "execution_phase": "FINAL_HOLDOUT"})
        )


def test_restart_safe_load_from_file_backed_cas(tmp_path: Path) -> None:
    root = tmp_path / "cas"
    store1 = ArtifactStore(root)
    registry = HypothesisRegistry(tmp_path / "registry.db")
    _register(registry)
    manifest = _manifest(store1, registry)
    ref = _persist(store1, manifest)
    result = _build_result(manifest, ref)
    artifact = persist_result_v1(result, artifact_store=store1)
    # Simulate a restart by opening a fresh ArtifactStore over the same root.
    store2 = ArtifactStore(root)
    loaded = load_result_v1(
        artifact.hash_ref,
        artifact_store=store2,
        manifest=manifest,
        manifest_artifact_hash_ref=ref,
        registry=_registry(),
    )
    assert loaded == result
