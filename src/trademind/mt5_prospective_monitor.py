"""MT5 Prospective Monitor V1: operational plumbing over frozen protocols.

This module is deliberately NOT a new scientific framework. It does not
mine, filter-tune, or select candidates; it does not create a registry, an
evaluator, a provenance model, or a criteria model. It exists only to run
the three already-frozen ``ProspectiveConfirmationProtocol`` candidates
against a repeatedly growing MT5 live signal journal and report their
current status, using the existing V1/V2 evaluation primitives unchanged:

  1) ``.USTECHCASH`` SELL,        horizon=12  (ProspectiveConfirmationProtocolV1)
  2) ``.US30CASH``  BULLISH_FVG,  horizon=12  (ProspectiveConfirmationProtocolV2)
  3) ``XAGUSD``      SELL BEARISH_FVG, horizon=12 (ProspectiveConfirmationProtocolV2)

The three frozen protocol objects below are reconstructed from their exact
already-frozen parameters (the same values used when each was originally
built and persisted to Verified CAS in prior tasks); every field, including
the two predeclared criteria, is validated by the protocol dataclasses'
own ``__post_init__`` exactly as before. The module-level assertions
immediately after construction pin each protocol's recomputed
``protocol_semantic_identity`` to the exact value it was frozen with, so
any accidental drift in these hardcoded parameters fails loudly at import
time rather than silently producing a different, unfrozen candidate.

Evaluation is 100% delegated to ``evaluate_prospective_snapshot`` (V1) and
``evaluate_prospective_snapshot_v2`` (V2) -- same cutoff enforcement, same
non-overlapping-trade methodology (``trademind.validation.validate_rows``),
same PASS/FAIL/WAITING_FOR_DATA criteria. This module only reads a CSV
snapshot (never writes to it) and reshapes each protocol's
``ProspectiveEvaluationResult`` into one concise, machine-readable report.

The monitor is observational only: it never opens, closes, or amends a
trade, never contacts a broker, and never calls a model/provider. Nothing
in this module performs a network request.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

from trademind.prospective_confirmation import (
    ProspectiveConfirmationProtocolV1,
    ProspectiveConfirmationProtocolV2,
    ProspectiveEvaluationResult,
    build_prospective_confirmation_protocol_v1,
    build_prospective_confirmation_protocol_v2,
    evaluate_prospective_snapshot,
    evaluate_prospective_snapshot_v2,
)
from trademind.signal_statistics_provenance import CodeProvenance

_CUTOFF = "2026-07-31T23:45:00+00:00"
_CREATED_AT = "2026-08-16T00:00:00+00:00"
_MINIMUM_SAMPLE = 30
_PRIMARY_METRIC = "avg_net_atr"


def _frozen_code_provenance() -> CodeProvenance:
    # Identical to the code_provenance every already-frozen candidate in
    # this batch was built with; CodeProvenance performs no Git/filesystem
    # lookup of its own (see its own docstring), so this is a stable,
    # replayable claim, not a live re-derivation.
    return CodeProvenance(
        producer_name="trademind",
        producer_version="1.31.1",
        git_commit="f" * 40,
        revision_source="git_worktree",
    )


# --- Candidate 1: .USTECHCASH SELL, horizon=12 (frozen prospective_confirmation V1). ---
USTECHCASH_SELL_H12 = build_prospective_confirmation_protocol_v1(
    symbol=".USTECHCASH",
    action="SELL",
    horizon=12,
    cutoff_time=_CUTOFF,
    minimum_sample=_MINIMUM_SAMPLE,
    primary_metric=_PRIMARY_METRIC,
    source_hypothesis_id="rpi-v1:sha256:71af0449dfdeeb0cae7d76841b034edad1630ce1f9aad8607daa302568a1c9ee:0",
    source_manifest_semantic_hash="sha256:0f543ee0789276e7df0e1171c4d294e7cee932c5be3afab140d316c3c30c1f94",
    source_manifest_artifact_hash_ref="sha256:8b09619d7b8ac9d211c0e22cf4edbd5a79e5d3bdabfa8e0bdd0c018b1959208c",
    source_final_holdout_result_artifact_hash_ref=(
        "sha256:269123a6a121a5d9c89deb0e3dc27b3314af09c54ec4027741d3372f1e9c0a28"
    ),
    code_provenance=_frozen_code_provenance(),
    created_at=_CREATED_AT,
    created_by="operator:prospective-confirmation-v1",
)
assert (
    USTECHCASH_SELL_H12.protocol_semantic_identity
    == "sha256:3db7d16ca43300eee5becdcf66fdcc3c0621a26f7730cc4d9b72552329384259"
), "USTECHCASH SELL H12 protocol drifted from its frozen identity"

# --- Candidate 2: .US30CASH BULLISH_FVG, no action filter, horizon=12 (V2). ---
US30CASH_BULLISH_FVG_H12 = build_prospective_confirmation_protocol_v2(
    symbol=".US30CASH",
    action=None,
    pattern="BULLISH_FVG",
    horizon=12,
    cutoff_time=_CUTOFF,
    minimum_sample=_MINIMUM_SAMPLE,
    primary_metric=_PRIMARY_METRIC,
    source_dataset_description="discovery-only partition of signals_real_20260731.csv",
    source_discovery_row_count=13538,
    sibling_protocol_semantic_identity=USTECHCASH_SELL_H12.protocol_semantic_identity,
    code_provenance=_frozen_code_provenance(),
    created_at=_CREATED_AT,
    created_by="operator:prospective-candidate-batch-v1:candidate-a",
)
assert (
    US30CASH_BULLISH_FVG_H12.protocol_semantic_identity
    == "sha256:c2e2d46d5fa0fe07cbf581c6618d8795fd877e77c3f8087e5aa4a73dae8c32df"
), ".US30CASH BULLISH_FVG protocol drifted from its frozen identity"

# --- Candidate 3: XAGUSD SELL BEARISH_FVG, horizon=12 (V2). ---
XAGUSD_SELL_BEARISH_FVG_H12 = build_prospective_confirmation_protocol_v2(
    symbol="XAGUSD",
    action="SELL",
    pattern="BEARISH_FVG",
    horizon=12,
    cutoff_time=_CUTOFF,
    minimum_sample=_MINIMUM_SAMPLE,
    primary_metric=_PRIMARY_METRIC,
    source_dataset_description="discovery-only partition of signals_real_20260731.csv",
    source_discovery_row_count=13538,
    sibling_protocol_semantic_identity=USTECHCASH_SELL_H12.protocol_semantic_identity,
    code_provenance=_frozen_code_provenance(),
    created_at=_CREATED_AT,
    created_by="operator:prospective-candidate-batch-v1:candidate-b",
)
assert (
    XAGUSD_SELL_BEARISH_FVG_H12.protocol_semantic_identity
    == "sha256:90d24afa47c706572318ca43095d240e3a7386b98a4e0af0ef55259a052bf405"
), "XAGUSD SELL BEARISH_FVG protocol drifted from its frozen identity"


def _describe_filter(
    protocol: ProspectiveConfirmationProtocolV1 | ProspectiveConfirmationProtocolV2,
) -> str:
    action = protocol.action
    pattern = getattr(protocol, "pattern", None)
    parts = [f"symbol={protocol.symbol}"]
    if action is not None:
        parts.append(f"action={action}")
    if pattern is not None:
        parts.append(f"pattern={pattern}")
    parts.append(f"horizon={protocol.horizon}")
    return " ".join(parts)


@dataclass(frozen=True, slots=True)
class CandidateStatusReport:
    candidate_id: str
    filter: str
    cutoff: str
    new_rows_seen: int
    eligible_rows: int
    completed_non_overlapping_trades: int
    remaining_until_30: int
    avg_net_atr_if_available: float | None
    win_rate_if_available: float | None
    status: str
    last_eligible_signal_time: str | None

    def to_payload(self) -> dict[str, object]:
        return asdict(self)


# candidate_id -> (protocol, evaluate function). Every entry reuses the
# existing V1/V2 evaluate_prospective_snapshot* function unchanged; no
# candidate-specific evaluation logic lives in this module.
_CANDIDATES: tuple[
    tuple[
        str,
        ProspectiveConfirmationProtocolV1 | ProspectiveConfirmationProtocolV2,
        Callable[..., ProspectiveEvaluationResult],
    ],
    ...,
] = (
    ("USTECHCASH_SELL_H12", USTECHCASH_SELL_H12, evaluate_prospective_snapshot),
    ("US30CASH_BULLISH_FVG_H12", US30CASH_BULLISH_FVG_H12, evaluate_prospective_snapshot_v2),
    ("XAGUSD_SELL_BEARISH_FVG_H12", XAGUSD_SELL_BEARISH_FVG_H12, evaluate_prospective_snapshot_v2),
)


def read_journal_snapshot(journal_path: str | Path) -> list[dict[str, str]]:
    """Read-only ingest of a journal-schema CSV snapshot.

    Opens the source file strictly for reading ("r") and never writes to
    it, appends to it, or otherwise mutates it -- the growing MT5 journal
    on disk is treated as read-only input, exactly like every existing
    ``evaluate_prospective_snapshot*_csv`` convenience wrapper already does.
    """
    path = Path(journal_path)
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def evaluate_candidate(
    candidate_id: str,
    protocol: ProspectiveConfirmationProtocolV1 | ProspectiveConfirmationProtocolV2,
    evaluate_fn: Callable[..., ProspectiveEvaluationResult],
    rows: list[dict[str, str]],
) -> CandidateStatusReport:
    """Score one frozen candidate against the given snapshot rows.

    ``evaluate_fn`` is always one of the two closed, unmodified
    ``evaluate_prospective_snapshot`` / ``evaluate_prospective_snapshot_v2``
    functions -- the actual cutoff/filter/non-overlapping-trade/criteria
    logic is entirely theirs; this function only reshapes the result.
    """
    result = evaluate_fn(protocol, rows)
    trades = result.completed_non_overlapping_trades
    return CandidateStatusReport(
        candidate_id=candidate_id,
        filter=_describe_filter(protocol),
        cutoff=protocol.cutoff_time,
        new_rows_seen=len(rows),
        eligible_rows=result.eligible_rows_considered,
        completed_non_overlapping_trades=trades,
        remaining_until_30=max(0, protocol.minimum_sample - trades),
        avg_net_atr_if_available=result.avg_net_atr if trades > 0 else None,
        win_rate_if_available=result.win_rate if trades > 0 else None,
        status=result.outcome.value,
        last_eligible_signal_time=result.last_eligible_signal_time,
    )


def run_monitor(journal_path: str | Path) -> list[CandidateStatusReport]:
    """Read one snapshot once, then score all three frozen candidates
    against it. Pure and deterministic: identical bytes on disk always
    yield identical reports, and repeated calls never mutate the source
    file or any candidate's frozen protocol."""
    rows = read_journal_snapshot(journal_path)
    return [
        evaluate_candidate(candidate_id, protocol, evaluate_fn, rows)
        for candidate_id, protocol, evaluate_fn in _CANDIDATES
    ]


def render_report(reports: list[CandidateStatusReport], *, journal_path: str | Path) -> dict[str, object]:
    """One concise, machine-readable status report for all three candidates."""
    return {
        "schema_version": "mt5-prospective-monitor-report-v1",
        "journal_path": str(journal_path),
        "candidates": [report.to_payload() for report in reports],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    default_journal = Path(os.getenv("TRADEMIND_JOURNAL_DIR", "data/journal_ecn")) / "signals.csv"
    parser.add_argument(
        "--journal",
        type=Path,
        default=default_journal,
        help="Path to the live MT5 signal journal CSV snapshot (read-only). "
        "Defaults to $TRADEMIND_JOURNAL_DIR/signals.csv, or data/journal_ecn/signals.csv.",
    )
    args = parser.parse_args(argv)

    reports = run_monitor(args.journal)
    print(json.dumps(render_report(reports, journal_path=args.journal), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
