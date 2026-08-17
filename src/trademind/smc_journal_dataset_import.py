"""Minimal deterministic import adapter for real SMC pattern-journal exports.

This adapter exists for two structural reasons the closed Discovery Engine
primitives do not, by themselves, accommodate for a real multi-symbol
journal:

1. ``experiment_execution_runtime.py`` (unmodified) requires a ``time``
   column, while the production signal journal this evaluator consumes uses
   ``signal_time``.
2. A real multi-symbol scanner journal is written with many rows sharing the
   exact same ``signal_time`` (multiple symbols/patterns detected at the same
   candle close) and is not always in strict file-order by time (near-
   simultaneous writes across symbols). ``trademind.discovery.split_engine
   .chronological_split`` (unmodified) requires *strictly increasing*
   timestamps, so it cannot be handed 22k+ rows with duplicate timestamps
   directly. This adapter instead constructs a ``SplitPlan`` directly --
   ``SplitPlan`` itself only requires the ordering relationship between
   segment boundaries, not unique per-row timestamps -- using the exact same
   60/20/20 default proportions ``chronological_split`` uses, with boundary
   indices pushed forward just far enough that no single timestamp's rows are
   split across two segments (a purely structural, deterministic rule; ties
   always resolve toward the *later* segment, applied identically at every
   boundary).

This adapter performs no outcome-value inspection, no statistics, and no
tuning -- it only verifies schema/order and mechanically copies every
original column through unchanged, adding one duplicate ``time`` column and
reordering rows into chronological order. Only structural facts are used:
column names, row count, and ``signal_time`` values (timestamps are not
outcomes). Nothing in this module computes or reports any aggregate
statistic, and it never writes to the source path.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from trademind.discovery.split_engine import SplitPlan

REQUIRED_COLUMNS = frozenset({"signal_time", "symbol", "timeframe", "action", "atr"})
TIME_ALIAS_COLUMN = "time"
SIGNAL_TIME_COLUMN = "signal_time"

DISCOVERY_FRACTION = 0.60
VALIDATION_FRACTION = 0.20
HOLDOUT_FRACTION = 0.20


class SMCJournalImportError(RuntimeError):
    """Raised when the source journal fails schema, integrity, or ordering checks."""


@dataclass(frozen=True, slots=True)
class DatasetImportSummary:
    source_path: str
    destination_path: str
    row_count: int
    unique_timestamp_count: int
    first_signal_time: str
    last_signal_time: str
    horizon: int
    split_plan: SplitPlan


def _resolve_horizon(fieldnames: frozenset[str], *, preferred_horizons: tuple[int, ...]) -> int:
    """Deterministic, outcome-blind horizon selection from column names only.

    Picks the first horizon (in caller-declared preference order) whose
    ``outcome_<horizon>`` column, and at least one of
    ``progress_atr_<horizon>``/``net_move_<horizon>``, are present. This
    reads only column *names*; no outcome value is inspected.
    """
    for horizon in preferred_horizons:
        has_outcome = f"outcome_{horizon}" in fieldnames
        has_net = f"net_move_{horizon}" in fieldnames or f"progress_atr_{horizon}" in fieldnames
        if has_outcome and has_net:
            return horizon
    raise SMCJournalImportError(
        f"none of the preferred horizons {preferred_horizons} have both an outcome_<horizon> "
        "and a net_move_<horizon>/progress_atr_<horizon> column in the source header"
    )


def _push_boundary_past_ties(times: list[datetime], index: int) -> int:
    """Advance ``index`` until it no longer splits a run of equal timestamps.

    Ties always resolve toward the later segment: every row sharing the
    timestamp straddling the boundary is pushed into the segment that starts
    at or after ``index``.
    """
    while 0 < index < len(times) and times[index - 1] == times[index]:
        index += 1
    return index


def _build_split_plan(times: list[datetime]) -> tuple[SplitPlan, int, int]:
    """Direct SplitPlan construction: exactly chronological_split's default
    proportions, applied to row-index space with tie-safe boundaries."""
    total = len(times)
    discovery_count = _push_boundary_past_ties(times, int(total * DISCOVERY_FRACTION))
    validation_end_index = _push_boundary_past_ties(
        times, discovery_count + max(1, int(total * VALIDATION_FRACTION))
    )
    validation_count = validation_end_index - discovery_count
    holdout_count = total - discovery_count - validation_count
    if min(discovery_count, validation_count, holdout_count) < 1:
        raise SMCJournalImportError(
            "journal is too small, or too concentrated on too few distinct timestamps, "
            "for three non-empty tie-safe chronological segments"
        )
    plan = SplitPlan(
        total_rows=total,
        discovery_count=discovery_count,
        validation_count=validation_count,
        holdout_count=holdout_count,
        discovery_start=times[0].isoformat(),
        discovery_end=times[discovery_count - 1].isoformat(),
        validation_start=times[discovery_count].isoformat(),
        validation_end=times[validation_end_index - 1].isoformat(),
        holdout_start=times[validation_end_index].isoformat(),
        holdout_end=times[-1].isoformat(),
    )
    return plan, discovery_count, validation_end_index


def prepare_smc_journal_dataset(
    source_path: str | Path,
    destination_path: str | Path,
    *,
    preferred_horizons: tuple[int, ...] = (12, 6, 3),
) -> DatasetImportSummary:
    """Verify schema/order, sort, and write an augmented copy; never touches the source.

    Fails closed (``SMCJournalImportError``) on: missing required columns, no
    resolvable horizon, an unparseable or timezone-naive ``signal_time``, an
    empty journal, or a journal too small/concentrated for three non-empty
    chronological segments. Non-strict ordering (duplicate or out-of-file-
    order timestamps) is corrected by a stable, deterministic sort -- never
    by inventing or dropping a row.
    """
    source = Path(source_path)
    destination = Path(destination_path)
    if not source.is_file():
        raise SMCJournalImportError(f"source journal not found: {source}")
    if destination.exists():
        raise SMCJournalImportError(f"destination already exists, refusing to overwrite: {destination}")

    with source.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = frozenset(reader.fieldnames or ())
        missing = REQUIRED_COLUMNS - fieldnames
        if missing:
            raise SMCJournalImportError(
                f"source journal is missing required columns: {', '.join(sorted(missing))}"
            )
        if TIME_ALIAS_COLUMN in fieldnames:
            raise SMCJournalImportError(
                f"source journal already has a {TIME_ALIAS_COLUMN!r} column; "
                "this adapter only adds one to journals that lack it"
            )
        horizon = _resolve_horizon(fieldnames, preferred_horizons=preferred_horizons)

        parsed_rows: list[tuple[datetime, dict[str, str]]] = []
        for row_number, row in enumerate(reader, start=2):
            raw_time = (row.get(SIGNAL_TIME_COLUMN) or "").strip()
            if not raw_time:
                raise SMCJournalImportError(f"row {row_number}: empty {SIGNAL_TIME_COLUMN}")
            try:
                parsed = datetime.fromisoformat(raw_time)
            except ValueError as exc:
                raise SMCJournalImportError(
                    f"row {row_number}: {SIGNAL_TIME_COLUMN} is not a valid ISO timestamp: {raw_time!r}"
                ) from exc
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                raise SMCJournalImportError(f"row {row_number}: {SIGNAL_TIME_COLUMN} must be timezone-aware")
            parsed_rows.append((parsed.astimezone(timezone.utc), dict(row)))

    if not parsed_rows:
        raise SMCJournalImportError("source journal has no data rows")

    # Stable sort: ties (including original file-order inversions) resolve by
    # original row order, so the transformation is fully deterministic.
    parsed_rows.sort(key=lambda item: item[0])
    times = [item[0] for item in parsed_rows]
    rows = [item[1] for item in parsed_rows]

    split_plan, discovery_count, validation_end_index = _build_split_plan(times)

    output_fieldnames = [TIME_ALIAS_COLUMN, *sorted(fieldnames)]
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=output_fieldnames)
        writer.writeheader()
        for row in rows:
            augmented = {TIME_ALIAS_COLUMN: row[SIGNAL_TIME_COLUMN], **row}
            writer.writerow(augmented)

    return DatasetImportSummary(
        source_path=str(source),
        destination_path=str(destination),
        row_count=len(rows),
        unique_timestamp_count=len(set(times)),
        first_signal_time=rows[0][SIGNAL_TIME_COLUMN],
        last_signal_time=rows[-1][SIGNAL_TIME_COLUMN],
        horizon=horizon,
        split_plan=split_plan,
    )


__all__ = [
    "DISCOVERY_FRACTION",
    "HOLDOUT_FRACTION",
    "REQUIRED_COLUMNS",
    "SIGNAL_TIME_COLUMN",
    "TIME_ALIAS_COLUMN",
    "VALIDATION_FRACTION",
    "DatasetImportSummary",
    "SMCJournalImportError",
    "prepare_smc_journal_dataset",
]
