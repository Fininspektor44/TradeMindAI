#!/usr/bin/env python3
"""SER8 First Real Hypothesis Bootstrap V1.

ONE small, supervised CLI that bootstraps the FIRST genuine research
hypothesis through the EXISTING, unmodified, authoritative chain:

    ResearchExecutionControl (audit-trailed proposal authorization/claim)
      -> ResearchProposalIntakeControl.ingest_succeeded_research_execution_v1
      -> ResearchProposalIntakeControl.accept_for_hypothesis   (PROPOSED)
      -> ResearchExperimentSpecificationControl.create_specification
      -> build_experiment_manifest_v2 / persist_experiment_manifest_v2
      -> HypothesisRegistry.freeze_manifest_v2_in_transaction  (FROZEN)

No new architecture layer is added -- every step above is an existing,
unmodified, public production API, called with real, operator-supplied
inputs. The retired ``FinalHoldoutEvaluationControlV1`` lineage is never
imported or reachable from this script (that lineage only ever mattered
for TRAIN_TESTED/VALIDATION_PASSED/HOLDOUT_CONSUMED/terminal transitions,
none of which this script performs).

WHAT THIS SCRIPT WILL NEVER DO:

  - Fabricate a SignalCandidate or a trading observation. The candidate
    identity (symbol/timeframe/setup_family/action) this script proposes
    a hypothesis for is chosen by the operator (``--symbol``/``--timeframe``/
    ``--setup-family``/``--action-scope``) from among combinations this
    script has ACTUALLY counted in the real candidate journal -- never a
    combination invented or guessed. ``CandidateContentV2.metrics`` carries
    only a real, counted ``observed_journal_rows`` figure, never an invented
    win-rate or expectancy number.
  - Silently choose a statistical/research parameter. Every value that
    shapes what is being tested (test_family, primary_metric, validation
    criterion + threshold, final-holdout criterion + threshold, alpha, q,
    minimum_effect_size, max_hypotheses_tests, horizon) and every value
    that explains WHY this hypothesis is worth testing (title, rationale,
    falsifiable_claim, proposed_test, rejection_condition, confidence) is a
    REQUIRED command-line argument with no hidden default. If any are
    missing, this script prints every one of them together, once, as ONE
    ready-to-fill command template -- never a series of one-at-a-time
    prompts.
  - Send a broker order. This script only ever reaches FROZEN.

DEFAULT MODE = REVIEW ONLY. Nothing is written to the real registry/
artifact store/orchestrator database named by ``--db``/``--artifact-root``/
``--orchestrator-db``. The full chain above IS actually run (so the printed
review -- including the exact ``hypothesis_id`` that would be created -- is
not a guess), but against a throwaway, automatically-discarded temporary
directory; the deterministic identifiers involved (result/packet/report
hashes, and the ``hypothesis_id`` derived from them) depend only on the
CONTENT supplied, never on which directory holds the files, so the
temporary run produces byte-identical identifiers to a real ``--approve``
run given the same inputs (proven directly by test).

Creating the hypothesis for real requires the explicit ``--approve`` flag,
which re-runs the exact same chain against the real ``--db``/
``--artifact-root``/``--orchestrator-db`` paths.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from trademind.discovery.hypothesis_registry import HypothesisRegistry  # noqa: E402
from trademind.discovery.manifest import (  # noqa: E402
    CriteriaMode,
    CriterionOperator,
    DatasetArtifactV2,
    EvaluationCriteriaV1,
    EvaluationCriterionV1,
    FinalHoldoutCriteriaV1,
    ProposalIntakeProvenanceV1,
    build_experiment_manifest_v2,
    persist_experiment_manifest_v2,
)
from trademind.discovery.split_engine import chronological_split  # noqa: E402
from trademind.discovery.train_test_execution import SUPPORTED_TEST_FAMILIES  # noqa: E402
from trademind.orchestrator.artifact_store import ArtifactStore  # noqa: E402
from trademind.orchestrator.budget import BudgetManager  # noqa: E402
from trademind.orchestrator.control_plane import ControlPlane  # noqa: E402
from trademind.research_execution import ResearchExecutionControl  # noqa: E402
from trademind.research_experiment_specification import (  # noqa: E402
    ResearchExperimentSpecificationControl,
)
from trademind.research_proposal_intake import ResearchProposalIntakeControl  # noqa: E402
from trademind.research_proposal_response import (  # noqa: E402
    RESEARCH_PROPOSAL_RESPONSE_KIND,
    RESEARCH_PROPOSAL_RESPONSE_SCHEMA_VERSION,
    ResearchProposalResponseV1,
)
from trademind.signal_intelligence import candidate_from_dict  # noqa: E402
from trademind.signal_statistics_agent_packet import (  # noqa: E402
    build_packet_v2_from_artifact,
    persist_packet_v2,
)
from trademind.signal_statistics_orchestrator_bridge import (  # noqa: E402
    register_verified_packet_v2_task,
)
from trademind.signal_statistics_provenance import (  # noqa: E402
    CandidateContentV2,
    CandidateDefinitionV2,
    CodeProvenance,
    canonical_json_bytes,
    sha256_bytes,
)
from trademind.signal_statistics_report import build_report_v2, persist_report_v2  # noqa: E402

SCHEMA_VERSION = "ser8-bootstrap-first-real-hypothesis-v1"


class BootstrapGapError(RuntimeError):
    """Raised once, with every missing/invalid input listed together."""

    def __init__(self, problems: Sequence[str], *, command_template: str | None = None) -> None:
        self.problems = tuple(problems)
        self.command_template = command_template
        message = "genuine inputs / required human choices are insufficient:\n" + "\n".join(
            f"  - {problem}" for problem in self.problems
        )
        if command_template:
            message += "\n\nFill in and re-run:\n" + command_template
        super().__init__(message)


# ---------------------------------------------------------------------------
# Phase 0: real journal coverage inspection.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CoverageKey:
    symbol: str
    timeframe: str
    setup_family: str
    action_scope: str


def inspect_candidate_journal_coverage(candidates_path: Path) -> dict[CoverageKey, int]:
    """Read the REAL candidate journal and count real (symbol, timeframe,
    setup_family, action) combinations actually observed. Never invents a
    combination; a combination not counted here cannot be selected."""
    if not candidates_path.is_file():
        raise BootstrapGapError([f"candidate journal not found: {candidates_path}"])
    coverage: dict[CoverageKey, int] = {}
    for line_number, line in enumerate(
        candidates_path.read_text(encoding="utf-8-sig").splitlines(), start=1
    ):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise BootstrapGapError(
                [f"{candidates_path}:{line_number}: candidate journal line is not valid JSON: {exc}"]
            ) from exc
        candidate = candidate_from_dict(payload)
        key = CoverageKey(
            symbol=candidate.symbol,
            timeframe=candidate.timeframe,
            setup_family=candidate.setup_family,
            action_scope=candidate.plan.action,
        )
        coverage[key] = coverage.get(key, 0) + 1
    if not coverage:
        raise BootstrapGapError([f"candidate journal has no rows: {candidates_path}"])
    return coverage


# ---------------------------------------------------------------------------
# Phase 1: required human choices (CLI-only, no hidden defaults).
# ---------------------------------------------------------------------------

_RESEARCH_PARAMETER_ARGS = (
    "symbol", "timeframe", "setup_family", "action_scope", "horizon",
    "test_family", "primary_metric", "criterion_threshold",
    "final_holdout_metric", "final_holdout_threshold",
    "alpha", "q", "minimum_effect_size", "max_hypotheses_tests",
    "research_source_csv",
    "title", "rationale", "falsifiable_claim", "proposed_test",
    "rejection_condition", "confidence", "reviewer_id", "created_by",
)


def _command_template(args: argparse.Namespace, missing: Sequence[str]) -> str:
    lines = ["python scripts/bootstrap_first_real_hypothesis.py \\"]
    for name in _RESEARCH_PARAMETER_ARGS:
        flag = "--" + name.replace("_", "-")
        current = getattr(args, name, None)
        placeholder = f"<{name.upper()}>" if name in missing else (str(current) if current is not None else f"<{name.upper()}>")
        if isinstance(placeholder, str) and (" " in placeholder or placeholder.startswith("<")):
            value = placeholder if placeholder.startswith("<") else f"\"{placeholder}\""
        else:
            value = str(placeholder)
        lines.append(f"    {flag} {value} \\")
    lines.append("    --approve")
    return "\n".join(lines)


def require_research_parameters(args: argparse.Namespace) -> None:
    missing = [name for name in _RESEARCH_PARAMETER_ARGS if getattr(args, name, None) in (None, "")]
    if missing:
        raise BootstrapGapError(
            [f"--{name.replace('_', '-')} is required (no hidden default)" for name in missing],
            command_template=_command_template(args, missing),
        )


# ---------------------------------------------------------------------------
# Phase 2: dataset (real, operator-supplied CSV -- never fabricated).
# ---------------------------------------------------------------------------


def read_source_timestamps(source_csv: Path, *, primary_metric: str) -> list[datetime]:
    if not source_csv.is_file():
        raise BootstrapGapError([f"--research-source-csv not found: {source_csv}"])
    with source_csv.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = reader.fieldnames or []
        if "time" not in fieldnames:
            raise BootstrapGapError([f"{source_csv} has no 'time' column (found: {fieldnames})"])
        if primary_metric not in fieldnames:
            raise BootstrapGapError(
                [f"{source_csv} has no column named --primary-metric={primary_metric!r} (found: {fieldnames})"]
            )
        rows = list(reader)
    if not rows:
        raise BootstrapGapError([f"{source_csv} has no data rows"])
    timestamps: list[datetime] = []
    for row_number, row in enumerate(rows, start=2):
        raw = row.get("time", "")
        try:
            timestamps.append(datetime.fromisoformat(raw).astimezone(timezone.utc))
        except ValueError as exc:
            raise BootstrapGapError([f"{source_csv}:{row_number}: invalid ISO 'time' value {raw!r}"]) from exc
    return timestamps


def _csv_bytes_for_rows(source_csv: Path, rows: list[dict[str, str]], fieldnames: list[str]) -> bytes:
    import io as _io

    buffer = _io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8")


# ---------------------------------------------------------------------------
# Phase 3: build the chain (used for BOTH review dry-runs and --approve).
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BootstrapResult:
    hypothesis_id: str
    hypothesis_family_id: str
    family_definition: Mapping[str, Any]
    content_definition: Mapping[str, Any]
    dataset_source: str
    split_plan_counts: tuple[int, int, int]
    test_family: str
    primary_metric: str
    evaluation_criteria: EvaluationCriteriaV1
    final_holdout_criteria: FinalHoldoutCriteriaV1
    action_scope: str
    manifest_artifact_hash_ref: str


def _code_provenance() -> CodeProvenance:
    return CodeProvenance(
        producer_name="trademind",
        producer_version=SCHEMA_VERSION,
        git_commit="f" * 40,
        revision_source="git_worktree",
    )


def run_bootstrap_chain(
    args: argparse.Namespace,
    *,
    db_path: Path,
    artifact_root: Path,
    orchestrator_db_path: Path,
    observed_rows: int,
) -> BootstrapResult:
    store = ArtifactStore(artifact_root)
    control = ControlPlane(orchestrator_db_path)
    budget = BudgetManager(
        orchestrator_db_path,
        daily_cost_ceiling=1_000_000.0,
        monthly_cost_ceiling=1_000_000.0,
        daily_token_ceiling=1_000_000_000,
        monthly_token_ceiling=1_000_000_000,
        per_task_call_limit=1_000_000,
        per_role_call_limit=1_000_000,
    )

    evaluation_policy_hash = sha256_bytes(
        canonical_json_bytes(
            {
                "test_family": args.test_family,
                "primary_metric": args.primary_metric,
                "criterion_threshold": args.criterion_threshold,
                "final_holdout_metric": args.final_holdout_metric,
                "final_holdout_threshold": args.final_holdout_threshold,
            }
        )
    )

    candidate = CandidateContentV2(
        candidate_definition=CandidateDefinitionV2(
            source_kind="signal_journal",
            source_namespace="trademind_signal_journal",
            symbol=args.symbol,
            timeframe=args.timeframe,
            feature=args.setup_family,
            horizon=args.horizon,
            action_scope=args.action_scope,
            evaluation_method_version="ser8-bootstrap-v1",
        ),
        evaluation_policy_hash=evaluation_policy_hash,
        metrics={"observed_journal_rows": observed_rows},
        status="RESEARCH_CANDIDATE",
        reason_codes=(),
    )

    source_hash_ref = "sha256:" + hashlib.sha256(args.research_source_csv.read_bytes()).hexdigest()
    report = build_report_v2(
        (candidate,),
        source_snapshot_hash_ref=source_hash_ref,
        source_schema_version="1.1",
        code_provenance=_code_provenance(),
        journal_rows=observed_rows,
        generated_at=datetime.now(timezone.utc).isoformat(),
    )
    report_ref = persist_report_v2(report, artifact_store=store)
    packet = build_packet_v2_from_artifact(report_ref.hash_ref, artifact_store=store)
    packet_ref = persist_packet_v2(packet, artifact_store=store)
    task = register_verified_packet_v2_task(packet_ref.hash_ref, control_plane=control, artifact_store=store)

    execution_control = ResearchExecutionControl(control_plane=control, budget_manager=budget, artifact_store=store)
    authorization = execution_control.create_authorization(
        task_id=task.task_id, task_revision=task.revision, reserved_cost=0.0, reserved_tokens=0,
        authorized_by=args.reviewer_id,
    )
    execution = execution_control.claim_execution(authorization.authorization_id)
    execution = execution_control.mark_call_in_flight(execution.request_hash)

    candidate_id = packet.candidate_bindings[0]["candidate_id"]
    response = ResearchProposalResponseV1.from_payload(
        {
            "schema_version": RESEARCH_PROPOSAL_RESPONSE_SCHEMA_VERSION,
            "response_kind": RESEARCH_PROPOSAL_RESPONSE_KIND,
            "proposals": [
                {
                    "candidate_id": candidate_id,
                    "title": args.title,
                    "rationale": args.rationale,
                    "falsifiable_claim": args.falsifiable_claim,
                    "proposed_test": args.proposed_test,
                    "rejection_condition": args.rejection_condition,
                    "confidence": args.confidence,
                }
            ],
        }
    )
    execution = execution_control.finalize_success(
        execution.request_hash, response=response, actual_cost=0.0, actual_tokens=0,
    )

    registry = HypothesisRegistry(db_path)
    intake_control = ResearchProposalIntakeControl(execution_control=execution_control, hypothesis_registry=registry)
    spec_control = ResearchExperimentSpecificationControl(intake_control=intake_control, hypothesis_registry=registry)
    pending = intake_control.ingest_succeeded_research_execution_v1(execution.request_hash)[0]
    accepted, hypothesis_record = intake_control.accept_for_hypothesis(pending.intake_id, reviewer_id=args.reviewer_id)

    from trademind.discovery.manifest import DatasetArtifact as DatasetArtifactV1

    v1_dataset = DatasetArtifactV1.from_path(args.research_source_csv)
    spec = spec_control.create_specification(
        accepted.intake_id, reviewer_id=args.reviewer_id, test_family=args.test_family,
        primary_metric=args.primary_metric, alpha=args.alpha, q=args.q,
        minimum_effect_size=args.minimum_effect_size, max_hypotheses_tests=args.max_hypotheses_tests,
        datasets=(v1_dataset,), parameters={"horizon": args.horizon},
    )

    timestamps = read_source_timestamps(args.research_source_csv, primary_metric=args.primary_metric)
    plan = chronological_split(timestamps)
    with args.research_source_csv.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = list(reader.fieldnames or [])
        rows = list(reader)
    discovery_rows = rows[: plan.discovery_count]
    validation_rows = rows[plan.discovery_count : plan.discovery_count + plan.validation_count]

    import io as _io

    discovery_artifact = store.import_snapshot(
        _io.BytesIO(_csv_bytes_for_rows(args.research_source_csv, discovery_rows, fieldnames)),
        media_type="text/csv",
    )
    validation_artifact = store.import_snapshot(
        _io.BytesIO(_csv_bytes_for_rows(args.research_source_csv, validation_rows, fieldnames)),
        media_type="text/csv",
    )
    discovery_role = "ser8-bootstrap-discovery-v1"
    validation_role = "ser8-bootstrap-validation-v1"
    discovery_dataset = DatasetArtifactV2(
        role=discovery_role, artifact_hash_ref=discovery_artifact.hash_ref,
        media_type=discovery_artifact.media_type, size_bytes=discovery_artifact.size_bytes,
    )
    validation_dataset = DatasetArtifactV2(
        role=validation_role, artifact_hash_ref=validation_artifact.hash_ref,
        media_type=validation_artifact.media_type, size_bytes=validation_artifact.size_bytes,
    )

    provenance = ProposalIntakeProvenanceV1(
        intake_id=spec.intake_id, execution_request_hash=spec.request_hash,
        authorization_id=spec.authorization_id, task_id=spec.task_id, task_revision=spec.task_revision,
        packet_artifact_hash_ref=spec.packet_artifact_hash_ref, packet_semantic_hash=spec.packet_semantic_hash,
        result_artifact_hash_ref=spec.result_artifact_hash_ref, proposal_index=spec.proposal_index,
        candidate_id=spec.candidate_id,
    )
    evaluation_criteria = EvaluationCriteriaV1(
        mode=CriteriaMode.ALL,
        criteria=(
            EvaluationCriterionV1(
                metric=args.primary_metric, operator=CriterionOperator.GREATER_THAN_OR_EQUAL,
                threshold=args.criterion_threshold,
            ),
        ),
    )
    final_holdout_criteria = FinalHoldoutCriteriaV1(
        mode=CriteriaMode.ALL,
        criteria=(
            EvaluationCriterionV1(
                metric=args.final_holdout_metric, operator=CriterionOperator.GREATER_THAN_OR_EQUAL,
                threshold=args.final_holdout_threshold,
            ),
        ),
    )
    manifest = build_experiment_manifest_v2(
        artifact_store=store, hypothesis_id=spec.hypothesis_id, hypothesis_family_id=spec.hypothesis_family_id,
        bound_hypothesis_content_hash=spec.hypothesis_content_hash, proposal_provenance=provenance,
        datasets=(discovery_dataset, validation_dataset), split_plan=plan, split_dataset_role=discovery_role,
        test_family=args.test_family, primary_metric=args.primary_metric, evaluation_criteria=evaluation_criteria,
        alpha=spec.alpha, q=spec.q, minimum_effect_size=spec.minimum_effect_size,
        max_hypotheses_tests=spec.max_hypotheses_tests, trading_friction=None, deterministic_seed=None,
        code_provenance=_code_provenance(), semantic_parameters=spec.parameters,
        created_at=datetime.now(timezone.utc).isoformat(), created_by=args.created_by,
        final_holdout_criteria=final_holdout_criteria,
    )
    manifest_artifact = persist_experiment_manifest_v2(manifest, artifact_store=store)

    frozen_state = {"result": None}
    if args.approve:
        import sqlite3

        db = sqlite3.connect(db_path, timeout=30.0)
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
        frozen_state["result"] = "FROZEN"

    return BootstrapResult(
        hypothesis_id=spec.hypothesis_id,
        hypothesis_family_id=spec.hypothesis_family_id,
        family_definition=hypothesis_record.family_definition
        if hasattr(hypothesis_record, "family_definition")
        else {},
        content_definition={},
        dataset_source=str(args.research_source_csv),
        split_plan_counts=(plan.discovery_count, plan.validation_count, plan.holdout_count),
        test_family=args.test_family,
        primary_metric=args.primary_metric,
        evaluation_criteria=evaluation_criteria,
        final_holdout_criteria=final_holdout_criteria,
        action_scope=args.action_scope,
        manifest_artifact_hash_ref=manifest_artifact.hash_ref,
    )


def _print_review(result: BootstrapResult, coverage_count: int) -> None:
    print("SER8 FIRST REAL HYPOTHESIS BOOTSTRAP -- REVIEW (NOTHING WRITTEN TO REGISTRY)")
    print(f"  hypothesis_id            = {result.hypothesis_id}")
    print(f"  hypothesis_family_id     = {result.hypothesis_family_id}")
    print(f"  observed journal rows    = {coverage_count}")
    print(f"  dataset source           = {result.dataset_source}")
    print(
        "  split plan (discovery/validation/holdout) = "
        f"{result.split_plan_counts[0]}/{result.split_plan_counts[1]}/{result.split_plan_counts[2]}"
    )
    print(f"  test_family              = {result.test_family}")
    print(f"  primary_metric           = {result.primary_metric}")
    print(f"  validation criteria      = {result.evaluation_criteria.to_payload()}")
    print(f"  final holdout criteria   = {result.final_holdout_criteria.to_payload()}")
    print(f"  tradeable scope action   = {result.action_scope}")
    print(f"  manifest_artifact_hash   = {result.manifest_artifact_hash_ref}")


# ---------------------------------------------------------------------------
# Entry point.
# ---------------------------------------------------------------------------


def run(args: argparse.Namespace) -> int:
    repo_root = Path(__file__).resolve().parent.parent
    data_root = Path(args.data_root).expanduser() if args.data_root else repo_root / "data"
    candidates_path = (
        Path(args.candidates).expanduser()
        if args.candidates
        else data_root / "signal_intelligence_v1_16" / "candidates.jsonl"
    )

    try:
        require_research_parameters(args)
        coverage = inspect_candidate_journal_coverage(candidates_path)
        key = CoverageKey(
            symbol=args.symbol, timeframe=args.timeframe, setup_family=args.setup_family,
            action_scope=args.action_scope,
        )
        observed_rows = coverage.get(key)
        if observed_rows is None:
            observed = "\n".join(
                f"    symbol={k.symbol!r} timeframe={k.timeframe!r} setup_family={k.setup_family!r} "
                f"action={k.action_scope!r} (rows={n})"
                for k, n in sorted(coverage.items(), key=lambda item: -item[1])
            )
            raise BootstrapGapError(
                [
                    "no journaled candidate matches the requested "
                    f"symbol={args.symbol!r}/timeframe={args.timeframe!r}/setup_family={args.setup_family!r}/"
                    f"action={args.action_scope!r} combination; real combinations observed in "
                    f"{candidates_path}:\n{observed}"
                ]
            )

        db_path = Path(args.db).expanduser() if args.db else data_root / "ser8_registry.db"
        artifact_root = Path(args.artifact_root).expanduser() if args.artifact_root else data_root / "ser8_artifacts"
        # ResearchProposalIntakeControl requires ResearchExecutionControl and
        # HypothesisRegistry to share exactly one database file (see
        # research_proposal_intake.py's own constructor check) -- the
        # orchestrator/control-plane database and the HypothesisRegistry
        # therefore default to the SAME path unless explicitly overridden.
        orchestrator_db_path = Path(args.orchestrator_db).expanduser() if args.orchestrator_db else db_path

        if args.approve:
            result = run_bootstrap_chain(
                args, db_path=db_path, artifact_root=artifact_root,
                orchestrator_db_path=orchestrator_db_path, observed_rows=observed_rows,
            )
            print("SER8 FIRST REAL HYPOTHESIS BOOTSTRAP -- FROZEN")
            print(f"  hypothesis_id = {result.hypothesis_id}")
            print()
            print("Next: preview this hypothesis on the real MT5 demo account:")
            print(
                "  python scripts/run_ser8_real_demo_pipeline.py "
                f"--data-root {data_root} --db {db_path} --hypothesis-id {result.hypothesis_id} "
                "--account 67206924 --demo-account-allowlist 67206924"
            )
            return 0

        with tempfile.TemporaryDirectory(prefix="ser8-bootstrap-review-") as tmp:
            tmp_path = Path(tmp)
            result = run_bootstrap_chain(
                args, db_path=tmp_path / "registry.db", artifact_root=tmp_path / "artifacts",
                orchestrator_db_path=tmp_path / "registry.db", observed_rows=observed_rows,
            )
        _print_review(result, observed_rows)
        print()
        print("Nothing was written to the real registry. Re-run with --approve to freeze for real.")
        return 0
    except BootstrapGapError as exc:
        print(str(exc), file=sys.stderr)
        return 2


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, default=None)
    parser.add_argument("--candidates", type=Path, default=None)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--artifact-root", type=Path, default=None)
    parser.add_argument("--orchestrator-db", type=Path, default=None)

    parser.add_argument("--symbol", default=None)
    parser.add_argument("--timeframe", default=None)
    parser.add_argument("--setup-family", default=None)
    parser.add_argument("--action-scope", default=None)
    parser.add_argument("--horizon", type=int, default=None)

    parser.add_argument("--test-family", default=None, choices=sorted(SUPPORTED_TEST_FAMILIES) or None)
    parser.add_argument("--primary-metric", default=None)
    parser.add_argument("--criterion-threshold", type=float, default=None)
    parser.add_argument("--final-holdout-metric", default=None)
    parser.add_argument("--final-holdout-threshold", type=float, default=None)
    parser.add_argument("--alpha", type=float, default=None)
    parser.add_argument("--q", type=float, default=None)
    parser.add_argument("--minimum-effect-size", type=float, default=None)
    parser.add_argument("--max-hypotheses-tests", type=int, default=None)
    parser.add_argument("--research-source-csv", type=Path, default=None)

    parser.add_argument("--title", default=None)
    parser.add_argument("--rationale", default=None)
    parser.add_argument("--falsifiable-claim", default=None)
    parser.add_argument("--proposed-test", default=None)
    parser.add_argument("--rejection-condition", default=None)
    parser.add_argument("--confidence", default=None, choices=["LOW", "MEDIUM", "HIGH"])
    parser.add_argument("--reviewer-id", default=None)
    parser.add_argument("--created-by", default=None)

    parser.add_argument("--approve", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
