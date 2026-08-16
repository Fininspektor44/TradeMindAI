#!/usr/bin/env python3
"""Fresh predeclared validation of ONE candidate hypothesis found via
discovery-only mining: symbol==".USTECHCASH", action=="SELL", horizon=12.

PREDECLARED (fixed before this script ever reads a validation row):
  filter:        symbol == ".USTECHCASH" AND action == "SELL"
  horizon:       12
  primary metric: avg_net_atr
  success rule:  PASS iff (non-overlapping validation trades >= 30) AND
                 (avg_net_atr >= 0.0); otherwise FAIL, or INSUFFICIENT_SAMPLE
                 if the sample-size criterion is what failed.

This is a genuinely new, more specific hypothesis than the one used in the
prior (failed, all-symbols) pilot -- it gets its own hypothesis_id, its own
family (family_definition includes symbol/action/horizon, so this exact
candidate can never claim a second holdout entitlement later), and its own
fresh registry/CAS root. It does not touch, modify, or reuse any state from
the prior pilot's registry.db/artifacts/sealed_holdout.json.

Uses the SAME authoritative real dataset and the SAME chronological split
boundaries as the completed pilot (recomputed deterministically by the same
existing adapter over the same source file -- guaranteed identical). The
predeclared symbol/action filter (non-outcome identification fields) is
applied to select which rows fall in the manifest's public dataset; row
membership in each segment is decided ONLY by which of the three ALREADY-
FIXED time windows a row's timestamp falls into. No outcome value
(action's *result*, win/loss, atr-normalized return) is read or computed
until after the manifest is frozen.

Orchestrates only existing, unmodified production primitives -- the same
five pipeline layers, the same SMCPatternJournalExecutionEvaluator /
SMCPatternJournalHoldoutEvaluator, the same FinalHoldoutSealer/
HoldoutSealStore/FinalHoldoutRunner -- exactly as run_real_discovery_pilot_v1.py.
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

from trademind.discovery.holdout_runner import FinalHoldoutRunner, HoldoutRunError  # noqa: E402
from trademind.discovery.holdout_sealer import FinalHoldoutSealer, HoldoutSealerError  # noqa: E402
from trademind.discovery.holdout_store import HoldoutSealError, HoldoutSealStore  # noqa: E402
from trademind.discovery.hypothesis_registry import HypothesisRegistry, RegistryError  # noqa: E402
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
from trademind.discovery.split_engine import SplitPlan  # noqa: E402
from trademind.experiment_evidence import ExperimentEvidenceBuilderV1  # noqa: E402
from trademind.experiment_execution_contract import EvaluatorRegistry, ExecutionPhase  # noqa: E402
from trademind.experiment_execution_runtime import ExperimentExecutionRuntimeV1  # noqa: E402
from trademind.final_holdout_decision_gate import FinalHoldoutDecisionGateV1  # noqa: E402
from trademind.final_holdout_evaluation import FinalHoldoutEvaluationControlV1  # noqa: E402
from trademind.orchestrator.artifact_store import ArtifactStore  # noqa: E402
from trademind.signal_statistics_provenance import CodeProvenance  # noqa: E402
from trademind.smc_journal_dataset_import import prepare_smc_journal_dataset  # noqa: E402
from trademind.smc_pattern_journal_evaluator import (  # noqa: E402
    HOLDOUT_EVALUATOR_ID,
    PRIMARY_METRIC,
    TEST_FAMILY,
    SMCPatternJournalExecutionEvaluator,
    SMCPatternJournalHoldoutEvaluator,
)
import trademind.smc_pattern_journal_evaluator as smc_evaluator_module  # noqa: E402
from trademind.validation_decision import ValidationDecisionBuilderV1, ValidationOutcome  # noqa: E402

# --- Predeclared, fixed before any validation row is read. ---
FILTER_SYMBOL = ".USTECHCASH"
FILTER_ACTION = "SELL"
HORIZON = 12
MINIMUM_SAMPLE = 30
CANDIDATE_MINIMUM = 30   # existing production default, unchanged.
RESEARCH_MINIMUM = 300   # existing production default, unchanged.
SPLIT_ROLE = "signal-journal"
CREATED_AT = datetime(2026, 8, 16, 0, 0, 0, tzinfo=timezone.utc).isoformat()


def _sha256_hex(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _classify_split(
    filtered_rows: list[dict[str, str]], *, original_plan: SplitPlan
) -> tuple[list[dict[str, str]], list[dict[str, str]], list[dict[str, str]], SplitPlan]:
    """Same chronological split boundaries as the original pilot; row counts
    reflect only the predeclared symbol/action filter."""
    discovery_end = datetime.fromisoformat(original_plan.discovery_end)
    validation_start = datetime.fromisoformat(original_plan.validation_start)
    validation_end = datetime.fromisoformat(original_plan.validation_end)
    holdout_start = datetime.fromisoformat(original_plan.holdout_start)

    discovery_rows, validation_rows, holdout_rows = [], [], []
    for row in filtered_rows:
        row_time = datetime.fromisoformat(row["time"])
        if row_time <= discovery_end:
            discovery_rows.append(row)
        elif validation_start <= row_time <= validation_end:
            validation_rows.append(row)
        elif row_time >= holdout_start:
            holdout_rows.append(row)
        # A timestamp between discovery_end and validation_start, or between
        # validation_end and holdout_start, cannot occur: those gaps are
        # empty by construction of the original SplitPlan.

    if not discovery_rows or not validation_rows or not holdout_rows:
        raise SystemExit(
            "PRACTICAL BLOCKER: the predeclared filter leaves an empty discovery, "
            "validation, or holdout segment under the existing split boundaries "
            f"(discovery={len(discovery_rows)}, validation={len(validation_rows)}, "
            f"holdout={len(holdout_rows)}); a valid SplitPlan cannot be built."
        )

    plan = SplitPlan(
        total_rows=len(discovery_rows) + len(validation_rows) + len(holdout_rows),
        discovery_count=len(discovery_rows),
        validation_count=len(validation_rows),
        holdout_count=len(holdout_rows),
        discovery_start=discovery_rows[0]["time"],
        discovery_end=discovery_rows[-1]["time"],
        validation_start=validation_rows[0]["time"],
        validation_end=validation_rows[-1]["time"],
        holdout_start=holdout_rows[0]["time"],
        holdout_end=holdout_rows[-1]["time"],
    )
    return discovery_rows, validation_rows, holdout_rows, plan


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--workdir", required=True, type=Path)
    parser.add_argument("--created-by", default="operator:candidate-validation-ustechcash-sell-h12")
    args = parser.parse_args()

    workdir: Path = args.workdir.expanduser().resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    source: Path = args.source.expanduser().resolve()
    report: dict[str, object] = {
        "source_path": str(source),
        "predeclared_filter": {"symbol": FILTER_SYMBOL, "action": FILTER_ACTION, "horizon": HORIZON},
        "predeclared_rule": f"PASS iff trades >= {MINIMUM_SAMPLE} AND avg_net_atr >= 0.0",
    }

    # --- Same adapter, same source -> deterministically identical split boundaries. ---
    prepared_path = workdir / "prepared_journal.csv"
    if prepared_path.exists():
        prepared_path.unlink()
    summary = prepare_smc_journal_dataset(source, prepared_path)
    original_plan = summary.split_plan
    report["horizon_resolved_from_header"] = summary.horizon
    assert summary.horizon == HORIZON, "resolved horizon does not match the predeclared horizon"

    # --- Mechanical filter on non-outcome fields only (symbol, action, time). ---
    with prepared_path.open("r", encoding="utf-8", newline="") as handle:
        all_rows = list(csv.DictReader(handle))
    filtered_rows = [
        row for row in all_rows
        if row["symbol"].strip().upper() == FILTER_SYMBOL.upper() and row["action"] == FILTER_ACTION
    ]
    report["filtered_row_count_all_windows"] = len(filtered_rows)

    discovery_rows, validation_rows, holdout_rows, plan = _classify_split(
        filtered_rows, original_plan=original_plan
    )
    report["discovery_rows_filtered"] = len(discovery_rows)
    report["validation_rows_filtered"] = len(validation_rows)
    report["holdout_rows_filtered"] = len(holdout_rows)

    public_rows = discovery_rows + validation_rows
    fieldnames = list(all_rows[0].keys())
    public_path = workdir / "public_journal_filtered.csv"
    holdout_plaintext_path = workdir / "holdout_plaintext_filtered.csv"
    for path, subset in ((public_path, public_rows), (holdout_plaintext_path, holdout_rows)):
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(subset)

    # --- Fresh registry + CAS for this NEW, distinct hypothesis. ---
    registry = HypothesisRegistry(workdir / "registry.db")
    store = ArtifactStore(workdir / "artifacts")

    label = f"{TEST_FAMILY}:{FILTER_SYMBOL}:{FILTER_ACTION}:h{HORIZON}"
    result_ref = f"sha256:{_sha256_hex(f'real-candidate-validation-v1:{label}:{source.name}')}"
    hypothesis_id = f"rpi-v1:{result_ref}:0"
    candidate_id = f"ssc-v2-{_sha256_hex(f'candidate:{label}:{source.name}')}"
    packet_ref = f"sha256:{_sha256_hex(f'packet:{label}:{source.name}')}"
    packet_semantic_hash = f"sha256:{_sha256_hex(f'packet-semantic:{label}:{source.name}')}"
    request_hash = _sha256_hex(f"request:{label}:{source.name}")

    # New, distinct family: this exact candidate (symbol+action+horizon) can
    # never claim a second holdout entitlement later.
    family_definition = {
        "strategy": TEST_FAMILY,
        "symbol": FILTER_SYMBOL,
        "action": FILTER_ACTION,
        "horizon": HORIZON,
        "discovered_via": "discovery-only exploratory mining of the same real signal journal",
    }
    falsifiable_claim = (
        f"For {FILTER_SYMBOL} {FILTER_ACTION} signals (horizon={HORIZON} bars), identified via "
        "discovery-only exploratory mining of the same real signal journal used by the completed "
        "pilot, the aggregate ATR-normalized net return on the VALIDATION partition -- using the "
        "existing trademind.validation.validate_rows methodology -- is non-negative "
        f"(avg_net_atr >= 0.0), given at least {MINIMUM_SAMPLE} non-overlapping validation trades."
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

    with public_path.open("rb") as handle:
        dataset_artifact = store.import_snapshot(handle, media_type="text/csv")
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

    # Two predeclared criteria, ALL mode: distinguishes INSUFFICIENT_SAMPLE
    # (trades criterion fails) from a genuine FAIL (avg_net_atr criterion
    # fails with sufficient sample) without any new architecture -- both are
    # ordinary CriterionEvaluation entries in the existing CriteriaDecisionV1.
    criteria = EvaluationCriteriaV1(
        mode=CriteriaMode.ALL,
        criteria=(
            EvaluationCriterionV1(
                metric="trades", operator=CriterionOperator.GREATER_THAN_OR_EQUAL, threshold=MINIMUM_SAMPLE
            ),
            EvaluationCriterionV1(
                metric=PRIMARY_METRIC, operator=CriterionOperator.GREATER_THAN_OR_EQUAL, threshold=0.0
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
            task_id="candidate-validation-ustechcash-sell-h12",
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
            "horizon": HORIZON,
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

    report["hypothesis_id"] = hypothesis_id
    report["falsifiable_claim"] = falsifiable_claim
    report["manifest_semantic_hash"] = manifest.manifest_semantic_hash
    report["manifest_artifact_hash_ref"] = manifest_artifact.hash_ref
    report["dataset_artifact_hash_ref"] = dataset_artifact.hash_ref
    report["predeclared_criteria"] = criteria.to_payload()
    report["hypothesis_frozen_before_validation"] = True

    # --- Seal the real (filtered) final holdout now, while still FROZEN. ---
    seals = HoldoutSealStore(registry)
    holdout_key = os.urandom(32)

    class _StaticKey:
        def load_key(self, key_id: str) -> bytes:
            if key_id != "candidate-holdout-key-v1":
                raise KeyError(key_id)
            return holdout_key

    sealer = FinalHoldoutSealer(registry=registry, seals=seals, keys=_StaticKey())
    sealed_path = workdir / "sealed_holdout.json"
    evaluator_artifact_path = Path(smc_evaluator_module.__file__).resolve()
    try:
        seal_receipt = sealer.seal_file(
            hypothesis_id=hypothesis_id,
            plaintext_path=holdout_plaintext_path,
            destination_path=sealed_path,
            key_id="candidate-holdout-key-v1",
            evaluator_id=HOLDOUT_EVALUATOR_ID,
            evaluator_artifact_path=evaluator_artifact_path,
        )
    except (HoldoutSealError, HoldoutSealerError, RegistryError) as exc:
        report["holdout_still_legally_consumable"] = False
        report["architectural_blocker"] = f"holdout could not be sealed: {exc}"
        _emit(report)
        return 1

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
    holdout_plaintext_path.unlink()
    report["holdout_still_legally_consumable"] = True

    # --- Execution (VALIDATION phase) -> Evidence -> Validation Decision. ---
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
    observed = dict(execution.result.observed_metrics.values)
    report["validation_observed_metrics"] = observed
    report["non_overlapping_validation_trades"] = observed.get("trades")
    report["validation_avg_net_atr"] = observed.get("avg_net_atr")
    report["validation_win_rate"] = observed.get("win_rate")

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

    trades_eval = next(e for e in decision.criteria_decision.evaluations if e.metric == "trades")
    if not trades_eval.passed:
        validation_verdict = "INSUFFICIENT_SAMPLE"
    elif decision.outcome is ValidationOutcome.PASS:
        validation_verdict = "PASS"
    else:
        validation_verdict = "FAIL"
    report["validation_verdict"] = validation_verdict
    report["validation_decision_artifact_hash_ref"] = decision_artifact.hash_ref
    report["evidence_artifact_hash_ref"] = evidence_artifact.hash_ref
    report["execution_result_artifact_hash_ref"] = execution.result_artifact.hash_ref

    if decision.outcome is not ValidationOutcome.PASS:
        report["final_holdout_accessed"] = False
        report["holdout_authorization_created"] = False
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
    report["holdout_authorization_created"] = True
    report["authorization_artifact_hash_ref"] = authorization_artifact.hash_ref

    ledger = ResultLedger(workdir / "results.jsonl")
    holdout_evaluator = SMCPatternJournalHoldoutEvaluator(
        horizon=HORIZON, candidate_minimum=CANDIDATE_MINIMUM, research_minimum=RESEARCH_MINIMUM
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
    try:
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
    except HoldoutRunError as exc:
        report["final_holdout_accessed"] = False
        report["architectural_blocker"] = f"final holdout could not be lawfully consumed: {exc}"
        _emit(report)
        return 1

    report["final_holdout_accessed"] = True
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
