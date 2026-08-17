"""Plumbing tests for the SMC pattern-journal EvaluatorBinding wiring.

IMPORTANT: every journal row in this file is synthetic fixture data written
to exercise the WIRING (EvaluatorBinding registration, chronological split
handling, metric propagation, criteria evaluation) end to end. None of it is
a real trading signal and none of the PASS/FAIL outcomes below constitute
scientific evidence about any strategy. See test_final_holdout_evaluation.py
for the underlying VALIDATION-phase pipeline's own test suite.

``FinalHoldoutEvaluationControlV1.evaluate`` (the SER8 lineage's final-holdout
entry point) is retired as a non-authoritative research lifecycle path -- see
that module's own docstring. The full-pipeline tests below therefore exercise
this evaluator's VALIDATION-phase wiring through the real, still-functioning
execution/evidence/decision/authorization steps, then assert that the
retired evaluate() call fails closed rather than reaching ACCEPTED/
REJECTED_FINAL through this lineage; ``test_holdout_evaluator_never_reads_
public_rows`` calls ``SMCPatternJournalHoldoutEvaluator.evaluate`` directly
(bypassing the control layer) since that behavior belongs to the evaluator
itself, not to the retired lifecycle plumbing.
"""

from __future__ import annotations

import hashlib
import io
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from trademind.discovery.holdout_runner import FinalHoldoutRunner
from trademind.discovery.holdout_sealer import FinalHoldoutSealer
from trademind.discovery.holdout_store import HoldoutSealStore
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
from trademind.discovery.result_ledger import ResultLedger
from trademind.discovery.split_engine import SplitPlan, chronological_split
from trademind.experiment_evidence import ExperimentEvidenceBuilderV1
from trademind.experiment_execution_contract import EvaluatorRegistry, ExecutionPhase
from trademind.experiment_execution_runtime import ExperimentExecutionRuntimeError, ExperimentExecutionRuntimeV1
from trademind.final_holdout_decision_gate import FinalHoldoutDecisionGateV1
from trademind.final_holdout_evaluation import (
    FinalHoldoutEvaluationControlV1,
    FinalHoldoutEvaluationNonAuthoritativeError,
)
from trademind.orchestrator.artifact_store import ArtifactStore
from trademind.signal_statistics_provenance import CodeProvenance
from trademind.smc_pattern_journal_evaluator import (
    EVALUATOR_ID,
    EVALUATOR_VERSION,
    HOLDOUT_EVALUATOR_ID,
    PRIMARY_METRIC,
    TEST_FAMILY,
    SMCPatternJournalConfigError,
    SMCPatternJournalExecutionEvaluator,
    SMCPatternJournalHoldoutEvaluator,
    binding,
)
from trademind.validation import validate_rows
from trademind.validation_decision import ValidationDecisionBuilderV1, ValidationOutcome

import trademind.smc_pattern_journal_evaluator as smc_evaluator_module

SPLIT_ROLE = "signal-journal"
RESULT_REF = f"sha256:{'a' * 64}"
PACKET_REF = f"sha256:{'b' * 64}"
PACKET_SEMANTIC_HASH = f"sha256:{'c' * 64}"
REQUEST_HASH = "d" * 64
CANDIDATE_ID = f"ssc-v2-{'e' * 64}"
HYPOTHESIS_ID = f"rpi-v1:{RESULT_REF}:0"
CREATED_AT = "2026-08-14T10:00:00+00:00"
CREATED_BY = "operator:test"
HORIZON = 1  # rows are 1h apart; validate_rows' reused _non_overlapping logic
# (one open fixed-horizon trade per symbol) would otherwise collapse this
# fixture's hourly rows into a single surviving trade at a larger horizon.
HOLDOUT_KEY = bytes(range(32))
HOLDOUT_KEY_ID = "holdout-key-v1"
# The holdout evaluator class is defined in the production module, not this
# test file, so the artifact FinalHoldoutRunner hashes/verifies must be that
# module's own source file.
EVALUATOR_ARTIFACT = Path(smc_evaluator_module.__file__).resolve()


class StaticKeys:
    def __init__(self, key: bytes = HOLDOUT_KEY, key_id: str = HOLDOUT_KEY_ID) -> None:
        self.key = key
        self.key_id = key_id

    def load_key(self, key_id: str) -> bytes:
        if key_id != self.key_id:
            raise KeyError(key_id)
        return self.key


def _code_provenance() -> CodeProvenance:
    return CodeProvenance(
        producer_name="trademind",
        producer_version="1.31.1",
        git_commit="f" * 40,
        revision_source="git_worktree",
    )


def _family() -> dict[str, object]:
    return {"candidate_id": CANDIDATE_ID, "claim": "smc pattern journal fixture claim"}


def _content() -> dict[str, object]:
    return {
        "family_definition": _family(),
        "proposal": {"falsifiable_claim": "smc pattern journal fixture claim"},
        "provenance": {"intake_id": HYPOTHESIS_ID},
    }


def _register(registry: HypothesisRegistry) -> None:
    registry.register(
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


def _journal_row(hour: int, *, action: str, outcome: str, atr: float, net_move: float) -> dict[str, str]:
    signal_time = (datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(hours=hour)).isoformat()
    return {
        "time": signal_time,
        "signal_time": signal_time,
        "symbol": "EURUSD",
        "timeframe": "H1",
        "action": action,
        f"outcome_{HORIZON}": outcome,
        "atr": str(atr),
        f"net_move_{HORIZON}": str(net_move),
    }


def _public_journal_rows(plan: SplitPlan) -> list[dict[str, str]]:
    # Fixture only: 7 discovery rows (mixed win/loss) + 2 validation rows (both wins).
    rows = []
    outcomes = ["WIN", "WIN", "WIN", "WIN", "WIN", "LOSS", "LOSS"]  # discovery: hours 0-6
    for hour, outcome in enumerate(outcomes):
        net = 2.0 if outcome == "WIN" else -1.0
        rows.append(_journal_row(hour, action="BUY", outcome=outcome, atr=1.0, net_move=net))
    for hour in (7, 8):  # validation: both wins
        rows.append(_journal_row(hour, action="BUY", outcome="WIN", atr=1.0, net_move=2.0))
    assert len(rows) == plan.public_count
    return rows


def _journal_csv_bytes(rows: list[dict[str, str]]) -> bytes:
    fieldnames = ["time", "signal_time", "symbol", "timeframe", "action", f"outcome_{HORIZON}", "atr", f"net_move_{HORIZON}"]
    lines = [",".join(fieldnames)]
    for row in rows:
        lines.append(",".join(row[name] for name in fieldnames))
    return ("\n".join(lines) + "\n").encode("utf-8")


def _dataset(store: ArtifactStore, plan: SplitPlan) -> DatasetArtifactV2:
    payload = _journal_csv_bytes(_public_journal_rows(plan))
    artifact = store.import_snapshot(io.BytesIO(payload), media_type="text/csv")
    return DatasetArtifactV2(
        role=SPLIT_ROLE,
        artifact_hash_ref=artifact.hash_ref,
        media_type=artifact.media_type,
        size_bytes=artifact.size_bytes,
    )


def _criteria(*, threshold: float = 0.0) -> EvaluationCriteriaV1:
    return EvaluationCriteriaV1(
        mode=CriteriaMode.ALL,
        criteria=(
            EvaluationCriterionV1(
                metric=PRIMARY_METRIC,
                operator=CriterionOperator.GREATER_THAN_OR_EQUAL,
                threshold=threshold,
            ),
        ),
    )


def _manifest(store: ArtifactStore, registry: HypothesisRegistry, *, threshold: float = 0.0) -> ExperimentManifestV2:
    record = registry.get(HYPOTHESIS_ID)
    plan = _split()
    result_ref = HYPOTHESIS_ID.removeprefix("rpi-v1:").rsplit(":", 1)[0]
    ds = _dataset(store, plan)
    return build_experiment_manifest_v2(
        artifact_store=store,
        hypothesis_id=HYPOTHESIS_ID,
        hypothesis_family_id=record.hypothesis_family_id,
        bound_hypothesis_content_hash=record.content_hash,
        proposal_provenance=ProposalIntakeProvenanceV1(
            intake_id=HYPOTHESIS_ID,
            execution_request_hash=REQUEST_HASH,
            authorization_id=1,
            task_id="smc-journal-task",
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
        test_family=TEST_FAMILY,
        primary_metric=PRIMARY_METRIC,
        evaluation_criteria=_criteria(threshold=threshold),
        alpha=0.05,
        q=0.10,
        minimum_effect_size=0.05,
        max_hypotheses_tests=20,
        trading_friction=None,  # journal values are already net of applied cost.
        deterministic_seed=None,
        code_provenance=_code_provenance(),
        semantic_parameters={"horizon": HORIZON, "candidate_minimum": 1, "research_minimum": 1},
        created_at=CREATED_AT,
        created_by=CREATED_BY,
    )


def _freeze(registry: HypothesisRegistry, store: ArtifactStore, manifest: ExperimentManifestV2) -> str:
    artifact = persist_experiment_manifest_v2(manifest, artifact_store=store)
    db = _transaction(registry)
    try:
        registry.freeze_manifest_v2_in_transaction(
            db, manifest_artifact_hash_ref=artifact.hash_ref, artifact_store=store
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
    return artifact.hash_ref


def _evaluator_registry() -> EvaluatorRegistry:
    return EvaluatorRegistry({TEST_FAMILY: binding()})


def _seal_and_isolate(tmp_path: Path, registry: HypothesisRegistry, manifest: ExperimentManifestV2, *, holdout_rows: list[dict[str, str]]):
    seals = HoldoutSealStore(registry)
    keys = StaticKeys()
    sealer = FinalHoldoutSealer(registry=registry, seals=seals, keys=keys)
    plaintext_path = tmp_path / "holdout.csv"
    plaintext_path.write_bytes(_journal_csv_bytes(holdout_rows))
    sealed_path = tmp_path / "sealed.json"
    receipt = sealer.seal_file(
        hypothesis_id=manifest.hypothesis_id,
        plaintext_path=plaintext_path,
        destination_path=sealed_path,
        key_id=HOLDOUT_KEY_ID,
        evaluator_id=HOLDOUT_EVALUATOR_ID,
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
    encoded = json.dumps(attestation, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()
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
    return seals, keys, sealed_path


def _run_pipeline(tmp_path: Path, *, holdout_rows: list[dict[str, str]], threshold: float = 0.0):
    registry = HypothesisRegistry(tmp_path / "registry.db")
    _register(registry)
    store = ArtifactStore(tmp_path / "artifacts")
    manifest = _manifest(store, registry, threshold=threshold)
    _freeze(registry, store, manifest)
    seals, keys, sealed_path = _seal_and_isolate(tmp_path, registry, manifest, holdout_rows=holdout_rows)

    runtime = ExperimentExecutionRuntimeV1(
        registry=registry,
        artifact_store=store,
        evaluator_registry=_evaluator_registry(),
        evaluator=SMCPatternJournalExecutionEvaluator(),
    )
    execution = runtime.execute(
        HYPOTHESIS_ID,
        execution_phase=ExecutionPhase.VALIDATION,
        execution_code_provenance=_code_provenance(),
        evaluator_friction=None,
        created_at=CREATED_AT,
        created_by=CREATED_BY,
    )
    evidence, evidence_artifact = ExperimentEvidenceBuilderV1(
        registry=registry, artifact_store=store, evaluator_registry=_evaluator_registry()
    ).build(HYPOTHESIS_ID, execution=execution, created_at=CREATED_AT, created_by=CREATED_BY)
    decision, decision_artifact = ValidationDecisionBuilderV1(
        registry=registry, artifact_store=store, evaluator_registry=_evaluator_registry()
    ).decide(
        HYPOTHESIS_ID,
        evidence_artifact_hash_ref=evidence_artifact.hash_ref,
        result_artifact_hash_ref=execution.result_artifact.hash_ref,
        created_at=CREATED_AT,
        created_by=CREATED_BY,
    )
    authorization, authorization_artifact = FinalHoldoutDecisionGateV1(
        registry=registry, artifact_store=store, evaluator_registry=_evaluator_registry()
    ).authorize(
        HYPOTHESIS_ID,
        validation_decision_artifact_hash_ref=decision_artifact.hash_ref,
        evidence_artifact_hash_ref=evidence_artifact.hash_ref,
        result_artifact_hash_ref=execution.result_artifact.hash_ref,
        created_at=CREATED_AT,
        created_by=CREATED_BY,
    )

    ledger = ResultLedger(tmp_path / "results.jsonl")
    holdout_evaluator = SMCPatternJournalHoldoutEvaluator(horizon=HORIZON, candidate_minimum=1, research_minimum=1)
    runner = FinalHoldoutRunner(
        registry=registry,
        seals=seals,
        keys=keys,
        ledger=ledger,
        evaluator=holdout_evaluator,
        evaluator_artifact_path=EVALUATOR_ARTIFACT,
    )
    control = FinalHoldoutEvaluationControlV1(
        runner=runner, artifact_store=store, evaluator_registry=_evaluator_registry()
    )
    # FinalHoldoutEvaluationControlV1.evaluate is retired (non-authoritative,
    # fail-closed) -- see trademind.final_holdout_evaluation's module
    # docstring. This proves the retirement holds even for this real
    # evaluator's own fully-valid, otherwise-successful authorization chain.
    with pytest.raises(FinalHoldoutEvaluationNonAuthoritativeError):
        control.evaluate(
            HYPOTHESIS_ID,
            authorization_artifact_hash_ref=authorization_artifact.hash_ref,
            validation_decision_artifact_hash_ref=decision_artifact.hash_ref,
            evidence_artifact_hash_ref=evidence_artifact.hash_ref,
            result_artifact_hash_ref=execution.result_artifact.hash_ref,
            sealed_holdout_path=sealed_path,
            created_at=CREATED_AT,
            created_by=CREATED_BY,
        )
    return registry, manifest, execution, decision


# ---------------------------------------------------------------------------
# Direct unit correctness: no double implementation of validate_rows
# ---------------------------------------------------------------------------


class _StubManifest:
    def __init__(self, **params: object) -> None:
        self.semantic_parameters = params


class _StubRow:
    def __init__(self, fields: dict[str, str]) -> None:
        self.fields = fields


def test_execution_evaluator_reuses_validate_rows_unchanged() -> None:
    rows = [
        _journal_row(0, action="BUY", outcome="WIN", atr=1.0, net_move=2.0),
        _journal_row(1, action="BUY", outcome="LOSS", atr=1.0, net_move=-1.0),
        _journal_row(2, action="BUY", outcome="WIN", atr=2.0, net_move=4.0),
    ]
    manifest = _StubManifest(horizon=HORIZON, candidate_minimum=1, research_minimum=1)
    evaluator = SMCPatternJournalExecutionEvaluator()
    observed = evaluator.evaluate(
        [_StubRow(row) for row in rows], manifest=manifest, execution_phase=ExecutionPhase.VALIDATION
    )
    expected = validate_rows(rows, HORIZON, candidate_minimum=1, research_minimum=1)
    assert observed["trades"] == pytest.approx(float(expected.total.trades))
    assert observed["win_rate"] == pytest.approx(expected.total.win_rate)
    assert observed["avg_net_atr"] == pytest.approx(expected.total.avg_net_atr)


def test_missing_semantic_parameters_fails_closed() -> None:
    evaluator = SMCPatternJournalExecutionEvaluator()
    with pytest.raises(SMCPatternJournalConfigError):
        evaluator.evaluate([], manifest=_StubManifest(), execution_phase=ExecutionPhase.VALIDATION)


def test_holdout_evaluator_rejects_invalid_thresholds() -> None:
    with pytest.raises(SMCPatternJournalConfigError):
        SMCPatternJournalHoldoutEvaluator(horizon=0, candidate_minimum=1, research_minimum=1)
    with pytest.raises(SMCPatternJournalConfigError):
        SMCPatternJournalHoldoutEvaluator(horizon=1, candidate_minimum=5, research_minimum=1)


# ---------------------------------------------------------------------------
# Full pipeline plumbing (fixture data only -- see module docstring)
# ---------------------------------------------------------------------------


def test_full_pipeline_pass_wiring(tmp_path: Path) -> None:
    holdout_rows = [
        _journal_row(9, action="BUY", outcome="WIN", atr=1.0, net_move=2.0),
        _journal_row(10, action="BUY", outcome="WIN", atr=1.0, net_move=2.0),
        _journal_row(11, action="BUY", outcome="LOSS", atr=1.0, net_move=-1.0),
    ]
    registry, manifest, execution, decision = _run_pipeline(
        tmp_path, holdout_rows=holdout_rows, threshold=0.0
    )
    del manifest
    assert decision.outcome is ValidationOutcome.PASS
    assert execution.result.evaluator_id == EVALUATOR_ID
    assert execution.result.evaluator_version == EVALUATOR_VERSION
    # The retired final-holdout entry point never advances the hypothesis:
    # it stays exactly where the frozen manifest left it (the SER8 lineage's
    # own VALIDATION_PASSED transition also lives inside the now-unreachable
    # evaluate() body, so it is never reached either).
    assert registry.get(HYPOTHESIS_ID).state is HypothesisState.FROZEN


def test_full_pipeline_fail_wiring(tmp_path: Path) -> None:
    # Validation still passes (its own rows are unaffected), authorizing the
    # gate; the final holdout's own rows would have failed the same
    # predeclared threshold, but the retired entry point never gets far
    # enough to evaluate them.
    holdout_rows = [
        _journal_row(9, action="BUY", outcome="LOSS", atr=1.0, net_move=-3.0),
        _journal_row(10, action="BUY", outcome="LOSS", atr=1.0, net_move=-4.0),
        _journal_row(11, action="BUY", outcome="LOSS", atr=1.0, net_move=-5.0),
    ]
    registry, manifest, execution, decision = _run_pipeline(
        tmp_path, holdout_rows=holdout_rows, threshold=0.0
    )
    del manifest, execution
    assert decision.outcome is ValidationOutcome.PASS
    assert registry.get(HYPOTHESIS_ID).state is HypothesisState.FROZEN


def test_holdout_evaluator_never_reads_public_rows() -> None:
    # Sanity: the holdout evaluation result must reflect ONLY the sealed
    # holdout rows, not the public discovery/validation journal used
    # earlier. Exercised directly against the evaluator (its own
    # evaluate(plaintext) method), bypassing the retired control layer,
    # since this behavior belongs to the evaluator, not to the lifecycle
    # plumbing around it.
    holdout_rows = [_journal_row(9, action="BUY", outcome="WIN", atr=1.0, net_move=10.0)]
    evaluator = SMCPatternJournalHoldoutEvaluator(horizon=HORIZON, candidate_minimum=1, research_minimum=1)
    observed = evaluator.evaluate(_journal_csv_bytes(holdout_rows))
    assert observed["avg_net_atr"] == pytest.approx(10.0)
    assert observed["trades"] == pytest.approx(1.0)


def test_evaluator_config_error_wrapped_by_runtime(tmp_path: Path) -> None:
    registry = HypothesisRegistry(tmp_path / "registry.db")
    _register(registry)
    store = ArtifactStore(tmp_path / "artifacts")
    record = registry.get(HYPOTHESIS_ID)
    plan = _split()
    result_ref = HYPOTHESIS_ID.removeprefix("rpi-v1:").rsplit(":", 1)[0]
    ds = _dataset(store, plan)
    manifest = build_experiment_manifest_v2(
        artifact_store=store,
        hypothesis_id=HYPOTHESIS_ID,
        hypothesis_family_id=record.hypothesis_family_id,
        bound_hypothesis_content_hash=record.content_hash,
        proposal_provenance=ProposalIntakeProvenanceV1(
            intake_id=HYPOTHESIS_ID,
            execution_request_hash=REQUEST_HASH,
            authorization_id=1,
            task_id="smc-journal-task",
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
        test_family=TEST_FAMILY,
        primary_metric=PRIMARY_METRIC,
        evaluation_criteria=_criteria(),
        alpha=0.05,
        q=0.10,
        minimum_effect_size=0.05,
        max_hypotheses_tests=20,
        trading_friction=None,
        deterministic_seed=None,
        code_provenance=_code_provenance(),
        semantic_parameters={"horizon": HORIZON},  # missing candidate_minimum/research_minimum.
        created_at=CREATED_AT,
        created_by=CREATED_BY,
    )
    _freeze(registry, store, manifest)
    runtime = ExperimentExecutionRuntimeV1(
        registry=registry,
        artifact_store=store,
        evaluator_registry=_evaluator_registry(),
        evaluator=SMCPatternJournalExecutionEvaluator(),
    )
    with pytest.raises(ExperimentExecutionRuntimeError, match="failed during execution"):
        runtime.execute(
            HYPOTHESIS_ID,
            execution_phase=ExecutionPhase.VALIDATION,
            execution_code_provenance=_code_provenance(),
            evaluator_friction=None,
            created_at=CREATED_AT,
            created_by=CREATED_BY,
        )
