from __future__ import annotations

import io
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier

import pytest

from trademind.discovery.hypothesis_registry import (
    HypothesisRegistry,
    HypothesisState,
    RegistryError,
)
from trademind.discovery.manifest import (
    EXPERIMENT_MANIFEST_V2_MEDIA_TYPE,
    FINAL_HOLDOUT_CRITERIA_SCHEMA_VERSION,
    CriteriaMode,
    CriterionOperator,
    DatasetArtifactV2,
    EvaluationCriteriaV1,
    EvaluationCriterionV1,
    ExperimentManifestV2,
    FinalHoldoutCriteriaV1,
    ManifestV2ValidationError,
    ProposalIntakeProvenanceV1,
    TradingFrictionV1,
    build_experiment_manifest_v2,
    load_experiment_manifest_v2,
    persist_experiment_manifest_v2,
    verify_experiment_manifest_v2,
    verify_final_holdout_metric_vocabulary,
)
from trademind.discovery.split_engine import SplitPlan, chronological_split
from trademind.orchestrator.artifact_store import (
    ArtifactIntegrityError,
    ArtifactMediaTypeError,
    ArtifactNotFoundError,
    ArtifactStore,
)
from trademind.signal_statistics_provenance import CodeProvenance


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


def _provenance(
    *,
    intake_id: str = HYPOTHESIS_ID,
    result_ref: str = RESULT_REF,
) -> ProposalIntakeProvenanceV1:
    return ProposalIntakeProvenanceV1(
        intake_id=intake_id,
        execution_request_hash=REQUEST_HASH,
        authorization_id=1,
        task_id="signal-stats-v2-task",
        task_revision=1,
        packet_artifact_hash_ref=PACKET_REF,
        packet_semantic_hash=PACKET_SEMANTIC_HASH,
        result_artifact_hash_ref=result_ref,
        proposal_index=0,
        candidate_id=CANDIDATE_ID,
    )


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


def _criteria(
    *,
    metric: str = "mean_net_r",
    threshold: int | float = 0.05,
) -> EvaluationCriteriaV1:
    return EvaluationCriteriaV1(
        mode=CriteriaMode.ALL,
        criteria=(
            EvaluationCriterionV1(
                metric=metric,
                operator=CriterionOperator.GREATER_THAN_OR_EQUAL,
                threshold=threshold,
            ),
        ),
    )


def _final_holdout_criteria(
    *,
    metric: str = "holdout_rows",
    threshold: int | float = 1,
    operator: CriterionOperator = CriterionOperator.GREATER_THAN_OR_EQUAL,
    mode: CriteriaMode = CriteriaMode.ALL,
) -> FinalHoldoutCriteriaV1:
    return FinalHoldoutCriteriaV1(
        mode=mode,
        criteria=(EvaluationCriterionV1(metric=metric, operator=operator, threshold=threshold),),
    )


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


def _manifest(
    store: ArtifactStore,
    registry: HypothesisRegistry,
    *,
    created_at: str = CREATED_AT,
    created_by: str = "operator:test",
    dataset: DatasetArtifactV2 | None = None,
    split_plan: SplitPlan | None = None,
    primary_metric: str = "mean_net_r",
    criteria: EvaluationCriteriaV1 | None = None,
    friction: TradingFrictionV1 | None = None,
    seed: int | None = 7,
    parameters: dict[str, object] | None = None,
    hypothesis_id: str = HYPOTHESIS_ID,
    family_id: str | None = None,
    content_hash: str | None = None,
    final_holdout_criteria: FinalHoldoutCriteriaV1 | None = None,
) -> ExperimentManifestV2:
    record = registry.get(HYPOTHESIS_ID)
    result_ref = hypothesis_id.removeprefix("rpi-v1:").rsplit(":", 1)[0]
    return build_experiment_manifest_v2(
        artifact_store=store,
        hypothesis_id=hypothesis_id,
        hypothesis_family_id=family_id or record.hypothesis_family_id,
        bound_hypothesis_content_hash=content_hash or record.content_hash,
        proposal_provenance=_provenance(intake_id=hypothesis_id, result_ref=result_ref),
        datasets=(dataset or _dataset(store),),
        split_plan=split_plan or _split(),
        split_dataset_role="market-data",
        test_family="chronological-signal-evaluation-v1",
        primary_metric=primary_metric,
        evaluation_criteria=criteria or _criteria(metric=primary_metric),
        alpha=0.05,
        q=0.10,
        minimum_effect_size=0.05,
        max_hypotheses_tests=20,
        trading_friction=friction if friction is not None else _friction(),
        deterministic_seed=seed,
        code_provenance=_code_provenance(),
        semantic_parameters=parameters or {"horizon": 12, "target": "forward_net_r"},
        created_at=created_at,
        created_by=created_by,
        final_holdout_criteria=final_holdout_criteria,
    )


def _transaction(registry: HypothesisRegistry) -> sqlite3.Connection:
    db = sqlite3.connect(registry.path, timeout=30.0)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("BEGIN IMMEDIATE")
    return db


def test_v2_binds_registry_identity_without_recomputing_hypothesis_hash(tmp_path: Path) -> None:
    registry = HypothesisRegistry(tmp_path / "registry.db")
    record = _register(registry)
    store = ArtifactStore(tmp_path / "artifacts")

    manifest = _manifest(store, registry)

    assert manifest.hypothesis_id == record.hypothesis_id
    assert manifest.hypothesis_family_id == record.hypothesis_family_id
    assert manifest.bound_hypothesis_content_hash == record.content_hash
    assert manifest.semantic_projection()["hypothesis"]["content_hash"] == record.content_hash
    assert "family_definition" not in manifest.semantic_projection()["hypothesis"]


def test_proposal_lineage_identity_mismatch_fails_closed() -> None:
    with pytest.raises(ManifestV2ValidationError, match="result artifact"):
        ProposalIntakeProvenanceV1(
            intake_id=HYPOTHESIS_ID,
            execution_request_hash=REQUEST_HASH,
            authorization_id=1,
            task_id="signal-stats-v2-task",
            task_revision=1,
            packet_artifact_hash_ref=PACKET_REF,
            packet_semantic_hash=PACKET_SEMANTIC_HASH,
            result_artifact_hash_ref=f"sha256:{'9' * 64}",
            proposal_index=0,
            candidate_id=CANDIDATE_ID,
        )


@pytest.mark.parametrize(
    ("change", "value"),
    [
        ("primary_metric", "median_net_r"),
        ("split_plan", _split(start_hour=1)),
        ("friction", _friction(spread=2.0)),
        ("seed", 8),
        ("parameters", {"horizon": 24, "target": "forward_net_r"}),
    ],
)
def test_scientific_changes_alter_semantic_identity(
    tmp_path: Path,
    change: str,
    value: object,
) -> None:
    registry = HypothesisRegistry(tmp_path / "registry.db")
    _register(registry)
    store = ArtifactStore(tmp_path / "artifacts")
    original = _manifest(store, registry)
    kwargs = {change: value}
    if change == "primary_metric":
        kwargs["criteria"] = _criteria(metric=value)

    changed = _manifest(store, registry, **kwargs)

    assert changed.manifest_semantic_hash != original.manifest_semantic_hash


def test_dataset_change_alters_identity_and_created_metadata_does_not(tmp_path: Path) -> None:
    registry = HypothesisRegistry(tmp_path / "registry.db")
    _register(registry)
    store = ArtifactStore(tmp_path / "artifacts")
    first_dataset = _dataset(store, b"first")
    second_dataset = _dataset(store, b"second")
    first = _manifest(store, registry, dataset=first_dataset)
    later = _manifest(
        store,
        registry,
        dataset=first_dataset,
        created_at="2026-08-15T10:00:00+00:00",
        created_by="operator:other",
    )
    changed_data = _manifest(store, registry, dataset=second_dataset)

    assert first.manifest_semantic_hash == later.manifest_semantic_hash
    assert first.canonical_bytes() != later.canonical_bytes()
    assert first.manifest_semantic_hash != changed_data.manifest_semantic_hash


def test_all_predeclared_scientific_contracts_participate_in_identity(tmp_path: Path) -> None:
    registry = HypothesisRegistry(tmp_path / "registry.db")
    _register(registry)
    store = ArtifactStore(tmp_path / "artifacts")
    original = _manifest(store, registry)
    revised_provenance = replace(original.proposal_provenance, task_revision=2)
    revised_code = replace(original.code_provenance, producer_version="1.31.2")
    variants = (
        replace(original, proposal_provenance=revised_provenance),
        replace(original, evaluation_criteria=_criteria(threshold=0.10)),
        replace(original, alpha=0.01),
        replace(original, q=0.05),
        replace(original, minimum_effect_size=0.10),
        replace(original, max_hypotheses_tests=21),
        replace(original, code_provenance=revised_code),
    )

    assert all(
        variant.manifest_semantic_hash != original.manifest_semantic_hash for variant in variants
    )


def test_explicit_non_trading_friction_absence_is_semantic(tmp_path: Path) -> None:
    registry = HypothesisRegistry(tmp_path / "registry.db")
    _register(registry)
    store = ArtifactStore(tmp_path / "artifacts")
    trading = _manifest(store, registry)
    non_trading = replace(trading, trading_friction=None)

    assert non_trading.trading_friction is None
    assert non_trading.manifest_semantic_hash != trading.manifest_semantic_hash


def test_manifest_is_deeply_immutable_and_cannot_contain_observed_results(tmp_path: Path) -> None:
    registry = HypothesisRegistry(tmp_path / "registry.db")
    _register(registry)
    store = ArtifactStore(tmp_path / "artifacts")
    parameters = {"nested": {"window": [1, 2]}}
    manifest = _manifest(store, registry, parameters=parameters)
    original = manifest.canonical_bytes()
    parameters["nested"]["window"].append(3)
    payload = manifest.to_payload()
    payload["experiment"]["observed_sharpe"] = 2.0

    assert manifest.canonical_bytes() == original
    with pytest.raises(ManifestV2ValidationError, match="unknown fields"):
        ExperimentManifestV2.from_payload(payload)


def test_strict_schema_noncanonical_wire_and_safety_flags_fail_closed(tmp_path: Path) -> None:
    registry = HypothesisRegistry(tmp_path / "registry.db")
    _register(registry)
    store = ArtifactStore(tmp_path / "artifacts")
    manifest = _manifest(store, registry)

    noncanonical = json.dumps(manifest.to_payload(), ensure_ascii=False).encode()
    with pytest.raises(ManifestV2ValidationError, match="canonical JSON"):
        verify_experiment_manifest_v2(noncanonical)

    for field_name, value in (("read_only", False), ("orders_enabled", True)):
        payload = manifest.to_payload()
        payload[field_name] = value
        with pytest.raises(ManifestV2ValidationError, match="read-only"):
            ExperimentManifestV2.from_payload(payload)

    payload = manifest.to_payload()
    payload["schema_version"] = "experiment-manifest-v999"
    with pytest.raises(ManifestV2ValidationError, match="unsupported"):
        ExperimentManifestV2.from_payload(payload)


def test_numeric_and_collection_bounds_fail_closed(tmp_path: Path) -> None:
    with pytest.raises(ManifestV2ValidationError, match="threshold"):
        _criteria(threshold=True)
    with pytest.raises(ManifestV2ValidationError, match="threshold"):
        _criteria(threshold=float("nan"))
    with pytest.raises(ManifestV2ValidationError, match="between"):
        EvaluationCriteriaV1(mode=CriteriaMode.ALL, criteria=())
    with pytest.raises(ManifestV2ValidationError, match="between"):
        EvaluationCriteriaV1(
            mode=CriteriaMode.ALL,
            criteria=tuple(
                EvaluationCriterionV1(
                    metric=f"metric-{index}",
                    operator=CriterionOperator.GREATER_THAN,
                    threshold=0.0,
                )
                for index in range(17)
            ),
        )
    with pytest.raises(ManifestV2ValidationError, match="unsupported criterion operator"):
        EvaluationCriterionV1.from_payload(
            {"metric": "mean_net_r", "operator": "==", "threshold": 0.0}
        )
    with pytest.raises(ManifestV2ValidationError, match="finite float"):
        _friction(spread=float("inf"))
    with pytest.raises(ManifestV2ValidationError, match=r"\[0.0"):
        _friction(spread=-0.1)

    registry = HypothesisRegistry(tmp_path / "registry.db")
    _register(registry)
    store = ArtifactStore(tmp_path / "artifacts")
    with pytest.raises(ManifestV2ValidationError, match="maximum keys"):
        _manifest(store, registry, parameters={f"key-{index}": index for index in range(65)})


def test_malformed_and_missing_dataset_cas_fail_before_persistence(tmp_path: Path) -> None:
    with pytest.raises(ManifestV2ValidationError, match="artifact_hash_ref"):
        DatasetArtifactV2(
            role="market-data",
            artifact_hash_ref=f"sha256:{'A' * 64}",
            media_type="text/csv",
            size_bytes=10,
        )

    registry = HypothesisRegistry(tmp_path / "registry.db")
    _register(registry)
    store = ArtifactStore(tmp_path / "artifacts")
    missing = DatasetArtifactV2(
        role="market-data",
        artifact_hash_ref=f"sha256:{'0' * 64}",
        media_type="text/csv",
        size_bytes=10,
    )
    with pytest.raises(ArtifactNotFoundError):
        _manifest(store, registry, dataset=missing)


def test_duplicate_json_keys_and_unknown_nested_fields_fail_closed(tmp_path: Path) -> None:
    registry = HypothesisRegistry(tmp_path / "registry.db")
    _register(registry)
    store = ArtifactStore(tmp_path / "artifacts")
    manifest = _manifest(store, registry)
    encoded = manifest.canonical_bytes()
    duplicate = b'{"diagnostics":{},' + encoded[1:]
    with pytest.raises(ManifestV2ValidationError, match="wire payload is invalid"):
        verify_experiment_manifest_v2(duplicate)

    payload = manifest.to_payload()
    payload["split"]["plan"]["random_shuffle"] = True
    with pytest.raises(ValueError, match="unknown fields"):
        ExperimentManifestV2.from_payload(payload)


def test_split_plan_roundtrip_identity_and_temporal_guards() -> None:
    plan = _split()
    restored = SplitPlan.from_payload(plan.to_payload())

    assert restored == plan
    assert restored.semantic_hash == plan.semantic_hash
    assert "rows" not in plan.to_payload()
    assert "observations" not in plan.to_payload()

    payload = plan.to_payload()
    payload["validation_start"] = payload["discovery_end"]
    with pytest.raises(ValueError, match="strictly before"):
        SplitPlan.from_payload(payload)
    payload = plan.to_payload()
    payload["holdout_start"] = payload["validation_start"]
    with pytest.raises(ValueError, match="strictly before"):
        SplitPlan.from_payload(payload)
    payload = plan.to_payload()
    payload["unknown"] = 1
    with pytest.raises(ValueError, match="unknown fields"):
        SplitPlan.from_payload(payload)


def test_manifest_split_is_bound_to_one_existing_dataset_role(tmp_path: Path) -> None:
    registry = HypothesisRegistry(tmp_path / "registry.db")
    _register(registry)
    store = ArtifactStore(tmp_path / "artifacts")
    manifest = _manifest(store, registry)
    payload = manifest.to_payload()
    payload["split"]["dataset_role"] = "missing-role"

    with pytest.raises(ManifestV2ValidationError, match="dataset binding"):
        ExperimentManifestV2.from_payload(payload)


def test_verified_dataset_contract_uses_cas_metadata_without_authoritative_path(
    tmp_path: Path,
) -> None:
    registry = HypothesisRegistry(tmp_path / "registry.db")
    _register(registry)
    store = ArtifactStore(tmp_path / "artifacts")
    dataset = _dataset(store)
    manifest = _manifest(store, registry, dataset=dataset)

    assert "path" not in dataset.to_payload()
    dataset.verify(store)
    wrong_size = replace(dataset, size_bytes=dataset.size_bytes + 1)
    with pytest.raises(ManifestV2ValidationError, match="metadata size"):
        wrong_size.verify(store)
    wrong_media = replace(dataset, media_type="application/octet-stream")
    with pytest.raises(ArtifactMediaTypeError):
        wrong_media.verify(store)

    object_path = Path(dataset.verify(store).path)
    object_path.write_bytes(b"tampered")
    with pytest.raises(ArtifactIntegrityError):
        manifest.verify_datasets(store)


def test_exact_cas_persist_load_restart_and_media_validation(tmp_path: Path) -> None:
    registry = HypothesisRegistry(tmp_path / "registry.db")
    _register(registry)
    root = tmp_path / "artifacts"
    store = ArtifactStore(root)
    manifest = _manifest(store, registry)
    artifact = persist_experiment_manifest_v2(manifest, artifact_store=store)

    restarted = ArtifactStore(root)
    loaded = load_experiment_manifest_v2(artifact.hash_ref, artifact_store=restarted)

    assert loaded == manifest
    assert loaded.manifest_semantic_hash == manifest.manifest_semantic_hash
    assert restarted.read_verified(artifact.hash_ref) == manifest.canonical_bytes()

    other_store = ArtifactStore(tmp_path / "other-artifacts")
    wrong_media = other_store.import_snapshot(
        io.BytesIO(manifest.canonical_bytes()),
        media_type="application/json",
    )
    with pytest.raises(ArtifactMediaTypeError):
        load_experiment_manifest_v2(wrong_media.hash_ref, artifact_store=other_store)


def test_missing_corrupt_and_noncanonical_manifest_cas_fail_closed(tmp_path: Path) -> None:
    registry = HypothesisRegistry(tmp_path / "registry.db")
    _register(registry)
    store = ArtifactStore(tmp_path / "artifacts")
    manifest = _manifest(store, registry)
    artifact = persist_experiment_manifest_v2(manifest, artifact_store=store)

    with pytest.raises(ArtifactNotFoundError):
        load_experiment_manifest_v2(f"sha256:{'0' * 64}", artifact_store=store)

    noncanonical = store.import_snapshot(
        io.BytesIO(json.dumps(manifest.to_payload()).encode()),
        media_type=EXPERIMENT_MANIFEST_V2_MEDIA_TYPE,
    )
    with pytest.raises(ManifestV2ValidationError, match="canonical JSON"):
        load_experiment_manifest_v2(noncanonical.hash_ref, artifact_store=store)

    Path(artifact.path).write_bytes(b"corrupt")
    with pytest.raises(ArtifactIntegrityError):
        load_experiment_manifest_v2(artifact.hash_ref, artifact_store=store)


def test_manifest_cas_import_failure_cannot_mutate_registry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = HypothesisRegistry(tmp_path / "registry.db")
    _register(registry)
    store = ArtifactStore(tmp_path / "artifacts")
    manifest = _manifest(store, registry)

    def fail_import(*args: object, **kwargs: object) -> None:
        raise OSError("simulated CAS import failure")

    monkeypatch.setattr(store, "import_snapshot", fail_import)
    with pytest.raises(OSError, match="simulated"):
        persist_experiment_manifest_v2(manifest, artifact_store=store)

    assert registry.get(HYPOTHESIS_ID).state is HypothesisState.PROPOSED


def test_registry_transactional_v2_freeze_idempotency_and_restart(tmp_path: Path) -> None:
    registry = HypothesisRegistry(tmp_path / "registry.db")
    record = _register(registry)
    root = tmp_path / "artifacts"
    store = ArtifactStore(root)
    manifest = _manifest(store, registry)
    artifact = persist_experiment_manifest_v2(manifest, artifact_store=store)

    with _transaction(registry) as db:
        created = registry.freeze_manifest_v2_in_transaction(
            db,
            manifest_artifact_hash_ref=artifact.hash_ref,
            artifact_store=store,
        )
        assert created.created is True
        assert created.record.state is HypothesisState.FROZEN
        assert created.record.content_hash == record.content_hash

    restarted_registry = HypothesisRegistry(registry.path)
    restarted_store = ArtifactStore(root)
    with _transaction(restarted_registry) as db:
        retry = restarted_registry.freeze_manifest_v2_in_transaction(
            db,
            manifest_artifact_hash_ref=artifact.hash_ref,
            artifact_store=restarted_store,
        )
        assert retry.created is False

    loaded = restarted_registry.load_bound_manifest_v2(
        HYPOTHESIS_ID,
        artifact_store=restarted_store,
    )
    assert loaded == manifest


def test_registry_v2_freeze_requires_active_matching_transaction(tmp_path: Path) -> None:
    registry = HypothesisRegistry(tmp_path / "registry.db")
    _register(registry)
    store = ArtifactStore(tmp_path / "artifacts")
    artifact = persist_experiment_manifest_v2(
        _manifest(store, registry),
        artifact_store=store,
    )
    db = sqlite3.connect(registry.path)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    try:
        with pytest.raises(RegistryError, match="active caller transaction"):
            registry.freeze_manifest_v2_in_transaction(
                db,
                manifest_artifact_hash_ref=artifact.hash_ref,
                artifact_store=store,
            )
    finally:
        db.close()


def test_registry_v2_freeze_rollback_leaves_proposed_and_orphan_cas_is_safe(
    tmp_path: Path,
) -> None:
    registry = HypothesisRegistry(tmp_path / "registry.db")
    _register(registry)
    store = ArtifactStore(tmp_path / "artifacts")
    artifact = persist_experiment_manifest_v2(
        _manifest(store, registry),
        artifact_store=store,
    )
    db = _transaction(registry)
    try:
        result = registry.freeze_manifest_v2_in_transaction(
            db,
            manifest_artifact_hash_ref=artifact.hash_ref,
            artifact_store=store,
        )
        assert result.created
        db.rollback()
    finally:
        db.close()

    record = registry.get(HYPOTHESIS_ID)
    assert record.state is HypothesisState.PROPOSED
    assert record.manifest_hash is None
    assert record.manifest_artifact_hash_ref is None
    assert store.resolve_verified(artifact.hash_ref).hash_ref == artifact.hash_ref


def test_invalid_manifest_cas_cannot_partially_freeze_registry(tmp_path: Path) -> None:
    registry = HypothesisRegistry(tmp_path / "registry.db")
    _register(registry)
    store = ArtifactStore(tmp_path / "artifacts")
    db = _transaction(registry)
    try:
        with pytest.raises(ArtifactNotFoundError):
            registry.freeze_manifest_v2_in_transaction(
                db,
                manifest_artifact_hash_ref=f"sha256:{'0' * 64}",
                artifact_store=store,
            )
        db.rollback()
    finally:
        db.close()

    record = registry.get(HYPOTHESIS_ID)
    assert record.state is HypothesisState.PROPOSED
    assert record.manifest_hash is None


def test_registry_update_failure_rolls_back_without_half_frozen_binding(tmp_path: Path) -> None:
    registry = HypothesisRegistry(tmp_path / "registry.db")
    _register(registry)
    store = ArtifactStore(tmp_path / "artifacts")
    artifact = persist_experiment_manifest_v2(
        _manifest(store, registry),
        artifact_store=store,
    )
    db = _transaction(registry)
    try:
        db.executescript(
            """
            CREATE TEMP TRIGGER fail_manifest_v2_freeze
            BEFORE UPDATE ON hypotheses
            BEGIN
                SELECT RAISE(ABORT, 'simulated registry failure');
            END;
            """
        )
        db.execute("BEGIN IMMEDIATE")
        with pytest.raises(sqlite3.IntegrityError, match="simulated registry failure"):
            registry.freeze_manifest_v2_in_transaction(
                db,
                manifest_artifact_hash_ref=artifact.hash_ref,
                artifact_store=store,
            )
        db.rollback()
    finally:
        db.close()

    record = registry.get(HYPOTHESIS_ID)
    assert record.state is HypothesisState.PROPOSED
    assert record.manifest_hash is None
    assert record.manifest_artifact_hash_ref is None


@pytest.mark.parametrize(
    ("family_id", "content_hash", "error"),
    [
        (f"hf_{'1' * 64}", None, "family identity"),
        (None, "2" * 64, "content identity"),
    ],
)
def test_registry_v2_freeze_rejects_hypothesis_identity_mismatch(
    tmp_path: Path,
    family_id: str | None,
    content_hash: str | None,
    error: str,
) -> None:
    registry = HypothesisRegistry(tmp_path / "registry.db")
    _register(registry)
    store = ArtifactStore(tmp_path / "artifacts")
    manifest = _manifest(
        store,
        registry,
        family_id=family_id,
        content_hash=content_hash,
    )
    artifact = persist_experiment_manifest_v2(manifest, artifact_store=store)

    db = _transaction(registry)
    try:
        with pytest.raises(RegistryError, match=error):
            registry.freeze_manifest_v2_in_transaction(
                db,
                manifest_artifact_hash_ref=artifact.hash_ref,
                artifact_store=store,
            )
        db.rollback()
    finally:
        db.close()
    assert registry.get(HYPOTHESIS_ID).state is HypothesisState.PROPOSED


def test_registry_v2_freeze_rejects_manifest_for_another_hypothesis(tmp_path: Path) -> None:
    registry = HypothesisRegistry(tmp_path / "registry.db")
    _register(registry)
    store = ArtifactStore(tmp_path / "artifacts")
    other_result_ref = f"sha256:{'9' * 64}"
    other_id = f"rpi-v1:{other_result_ref}:0"
    manifest = _manifest(store, registry, hypothesis_id=other_id)
    artifact = persist_experiment_manifest_v2(manifest, artifact_store=store)

    db = _transaction(registry)
    try:
        with pytest.raises(KeyError):
            registry.freeze_manifest_v2_in_transaction(
                db,
                manifest_artifact_hash_ref=artifact.hash_ref,
                artifact_store=store,
            )
        db.rollback()
    finally:
        db.close()
    assert registry.get(HYPOTHESIS_ID).state is HypothesisState.PROPOSED


def test_concurrent_identical_v2_freeze_creates_one_logical_binding(tmp_path: Path) -> None:
    registry = HypothesisRegistry(tmp_path / "registry.db")
    _register(registry)
    store = ArtifactStore(tmp_path / "artifacts")
    artifact = persist_experiment_manifest_v2(
        _manifest(store, registry),
        artifact_store=store,
    )
    barrier = Barrier(16)

    def bind() -> bool:
        barrier.wait(timeout=10)
        with _transaction(registry) as db:
            return registry.freeze_manifest_v2_in_transaction(
                db,
                manifest_artifact_hash_ref=artifact.hash_ref,
                artifact_store=store,
            ).created

    with ThreadPoolExecutor(max_workers=16) as executor:
        outcomes = list(executor.map(lambda _: bind(), range(16)))

    assert outcomes.count(True) == 1
    assert outcomes.count(False) == 15
    assert registry.get(HYPOTHESIS_ID).state is HypothesisState.FROZEN


def test_concurrent_conflicting_v2_manifests_allow_only_one_winner(tmp_path: Path) -> None:
    registry = HypothesisRegistry(tmp_path / "registry.db")
    _register(registry)
    store = ArtifactStore(tmp_path / "artifacts")
    first = persist_experiment_manifest_v2(
        _manifest(store, registry),
        artifact_store=store,
    )
    second = persist_experiment_manifest_v2(
        _manifest(
            store,
            registry,
            primary_metric="median_net_r",
            criteria=_criteria(metric="median_net_r"),
        ),
        artifact_store=store,
    )
    barrier = Barrier(2)

    def bind(hash_ref: str) -> str:
        barrier.wait(timeout=10)
        try:
            with _transaction(registry) as db:
                registry.freeze_manifest_v2_in_transaction(
                    db,
                    manifest_artifact_hash_ref=hash_ref,
                    artifact_store=store,
                )
            return "bound"
        except RegistryError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(bind, (first.hash_ref, second.hash_ref)))

    assert sorted(outcomes) == ["bound", "conflict"]


def test_first_exact_cas_wins_for_same_semantics_with_different_diagnostics(
    tmp_path: Path,
) -> None:
    registry = HypothesisRegistry(tmp_path / "registry.db")
    _register(registry)
    store = ArtifactStore(tmp_path / "artifacts")
    first = _manifest(store, registry)
    second = _manifest(
        store,
        registry,
        created_at="2026-08-15T10:00:00+00:00",
        created_by="operator:retry",
    )
    first_ref = persist_experiment_manifest_v2(first, artifact_store=store)
    second_ref = persist_experiment_manifest_v2(second, artifact_store=store)
    assert first_ref.hash_ref != second_ref.hash_ref

    with _transaction(registry) as db:
        initial = registry.freeze_manifest_v2_in_transaction(
            db,
            manifest_artifact_hash_ref=first_ref.hash_ref,
            artifact_store=store,
        )
        assert initial.created
    with _transaction(registry) as db:
        retry = registry.freeze_manifest_v2_in_transaction(
            db,
            manifest_artifact_hash_ref=second_ref.hash_ref,
            artifact_store=store,
        )
        assert retry.created is False
        assert retry.record.manifest_artifact_hash_ref == first_ref.hash_ref


def test_bound_manifest_missing_or_corrupt_fails_closed(tmp_path: Path) -> None:
    registry = HypothesisRegistry(tmp_path / "registry.db")
    _register(registry)
    store = ArtifactStore(tmp_path / "artifacts")
    artifact = persist_experiment_manifest_v2(
        _manifest(store, registry),
        artifact_store=store,
    )
    with _transaction(registry) as db:
        registry.freeze_manifest_v2_in_transaction(
            db,
            manifest_artifact_hash_ref=artifact.hash_ref,
            artifact_store=store,
        )

    Path(artifact.path).write_bytes(b"corrupt")
    with pytest.raises(ArtifactIntegrityError):
        registry.load_bound_manifest_v2(HYPOTHESIS_ID, artifact_store=store)


def test_corrupt_registry_v2_binding_metadata_fails_closed(tmp_path: Path) -> None:
    registry = HypothesisRegistry(tmp_path / "registry.db")
    _register(registry)
    store = ArtifactStore(tmp_path / "artifacts")
    artifact = persist_experiment_manifest_v2(
        _manifest(store, registry),
        artifact_store=store,
    )
    with _transaction(registry) as db:
        registry.freeze_manifest_v2_in_transaction(
            db,
            manifest_artifact_hash_ref=artifact.hash_ref,
            artifact_store=store,
        )
    with sqlite3.connect(registry.path) as db:
        db.execute(
            "UPDATE hypotheses SET manifest_schema_version=? WHERE hypothesis_id=?",
            ("experiment-manifest-v999", HYPOTHESIS_ID),
        )

    with pytest.raises(RegistryError, match="unsupported"):
        registry.get(HYPOTHESIS_ID)


def test_manifest_builder_performs_no_provider_holdout_or_trading_action(tmp_path: Path) -> None:
    registry = HypothesisRegistry(tmp_path / "registry.db")
    _register(registry)
    store = ArtifactStore(tmp_path / "artifacts")
    manifest = _manifest(store, registry)
    payload = manifest.to_payload()

    assert payload["read_only"] is True
    assert payload["orders_enabled"] is False
    assert "result" not in payload
    assert "holdout_metrics" not in payload
    assert registry.get(HYPOTHESIS_ID).state is HypothesisState.PROPOSED


# ---------------------------------------------------------------------------
# Final Holdout Acceptance Contract V1 (FinalHoldoutCriteriaV1): the
# explicit, stage-scoped decision contract for the final-holdout stage,
# additive to ExperimentManifestV2. Consuming it to decide
# ACCEPTED/REJECTED_FINAL is explicitly out of scope for this contract layer
# -- see discovery/manifest.py's own FinalHoldoutCriteriaV1 docstring.
# ---------------------------------------------------------------------------


# 1: manifest with valid final_holdout_criteria round-trips.


def test_final_holdout_criteria_round_trips_through_manifest_payload(tmp_path: Path) -> None:
    registry = HypothesisRegistry(tmp_path / "registry.db")
    _register(registry)
    store = ArtifactStore(tmp_path / "artifacts")
    criteria = _final_holdout_criteria(metric="holdout_rows", threshold=2)
    manifest = _manifest(store, registry, final_holdout_criteria=criteria)

    assert manifest.final_holdout_criteria is not None
    assert manifest.final_holdout_criteria.required_metric_names == frozenset({"holdout_rows"})

    restored = ExperimentManifestV2.from_payload(manifest.to_payload())
    assert restored.final_holdout_criteria is not None
    assert restored.final_holdout_criteria.to_payload() == criteria.to_payload()
    assert restored.manifest_semantic_hash == manifest.manifest_semantic_hash

    wire = manifest.canonical_bytes()
    reloaded = verify_experiment_manifest_v2(wire)
    assert reloaded.final_holdout_criteria.to_payload() == criteria.to_payload()

    artifact = persist_experiment_manifest_v2(manifest, artifact_store=store)
    loaded = load_experiment_manifest_v2(artifact.hash_ref, artifact_store=store)
    assert loaded.final_holdout_criteria.to_payload() == criteria.to_payload()


def test_final_holdout_criteria_absent_round_trips_as_none(tmp_path: Path) -> None:
    registry = HypothesisRegistry(tmp_path / "registry.db")
    _register(registry)
    store = ArtifactStore(tmp_path / "artifacts")
    manifest = _manifest(store, registry)

    assert manifest.final_holdout_criteria is None
    payload = manifest.to_payload()
    assert payload["final_holdout_criteria"] is None
    restored = ExperimentManifestV2.from_payload(payload)
    assert restored.final_holdout_criteria is None


# 2: semantic hash changes when holdout criteria change.


def test_final_holdout_criteria_participates_in_manifest_semantic_hash(tmp_path: Path) -> None:
    registry = HypothesisRegistry(tmp_path / "registry.db")
    _register(registry)
    store = ArtifactStore(tmp_path / "artifacts")
    without = _manifest(store, registry)
    with_criteria = _manifest(
        store, registry, final_holdout_criteria=_final_holdout_criteria(threshold=1)
    )
    changed_threshold = _manifest(
        store, registry, final_holdout_criteria=_final_holdout_criteria(threshold=2)
    )
    changed_metric = _manifest(
        store,
        registry,
        final_holdout_criteria=_final_holdout_criteria(metric="other_metric", threshold=1),
    )

    hashes = {
        without.manifest_semantic_hash,
        with_criteria.manifest_semantic_hash,
        changed_threshold.manifest_semantic_hash,
        changed_metric.manifest_semantic_hash,
    }
    assert len(hashes) == 4

    # FinalHoldoutCriteriaV1's own semantic_hash is likewise sensitive to
    # every field that determines its identity.
    base = _final_holdout_criteria(threshold=1)
    assert base.semantic_hash != _final_holdout_criteria(threshold=2).semantic_hash
    assert base.semantic_hash != _final_holdout_criteria(metric="other_metric").semantic_hash
    assert base.semantic_hash != _final_holdout_criteria(mode=CriteriaMode.ANY).semantic_hash
    assert base.semantic_hash == _final_holdout_criteria(threshold=1).semantic_hash


# 3: undeclared metric cannot satisfy criteria.


def test_undeclared_metric_cannot_satisfy_final_holdout_criteria() -> None:
    criteria = _final_holdout_criteria(metric="holdout_rows")
    # A metrics mapping that only carries an unrelated, undeclared name does
    # not satisfy the contract, regardless of its value.
    with pytest.raises(ManifestV2ValidationError, match="missing required names"):
        verify_final_holdout_metric_vocabulary(criteria, {"some_other_metric": 999})


# 4: missing required metric fails closed.


def test_missing_required_final_holdout_metric_fails_closed() -> None:
    criteria = FinalHoldoutCriteriaV1(
        mode=CriteriaMode.ALL,
        criteria=(
            EvaluationCriterionV1(
                metric="holdout_rows", operator=CriterionOperator.GREATER_THAN_OR_EQUAL, threshold=1
            ),
            EvaluationCriterionV1(
                metric="holdout_mean", operator=CriterionOperator.GREATER_THAN, threshold=0.0
            ),
        ),
    )
    with pytest.raises(ManifestV2ValidationError, match="holdout_mean"):
        verify_final_holdout_metric_vocabulary(criteria, {"holdout_rows": 3})
    with pytest.raises(ManifestV2ValidationError, match="metrics must be a mapping"):
        verify_final_holdout_metric_vocabulary(criteria, None)  # type: ignore[arg-type]
    with pytest.raises(ManifestV2ValidationError, match="exact FinalHoldoutCriteriaV1"):
        verify_final_holdout_metric_vocabulary(object(), {"holdout_rows": 1})  # type: ignore[arg-type]


# 5: NaN/Inf fails closed.


def test_final_holdout_criteria_rejects_nan_and_inf_threshold() -> None:
    with pytest.raises(ManifestV2ValidationError, match="threshold"):
        FinalHoldoutCriteriaV1(
            mode=CriteriaMode.ALL,
            criteria=(
                EvaluationCriterionV1(
                    metric="holdout_rows",
                    operator=CriterionOperator.GREATER_THAN_OR_EQUAL,
                    threshold=float("nan"),
                ),
            ),
        )
    with pytest.raises(ManifestV2ValidationError, match="threshold"):
        FinalHoldoutCriteriaV1(
            mode=CriteriaMode.ALL,
            criteria=(
                EvaluationCriterionV1(
                    metric="holdout_rows",
                    operator=CriterionOperator.GREATER_THAN_OR_EQUAL,
                    threshold=float("inf"),
                ),
            ),
        )


# 6: unsupported operator/mode rejected.


def test_final_holdout_criteria_rejects_unsupported_operator_and_mode() -> None:
    with pytest.raises(ManifestV2ValidationError, match="unsupported criterion operator"):
        EvaluationCriterionV1.from_payload(
            {"metric": "holdout_rows", "operator": "==", "threshold": 1}
        )
    with pytest.raises(ManifestV2ValidationError, match="unsupported final holdout criteria mode"):
        FinalHoldoutCriteriaV1.from_payload(
            {
                "schema_version": FINAL_HOLDOUT_CRITERIA_SCHEMA_VERSION,
                "mode": "MAJORITY",
                "criteria": (
                    {"metric": "holdout_rows", "operator": ">=", "threshold": 1},
                ),
                "semantic_hash": f"sha256:{'0' * 64}",
            }
        )
    with pytest.raises(ManifestV2ValidationError, match="mode must be CriteriaMode"):
        FinalHoldoutCriteriaV1(mode="ALL", criteria=(EvaluationCriterionV1(  # type: ignore[arg-type]
            metric="holdout_rows", operator=CriterionOperator.GREATER_THAN_OR_EQUAL, threshold=1
        ),))
    with pytest.raises(ManifestV2ValidationError, match="unsupported final holdout criteria schema_version"):
        FinalHoldoutCriteriaV1(
            mode=CriteriaMode.ALL,
            criteria=(
                EvaluationCriterionV1(
                    metric="holdout_rows",
                    operator=CriterionOperator.GREATER_THAN_OR_EQUAL,
                    threshold=1,
                ),
            ),
            schema_version="final-holdout-criteria-v999",
        )


# 7: evaluator metric vocabulary matches declared contract.


def test_evaluator_metric_vocabulary_matches_declared_contract() -> None:
    criteria = FinalHoldoutCriteriaV1(
        mode=CriteriaMode.ALL,
        criteria=(
            EvaluationCriterionV1(
                metric="holdout_rows", operator=CriterionOperator.GREATER_THAN_OR_EQUAL, threshold=1
            ),
            EvaluationCriterionV1(
                metric="holdout_mean_return", operator=CriterionOperator.GREATER_THAN, threshold=0.0
            ),
        ),
    )
    # Simulates a HoldoutRunReceipt.aggregate_metrics mapping that satisfies
    # the declared vocabulary (extra, undeclared keys are harmless).
    receipt_like_metrics = {"holdout_rows": 5, "holdout_mean_return": 0.02, "extra_diagnostic": True}
    verify_final_holdout_metric_vocabulary(criteria, receipt_like_metrics)  # does not raise


# 8: validation criteria remain separate from holdout criteria.


def test_validation_and_final_holdout_criteria_are_independent(tmp_path: Path) -> None:
    registry = HypothesisRegistry(tmp_path / "registry.db")
    _register(registry)
    store = ArtifactStore(tmp_path / "artifacts")
    # Deliberately using the SAME metric name in both containers to prove
    # they are separate namespaces, not merged or cross-referenced.
    shared_name = "mean_net_r"
    manifest = _manifest(
        store,
        registry,
        primary_metric=shared_name,
        criteria=_criteria(metric=shared_name, threshold=0.05),
        final_holdout_criteria=_final_holdout_criteria(metric=shared_name, threshold=0.10),
    )
    assert manifest.evaluation_criteria.criteria[0].threshold == 0.05
    assert manifest.final_holdout_criteria.criteria[0].threshold == 0.10

    only_validation_changed = _manifest(
        store,
        registry,
        primary_metric=shared_name,
        criteria=_criteria(metric=shared_name, threshold=0.99),
        final_holdout_criteria=_final_holdout_criteria(metric=shared_name, threshold=0.10),
    )
    only_holdout_changed = _manifest(
        store,
        registry,
        primary_metric=shared_name,
        criteria=_criteria(metric=shared_name, threshold=0.05),
        final_holdout_criteria=_final_holdout_criteria(metric=shared_name, threshold=0.99),
    )
    assert only_validation_changed.final_holdout_criteria.to_payload() == manifest.final_holdout_criteria.to_payload()
    assert only_holdout_changed.evaluation_criteria.to_payload() == manifest.evaluation_criteria.to_payload()
    assert only_validation_changed.manifest_semantic_hash != manifest.manifest_semantic_hash
    assert only_holdout_changed.manifest_semantic_hash != manifest.manifest_semantic_hash
    assert only_validation_changed.manifest_semantic_hash != only_holdout_changed.manifest_semantic_hash

    payload = manifest.to_payload()
    assert "evaluation_criteria" in payload["experiment"]
    assert "final_holdout_criteria" not in payload["experiment"]
    assert payload["final_holdout_criteria"] is not None
    assert payload["final_holdout_criteria"] != payload["experiment"]["evaluation_criteria"]


# 9: alpha/q/minimum_effect_size/max_hypotheses_tests are not silently reused.


def test_final_holdout_criteria_never_reuses_validation_numeric_fields(tmp_path: Path) -> None:
    import dataclasses

    field_names = {item.name for item in dataclasses.fields(FinalHoldoutCriteriaV1)}
    assert field_names.isdisjoint({"alpha", "q", "minimum_effect_size", "max_hypotheses_tests"})

    import inspect

    signature = inspect.signature(verify_final_holdout_metric_vocabulary)
    assert set(signature.parameters) == {"criteria", "metrics"}

    registry = HypothesisRegistry(tmp_path / "registry.db")
    _register(registry)
    store = ArtifactStore(tmp_path / "artifacts")
    base = _manifest(store, registry, final_holdout_criteria=_final_holdout_criteria())
    # Changing alpha/q/minimum_effect_size/max_hypotheses_tests changes the
    # manifest's overall identity (they are still part of the manifest, as
    # validation-stage fields) but never mutates final_holdout_criteria's
    # own payload or semantic_hash.
    changed = build_experiment_manifest_v2(
        artifact_store=store,
        hypothesis_id=HYPOTHESIS_ID,
        hypothesis_family_id=base.hypothesis_family_id,
        bound_hypothesis_content_hash=base.bound_hypothesis_content_hash,
        proposal_provenance=base.proposal_provenance,
        datasets=base.datasets,
        split_plan=base.split_plan,
        split_dataset_role=base.split_dataset_role,
        test_family=base.test_family,
        primary_metric=base.primary_metric,
        evaluation_criteria=base.evaluation_criteria,
        alpha=0.01,
        q=0.01,
        minimum_effect_size=0.5,
        max_hypotheses_tests=999,
        trading_friction=base.trading_friction,
        deterministic_seed=base.deterministic_seed,
        code_provenance=base.code_provenance,
        semantic_parameters=base.semantic_parameters,
        created_at=base.created_at,
        created_by=base.created_by,
        final_holdout_criteria=base.final_holdout_criteria,
    )
    assert changed.alpha != base.alpha
    assert changed.final_holdout_criteria.to_payload() == base.final_holdout_criteria.to_payload()
    assert changed.final_holdout_criteria.semantic_hash == base.final_holdout_criteria.semantic_hash


# 10: old closed manifest/validation/holdout tests remain green -- verified
# by running the full existing suites as part of the required regression
# (see VALIDATION section of the implementation report); every pre-existing
# test in this file above this section is unmodified.


# 11-13: no holdout plaintext access, no provider/network, no broker/MT5
# added by this additive contract.


def test_final_holdout_criteria_contract_adds_no_holdout_provider_or_broker_imports() -> None:
    import ast

    source = Path("src/trademind/discovery/manifest.py").read_text(encoding="utf-8")
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
    forbidden_calls = {"decrypt", "decrypt_bytes", "evaluate", "OrderSend", "PositionClose", "PositionModify"}
    assert not (called_names & forbidden_calls), called_names & forbidden_calls
    assert "CTrade" not in source
    assert "TRADE_ACTION_DEAL" not in source


def test_final_holdout_criteria_does_not_apply_a_decision_or_transition(tmp_path: Path) -> None:
    """This contract layer never applies its own rule and never transitions
    a hypothesis -- verify_final_holdout_metric_vocabulary only checks name
    presence, and no ACCEPTED/REJECTED_FINAL reference exists anywhere in
    the module (that remains the explicitly out-of-scope Final Verdict /
    Acceptance Control layer's responsibility)."""
    source = Path("src/trademind/discovery/manifest.py").read_text(encoding="utf-8")
    assert "HypothesisState" not in source
    assert "ACCEPTED" not in source
    assert "REJECTED_FINAL" not in source
    assert "registry.transition" not in source

    registry = HypothesisRegistry(tmp_path / "registry.db")
    _register(registry)
    assert registry.get(HYPOTHESIS_ID).state is HypothesisState.PROPOSED
    store = ArtifactStore(tmp_path / "artifacts")
    criteria = _final_holdout_criteria(metric="holdout_rows", threshold=1_000_000)
    _manifest(store, registry, final_holdout_criteria=criteria)
    # Building a manifest with an intentionally-unsatisfiable final holdout
    # criterion has no effect on hypothesis state at all -- this layer only
    # declares the contract, it never evaluates or enforces it.
    assert registry.get(HYPOTHESIS_ID).state is HypothesisState.PROPOSED
