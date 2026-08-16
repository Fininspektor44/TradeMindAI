from __future__ import annotations

import csv
import hashlib
import io
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from trademind.discovery.hypothesis_registry import HypothesisRegistry, HypothesisState
from trademind.discovery.holdout_runner import FinalHoldoutRunner, HoldoutRunError
from trademind.discovery.holdout_sealer import FinalHoldoutSealer
from trademind.discovery.holdout_store import HoldoutSealStore
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
from trademind.discovery.result_ledger import ResultLedger
from trademind.discovery.split_engine import SplitPlan, chronological_split
from trademind.experiment_evidence import ExperimentEvidenceBuilderV1
from trademind.experiment_execution_contract import (
    EvaluatorBinding,
    EvaluatorRegistry,
    ExecutionPhase,
    ExperimentExecutionContractError,
)
from trademind.experiment_execution_runtime import ExperimentExecutionRuntimeV1
from trademind.final_holdout_decision_gate import FinalHoldoutDecisionGateV1
from trademind.final_holdout_evaluation import (
    FinalHoldoutEvaluationConflictError,
    FinalHoldoutEvaluationControlV1,
    build_final_holdout_result_v1,
    load_final_holdout_result_v1,
    persist_final_holdout_result_v1,
    verify_final_holdout_result_v1,
)
from trademind.orchestrator.artifact_store import (
    ArtifactIntegrityError,
    ArtifactNotFoundError,
    ArtifactStore,
)
from trademind.signal_statistics_provenance import CodeProvenance
from trademind.validation_decision import ValidationDecisionBuilderV1, ValidationOutcome

FAMILY = "final-eval-v1"
EVALUATOR_ID = "fake-final-eval-v1"
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

HOLDOUT_EVALUATOR_ID = "final-holdout-mean-v1"
HOLDOUT_KEY = bytes(range(32))
HOLDOUT_KEY_ID = "holdout-key-v1"
EVALUATOR_ARTIFACT = Path(__file__).resolve()


class MeanValueEvaluator:
    """Discovery/validation execution evaluator (public rows only)."""

    evaluator_id = EVALUATOR_ID
    evaluator_version = EVALUATOR_VERSION

    def evaluate(self, rows, *, manifest, execution_phase):
        del manifest, execution_phase
        values = [float(row.fields["value"]) for row in rows]
        return {"mean_value": sum(values) / len(values)}


class HoldoutMeanEvaluator:
    """Trusted final-holdout evaluator: receives raw decrypted plaintext bytes."""

    evaluator_id = HOLDOUT_EVALUATOR_ID

    def __init__(self) -> None:
        self.calls = 0

    def evaluate(self, plaintext: bytes):
        self.calls += 1
        text = plaintext.decode("utf-8")
        reader = csv.DictReader(io.StringIO(text))
        values = [float(row["value"]) for row in reader]
        return {"mean_value": sum(values) / len(values)}


class StaticKeys:
    def __init__(self, key: bytes = HOLDOUT_KEY, key_id: str = HOLDOUT_KEY_ID) -> None:
        self.key = key
        self.key_id = key_id

    def load_key(self, key_id: str) -> bytes:
        if key_id != self.key_id:
            raise KeyError(key_id)
        return self.key


def _family(hypothesis_id: str = HYPOTHESIS_ID) -> dict[str, object]:
    return {"candidate_id": CANDIDATE_ID, "claim": f"final effect for {hypothesis_id}"}


def _content(hypothesis_id: str = HYPOTHESIS_ID) -> dict[str, object]:
    return {
        "family_definition": _family(hypothesis_id),
        "proposal": {"falsifiable_claim": "final effect remains positive"},
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
            task_id="final-eval-task",
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


def _case(tmp_path: Path, *, hypothesis_id: str = HYPOTHESIS_ID, **manifest_kwargs):
    registry = HypothesisRegistry(tmp_path / "registry.db")
    _register(registry, hypothesis_id)
    store = ArtifactStore(tmp_path / "artifacts")
    manifest = _manifest(store, registry, hypothesis_id=hypothesis_id, **manifest_kwargs)
    manifest_ref = _freeze(registry, store, manifest)
    return registry, store, manifest, manifest_ref


def _holdout_csv_bytes(values: list[float]) -> bytes:
    start = datetime(2027, 1, 1, tzinfo=timezone.utc)
    lines = ["time,value"]
    lines.extend(
        f"{(start + timedelta(hours=index)).isoformat()},{value}"
        for index, value in enumerate(values)
    )
    return ("\n".join(lines) + "\n").encode("utf-8")


def _seal_and_isolate(
    tmp_path: Path,
    registry: HypothesisRegistry,
    manifest: ExperimentManifestV2,
    *,
    holdout_values: list[float],
    evaluator_id: str = HOLDOUT_EVALUATOR_ID,
    key: bytes = HOLDOUT_KEY,
    key_id: str = HOLDOUT_KEY_ID,
):
    """Seal + isolation-attest a V2 hypothesis using the closed public primitives.

    ``FinalHoldoutSealer.seal_and_quarantine`` verifies its holdout boundary
    against a legacy V1 manifest FILE (``verify_frozen_manifest``, file-path
    datasets) and is structurally incompatible with V2 CAS-based datasets, so
    this fixture uses the existing low-level ``seal_file`` (already V1/V2-
    agnostic -- it only reads ``record.manifest_hash``) plus the existing
    public ``HoldoutSealStore.mark_isolated`` directly, supplying boundary
    values already verified by the frozen V2 ``SplitPlan`` itself. No sealing
    primitive is modified or reimplemented; this composes two already-public
    methods exactly as ``seal_and_quarantine`` itself does internally.
    """
    seals = HoldoutSealStore(registry)
    keys = StaticKeys(key=key, key_id=key_id)
    sealer = FinalHoldoutSealer(registry=registry, seals=seals, keys=keys)
    safe_id = manifest.hypothesis_id.replace(":", "_").replace("/", "_")
    plaintext_path = tmp_path / f"{safe_id}-holdout.csv"
    plaintext_path.write_bytes(_holdout_csv_bytes(holdout_values))
    sealed_path = tmp_path / f"{safe_id}-sealed.json"
    receipt = sealer.seal_file(
        hypothesis_id=manifest.hypothesis_id,
        plaintext_path=plaintext_path,
        destination_path=sealed_path,
        key_id=key_id,
        evaluator_id=evaluator_id,
        evaluator_artifact_path=EVALUATOR_ARTIFACT,
    )
    plan = manifest.split_plan
    attestation = {
        "schema_version": "final-holdout-isolation-attestation-v1",
        "hypothesis_id": receipt.hypothesis_id,
        "hypothesis_family_id": receipt.hypothesis_family_id,
        "manifest_hash": receipt.manifest_hash,
        "envelope_hash": receipt.envelope_hash,
        "evaluator_id": receipt.evaluator_id,
        "evaluator_hash": receipt.evaluator_hash,
        "plaintext_sha256": receipt.plaintext_sha256,
        "plaintext_size": receipt.plaintext_size,
        "public_max_time": plan.validation_end,
        "holdout_start_time": plan.holdout_start,
        "holdout_end_time": plan.holdout_end,
        "public_row_count": plan.public_count,
        "holdout_row_count": plan.holdout_count,
    }
    encoded = json.dumps(
        attestation, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")
    isolation_hash = hashlib.sha256(encoded).hexdigest()
    seals.mark_isolated(
        manifest.hypothesis_id,
        isolation_receipt_hash=isolation_hash,
        public_max_time=plan.validation_end,
        holdout_start_time=plan.holdout_start,
        holdout_end_time=plan.holdout_end,
        public_row_count=plan.public_count,
        holdout_row_count=plan.holdout_count,
    )
    return seals, keys, sealed_path, receipt


def _authorize(registry: HypothesisRegistry, store: ArtifactStore, hypothesis_id: str = HYPOTHESIS_ID):
    runtime = ExperimentExecutionRuntimeV1(
        registry=registry,
        artifact_store=store,
        evaluator_registry=_evaluator_registry(),
        evaluator=MeanValueEvaluator(),
    )
    execution = runtime.execute(
        hypothesis_id,
        execution_phase=ExecutionPhase.VALIDATION,
        execution_code_provenance=_code_provenance(),
        evaluator_friction=None,
        created_at=CREATED_AT,
        created_by=CREATED_BY,
    )
    evidence, evidence_artifact = ExperimentEvidenceBuilderV1(
        registry=registry, artifact_store=store, evaluator_registry=_evaluator_registry()
    ).build(hypothesis_id, execution=execution, created_at=CREATED_AT, created_by=CREATED_BY)
    decision, decision_artifact = ValidationDecisionBuilderV1(
        registry=registry, artifact_store=store, evaluator_registry=_evaluator_registry()
    ).decide(
        hypothesis_id,
        evidence_artifact_hash_ref=evidence_artifact.hash_ref,
        result_artifact_hash_ref=execution.result_artifact.hash_ref,
        created_at=CREATED_AT,
        created_by=CREATED_BY,
    )
    authorization, authorization_artifact = FinalHoldoutDecisionGateV1(
        registry=registry, artifact_store=store, evaluator_registry=_evaluator_registry()
    ).authorize(
        hypothesis_id,
        validation_decision_artifact_hash_ref=decision_artifact.hash_ref,
        evidence_artifact_hash_ref=evidence_artifact.hash_ref,
        result_artifact_hash_ref=execution.result_artifact.hash_ref,
        created_at=CREATED_AT,
        created_by=CREATED_BY,
    )
    return execution, evidence_artifact, decision_artifact, authorization, authorization_artifact


def _full_case(tmp_path: Path, *, threshold: float = -1.0, holdout_values: list[float]):
    """Full happy-path setup: frozen+sealed+isolated manifest, authorized lineage."""
    registry, store, manifest, manifest_ref = _case(tmp_path, threshold=threshold)
    seals, keys, sealed_path, seal_receipt = _seal_and_isolate(
        tmp_path, registry, manifest, holdout_values=holdout_values
    )
    execution, evidence_artifact, decision_artifact, authorization, authorization_artifact = _authorize(
        registry, store
    )
    ledger = ResultLedger(tmp_path / "results.jsonl")
    evaluator = HoldoutMeanEvaluator()
    runner = FinalHoldoutRunner(
        registry=registry,
        seals=seals,
        keys=keys,
        ledger=ledger,
        evaluator=evaluator,
        evaluator_artifact_path=EVALUATOR_ARTIFACT,
    )
    control = FinalHoldoutEvaluationControlV1(
        runner=runner, artifact_store=store, evaluator_registry=_evaluator_registry()
    )
    return {
        "registry": registry,
        "store": store,
        "manifest": manifest,
        "manifest_ref": manifest_ref,
        "sealed_path": sealed_path,
        "seal_receipt": seal_receipt,
        "execution": execution,
        "evidence_artifact": evidence_artifact,
        "decision_artifact": decision_artifact,
        "authorization": authorization,
        "authorization_artifact": authorization_artifact,
        "ledger": ledger,
        "evaluator": evaluator,
        "runner": runner,
        "control": control,
    }


def _evaluate(case: dict, **overrides):
    kwargs = {
        "authorization_artifact_hash_ref": case["authorization_artifact"].hash_ref,
        "validation_decision_artifact_hash_ref": case["decision_artifact"].hash_ref,
        "evidence_artifact_hash_ref": case["evidence_artifact"].hash_ref,
        "result_artifact_hash_ref": case["execution"].result_artifact.hash_ref,
        "sealed_holdout_path": case["sealed_path"],
        "created_at": CREATED_AT,
        "created_by": CREATED_BY,
    }
    kwargs.update(overrides)
    return case["control"].evaluate(HYPOTHESIS_ID, **kwargs)


# ---------------------------------------------------------------------------
# Authorized PASS / FAIL final evaluation
# ---------------------------------------------------------------------------


def test_authorized_pass_final_evaluation(tmp_path: Path) -> None:
    case = _full_case(tmp_path, threshold=-1.0, holdout_values=[5.0, 6.0, 7.0])
    outcome = _evaluate(case)
    assert outcome.result.outcome is ValidationOutcome.PASS
    assert outcome.result.criteria_decision.passed is True
    assert outcome.result.observed_metrics.values["mean_value"] == pytest.approx(6.0)
    markers = outcome.result.semantic_projection()["final_holdout_semantic_markers"]
    assert markers["scientifically_validated"] is True
    assert markers["final_holdout_consumed"] is True
    assert markers["trading_authorized"] is False
    assert case["registry"].get(HYPOTHESIS_ID).state is HypothesisState.ACCEPTED
    assert verify_final_holdout_result_v1(outcome.result.canonical_bytes()) == outcome.result


def test_authorized_fail_final_evaluation(tmp_path: Path) -> None:
    # Validation passes (public rows mean ~3.5 >= -1.0), authorizing the gate,
    # but the actual final holdout mean fails the SAME predeclared threshold.
    case = _full_case(tmp_path, threshold=-1.0, holdout_values=[-50.0, -60.0, -70.0])
    outcome = _evaluate(case)
    assert outcome.result.outcome is ValidationOutcome.FAIL
    assert outcome.result.criteria_decision.passed is False
    markers = outcome.result.semantic_projection()["final_holdout_semantic_markers"]
    assert markers["scientifically_validated"] is False
    assert markers["final_holdout_consumed"] is True
    assert case["registry"].get(HYPOTHESIS_ID).state is HypothesisState.REJECTED_FINAL


# ---------------------------------------------------------------------------
# Missing / invalid authorization
# ---------------------------------------------------------------------------


def test_missing_authorization_ref_fails_closed(tmp_path: Path) -> None:
    case = _full_case(tmp_path, holdout_values=[1.0, 2.0, 3.0])
    with pytest.raises(ArtifactNotFoundError):
        _evaluate(case, authorization_artifact_hash_ref=f"sha256:{'9' * 64}")


def test_no_authorization_on_file_fails_closed(tmp_path: Path) -> None:
    # A well-formed authorization artifact that was never registered as the
    # one authoritative grant (simulated: different hypothesis's own grant).
    registry, store, manifest, manifest_ref = _case(tmp_path, hypothesis_id=HYPOTHESIS_ID)
    _seal_and_isolate(tmp_path, registry, manifest, holdout_values=[1.0, 2.0, 3.0])
    _register(registry, OTHER_HYPOTHESIS_ID)
    other_manifest = _manifest(store, registry, hypothesis_id=OTHER_HYPOTHESIS_ID)
    _freeze(registry, store, other_manifest)
    _seal_and_isolate(tmp_path, registry, other_manifest, holdout_values=[1.0, 2.0, 3.0])

    execution, evidence_artifact, decision_artifact, authorization, authorization_artifact = _authorize(
        registry, store, hypothesis_id=OTHER_HYPOTHESIS_ID
    )
    ledger = ResultLedger(tmp_path / "results.jsonl")
    seals = HoldoutSealStore(registry)
    runner = FinalHoldoutRunner(
        registry=registry,
        seals=seals,
        keys=StaticKeys(),
        ledger=ledger,
        evaluator=HoldoutMeanEvaluator(),
        evaluator_artifact_path=EVALUATOR_ARTIFACT,
    )
    control = FinalHoldoutEvaluationControlV1(
        runner=runner, artifact_store=store, evaluator_registry=_evaluator_registry()
    )
    with pytest.raises(ExperimentExecutionContractError, match="manifest"):
        control.evaluate(
            HYPOTHESIS_ID,
            authorization_artifact_hash_ref=authorization_artifact.hash_ref,
            validation_decision_artifact_hash_ref=decision_artifact.hash_ref,
            evidence_artifact_hash_ref=evidence_artifact.hash_ref,
            result_artifact_hash_ref=execution.result_artifact.hash_ref,
            sealed_holdout_path=tmp_path / "irrelevant.json",
            created_at=CREATED_AT,
            created_by=CREATED_BY,
        )


# ---------------------------------------------------------------------------
# One-shot enforcement / idempotent reload
# ---------------------------------------------------------------------------


def test_one_shot_enforcement_via_runner(tmp_path: Path) -> None:
    case = _full_case(tmp_path, holdout_values=[1.0, 2.0, 3.0])
    _evaluate(case)
    with pytest.raises(HoldoutRunError):
        case["runner"].run_once(hypothesis_id=HYPOTHESIS_ID, sealed_path=case["sealed_path"])


def test_second_consumption_returns_idempotent_result_without_redecrypting(tmp_path: Path) -> None:
    case = _full_case(tmp_path, holdout_values=[1.0, 2.0, 3.0])
    first = _evaluate(case)
    assert case["evaluator"].calls == 1
    second = _evaluate(case)
    assert case["evaluator"].calls == 1  # never decrypted a second time.
    assert first.result == second.result
    assert first.result_artifact.hash_ref == second.result_artifact.hash_ref
    assert case["registry"].get(HYPOTHESIS_ID).state is HypothesisState.ACCEPTED


# ---------------------------------------------------------------------------
# Corrupt / tampered seal
# ---------------------------------------------------------------------------


def test_corrupt_tampered_seal_fails_closed(tmp_path: Path) -> None:
    case = _full_case(tmp_path, holdout_values=[1.0, 2.0, 3.0])
    document = json.loads(case["sealed_path"].read_text(encoding="utf-8"))
    document["header"]["evaluator_id"] = "tampered-after-seal"
    case["sealed_path"].write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(HoldoutRunError):
        _evaluate(case)
    # Tampering detected before any consumption; state is unchanged (still
    # short of even VALIDATION_PASSED bootstrap having been attempted here
    # is irrelevant -- the key point is no ACCEPTED/REJECTED_FINAL leaked).
    assert case["registry"].get(HYPOTHESIS_ID).state is not HypothesisState.ACCEPTED
    assert case["registry"].get(HYPOTHESIS_ID).state is not HypothesisState.REJECTED_FINAL


# ---------------------------------------------------------------------------
# Lineage / provenance mismatch
# ---------------------------------------------------------------------------


def test_lineage_substitution_wrong_hypothesis_fails_closed(tmp_path: Path) -> None:
    case = _full_case(tmp_path, holdout_values=[1.0, 2.0, 3.0])
    registry, store = case["registry"], case["store"]
    _register(registry, OTHER_HYPOTHESIS_ID)
    other_manifest = _manifest(store, registry, hypothesis_id=OTHER_HYPOTHESIS_ID)
    _freeze(registry, store, other_manifest)
    _seal_and_isolate(tmp_path, registry, other_manifest, holdout_values=[1.0, 2.0, 3.0])

    with pytest.raises(ExperimentExecutionContractError, match="manifest"):
        case["control"].evaluate(
            OTHER_HYPOTHESIS_ID,
            authorization_artifact_hash_ref=case["authorization_artifact"].hash_ref,
            validation_decision_artifact_hash_ref=case["decision_artifact"].hash_ref,
            evidence_artifact_hash_ref=case["evidence_artifact"].hash_ref,
            result_artifact_hash_ref=case["execution"].result_artifact.hash_ref,
            sealed_holdout_path=case["sealed_path"],
            created_at=CREATED_AT,
            created_by=CREATED_BY,
        )


# ---------------------------------------------------------------------------
# Precommitted criteria only
# ---------------------------------------------------------------------------


def test_build_final_holdout_result_has_no_ad_hoc_criteria_parameter() -> None:
    import inspect

    signature = inspect.signature(build_final_holdout_result_v1)
    forbidden_names = {"criteria", "threshold", "evaluation_criteria", "ad_hoc_criteria"}
    assert not (set(signature.parameters) & forbidden_names)


def test_final_criteria_decision_uses_manifest_predeclared_criteria(tmp_path: Path) -> None:
    case = _full_case(tmp_path, threshold=-1.0, holdout_values=[5.0, 6.0, 7.0])
    outcome = _evaluate(case)
    assert outcome.result.criteria_decision.mode == case["manifest"].evaluation_criteria.mode
    expected_metrics = {c.metric for c in case["manifest"].evaluation_criteria.criteria}
    actual_metrics = {ev.metric for ev in outcome.result.criteria_decision.evaluations}
    assert actual_metrics == expected_metrics


# ---------------------------------------------------------------------------
# Crash / restart safety around the consumption boundary
# ---------------------------------------------------------------------------


def test_crash_after_runner_consumption_recovers_from_ledger_not_replaintext(tmp_path: Path) -> None:
    case = _full_case(tmp_path, holdout_values=[1.0, 2.0, 3.0])
    # Simulate a prior control.evaluate() call that got as far as run_once()
    # succeeding (registry -> HOLDOUT_CONSUMED, runner's own ledger record
    # written) but crashed before persisting this layer's own CAS result.
    case["control"]._ensure_validation_passed(HYPOTHESIS_ID)
    receipt = case["runner"].run_once(hypothesis_id=HYPOTHESIS_ID, sealed_path=case["sealed_path"])
    assert case["evaluator"].calls == 1
    assert case["registry"].get(HYPOTHESIS_ID).state is HypothesisState.HOLDOUT_CONSUMED

    # A fresh control instance now completes evaluation: it must recover the
    # already-decrypted aggregate metrics from the runner's ledger record,
    # never re-invoking the evaluator (which would mean a second plaintext read).
    fresh_control = FinalHoldoutEvaluationControlV1(
        runner=case["runner"], artifact_store=case["store"], evaluator_registry=_evaluator_registry()
    )
    outcome = fresh_control.evaluate(
        HYPOTHESIS_ID,
        authorization_artifact_hash_ref=case["authorization_artifact"].hash_ref,
        validation_decision_artifact_hash_ref=case["decision_artifact"].hash_ref,
        evidence_artifact_hash_ref=case["evidence_artifact"].hash_ref,
        result_artifact_hash_ref=case["execution"].result_artifact.hash_ref,
        sealed_holdout_path=case["sealed_path"],
        created_at=CREATED_AT,
        created_by=CREATED_BY,
    )
    assert case["evaluator"].calls == 1  # still only the one legitimate decrypt.
    assert outcome.result.observed_metrics.values["mean_value"] == pytest.approx(receipt.aggregate_metrics["mean_value"])
    assert case["registry"].get(HYPOTHESIS_ID).state is HypothesisState.ACCEPTED


def test_restart_reload_reproduces_identical_result(tmp_path: Path) -> None:
    case = _full_case(tmp_path, holdout_values=[1.0, 2.0, 3.0])
    first = _evaluate(case)

    restarted_store = ArtifactStore(case["store"].root)
    restarted_registry = HypothesisRegistry(case["registry"].path)
    restarted_seals = HoldoutSealStore(restarted_registry)
    restarted_ledger = ResultLedger(case["ledger"].path)
    restarted_runner = FinalHoldoutRunner(
        registry=restarted_registry,
        seals=restarted_seals,
        keys=StaticKeys(),
        ledger=restarted_ledger,
        evaluator=HoldoutMeanEvaluator(),
        evaluator_artifact_path=EVALUATOR_ARTIFACT,
    )
    restarted_control = FinalHoldoutEvaluationControlV1(
        runner=restarted_runner, artifact_store=restarted_store, evaluator_registry=_evaluator_registry()
    )
    second = restarted_control.evaluate(
        HYPOTHESIS_ID,
        authorization_artifact_hash_ref=case["authorization_artifact"].hash_ref,
        validation_decision_artifact_hash_ref=case["decision_artifact"].hash_ref,
        evidence_artifact_hash_ref=case["evidence_artifact"].hash_ref,
        result_artifact_hash_ref=case["execution"].result_artifact.hash_ref,
        sealed_holdout_path=case["sealed_path"],
        created_at=CREATED_AT,
        created_by=CREATED_BY,
    )
    assert first.result == second.result
    assert first.result_artifact.hash_ref == second.result_artifact.hash_ref


# ---------------------------------------------------------------------------
# Immutable result persistence / reload
# ---------------------------------------------------------------------------


def test_persist_load_verified_cas_round_trip(tmp_path: Path) -> None:
    case = _full_case(tmp_path, holdout_values=[1.0, 2.0, 3.0])
    outcome = _evaluate(case)
    loaded = load_final_holdout_result_v1(
        outcome.result_artifact.hash_ref,
        artifact_store=case["store"],
        manifest=case["manifest"],
        authorization=case["authorization"],
        authorization_artifact_hash_ref=case["authorization_artifact"].hash_ref,
    )
    assert loaded == outcome.result


def test_corrupt_result_cas_fails_closed_on_reload(tmp_path: Path) -> None:
    case = _full_case(tmp_path, holdout_values=[1.0, 2.0, 3.0])
    outcome = _evaluate(case)
    resolved = case["store"].resolve_verified(outcome.result_artifact.hash_ref)
    Path(resolved.path).write_bytes(b"corrupted final holdout result bytes")
    with pytest.raises(ArtifactIntegrityError):
        load_final_holdout_result_v1(
            outcome.result_artifact.hash_ref,
            artifact_store=case["store"],
            manifest=case["manifest"],
            authorization=case["authorization"],
            authorization_artifact_hash_ref=case["authorization_artifact"].hash_ref,
        )


def test_conflicting_final_result_rejected(tmp_path: Path) -> None:
    # A self-consistent-but-untruthful result: observed metrics pass the
    # predeclared criterion (6.0 >= -1.0), yet the embedded decision and
    # top-level outcome falsely claim FAIL.
    import dataclasses

    from trademind.experiment_execution_contract import CriteriaDecisionV1

    case = _full_case(tmp_path, threshold=-1.0, holdout_values=[5.0, 6.0, 7.0])
    outcome = _evaluate(case)
    real = outcome.result
    lying_decision = CriteriaDecisionV1(
        schema_version=real.criteria_decision.schema_version,
        mode=real.criteria_decision.mode,
        passed=False,
        primary_metric=real.criteria_decision.primary_metric,
        primary_metric_value=real.criteria_decision.primary_metric_value,
        evaluations=tuple(
            dataclasses.replace(ev, passed=False) for ev in real.criteria_decision.evaluations
        ),
    )
    tampered = dataclasses.replace(
        real, outcome=ValidationOutcome.FAIL, criteria_decision=lying_decision
    )
    persisted = persist_final_holdout_result_v1(tampered, artifact_store=case["store"])
    with pytest.raises(FinalHoldoutEvaluationConflictError):
        load_final_holdout_result_v1(
            persisted.hash_ref,
            artifact_store=case["store"],
            manifest=case["manifest"],
            authorization=case["authorization"],
            authorization_artifact_hash_ref=case["authorization_artifact"].hash_ref,
        )


# ---------------------------------------------------------------------------
# Correct existing HypothesisRegistry transition
# ---------------------------------------------------------------------------


def test_registry_transitions_through_existing_lifecycle_states(tmp_path: Path) -> None:
    case = _full_case(tmp_path, holdout_values=[1.0, 2.0, 3.0])
    registry = case["registry"]
    assert registry.get(HYPOTHESIS_ID).state is HypothesisState.FROZEN
    _evaluate(case)
    final_state = registry.get(HYPOTHESIS_ID).state
    assert final_state in (HypothesisState.ACCEPTED, HypothesisState.REJECTED_FINAL)
    assert registry.family_status(case["manifest"].hypothesis_family_id)["holdout_consumed"] is True
    assert registry.family_status(case["manifest"].hypothesis_family_id)["terminal_state"] == final_state.value


def test_terminal_outcome_conflict_fails_closed(tmp_path: Path) -> None:
    case = _full_case(tmp_path, holdout_values=[1.0, 2.0, 3.0])
    _evaluate(case)
    # Force the registry into the OPPOSITE terminal state and confirm the
    # control layer refuses to silently agree with a conflicting outcome.
    with sqlite3.connect(case["registry"].path) as db:
        db.execute(
            "UPDATE hypotheses SET state=? WHERE hypothesis_id=?",
            (HypothesisState.REJECTED_FINAL.value, HYPOTHESIS_ID),
        )
        db.execute(
            "UPDATE hypothesis_families SET terminal_state=? WHERE family_id=?",
            (HypothesisState.REJECTED_FINAL.value, case["manifest"].hypothesis_family_id),
        )
    with pytest.raises(FinalHoldoutEvaluationConflictError):
        _evaluate(case)


# ---------------------------------------------------------------------------
# No model / network / trading side effects
# ---------------------------------------------------------------------------


def test_module_never_calls_provider_network_or_broker() -> None:
    import ast
    import inspect

    import trademind.final_holdout_evaluation as module

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
        "trademind.discovery.holdout_store",
    }
    assert not (imported & forbidden), imported & forbidden


def test_module_never_reimplements_sealer_or_ledger_or_creates_second_registry() -> None:
    import inspect

    import trademind.final_holdout_evaluation as module

    source = inspect.getsource(module)
    for forbidden_call in (
        "seal_bytes(",
        "seal_file(",
        "seal_and_quarantine(",
        "mark_isolated(",
        "decrypt_bytes(",
        "verify_envelope(",
        "verify_key(",
        "AESGCM(",
        "sqlite3.connect(",
        "CREATE TABLE",
    ):
        assert forbidden_call not in source, forbidden_call


def test_module_never_imports_task_engine() -> None:
    import ast
    import inspect

    import trademind.final_holdout_evaluation as module

    source = inspect.getsource(module)
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    assert "trademind.orchestrator.engine" not in imported
