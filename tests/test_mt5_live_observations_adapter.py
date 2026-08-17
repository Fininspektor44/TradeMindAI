"""Tests for the live ECN observations.csv adapter behavior of the MT5
Prospective Monitor: the same three frozen candidates, evaluated against a
snapshot shaped exactly like the real live-signal-runtime observations.csv
schema (``trademind.fx_research._OBSERVATION_FIELDS``), rather than the
older, narrower journal_ecn/signals.csv shape already covered by
``tests/test_mt5_prospective_monitor.py``.
"""

from __future__ import annotations

import csv
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import trademind.mt5_prospective_monitor as mt5_monitor
from trademind.fx_research import _OBSERVATION_FIELDS
from trademind.mt5_prospective_monitor import (
    LIVE_OBSERVATIONS_REQUIRED_COLUMNS,
    read_journal_snapshot,
    run_monitor,
)

# Referenced as ``mt5_monitor.LiveObservationsSchemaError`` (module attribute,
# not a direct `from ... import`) below on purpose: another test module in
# this suite exercises ``importlib.reload`` on this same module, which
# rebinds class objects in its shared namespace in place. A directly
# imported class reference captured at collection time would go stale
# relative to whatever the reloaded module actually raises; a module
# attribute lookup always resolves the current binding, exactly like the
# production code's own (dynamic, globals-dict) lookup does.

CUTOFF = "2026-07-31T23:45:00+00:00"


def _observation_row(
    hours_after_cutoff: float,
    *,
    symbol: str = ".USTECHCASH",
    action: str = "SELL",
    timeframe: str = "M5",
    fvg_direction: str = "",
    net_move: float = 2.0,
    atr: float = 1.0,
    outcome: str = "WIN",
    progress_atr: float | None = None,
) -> dict[str, str]:
    cutoff_dt = datetime.fromisoformat(CUTOFF)
    signal_time = (cutoff_dt + timedelta(hours=hours_after_cutoff)).isoformat()
    row = dict.fromkeys(_OBSERVATION_FIELDS, "")
    row.update(
        {
            "schema_version": "fx-research-observation-v1",
            "observation_id": f"{symbol}:M5:{hash((symbol, signal_time)) & 0xFFFF}",
            "signal_time": signal_time,
            "symbol": symbol,
            "timeframe": timeframe,
            "session": "LONDON",
            "action": action,
            "atr": str(atr),
            "fvg_direction": fvg_direction,
            "net_move_12": str(net_move),
            "outcome_12": outcome,
        }
    )
    if progress_atr is not None:
        row["progress_atr_12"] = str(progress_atr)
    return row


def _rows_spaced(
    count: int, *, start_hours_after_cutoff: float = 1.0, step_hours: float = 24.0, **kwargs
) -> list[dict[str, str]]:
    return [_observation_row(start_hours_after_cutoff + step_hours * index, **kwargs) for index in range(count)]


def _write_observations_csv(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(_OBSERVATION_FIELDS))
        writer.writeheader()
        writer.writerows(rows)


# ---------------------------------------------------------------------------
# Real live schema accepted end-to-end.
# ---------------------------------------------------------------------------


def test_real_live_observations_schema_is_accepted(tmp_path: Path) -> None:
    journal = tmp_path / "observations.csv"
    _write_observations_csv(journal, _rows_spaced(5, symbol=".USTECHCASH", action="SELL"))
    reports = run_monitor(journal)
    ustech = next(r for r in reports if r.candidate_id == "USTECHCASH_SELL_H12")
    assert ustech.eligible_rows == 5
    assert ustech.new_rows_seen == 5


def test_all_required_columns_are_present_in_the_real_schema() -> None:
    # The real observations.csv schema is a superset of what the adapter
    # requires -- no renaming/reshaping is needed.
    missing = [c for c in LIVE_OBSERVATIONS_REQUIRED_COLUMNS if c not in _OBSERVATION_FIELDS]
    assert missing == []


# ---------------------------------------------------------------------------
# Cutoff / pre-cutoff isolation (timestamp semantics preserved).
# ---------------------------------------------------------------------------


def test_pre_cutoff_observations_never_count(tmp_path: Path) -> None:
    journal = tmp_path / "observations.csv"
    _write_observations_csv(journal, _rows_spaced(10, start_hours_after_cutoff=-24 * 100))
    reports = run_monitor(journal)
    for report in reports:
        assert report.eligible_rows == 0
        assert report.status == "WAITING_FOR_DATA"


def test_cutoff_row_itself_never_counts(tmp_path: Path) -> None:
    journal = tmp_path / "observations.csv"
    exact_cutoff_row = _observation_row(0.0)
    assert exact_cutoff_row["signal_time"] == CUTOFF
    _write_observations_csv(journal, [exact_cutoff_row])
    reports = run_monitor(journal)
    ustech = next(r for r in reports if r.candidate_id == "USTECHCASH_SELL_H12")
    assert ustech.eligible_rows == 0


def test_post_cutoff_observations_count(tmp_path: Path) -> None:
    journal = tmp_path / "observations.csv"
    _write_observations_csv(journal, _rows_spaced(3, start_hours_after_cutoff=1.0))
    reports = run_monitor(journal)
    ustech = next(r for r in reports if r.candidate_id == "USTECHCASH_SELL_H12")
    assert ustech.eligible_rows == 3


# ---------------------------------------------------------------------------
# All three candidate filters against the real schema shape.
# ---------------------------------------------------------------------------


def test_ustechcash_sell_candidate_filter(tmp_path: Path) -> None:
    journal = tmp_path / "observations.csv"
    rows = [
        _observation_row(1.0, symbol=".USTECHCASH", action="SELL"),
        _observation_row(2.0, symbol=".USTECHCASH", action="BUY"),
        _observation_row(3.0, symbol="XAUUSD", action="SELL"),
    ]
    _write_observations_csv(journal, rows)
    reports = run_monitor(journal)
    ustech = next(r for r in reports if r.candidate_id == "USTECHCASH_SELL_H12")
    assert ustech.eligible_rows == 1


def test_us30cash_bullish_fvg_candidate_ignores_action(tmp_path: Path) -> None:
    journal = tmp_path / "observations.csv"
    rows = [
        _observation_row(1.0, symbol=".US30CASH", action="BUY", fvg_direction="BULLISH"),
        _observation_row(2.0, symbol=".US30CASH", action="SELL", fvg_direction="BULLISH"),
        _observation_row(3.0, symbol=".US30CASH", action="BUY", fvg_direction="BEARISH"),
    ]
    _write_observations_csv(journal, rows)
    reports = run_monitor(journal)
    us30 = next(r for r in reports if r.candidate_id == "US30CASH_BULLISH_FVG_H12")
    assert us30.eligible_rows == 2


def test_xagusd_sell_bearish_fvg_candidate_requires_both(tmp_path: Path) -> None:
    journal = tmp_path / "observations.csv"
    rows = [
        _observation_row(1.0, symbol="XAGUSD", action="SELL", fvg_direction="BEARISH"),
        _observation_row(2.0, symbol="XAGUSD", action="BUY", fvg_direction="BEARISH"),
        _observation_row(3.0, symbol="XAGUSD", action="SELL", fvg_direction="BULLISH"),
    ]
    _write_observations_csv(journal, rows)
    reports = run_monitor(journal)
    xag = next(r for r in reports if r.candidate_id == "XAGUSD_SELL_BEARISH_FVG_H12")
    assert xag.eligible_rows == 1


# ---------------------------------------------------------------------------
# BULLISH_FVG / BEARISH_FVG extraction directly from the live fvg_direction
# field (same closed trademind.smc_stats._event_groups grouping).
# ---------------------------------------------------------------------------


def test_fvg_direction_extraction_matches_the_closed_event_grouping(tmp_path: Path) -> None:
    journal = tmp_path / "observations.csv"
    rows = [
        _observation_row(1.0, symbol=".US30CASH", fvg_direction="BULLISH"),
        _observation_row(2.0, symbol=".US30CASH", fvg_direction="bullish"),  # smc_stats upper()s first: matches.
        _observation_row(3.0, symbol=".US30CASH", fvg_direction="NONE"),  # no FVG: excluded.
        _observation_row(4.0, symbol=".US30CASH", fvg_direction=""),  # empty: excluded.
    ]
    _write_observations_csv(journal, rows)
    reports = run_monitor(journal)
    us30 = next(r for r in reports if r.candidate_id == "US30CASH_BULLISH_FVG_H12")
    assert us30.eligible_rows == 2
    assert us30.last_eligible_signal_time == rows[1]["signal_time"]


# ---------------------------------------------------------------------------
# H12 completion: only completed (WIN/LOSS/FLAT) outcome_12 rows count;
# still-open/pending observations (empty outcome_12) never count as trades.
# ---------------------------------------------------------------------------


def test_pending_h12_outcome_is_not_a_completed_trade(tmp_path: Path) -> None:
    journal = tmp_path / "observations.csv"
    rows = [
        _observation_row(1.0, outcome="WIN"),
        _observation_row(25.0, outcome=""),  # H12 hasn't closed yet: not yet a trade.
    ]
    _write_observations_csv(journal, rows)
    reports = run_monitor(journal)
    ustech = next(r for r in reports if r.candidate_id == "USTECHCASH_SELL_H12")
    # Both rows are "eligible" (post-cutoff, symbol/action match); only one
    # is a completed non-overlapping trade.
    assert ustech.eligible_rows == 2
    assert ustech.completed_non_overlapping_trades == 1


def test_progress_atr_field_is_used_when_present(tmp_path: Path) -> None:
    journal = tmp_path / "observations.csv"
    # net_move/atr would normally give avg_net_atr == 2.0; progress_atr_12
    # (the richer, already-present field) should take precedence.
    rows = _rows_spaced(30, net_move=2.0, atr=1.0, progress_atr=9.0)
    _write_observations_csv(journal, rows)
    reports = run_monitor(journal)
    ustech = next(r for r in reports if r.candidate_id == "USTECHCASH_SELL_H12")
    assert ustech.avg_net_atr_if_available == pytest.approx(9.0)


# ---------------------------------------------------------------------------
# Non-overlapping trade logic preserved (M5 * horizon=12 == 60 minutes).
# ---------------------------------------------------------------------------


def test_non_overlapping_trades_collapse_within_the_horizon_window(tmp_path: Path) -> None:
    journal = tmp_path / "observations.csv"
    # M5 timeframe * horizon=12 == a 60-minute exclusion window; 30 minutes
    # apart falls strictly inside it, so the second signal is excluded.
    rows = _rows_spaced(2, start_hours_after_cutoff=1.0, step_hours=0.5)
    _write_observations_csv(journal, rows)
    reports = run_monitor(journal)
    ustech = next(r for r in reports if r.candidate_id == "USTECHCASH_SELL_H12")
    assert ustech.eligible_rows == 2
    assert ustech.completed_non_overlapping_trades == 1  # only the first survives.


def test_rows_spaced_beyond_the_horizon_window_do_not_collapse(tmp_path: Path) -> None:
    journal = tmp_path / "observations.csv"
    rows = _rows_spaced(3, start_hours_after_cutoff=1.0, step_hours=2.0)  # > 60 min window.
    _write_observations_csv(journal, rows)
    reports = run_monitor(journal)
    ustech = next(r for r in reports if r.candidate_id == "USTECHCASH_SELL_H12")
    assert ustech.completed_non_overlapping_trades == 3


# ---------------------------------------------------------------------------
# WAITING / PASS / FAIL.
# ---------------------------------------------------------------------------


def test_below_30_trades_is_waiting_for_data(tmp_path: Path) -> None:
    journal = tmp_path / "observations.csv"
    _write_observations_csv(journal, _rows_spaced(29, net_move=5.0))
    reports = run_monitor(journal)
    ustech = next(r for r in reports if r.candidate_id == "USTECHCASH_SELL_H12")
    assert ustech.status == "WAITING_FOR_DATA"
    assert ustech.remaining_until_30 == 1


def test_at_or_above_30_trades_positive_average_is_pass(tmp_path: Path) -> None:
    journal = tmp_path / "observations.csv"
    _write_observations_csv(journal, _rows_spaced(30, net_move=5.0))
    reports = run_monitor(journal)
    ustech = next(r for r in reports if r.candidate_id == "USTECHCASH_SELL_H12")
    assert ustech.status == "PASS"


def test_at_or_above_30_trades_negative_average_is_fail(tmp_path: Path) -> None:
    journal = tmp_path / "observations.csv"
    _write_observations_csv(journal, _rows_spaced(30, net_move=-5.0, outcome="LOSS"))
    reports = run_monitor(journal)
    ustech = next(r for r in reports if r.candidate_id == "USTECHCASH_SELL_H12")
    assert ustech.status == "FAIL"


# ---------------------------------------------------------------------------
# Source file discipline / determinism.
# ---------------------------------------------------------------------------


def test_source_observations_file_is_never_modified(tmp_path: Path) -> None:
    journal = tmp_path / "observations.csv"
    _write_observations_csv(journal, _rows_spaced(40, net_move=3.0))
    before_bytes = journal.read_bytes()
    before_mtime = journal.stat().st_mtime_ns

    run_monitor(journal)
    run_monitor(journal)

    assert journal.read_bytes() == before_bytes
    assert journal.stat().st_mtime_ns == before_mtime


def test_deterministic_replay_of_the_same_snapshot(tmp_path: Path) -> None:
    journal = tmp_path / "observations.csv"
    _write_observations_csv(journal, _rows_spaced(35, net_move=1.5))
    first = run_monitor(journal)
    second = run_monitor(journal)
    assert first == second


# ---------------------------------------------------------------------------
# Fail-closed on missing/incompatible required columns.
# ---------------------------------------------------------------------------


def test_missing_required_column_fails_closed(tmp_path: Path) -> None:
    journal = tmp_path / "observations.csv"
    fieldnames = [c for c in _OBSERVATION_FIELDS if c != "fvg_direction"]  # drop a required column.
    with journal.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
    with pytest.raises(mt5_monitor.LiveObservationsSchemaError, match="fvg_direction"):
        read_journal_snapshot(journal)


def test_multiple_missing_required_columns_are_all_named(tmp_path: Path) -> None:
    journal = tmp_path / "observations.csv"
    fieldnames = ["symbol", "action"]  # drop almost everything required.
    with journal.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
    with pytest.raises(mt5_monitor.LiveObservationsSchemaError) as excinfo:
        read_journal_snapshot(journal)
    for column in ("signal_time", "timeframe", "atr", "fvg_direction", "net_move_12", "outcome_12"):
        assert column in str(excinfo.value)


def test_completely_empty_file_fails_closed(tmp_path: Path) -> None:
    journal = tmp_path / "observations.csv"
    journal.write_text("", encoding="utf-8")
    with pytest.raises(mt5_monitor.LiveObservationsSchemaError):
        read_journal_snapshot(journal)


def test_run_monitor_propagates_the_schema_error(tmp_path: Path) -> None:
    journal = tmp_path / "observations.csv"
    journal.write_text("symbol,action\n", encoding="utf-8")
    with pytest.raises(mt5_monitor.LiveObservationsSchemaError):
        run_monitor(journal)
