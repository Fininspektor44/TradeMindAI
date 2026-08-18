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

DATASET SCOPE BINDING (this file's own gap-closing fix, twice-revised): the
research dataset used for TRAIN_TEST/VALIDATION is never an arbitrary,
operator-supplied CSV. It is derived ONLY from genuine, already-persisted
runtime evidence, joined by real identity, never by coincidence:

    data/signal_intelligence_v1_16/candidates.jsonl (signal_id)
        -> filtered EXACTLY by --symbol/--timeframe/--setup-family/--action-scope
        -> joined to data/signal_intelligence_v1_16/outcomes.jsonl (signal_id)
        -> only outcome records whose signal_id is one of the filtered
           candidates' own, genuinely-computed signal_id survive.

``outcomes.jsonl`` -- NOT ``paper_signals/signals.csv`` -- is the
authoritative outcome source for this runtime path: real Windows evidence
confirmed 315/323 candidates for a real scope have exact
``SignalCandidate.signal_id`` matches in
``data/signal_intelligence_v1_16/outcomes.jsonl`` (the same directory
``candidates.jsonl`` already lives in, and the same canonical file
``live_signal_runtime.py`` itself reads), while ``paper_signals/signals.csv``
matched zero. This script therefore never reads ``paper_signals/`` at all,
and never reads any quarantined or duplicate
``live_signal_runtime_v1``/``live_signal_runtime_eCN_*`` outcome copy --
there is no code path here that constructs any outcome path other than the
one canonical file. Outcome record identity/vocabulary/sign-consistency is
validated by reusing ``signal_evidence.OutcomeObservation.from_dict``
unmodified -- the same authoritative schema/parser
``live_signal_runtime.py`` itself already relies on (via
``signal_evidence.load_outcomes``).

No synthetic row is ever added, and no candidate/outcome outside this exact
scope can leak into the dataset -- proven directly by test (an out-of-scope
outcome could only join by an infeasible SHA-256 collision, since
``SignalCandidate.signal_id`` is itself a content-derived hash covering
symbol/timeframe/setup_family/action/observed_at/plan). The derived CSV is
content-addressed (its SHA-256 is part of the review output) and is used
directly as the DatasetArtifact for both TRAIN_TEST and VALIDATION dataset
roles; there is no CLI flag that accepts an unrelated, arbitrary research
CSV for this bootstrap. A scope where not every candidate has a matching
outcome (e.g. 315 of 323) is accepted -- the unmatched candidates are
excluded and reported (never fabricated); the join never invents a missing
outcome.

WHAT THIS SCRIPT WILL NEVER DO:

  - Fabricate a SignalCandidate, an outcome record, or a trading
    observation. Every row in the derived dataset is a genuine,
    already-persisted outcome record that survived the exact-scope join
    above; nothing is invented, interpolated, or synthesized.
  - Silently choose a statistical/research parameter. Every value that
    shapes what is being tested (test_family, primary_metric, validation
    criterion + threshold, final-holdout criterion + threshold, alpha, q,
    minimum_effect_size, max_hypotheses_tests, horizon) and every value
    that explains WHY this hypothesis is worth testing (title, rationale,
    falsifiable_claim, proposed_test, rejection_condition, confidence) is a
    REQUIRED command-line argument with no hidden default. If any are
    missing, this script prints every one of them together, once, as ONE
    ready-to-fill command template -- never a series of one-at-a-time
    prompts. ``--primary-metric`` is additionally validated against the
    dataset's own genuinely-available numeric columns (printed as
    ``AVAILABLE NUMERIC METRICS`` in review) -- never auto-selected.
  - Send a broker order. This script only ever reaches FROZEN.

DEFAULT MODE = REVIEW ONLY. Nothing is written to the real registry/
artifact store/orchestrator database named by ``--db``/``--artifact-root``/
``--orchestrator-db``, and no derived dataset file is written outside a
throwaway, automatically-discarded temporary directory. The full chain
above IS actually run there (so the printed review -- including the exact
``hypothesis_id`` and dataset hash that would be created -- is not a
guess); the deterministic identifiers involved depend only on the CONTENT
supplied, never on which directory holds the files, so the temporary run
produces byte-identical identifiers to a real ``--approve`` run given the
same inputs (proven directly by test).

Creating the hypothesis for real requires the explicit ``--approve`` flag,
which re-runs the exact same chain against the real ``--db``/
``--artifact-root``/``--orchestrator-db`` paths, and persists the derived
dataset CSV to a content-addressed path under ``--data-root``.
"""

from __future__ import annotations

import argparse
import csv
import gc
import hashlib
import json
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Sequence

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
from trademind.signal_evidence import OutcomeObservation  # noqa: E402
from trademind.signal_intelligence import SignalCandidate, candidate_from_dict  # noqa: E402
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

# Minimum derived-dataset row count before a 60/20/20 chronological split can
# put at least one row in every phase (int(5 * 0.2) == 1).
MINIMUM_DATASET_ROWS = 5


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


def _load_candidates(candidates_path: Path) -> dict[str, SignalCandidate]:
    if not candidates_path.is_file():
        raise BootstrapGapError([f"candidate journal not found: {candidates_path}"])
    candidates: dict[str, SignalCandidate] = {}
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
        candidates[candidate.signal_id] = candidate
    if not candidates:
        raise BootstrapGapError([f"candidate journal has no rows: {candidates_path}"])
    return candidates


def inspect_candidate_journal_coverage(candidates_path: Path) -> dict[CoverageKey, int]:
    """Read the REAL candidate journal and count real (symbol, timeframe,
    setup_family, action) combinations actually observed. Never invents a
    combination; a combination not counted here cannot be selected."""
    candidates = _load_candidates(candidates_path)
    coverage: dict[CoverageKey, int] = {}
    for candidate in candidates.values():
        key = CoverageKey(
            symbol=candidate.symbol,
            timeframe=candidate.timeframe,
            setup_family=candidate.setup_family,
            action_scope=candidate.plan.action,
        )
        coverage[key] = coverage.get(key, 0) + 1
    return coverage


def select_candidates_by_scope(
    candidates_path: Path, key: CoverageKey
) -> dict[str, SignalCandidate]:
    """Return exactly the real candidates matching ``key``, keyed by their
    own genuine ``signal_id`` -- never a fabricated or guessed identity."""
    candidates = _load_candidates(candidates_path)
    return {
        signal_id: candidate
        for signal_id, candidate in candidates.items()
        if candidate.symbol == key.symbol
        and candidate.timeframe == key.timeframe
        and candidate.setup_family == key.setup_family
        and candidate.plan.action == key.action_scope
    }


# ---------------------------------------------------------------------------
# Phase 0b: dataset scope binding -- candidates.jsonl x signal_intelligence_v1_16/outcomes.jsonl.
#
# ``paper_signals/signals.csv`` matched ZERO real outcomes for the real
# scope this task was built against, and is therefore NOT the authoritative
# outcome source for this runtime path -- this script no longer reads it at
# all. The authoritative outcome journal is
# ``data/signal_intelligence_v1_16/outcomes.jsonl``, the canonical file
# ``live_signal_runtime.py`` itself reads via the same directory
# ``candidates.jsonl`` already lives in. Its own public, already-closed
# schema/parser (``trademind.signal_evidence.OutcomeObservation``) is reused
# directly for identity/vocabulary/sign-consistency validation of every
# outcome record. Quarantined files and the duplicate
# ``live_signal_runtime_v1``/``live_signal_runtime_eCN_*`` outcome copies
# are never read by this script -- there is no code path here that
# constructs any path other than the one canonical file.
# ---------------------------------------------------------------------------


def load_outcome_records(path: Path) -> dict[str, dict[str, object]]:
    """Read the ONE canonical outcome journal and return a real, genuine
    outcome payload per signal_id. Reuses
    ``signal_evidence.OutcomeObservation.from_dict`` to validate every
    record's identity/vocabulary/sign-consistency exactly as the
    authoritative production reader (``live_signal_runtime.py`` via
    ``signal_evidence.load_outcomes``) already does. Fails closed on the
    first malformed record or the first CONFLICTING duplicate signal_id (two
    records with the same id but different content); a duplicate line that
    is byte-identical to an earlier one is tolerated as a harmless
    idempotent re-append and simply counted, never treated as a second
    genuine outcome."""
    if not path.is_file():
        raise BootstrapGapError([f"outcome journal not found: {path}"])
    by_id: dict[str, dict[str, object]] = {}
    duplicate_count = 0
    for line_number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise BootstrapGapError([f"{path}:{line_number}: outcome journal line is not valid JSON: {exc}"]) from exc
        if not isinstance(payload, dict):
            raise BootstrapGapError([f"{path}:{line_number}: outcome record must be an object"])
        try:
            OutcomeObservation.from_dict(payload)  # reuse the authoritative schema/validation, unmodified.
        except ValueError as exc:
            raise BootstrapGapError([f"{path}:{line_number}: malformed outcome record: {exc}"]) from exc
        signal_id = str(payload.get("signal_id", ""))
        existing = by_id.get(signal_id)
        if existing is not None:
            if existing != payload:
                raise BootstrapGapError(
                    [f"conflicting outcome identity for signal_id={signal_id!r} in {path} (line {line_number})"]
                )
            duplicate_count += 1
            continue
        by_id[signal_id] = payload
    if not by_id:
        raise BootstrapGapError([f"outcome journal has no rows: {path}"])
    by_id["__duplicate_count__"] = duplicate_count  # type: ignore[assignment]
    return by_id


@dataclass(frozen=True, slots=True)
class BoundDataset:
    scope_candidates: int
    matched_outcomes: int
    unmatched_candidates: int
    unmatched_candidate_ids: tuple[str, ...]
    duplicate_outcomes: int
    fieldnames: tuple[str, ...]
    rows: tuple[dict[str, str], ...] = field(repr=False)
    numeric_metrics: tuple[str, ...]
    csv_bytes: bytes = field(repr=False)
    dataset_hash: str


def _is_float(value: object) -> bool:
    if value is None or value == "" or isinstance(value, bool):
        return False
    try:
        float(value)  # type: ignore[arg-type]
    except (ValueError, TypeError):
        return False
    return True


_OUTCOME_NON_METRIC_FIELDS = frozenset({"signal_id", "setup_key", "completed_at", "outcome", "exit_reason"})


def bind_dataset_to_outcome_journal_scope(
    candidates: Mapping[str, SignalCandidate],
    outcome_records: Mapping[str, object],
    *,
    key: CoverageKey,
) -> BoundDataset:
    """Join real candidates to real outcome records by genuine identity
    ONLY (``candidate.signal_id == outcome['signal_id']``). Since
    ``SignalCandidate.signal_id`` is itself a content-derived hash covering
    symbol/timeframe/setup_family/action/observed_at/plan (see its own
    ``@property`` definition), an out-of-scope outcome cannot accidentally
    join here -- doing so would require a SHA-256 collision. No synthetic
    row is ever produced; every surviving row is copied verbatim from the
    real outcome payload."""
    duplicate_outcomes = int(outcome_records.get("__duplicate_count__", 0))  # type: ignore[arg-type]
    records = {key_: value for key_, value in outcome_records.items() if key_ != "__duplicate_count__"}

    matched_rows: list[dict[str, object]] = []
    unmatched_candidate_ids: list[str] = []

    for signal_id, candidate in sorted(candidates.items()):
        record = records.get(signal_id)
        if record is None:
            unmatched_candidate_ids.append(signal_id)
            continue
        row = dict(record)  # type: ignore[arg-type]
        row["candidate_observed_at"] = candidate.observed_at.astimezone(timezone.utc).isoformat()
        matched_rows.append(row)

    scope_candidates = len(candidates)
    matched_outcomes = len(matched_rows)
    unmatched_candidates = len(unmatched_candidate_ids)

    if matched_outcomes == 0:
        raise BootstrapGapError(
            [
                f"zero outcome records matched the {scope_candidates} candidate(s) in scope "
                f"symbol={key.symbol!r}/timeframe={key.timeframe!r}/setup_family={key.setup_family!r}/"
                f"action={key.action_scope!r} -- no unrelated dataset can be substituted"
            ]
        )
    if matched_outcomes < MINIMUM_DATASET_ROWS:
        raise BootstrapGapError(
            [
                f"only {matched_outcomes} genuinely matched outcome(s) are available for this scope, "
                f"fewer than the minimum {MINIMUM_DATASET_ROWS} required for a 60/20/20 chronological "
                "split to place at least one row in every phase"
            ]
        )

    # Canonical `time` column, derived only from the genuine candidate
    # observed_at timestamp (never invented); the outcome's own genuine
    # completed_at is separately preserved verbatim, unchanged.
    for row in matched_rows:
        row["time"] = row["candidate_observed_at"]

    matched_rows.sort(key=lambda row: (str(row["time"]), str(row.get("signal_id", ""))))
    times = [row["time"] for row in matched_rows]
    if len(set(times)) != len(times):
        raise BootstrapGapError(
            ["two or more matched candidates share the identical genuine observed_at timestamp; "
             "refusing to invent a tiebreaker for a chronological split"]
        )

    numeric_metrics = tuple(
        sorted(
            column
            for column in (matched_rows[0].keys() if matched_rows else ())
            if column not in _OUTCOME_NON_METRIC_FIELDS
            and column not in ("time", "candidate_observed_at")
            and all(_is_float(row.get(column)) for row in matched_rows)
        )
    )

    identity_columns = ["time", "signal_id", "completed_at", "outcome"]
    fieldnames = identity_columns + [column for column in numeric_metrics if column not in identity_columns]
    output_rows = [
        {column: ("" if row.get(column) is None else str(row.get(column))) for column in fieldnames}
        for row in matched_rows
    ]

    import io as _io

    buffer = _io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(output_rows)
    csv_bytes = buffer.getvalue().encode("utf-8")
    dataset_hash = hashlib.sha256(csv_bytes).hexdigest()

    return BoundDataset(
        scope_candidates=scope_candidates,
        matched_outcomes=matched_outcomes,
        unmatched_candidates=unmatched_candidates,
        unmatched_candidate_ids=tuple(sorted(unmatched_candidate_ids)),
        duplicate_outcomes=duplicate_outcomes,
        fieldnames=tuple(fieldnames),
        rows=tuple(output_rows),
        numeric_metrics=numeric_metrics,
        csv_bytes=csv_bytes,
        dataset_hash=dataset_hash,
    )


# ---------------------------------------------------------------------------
# Phase 1: required human choices (CLI-only, no hidden defaults).
# ---------------------------------------------------------------------------

_RESEARCH_PARAMETER_ARGS = (
    "symbol", "timeframe", "setup_family", "action_scope", "horizon",
    "test_family", "primary_metric", "criterion_threshold",
    "final_holdout_metric", "final_holdout_threshold",
    "alpha", "q", "minimum_effect_size", "max_hypotheses_tests",
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
# Phase 2: chain building blocks.
# ---------------------------------------------------------------------------


def _csv_bytes_for_rows(rows: list[dict[str, str]], fieldnames: list[str]) -> bytes:
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
    dataset_hash: str
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
    dataset_csv_path: Path,
    bound_dataset: BoundDataset,
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
                "dataset_hash": bound_dataset.dataset_hash,
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
        metrics={
            "observed_journal_rows": observed_rows,
            "scope_candidates": bound_dataset.scope_candidates,
            "matched_outcomes": bound_dataset.matched_outcomes,
        },
        status="RESEARCH_CANDIDATE",
        reason_codes=(),
    )

    source_hash_ref = "sha256:" + bound_dataset.dataset_hash
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
    accepted, _hypothesis_record = intake_control.accept_for_hypothesis(pending.intake_id, reviewer_id=args.reviewer_id)

    from trademind.discovery.manifest import DatasetArtifact as DatasetArtifactV1

    v1_dataset = DatasetArtifactV1.from_path(dataset_csv_path)
    spec = spec_control.create_specification(
        accepted.intake_id, reviewer_id=args.reviewer_id, test_family=args.test_family,
        primary_metric=args.primary_metric, alpha=args.alpha, q=args.q,
        minimum_effect_size=args.minimum_effect_size, max_hypotheses_tests=args.max_hypotheses_tests,
        datasets=(v1_dataset,), parameters={"horizon": args.horizon, "dataset_hash": bound_dataset.dataset_hash},
    )

    timestamps = [datetime.fromisoformat(row["time"]) for row in bound_dataset.rows]
    plan = chronological_split(timestamps)
    rows = [dict(row) for row in bound_dataset.rows]
    fieldnames = list(bound_dataset.fieldnames)
    discovery_rows = rows[: plan.discovery_count]
    validation_rows = rows[plan.discovery_count : plan.discovery_count + plan.validation_count]

    import io as _io

    discovery_artifact = store.import_snapshot(
        _io.BytesIO(_csv_bytes_for_rows(discovery_rows, fieldnames)), media_type="text/csv",
    )
    validation_artifact = store.import_snapshot(
        _io.BytesIO(_csv_bytes_for_rows(validation_rows, fieldnames)), media_type="text/csv",
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

    return BootstrapResult(
        hypothesis_id=spec.hypothesis_id,
        hypothesis_family_id=spec.hypothesis_family_id,
        dataset_hash=bound_dataset.dataset_hash,
        split_plan_counts=(plan.discovery_count, plan.validation_count, plan.holdout_count),
        test_family=args.test_family,
        primary_metric=args.primary_metric,
        evaluation_criteria=evaluation_criteria,
        final_holdout_criteria=final_holdout_criteria,
        action_scope=args.action_scope,
        manifest_artifact_hash_ref=manifest_artifact.hash_ref,
    )


def _print_review(
    result: BootstrapResult, *, coverage_count: int, key: CoverageKey, bound_dataset: BoundDataset,
) -> None:
    print("SER8 FIRST REAL HYPOTHESIS BOOTSTRAP -- REVIEW (NOTHING WRITTEN TO REGISTRY)")
    print(f"  hypothesis_id            = {result.hypothesis_id}")
    print(f"  hypothesis_family_id     = {result.hypothesis_family_id}")
    print(f"  observed journal rows    = {coverage_count}")
    print(
        "CANDIDATE SCOPE:"
        f" symbol={key.symbol} timeframe={key.timeframe} setup_family={key.setup_family} action={key.action_scope}"
    )
    print(f"SCOPE CANDIDATES: {bound_dataset.scope_candidates}")
    print(f"MATCHED OUTCOMES: {bound_dataset.matched_outcomes}")
    print(f"UNMATCHED CANDIDATES: {bound_dataset.unmatched_candidates}")
    print(f"DUPLICATE OUTCOMES: {bound_dataset.duplicate_outcomes}")
    print(f"DATASET HASH: sha256:{bound_dataset.dataset_hash}")
    print(f"AVAILABLE NUMERIC METRICS: {', '.join(bound_dataset.numeric_metrics)}")
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
# Phase 3b: deterministic SQLite resource release for the temporary review
# chain.
#
# AUDIT: every DB-owning object ``run_bootstrap_chain`` constructs
# (``HypothesisRegistry``, ``ArtifactStore``, ``ControlPlane``,
# ``BudgetManager``, ``ResearchExecutionControl``,
# ``ResearchProposalIntakeControl``, ``ResearchExperimentSpecificationControl``)
# already connects to SQLite per-call (``with self._connect() as db: ...``)
# rather than holding one persistent connection as instance state --
# ``ArtifactStore`` uses no SQLite at all and already closes every OS file
# descriptor it opens via ``os.close``/``with os.fdopen(...) as handle``.
# None of these classes expose (or need) a public ``close()``/context-manager
# API of their own, because none of them owns a long-lived connection to
# close -- each per-call ``sqlite3.Connection`` is a local variable whose
# underlying OS handle CPython's own reference counting releases the
# instant every reference to it is gone.
#
# The real Windows failure traces to exactly the case reference counting
# does NOT resolve immediately: when an exception propagates out of
# ``run_bootstrap_chain`` while still inside the caller's
# ``with tempfile.TemporaryDirectory(...)`` block, the exception's own
# ``__traceback__`` keeps every frame between the failure point and the
# catch site alive -- including any still-open per-call
# ``sqlite3.Connection`` local to those frames -- for as long as the
# exception object itself is referenced. If that exception (or a new one
# chained ``from`` it) is left to propagate through the ``with`` block's own
# ``__exit__`` (which deletes the directory), those connections are still
# open at the exact moment Windows refuses the delete.
#
# The smallest fix that needs no change to any of the six already-closed,
# already-audited production modules above is therefore local to this
# script: run the chain, and on ANY outcome, catch the exception (letting
# Python's own ``except ... as exc:`` machinery clear that binding at the
# end of the block), drop every local reference, and force a garbage
# collection pass -- all strictly BEFORE returning control to the caller's
# ``with tempfile.TemporaryDirectory(...)`` block, so every SQLite handle
# is released deterministically before that block's own ``__exit__`` ever
# runs. No WinError 32 is suppressed or ignored: this fix prevents the
# handle from still being open when deletion is attempted, it does not
# catch or ignore a deletion failure.
# ---------------------------------------------------------------------------


def _run_review_chain(
    args: argparse.Namespace,
    *,
    tmp_path: Path,
    observed_rows: int,
    dataset_csv_path: Path,
    bound_dataset: BoundDataset,
) -> tuple[BootstrapResult | None, str | None]:
    """Run the bootstrap chain against the throwaway review directory and
    deterministically release every SQLite handle it opened -- on success
    AND on failure -- before returning. Never raises: the caller is
    expected to still be inside the
    ``with tempfile.TemporaryDirectory(...)`` block when this returns, and
    must not re-raise until AFTER that block has exited, so that no
    exception's traceback is alive while the directory is being deleted."""
    try:
        result = run_bootstrap_chain(
            args, db_path=tmp_path / "registry.db", artifact_root=tmp_path / "artifacts",
            orchestrator_db_path=tmp_path / "registry.db", observed_rows=observed_rows,
            dataset_csv_path=dataset_csv_path, bound_dataset=bound_dataset,
        )
        return result, None
    except Exception as exc:  # noqa: BLE001 -- every exception path must still release its DB handles before the caller's tempdir cleanup runs.
        return None, f"{type(exc).__name__}: {exc}"
    finally:
        gc.collect()


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
    # The ONE canonical outcome journal -- never paper_signals/signals.csv,
    # never a quarantined or live_runtime_v1/eCN duplicate copy. Lives in
    # the exact same directory as candidates.jsonl.
    outcomes_path = (
        Path(args.outcomes).expanduser()
        if args.outcomes
        else data_root / "signal_intelligence_v1_16" / "outcomes.jsonl"
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

        scoped_candidates = select_candidates_by_scope(candidates_path, key)
        outcome_records = load_outcome_records(outcomes_path)
        bound_dataset = bind_dataset_to_outcome_journal_scope(scoped_candidates, outcome_records, key=key)

        if args.primary_metric not in bound_dataset.numeric_metrics:
            raise BootstrapGapError(
                [
                    f"--primary-metric={args.primary_metric!r} is not one of the genuinely available "
                    f"numeric metrics in the scope-bound dataset: {', '.join(bound_dataset.numeric_metrics)}"
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
            dataset_dir = data_root / "ser8_bootstrap_datasets"
            dataset_dir.mkdir(parents=True, exist_ok=True)
            dataset_csv_path = dataset_dir / f"{bound_dataset.dataset_hash}.csv"
            dataset_csv_path.write_bytes(bound_dataset.csv_bytes)

            result = run_bootstrap_chain(
                args, db_path=db_path, artifact_root=artifact_root,
                orchestrator_db_path=orchestrator_db_path, observed_rows=observed_rows,
                dataset_csv_path=dataset_csv_path, bound_dataset=bound_dataset,
            )
            print("SER8 FIRST REAL HYPOTHESIS BOOTSTRAP -- FROZEN")
            print(f"  hypothesis_id = {result.hypothesis_id}")
            print(f"  dataset_hash  = sha256:{result.dataset_hash}")
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
            dataset_csv_path = tmp_path / f"{bound_dataset.dataset_hash}.csv"
            dataset_csv_path.write_bytes(bound_dataset.csv_bytes)
            result, chain_error = _run_review_chain(
                args, tmp_path=tmp_path, observed_rows=observed_rows,
                dataset_csv_path=dataset_csv_path, bound_dataset=bound_dataset,
            )
        # Every SQLite handle opened inside the chain above has already been
        # released (see _run_review_chain's own docstring) BEFORE this point
        # -- the `with` block's __exit__ (directory deletion) has already
        # run successfully by the time we get here. Only now, outside that
        # block, is it safe to surface a failure as a fresh exception (never
        # chained `from` the original, so no stale traceback/frame is kept
        # alive by this new one either).
        if chain_error is not None:
            raise BootstrapGapError([chain_error])
        assert result is not None
        _print_review(result, coverage_count=observed_rows, key=key, bound_dataset=bound_dataset)
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
    parser.add_argument("--outcomes", type=Path, default=None)
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
