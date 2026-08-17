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
from trademind.discovery.holdout_runner import FinalHoldoutRunner
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
)
from trademind.experiment_execution_runtime import ExperimentExecutionRuntimeV1
from trademind.final_holdout_decision_gate import FinalHoldoutDecisionGateV1
from trademind.final_holdout_evaluation import (
    FinalHoldoutEvaluationControlV1,
    FinalHoldoutEvaluationError,
    FinalHoldoutEvaluationNonAuthoritativeError,
    build_final_holdout_result_v1,
    load_final_holdout_result_v1,
    persist_final_holdout_result_v1,
    verify_final_holdout_result_v1,
)
from trademind.orchestrator.artifact_store import ArtifactStore
from trademind.signal_statistics_provenance import CodeProvenance
from trademind.validation_decision import ValidationDecisionBuilderV1

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
# Retirement: FinalHoldoutEvaluationControlV1.evaluate is a non-authoritative,
# fail-closed entry point. This module (and the rest of the six-file SER8
# parallel lineage it anchors) is retired in favor of the single
# authoritative production path in trademind.discovery
# (train_test_execution -> validation_execution -> holdout_trigger_bridge ->
# final_verdict_control). See the module docstring for the full rationale.
# The tests below replace the previous authorized-pass/fail, one-shot,
# tampered-seal, lineage-substitution, crash-recovery, and CAS round-trip
# tests, which exercised evaluate() actually succeeding -- behavior this
# module can no longer exhibit by design.
# ---------------------------------------------------------------------------


def test_evaluate_fails_closed_unconditionally_even_with_a_fully_valid_case(tmp_path: Path) -> None:
    # A completely valid, otherwise-successful setup (the exact fixture that
    # used to reach ACCEPTED) still cannot advance through this entry point:
    # the guard is categorical, not a side effect of some validation failure.
    case = _full_case(tmp_path, threshold=-1.0, holdout_values=[5.0, 6.0, 7.0])
    with pytest.raises(FinalHoldoutEvaluationNonAuthoritativeError):
        _evaluate(case)


def test_evaluate_fails_closed_before_any_argument_is_used_or_validated(tmp_path: Path) -> None:
    # Garbage/malformed arguments -- an unregistered hypothesis_id, bogus
    # artifact refs, a nonexistent sealed-holdout path -- still surface the
    # SAME retirement error, proving the guard precedes all internal
    # validation rather than merely coinciding with some other failure.
    case = _full_case(tmp_path, holdout_values=[1.0, 2.0, 3.0])
    with pytest.raises(FinalHoldoutEvaluationNonAuthoritativeError):
        case["control"].evaluate(
            "not-a-real-hypothesis-id",
            authorization_artifact_hash_ref="not-a-real-hash-ref",
            validation_decision_artifact_hash_ref="not-a-real-hash-ref",
            evidence_artifact_hash_ref="not-a-real-hash-ref",
            result_artifact_hash_ref="not-a-real-hash-ref",
            sealed_holdout_path=tmp_path / "does-not-exist.json",
            created_at="not-a-real-timestamp",
            created_by="operator:test",
        )


def test_evaluate_does_not_mutate_registry_state(tmp_path: Path) -> None:
    case = _full_case(tmp_path, holdout_values=[1.0, 2.0, 3.0])
    before = case["registry"].get(HYPOTHESIS_ID).state
    assert before is HypothesisState.FROZEN
    with pytest.raises(FinalHoldoutEvaluationNonAuthoritativeError):
        _evaluate(case)
    after = case["registry"].get(HYPOTHESIS_ID).state
    assert after is before
    assert after not in (
        HypothesisState.HOLDOUT_CONSUMED,
        HypothesisState.ACCEPTED,
        HypothesisState.REJECTED_FINAL,
    )


def test_evaluate_does_not_persist_any_artifact_or_consume_the_seal(tmp_path: Path) -> None:
    case = _full_case(tmp_path, holdout_values=[1.0, 2.0, 3.0])
    before = sorted(p for p in case["store"].root.rglob("*") if p.is_file())
    with pytest.raises(FinalHoldoutEvaluationNonAuthoritativeError):
        _evaluate(case)
    after = sorted(p for p in case["store"].root.rglob("*") if p.is_file())
    assert after == before
    # The one-shot holdout evaluator was never invoked and the ledger never
    # gained a FINAL_HOLDOUT_CLAIM/RESULT record: the blocked evaluate() call
    # never reached the runner (it never got past its own first statement).
    assert case["evaluator"].calls == 0
    assert not case["ledger"].path.exists() or case["ledger"].path.read_text(encoding="utf-8") == ""
    assert case["registry"].get(HYPOTHESIS_ID).state is HypothesisState.FROZEN


def test_evaluate_error_is_a_final_holdout_evaluation_error(tmp_path: Path) -> None:
    # The retirement error is still a FinalHoldoutEvaluationError, so any
    # legacy caller that only catches the module's own base exception is not
    # left with an unhandled, unexpected exception type.
    case = _full_case(tmp_path, holdout_values=[1.0, 2.0, 3.0])
    with pytest.raises(FinalHoldoutEvaluationError):
        _evaluate(case)


def test_module_still_exposes_its_data_structures_and_verification_helpers() -> None:
    # The module is retired only at its single write-entry-point; everything
    # else (CAS build/persist/load/verify helpers) remains importable and
    # usable for historical/audit reference, per the module docstring.
    for helper in (
        build_final_holdout_result_v1,
        load_final_holdout_result_v1,
        persist_final_holdout_result_v1,
        verify_final_holdout_result_v1,
    ):
        assert callable(helper)


def test_final_holdout_evaluation_control_v1_construction_still_succeeds(tmp_path: Path) -> None:
    # Constructing the (retired) control object itself is not blocked -- only
    # its evaluate() entry point is. This confirms the guard is scoped to the
    # one method identified as the lineage's sole registry-mutating write
    # path, not to the whole module.
    case = _full_case(tmp_path, holdout_values=[1.0, 2.0, 3.0])
    assert isinstance(case["control"], FinalHoldoutEvaluationControlV1)


# ---------------------------------------------------------------------------
# Precommitted criteria only
# ---------------------------------------------------------------------------


def test_build_final_holdout_result_has_no_ad_hoc_criteria_parameter() -> None:
    import inspect

    signature = inspect.signature(build_final_holdout_result_v1)
    forbidden_names = {"criteria", "threshold", "evaluation_criteria", "ad_hoc_criteria"}
    assert not (set(signature.parameters) & forbidden_names)


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
