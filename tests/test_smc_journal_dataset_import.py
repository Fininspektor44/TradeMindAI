from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from trademind.smc_journal_dataset_import import (
    SMCJournalImportError,
    prepare_smc_journal_dataset,
)

_HEADER = [
    "signal_time",
    "symbol",
    "timeframe",
    "action",
    "atr",
    "net_move_12",
    "outcome_12",
]


def _write_source(path: Path, rows: list[list[str]], *, header: list[str] = _HEADER) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(rows)


def _row(hour: int, *, minute: int = 0, symbol: str = "EURUSD", action: str = "BUY") -> list[str]:
    time = (datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(hours=hour, minutes=minute)).isoformat()
    return [time, symbol, "H1", action, "1.0", "2.0", "WIN"]


def _ordered_rows(count: int = 10) -> list[list[str]]:
    return [_row(index) for index in range(count)]


def test_adapter_adds_time_column_preserves_all_original_columns(tmp_path: Path) -> None:
    source = tmp_path / "source.csv"
    _write_source(source, _ordered_rows())
    destination = tmp_path / "prepared.csv"

    summary = prepare_smc_journal_dataset(source, destination)

    assert summary.row_count == 10
    assert summary.unique_timestamp_count == 10
    assert summary.horizon == 12
    assert summary.split_plan.total_rows == 10

    with destination.open("r", encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 10
    for original, prepared in zip(_ordered_rows(), rows):
        assert prepared["time"] == prepared["signal_time"] == original[0]
        assert prepared["symbol"] == original[1]
        assert prepared["action"] == original[3]
        assert prepared["net_move_12"] == original[5]
        assert prepared["outcome_12"] == original[6]

    # Source file is byte-for-byte untouched.
    with source.open("r", encoding="utf-8") as handle:
        assert handle.readline().strip() == ",".join(_HEADER)


def test_adapter_never_overwrites_existing_destination(tmp_path: Path) -> None:
    source = tmp_path / "source.csv"
    _write_source(source, _ordered_rows())
    destination = tmp_path / "prepared.csv"
    destination.write_text("pre-existing", encoding="utf-8")
    with pytest.raises(SMCJournalImportError, match="already exists"):
        prepare_smc_journal_dataset(source, destination)
    assert destination.read_text(encoding="utf-8") == "pre-existing"


def test_adapter_rejects_missing_required_columns(tmp_path: Path) -> None:
    source = tmp_path / "source.csv"
    header = [c for c in _HEADER if c != "atr"]
    _write_source(source, [row[:4] + row[5:] for row in _ordered_rows()], header=header)
    with pytest.raises(SMCJournalImportError, match="missing required columns"):
        prepare_smc_journal_dataset(source, tmp_path / "out.csv")


def test_adapter_rejects_no_resolvable_horizon(tmp_path: Path) -> None:
    source = tmp_path / "source.csv"
    header = ["signal_time", "symbol", "timeframe", "action", "atr", "net_move_99", "outcome_99"]
    _write_source(source, _ordered_rows(), header=header)
    with pytest.raises(SMCJournalImportError, match="none of the preferred horizons"):
        prepare_smc_journal_dataset(source, tmp_path / "out.csv")


def test_adapter_resolves_largest_preferred_horizon_present(tmp_path: Path) -> None:
    source = tmp_path / "source.csv"
    header = [
        "signal_time",
        "symbol",
        "timeframe",
        "action",
        "atr",
        "net_move_3",
        "outcome_3",
        "net_move_6",
        "outcome_6",
    ]
    rows = [row + ["1.0", "WIN"] for row in _ordered_rows()]
    _write_source(source, rows, header=header)
    summary = prepare_smc_journal_dataset(source, tmp_path / "out.csv")
    assert summary.horizon == 6  # 12 absent, 6 preferred over 3.


def test_adapter_rejects_naive_timestamp(tmp_path: Path) -> None:
    source = tmp_path / "source.csv"
    rows = _ordered_rows()
    rows[1][0] = "2026-01-01T01:00:00"  # no timezone.
    _write_source(source, rows)
    with pytest.raises(SMCJournalImportError, match="timezone-aware"):
        prepare_smc_journal_dataset(source, tmp_path / "out.csv")


def test_adapter_rejects_source_already_having_time_column(tmp_path: Path) -> None:
    source = tmp_path / "source.csv"
    header = ["time", *_HEADER]
    rows = [[row[0], *row] for row in _ordered_rows()]
    _write_source(source, rows, header=header)
    with pytest.raises(SMCJournalImportError, match="already has"):
        prepare_smc_journal_dataset(source, tmp_path / "out.csv")


def test_adapter_rejects_missing_source(tmp_path: Path) -> None:
    with pytest.raises(SMCJournalImportError, match="not found"):
        prepare_smc_journal_dataset(tmp_path / "missing.csv", tmp_path / "out.csv")


def test_adapter_rejects_empty_journal(tmp_path: Path) -> None:
    source = tmp_path / "source.csv"
    _write_source(source, [])
    with pytest.raises(SMCJournalImportError, match="no data rows"):
        prepare_smc_journal_dataset(source, tmp_path / "out.csv")


# ---------------------------------------------------------------------------
# Real-world properties: out-of-file-order rows and duplicate timestamps
# ---------------------------------------------------------------------------


def test_adapter_deterministically_sorts_out_of_file_order_rows(tmp_path: Path) -> None:
    source = tmp_path / "source.csv"
    rows = _ordered_rows()
    rows[1], rows[2] = rows[2], rows[1]  # file-order inversion, like real near-simultaneous writes.
    _write_source(source, rows)
    destination = tmp_path / "out.csv"
    prepare_smc_journal_dataset(source, destination)
    with destination.open("r", encoding="utf-8", newline="") as handle:
        times = [row["time"] for row in csv.DictReader(handle)]
    assert times == sorted(times)


def test_adapter_ties_resolve_toward_later_segment_and_no_data_lost(tmp_path: Path) -> None:
    # 10 rows, but rows 4 and 5 (0-indexed) share one timestamp, straddling
    # where a naive 60% boundary (index 6) would otherwise fall.
    rows = _ordered_rows(10)
    rows[5][0] = rows[4][0]  # duplicate timestamp at the discovery/validation seam.
    source = tmp_path / "source.csv"
    _write_source(source, rows)
    destination = tmp_path / "out.csv"
    summary = prepare_smc_journal_dataset(source, destination)

    assert summary.row_count == 10
    assert summary.unique_timestamp_count == 9
    plan = summary.split_plan
    assert plan.discovery_count + plan.validation_count + plan.holdout_count == 10
    # The tied timestamp (rows 4 and 5) must not straddle discovery/validation.
    assert plan.discovery_end < plan.validation_start
    assert plan.validation_end < plan.holdout_start

    with destination.open("r", encoding="utf-8", newline="") as handle:
        prepared = list(csv.DictReader(handle))
    assert len(prepared) == 10
    prepared_times = [row["time"] for row in prepared]
    assert prepared_times == sorted(prepared_times)


def test_adapter_rejects_journal_too_concentrated_for_three_segments(tmp_path: Path) -> None:
    # All rows share one timestamp: no tie-safe boundary can separate them
    # into three non-empty segments at all.
    source = tmp_path / "source.csv"
    rows = [_row(0, minute=index) for index in range(5)]
    for row in rows:
        row[0] = rows[0][0]  # force identical timestamps.
    _write_source(source, rows)
    with pytest.raises(SMCJournalImportError, match="too concentrated"):
        prepare_smc_journal_dataset(source, tmp_path / "out.csv")


def test_split_plan_matches_default_chronological_split_proportions_when_unique(tmp_path: Path) -> None:
    from trademind.discovery.split_engine import chronological_split

    source = tmp_path / "source.csv"
    rows = _ordered_rows(20)  # all-unique timestamps, 1 hour apart.
    _write_source(source, rows)
    summary = prepare_smc_journal_dataset(source, tmp_path / "out.csv")

    timestamps = [datetime.fromisoformat(row[0]) for row in rows]
    expected = chronological_split(timestamps)
    assert summary.split_plan.discovery_count == expected.discovery_count
    assert summary.split_plan.validation_count == expected.validation_count
    assert summary.split_plan.holdout_count == expected.holdout_count
    assert summary.split_plan.to_payload() == expected.to_payload()
