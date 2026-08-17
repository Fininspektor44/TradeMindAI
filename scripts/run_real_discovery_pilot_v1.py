#!/usr/bin/env python3
"""Run the completed scientific discovery pipeline on one real signal journal.

This orchestrates ONLY existing, unmodified production primitives:
  smc_journal_dataset_import.prepare_smc_journal_dataset (schema/order adapter)
  -> HypothesisRegistry / ExperimentManifestV2 (predeclared hypothesis+criteria+split)
  -> ExperimentExecutionRuntimeV1 (VALIDATION phase, SMCPatternJournalExecutionEvaluator)
  -> ExperimentEvidenceBuilderV1
  -> ValidationDecisionBuilderV1
  -> FinalHoldoutSealer / HoldoutSealStore (real one-shot holdout, if validation PASS)
  -> FinalHoldoutDecisionGateV1
  -> FinalHoldoutEvaluationControlV1 (one-shot consumption, SMCPatternJournalHoldoutEvaluator)

The hypothesis, criteria, thresholds, and split are all fixed BEFORE any
outcome value is read: candidate_minimum/research_minimum use the existing
production defaults from signal_statistics_report.py (30/300), the single
predeclared criterion (avg_net_atr >= 0) mirrors validate_rows' own existing
internal stability gate, the chronological split uses the split_engine's own
default 60/20/20 proportions, and the evaluation horizon is chosen only from
which outcome_<horizon> *columns* exist in the source header (never from
outcome values). No code here computes, prints, or reacts to any aggregate
metric until after the manifest is frozen and the evaluation criteria are
committed to Verified CAS.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from trademind.discovery.holdout_runner import FinalHoldoutRunner  # noqa: E402
from trademind.discovery.holdout_sealer import FinalHoldoutSealer  # noqa: E402
from trademind.discovery.holdout_store import HoldoutSealStore  # noqa: E402
from trademind.discovery.hypothesis_registry import HypothesisRegistry  # noqa: E402
from trademind.discovery.manifest import (  # noqa: E402
    CriteriaMode,
    CriterionOperator,
    DatasetArtifactV2,
    EvaluationCriteriaV1,
    EvaluationCriterionV1,
    ProposalIntakeProvenanceV1,
    build_experiment_manifest_v2,
    persist_experiment_manifest_v2,
)
from trademind.discovery.result_ledger import ResultLedger  # noqa: E402
from trademind.experiment_evidence import ExperimentEvidenceBuilderV1  # noqa: E402
from trademind.experiment_execution_contract import (  # noqa: E402
    EvaluatorRegistry,
    ExecutionPhase,
)
from trademind.experiment_execution_runtime import ExperimentExecutionRuntimeV1  # noqa: E402
from trademind.final_holdout_decision_gate import FinalHoldoutDecisionGateV1  # noqa: E402
from trademind.final_holdout_evaluation import FinalHoldoutEvaluationControlV1  # noqa: E402
from trademind.orchestrator.artifact_store import ArtifactStore  # noqa: E402
from trademind.signal_statistics_provenance import CodeProvenance  # noqa: E402
from trademind.smc_journal_dataset_import import (  # noqa: E402
    prepare_smc_journal_dataset,
)
from trademind.smc_pattern_journal_evaluator import (  # noqa: E402
    HOLDOUT_EVALUATOR_ID,
    PRIMARY_METRIC,
    TEST_FAMILY,
    SMCPatternJournalExecutionEvaluator,
    SMCPatternJournalHoldoutEvaluator,
)
import trademind.smc_pattern_journal_evaluator as smc_evaluator_module  # noqa: E402
from trademind.validation_decision import ValidationDecisionBuilderV1, ValidationOutcome  # noqa: E402

# Predeclared, fixed before any outcome value is inspected.
CANDIDATE_MINIMUM = 30  # signal_statistics_report.py's own production default.
RESEARCH_MINIMUM = 300  # signal_statistics_report.py's own production default.
CRITERION_THRESHOLD = 0.0  # mirrors validate_rows' own "avg_net_atr <= 0" stability gate.
SPLIT_ROLE = "signal-journal"
CREATED_AT = datetime(2026, 8, 16, 0, 0, 0, tzinfo=timezone.utc).isoformat()


def _sha256_hex(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _split_journal_csv(prepared_path: Path, *, public_count: int, workdir: Path) -> tuple[Path, Path]:
    with prepared_path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = reader.fieldnames or []
        rows = list(reader)
    if len(rows) != public_count + (len(rows) - public_count):
        raise AssertionError("unreachable")  # defensive; slicing below is exact either way.

    public_rows = rows[:public_count]
    holdout_rows = rows[public_count:]

    public_path = workdir / "public_journal.csv"
    holdout_path = workdir / "holdout_plaintext.csv"
    for path, subset in ((public_path, public_rows), (holdout_path, holdout_rows)):
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(subset)
    return public_path, holdout_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path, help="Real signal journal CSV (read-only)")
    parser.add_argument("--workdir", required=True, type=Path, help="Scratch directory for registry/CAS/derived files")
    parser.add_argument("--created-by", default="operator:real-discovery-pilot-v1")
    args = parser.parse_args()

    workdir: Path = args.workdir.expanduser().resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    source: Path = args.source.expanduser().resolve()

    report: dict[str, object] = {"source_path": str(source)}

    # --- Phase A: schema/order verification + import adapter (no outcome peek) ---
    prepared_path = workdir / "prepared_journal.csv"
    if prepared_path.exists():
        prepared_path.unlink()
    summary = prepare_smc_journal_dataset(source, prepared_path)
    plan = summary.split_plan
    report["source_rows"] = summary.row_count
    report["unique_timestamps"] = summary.unique_timestamp_count
    report["horizon"] = summary.horizon
    report["discovery_rows"] = plan.discovery_count
    report["validation_rows"] = plan.validation_count
    report["final_holdout_rows"] = plan.holdout_count

    public_count = plan.discovery_count + plan.validation_count
    public_path, holdout_plaintext_path = _split_journal_csv(
        prepared_path, public_count=public_count, workdir=workdir
    )

    # --- Registry + Verified CAS ---
    registry = HypothesisRegistry(workdir / "registry.db")
    store = ArtifactStore(workdir / "artifacts")

    result_ref_hash = _sha256_hex(f"real-discovery-pilot-v1:{TEST_FAMILY}:{source.name}")
    result_ref = f"sha256:{result_ref_hash}"
    hypothesis_id = f"rpi-v1:{result_ref}:0"
    candidate_id = f"ssc-v2-{_sha256_hex(f'candidate:{TEST_FAMILY}:{source.name}')}"
    packet_ref = f"sha256:{_sha256_hex(f'packet:{TEST_FAMILY}:{source.name}')}"
    packet_semantic_hash = f"sha256:{_sha256_hex(f'packet-semantic:{TEST_FAMILY}:{source.name}')}"
    request_hash = _sha256_hex(f"request:{TEST_FAMILY}:{source.name}")

    # --- Predeclared hypothesis (BEFORE any outcome is read) ---
    family_definition = {
        "strategy": TEST_FAMILY,
        "instrument_scope": "all symbols present in the supplied real signal journal",
        "signal_source": "existing production SMC-pattern signal detector output, "
        "as already logged to the real-time signal journal",
    }
    falsifiable_claim = (
        "Across the chronologically split discovery, validation, and final-holdout "
        f"windows of this real signal journal, the aggregate ATR-normalized net return "
        f"of the already-generated SMC-pattern trading signals (horizon={summary.horizon} bars, "
        "using the existing trademind.validation.validate_rows methodology with production "
        f"default sample thresholds candidate_minimum={CANDIDATE_MINIMUM}/"
        f"research_minimum={RESEARCH_MINIMUM}) is non-negative (avg_net_atr >= "
        f"{CRITERION_THRESHOLD})."
    )
    content_definition = {
        "family_definition": family_definition,
        "proposal": {"falsifiable_claim": falsifiable_claim},
        "provenance": {"intake_id": hypothesis_id},
    }
    registry.register(
        hypothesis_id=hypothesis_id,
        family_definition=family_definition,
        content_definition=content_definition,
    )
    record = registry.get(hypothesis_id)

    dataset_artifact = store.import_snapshot(
        public_path.open("rb"), media_type="text/csv"
    )
    dataset = DatasetArtifactV2(
        role=SPLIT_ROLE,
        artifact_hash_ref=dataset_artifact.hash_ref,
        media_type=dataset_artifact.media_type,
        size_bytes=dataset_artifact.size_bytes,
    )

    code_provenance = CodeProvenance(
        producer_name="trademind",
        producer_version="1.31.1",
        git_commit=os.environ.get("TRADEMIND_PILOT_GIT_COMMIT", "f" * 40),
        revision_source="git_worktree",
    )

    criteria = EvaluationCriteriaV1(
        mode=CriteriaMode.ALL,
        criteria=(
            EvaluationCriterionV1(
                metric=PRIMARY_METRIC,
                operator=CriterionOperator.GREATER_THAN_OR_EQUAL,
                threshold=CRITERION_THRESHOLD,
            ),
        ),
    )

    manifest = build_experiment_manifest_v2(
        artifact_store=store,
        hypothesis_id=hypothesis_id,
        hypothesis_family_id=record.hypothesis_family_id,
        bound_hypothesis_content_hash=record.content_hash,
        proposal_provenance=ProposalIntakeProvenanceV1(
            intake_id=hypothesis_id,
            execution_request_hash=request_hash,
            authorization_id=1,
            task_id="real-discovery-pilot-v1",
            task_revision=1,
            packet_artifact_hash_ref=packet_ref,
            packet_semantic_hash=packet_semantic_hash,
            result_artifact_hash_ref=result_ref,
            proposal_index=0,
            candidate_id=candidate_id,
        ),
        datasets=(dataset,),
        split_plan=plan,
        split_dataset_role=SPLIT_ROLE,
        test_family=TEST_FAMILY,
        primary_metric=PRIMARY_METRIC,
        evaluation_criteria=criteria,
        alpha=0.05,
        q=0.10,
        minimum_effect_size=0.0,
        max_hypotheses_tests=1,
        trading_friction=None,
        deterministic_seed=None,
        code_provenance=code_provenance,
        semantic_parameters={
            "horizon": summary.horizon,
            "candidate_minimum": CANDIDATE_MINIMUM,
            "research_minimum": RESEARCH_MINIMUM,
        },
        created_at=CREATED_AT,
        created_by=args.created_by,
    )
    manifest_artifact = persist_experiment_manifest_v2(manifest, artifact_store=store)
    db = sqlite3.connect(registry.path, timeout=30.0)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    try:
        db.execute("BEGIN IMMEDIATE")
        registry.freeze_manifest_v2_in_transaction(
            db, manifest_artifact_hash_ref=manifest_artifact.hash_ref, artifact_store=store
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

    report["manifest_semantic_hash"] = manifest.manifest_semantic_hash
    report["manifest_artifact_hash_ref"] = manifest_artifact.hash_ref
    report["dataset_artifact_hash_ref"] = dataset_artifact.hash_ref
    report["hypothesis_id"] = hypothesis_id
    report["falsifiable_claim"] = falsifiable_claim
    report["predeclared_criteria"] = criteria.to_payload()
    report["semantic_parameters"] = dict(manifest.semantic_parameters)

    # --- Seal the real final holdout NOW, while still FROZEN ---
    seals = HoldoutSealStore(registry)
    holdout_key = os.urandom(32)

    class _StaticKey:
        def load_key(self, key_id: str) -> bytes:
            if key_id != "pilot-holdout-key-v1":
                raise KeyError(key_id)
            return holdout_key

    sealer = FinalHoldoutSealer(registry=registry, seals=seals, keys=_StaticKey())
    sealed_path = workdir / "sealed_holdout.json"
    evaluator_artifact_path = Path(smc_evaluator_module.__file__).resolve()
    seal_receipt = sealer.seal_file(
        hypothesis_id=hypothesis_id,
        plaintext_path=holdout_plaintext_path,
        destination_path=sealed_path,
        key_id="pilot-holdout-key-v1",
        evaluator_id=HOLDOUT_EVALUATOR_ID,
        evaluator_artifact_path=evaluator_artifact_path,
    )
    isolation_attestation = {
        "schema_version": "final-holdout-isolation-attestation-v1",
        "hypothesis_id": seal_receipt.hypothesis_id,
        "hypothesis_family_id": seal_receipt.hypothesis_family_id,
        "manifest_hash": seal_receipt.manifest_hash,
        "envelope_hash": seal_receipt.envelope_hash,
        "evaluator_id": seal_receipt.evaluator_id,
        "evaluator_hash": seal_receipt.evaluator_hash,
        "plaintext_sha256": seal_receipt.plaintext_sha256,
        "plaintext_size": seal_receipt.plaintext_size,
        "public_max_time": plan.validation_end,
        "holdout_start_time": plan.holdout_start,
        "holdout_end_time": plan.holdout_end,
        "public_row_count": plan.public_count,
        "holdout_row_count": plan.holdout_count,
    }
    encoded_attestation = json.dumps(
        isolation_attestation, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")
    isolation_hash = hashlib.sha256(encoded_attestation).hexdigest()
    seals.mark_isolated(
        hypothesis_id,
        isolation_receipt_hash=isolation_hash,
        public_max_time=plan.validation_end,
        holdout_start_time=plan.holdout_start,
        holdout_end_time=plan.holdout_end,
        public_row_count=plan.public_count,
        holdout_row_count=plan.holdout_count,
    )
    # Minimize the plaintext exposure window now that it is durably sealed.
    holdout_plaintext_path.unlink()

    # --- Execution (VALIDATION phase) -> Evidence -> Validation Decision ---
    evaluator_registry = EvaluatorRegistry({TEST_FAMILY: smc_evaluator_module.binding()})
    runtime = ExperimentExecutionRuntimeV1(
        registry=registry,
        artifact_store=store,
        evaluator_registry=evaluator_registry,
        evaluator=SMCPatternJournalExecutionEvaluator(),
    )
    execution = runtime.execute(
        hypothesis_id,
        execution_phase=ExecutionPhase.VALIDATION,
        execution_code_provenance=code_provenance,
        evaluator_friction=None,
        created_at=CREATED_AT,
        created_by=args.created_by,
    )
    report["validation_observed_metrics"] = dict(execution.result.observed_metrics.values)

    evidence, evidence_artifact = ExperimentEvidenceBuilderV1(
        registry=registry, artifact_store=store, evaluator_registry=evaluator_registry
    ).build(hypothesis_id, execution=execution, created_at=CREATED_AT, created_by=args.created_by)

    decision, decision_artifact = ValidationDecisionBuilderV1(
        registry=registry, artifact_store=store, evaluator_registry=evaluator_registry
    ).decide(
        hypothesis_id,
        evidence_artifact_hash_ref=evidence_artifact.hash_ref,
        result_artifact_hash_ref=execution.result_artifact.hash_ref,
        created_at=CREATED_AT,
        created_by=args.created_by,
    )
    report["validation_verdict"] = decision.outcome.value
    report["validation_decision_artifact_hash_ref"] = decision_artifact.hash_ref
    report["evidence_artifact_hash_ref"] = evidence_artifact.hash_ref
    report["execution_result_artifact_hash_ref"] = execution.result_artifact.hash_ref

    if decision.outcome is not ValidationOutcome.PASS:
        report["final_holdout_consumed"] = False
        report["final_verdict"] = "NOT_RUN"
        report["final_holdout_observed_metrics"] = None
        report["final_hypothesis_state"] = registry.get(hypothesis_id).state.value
        _emit(report)
        return 0

    authorization, authorization_artifact = FinalHoldoutDecisionGateV1(
        registry=registry, artifact_store=store, evaluator_registry=evaluator_registry
    ).authorize(
        hypothesis_id,
        validation_decision_artifact_hash_ref=decision_artifact.hash_ref,
        evidence_artifact_hash_ref=evidence_artifact.hash_ref,
        result_artifact_hash_ref=execution.result_artifact.hash_ref,
        created_at=CREATED_AT,
        created_by=args.created_by,
    )
    report["authorization_artifact_hash_ref"] = authorization_artifact.hash_ref

    ledger = ResultLedger(workdir / "results.jsonl")
    holdout_evaluator = SMCPatternJournalHoldoutEvaluator(
        horizon=summary.horizon,
        candidate_minimum=CANDIDATE_MINIMUM,
        research_minimum=RESEARCH_MINIMUM,
    )
    runner = FinalHoldoutRunner(
        registry=registry,
        seals=seals,
        keys=_StaticKey(),
        ledger=ledger,
        evaluator=holdout_evaluator,
        evaluator_artifact_path=evaluator_artifact_path,
    )
    control = FinalHoldoutEvaluationControlV1(
        runner=runner, artifact_store=store, evaluator_registry=evaluator_registry
    )
    outcome = control.evaluate(
        hypothesis_id,
        authorization_artifact_hash_ref=authorization_artifact.hash_ref,
        validation_decision_artifact_hash_ref=decision_artifact.hash_ref,
        evidence_artifact_hash_ref=evidence_artifact.hash_ref,
        result_artifact_hash_ref=execution.result_artifact.hash_ref,
        sealed_holdout_path=sealed_path,
        created_at=CREATED_AT,
        created_by=args.created_by,
    )
    report["final_holdout_consumed"] = True
    report["final_verdict"] = outcome.result.outcome.value
    report["final_holdout_observed_metrics"] = dict(outcome.result.observed_metrics.values)
    report["final_holdout_result_artifact_hash_ref"] = outcome.result_artifact.hash_ref
    report["final_hypothesis_state"] = registry.get(hypothesis_id).state.value
    _emit(report)
    return 0


def _emit(report: dict[str, object]) -> None:
    print(json.dumps(report, indent=2, sort_keys=True, default=str))


if __name__ == "__main__":
    raise SystemExit(main())
