from __future__ import annotations

import csv
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from trademind.mt5_prospective_monitor import (
    US30CASH_BULLISH_FVG_H12,
    USTECHCASH_SELL_H12,
    XAGUSD_SELL_BEARISH_FVG_H12,
    read_journal_snapshot,
    render_report,
    run_monitor,
)

CUTOFF = "2026-07-31T23:45:00+00:00"
FIELDNAMES = [
    "signal_time",
    "symbol",
    "timeframe",
    "action",
    "atr",
    "fvg_direction",
    "net_move_12",
    "outcome_12",
]


def _row(
    hours_after_cutoff: float,
    *,
    symbol: str = ".USTECHCASH",
    action: str = "SELL",
    fvg_direction: str = "",
    net_move: float = 2.0,
    atr: float = 1.0,
    outcome: str = "WIN",
) -> dict[str, str]:
    cutoff_dt = datetime.fromisoformat(CUTOFF)
    signal_time = (cutoff_dt + timedelta(hours=hours_after_cutoff)).isoformat()
    return {
        "signal_time": signal_time,
        "symbol": symbol,
        "timeframe": "M5",
        "action": action,
        "atr": str(atr),
        "fvg_direction": fvg_direction,
        "net_move_12": str(net_move),
        "outcome_12": outcome,
    }


def _rows_spaced(
    count: int, *, start_hours_after_cutoff: float = 1.0, step_hours: float = 24.0, **kwargs
) -> list[dict[str, str]]:
    return [_row(start_hours_after_cutoff + step_hours * index, **kwargs) for index in range(count)]


def _write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)


# ---------------------------------------------------------------------------
# Frozen candidate identity (module-level assertions already ran at import,
# but pin them explicitly here too so a test failure names the cause).
# ---------------------------------------------------------------------------


def test_three_frozen_candidates_loaded_with_exact_frozen_filters() -> None:
    assert USTECHCASH_SELL_H12.symbol == ".USTECHCASH"
    assert USTECHCASH_SELL_H12.action == "SELL"
    assert USTECHCASH_SELL_H12.horizon == 12
    assert USTECHCASH_SELL_H12.cutoff_time == CUTOFF
    assert USTECHCASH_SELL_H12.minimum_sample == 30

    assert US30CASH_BULLISH_FVG_H12.symbol == ".US30CASH"
    assert US30CASH_BULLISH_FVG_H12.action is None
    assert US30CASH_BULLISH_FVG_H12.pattern == "BULLISH_FVG"
    assert US30CASH_BULLISH_FVG_H12.horizon == 12

    assert XAGUSD_SELL_BEARISH_FVG_H12.symbol == "XAGUSD"
    assert XAGUSD_SELL_BEARISH_FVG_H12.action == "SELL"
    assert XAGUSD_SELL_BEARISH_FVG_H12.pattern == "BEARISH_FVG"
    assert XAGUSD_SELL_BEARISH_FVG_H12.horizon == 12


# ---------------------------------------------------------------------------
# Cutoff discipline: historical rows never count, cutoff row never counts,
# future rows count.
# ---------------------------------------------------------------------------


def test_historical_rows_never_count(tmp_path: Path) -> None:
    journal = tmp_path / "signals.csv"
    _write_csv(journal, _rows_spaced(10, start_hours_after_cutoff=-24 * 100))
    reports = run_monitor(journal)
    ustech = next(r for r in reports if r.candidate_id == "USTECHCASH_SELL_H12")
    assert ustech.eligible_rows == 0
    assert ustech.completed_non_overlapping_trades == 0
    assert ustech.status == "WAITING_FOR_DATA"
    assert ustech.last_eligible_signal_time is None


def test_cutoff_row_itself_never_counts(tmp_path: Path) -> None:
    journal = tmp_path / "signals.csv"
    exact_cutoff_row = _row(0.0)
    assert exact_cutoff_row["signal_time"] == CUTOFF
    _write_csv(journal, [exact_cutoff_row])
    reports = run_monitor(journal)
    ustech = next(r for r in reports if r.candidate_id == "USTECHCASH_SELL_H12")
    assert ustech.eligible_rows == 0


def test_future_rows_count(tmp_path: Path) -> None:
    journal = tmp_path / "signals.csv"
    _write_csv(journal, _rows_spaced(5, start_hours_after_cutoff=1.0))
    reports = run_monitor(journal)
    ustech = next(r for r in reports if r.candidate_id == "USTECHCASH_SELL_H12")
    assert ustech.eligible_rows == 5
    assert ustech.new_rows_seen == 5


def test_mixed_historical_and_future_only_counts_future(tmp_path: Path) -> None:
    journal = tmp_path / "signals.csv"
    historical = _rows_spaced(20, start_hours_after_cutoff=-24 * 100)
    future = _rows_spaced(5, start_hours_after_cutoff=1.0)
    _write_csv(journal, historical + future)
    reports = run_monitor(journal)
    ustech = next(r for r in reports if r.candidate_id == "USTECHCASH_SELL_H12")
    assert ustech.eligible_rows == 5
    assert ustech.new_rows_seen == 25  # total snapshot size, informational.


# ---------------------------------------------------------------------------
# All three protocol/filter variants (V1 action-only, V2 pattern-only, V2
# pattern+action) work correctly and independently.
# ---------------------------------------------------------------------------


def test_v1_candidate_filters_by_symbol_and_action(tmp_path: Path) -> None:
    journal = tmp_path / "signals.csv"
    rows = [
        _row(1.0, symbol=".USTECHCASH", action="SELL"),
        _row(2.0, symbol=".USTECHCASH", action="BUY"),  # wrong action.
        _row(3.0, symbol="XAUUSD", action="SELL"),  # wrong symbol.
    ]
    _write_csv(journal, rows)
    reports = run_monitor(journal)
    ustech = next(r for r in reports if r.candidate_id == "USTECHCASH_SELL_H12")
    assert ustech.eligible_rows == 1


def test_v2_pattern_only_candidate_ignores_action(tmp_path: Path) -> None:
    journal = tmp_path / "signals.csv"
    rows = [
        _row(1.0, symbol=".US30CASH", action="BUY", fvg_direction="BULLISH"),
        _row(2.0, symbol=".US30CASH", action="SELL", fvg_direction="BULLISH"),  # action irrelevant.
        _row(3.0, symbol=".US30CASH", action="BUY", fvg_direction="BEARISH"),  # wrong pattern.
    ]
    _write_csv(journal, rows)
    reports = run_monitor(journal)
    us30 = next(r for r in reports if r.candidate_id == "US30CASH_BULLISH_FVG_H12")
    assert us30.eligible_rows == 2


def test_v2_pattern_and_action_candidate_requires_both(tmp_path: Path) -> None:
    journal = tmp_path / "signals.csv"
    rows = [
        _row(1.0, symbol="XAGUSD", action="SELL", fvg_direction="BEARISH"),
        _row(2.0, symbol="XAGUSD", action="BUY", fvg_direction="BEARISH"),  # wrong action.
        _row(3.0, symbol="XAGUSD", action="SELL", fvg_direction="BULLISH"),  # wrong pattern.
    ]
    _write_csv(journal, rows)
    reports = run_monitor(journal)
    xag = next(r for r in reports if r.candidate_id == "XAGUSD_SELL_BEARISH_FVG_H12")
    assert xag.eligible_rows == 1


# ---------------------------------------------------------------------------
# Determinism / restart-reload.
# ---------------------------------------------------------------------------


def test_repeated_same_snapshot_is_deterministic(tmp_path: Path) -> None:
    journal = tmp_path / "signals.csv"
    _write_csv(journal, _rows_spaced(40, net_move=3.0))
    first = run_monitor(journal)
    second = run_monitor(journal)
    assert first == second


def test_deterministic_across_fresh_module_reimport(tmp_path: Path) -> None:
    # Simulates a process restart: reload the module fresh and rerun.
    import importlib

    import trademind.mt5_prospective_monitor as module

    journal = tmp_path / "signals.csv"
    _write_csv(journal, _rows_spaced(35, net_move=1.5))
    before = run_monitor(journal)

    reloaded = importlib.reload(module)
    after = reloaded.run_monitor(journal)
    assert [r.to_payload() for r in before] == [r.to_payload() for r in after]


def test_render_report_is_json_serializable(tmp_path: Path) -> None:
    import json

    journal = tmp_path / "signals.csv"
    _write_csv(journal, _rows_spaced(5))
    reports = run_monitor(journal)
    payload = render_report(reports, journal_path=journal)
    encoded = json.dumps(payload)
    assert "USTECHCASH_SELL_H12" in encoded
    assert "US30CASH_BULLISH_FVG_H12" in encoded
    assert "XAGUSD_SELL_BEARISH_FVG_H12" in encoded


# ---------------------------------------------------------------------------
# Sample-size gating.
# ---------------------------------------------------------------------------


def test_below_30_trades_remains_waiting(tmp_path: Path) -> None:
    journal = tmp_path / "signals.csv"
    _write_csv(journal, _rows_spaced(29, net_move=5.0))
    reports = run_monitor(journal)
    ustech = next(r for r in reports if r.candidate_id == "USTECHCASH_SELL_H12")
    assert ustech.completed_non_overlapping_trades == 29
    assert ustech.remaining_until_30 == 1
    assert ustech.status == "WAITING_FOR_DATA"
    assert ustech.avg_net_atr_if_available is not None  # trades > 0, so reported.


def test_zero_trades_reports_null_metrics(tmp_path: Path) -> None:
    journal = tmp_path / "signals.csv"
    _write_csv(journal, [])
    reports = run_monitor(journal)
    ustech = next(r for r in reports if r.candidate_id == "USTECHCASH_SELL_H12")
    assert ustech.completed_non_overlapping_trades == 0
    assert ustech.avg_net_atr_if_available is None
    assert ustech.win_rate_if_available is None
    assert ustech.remaining_until_30 == 30


def test_at_or_above_30_trades_produces_pass(tmp_path: Path) -> None:
    journal = tmp_path / "signals.csv"
    _write_csv(journal, _rows_spaced(30, net_move=5.0))
    reports = run_monitor(journal)
    ustech = next(r for r in reports if r.candidate_id == "USTECHCASH_SELL_H12")
    assert ustech.completed_non_overlapping_trades == 30
    assert ustech.remaining_until_30 == 0
    assert ustech.status == "PASS"
    assert ustech.avg_net_atr_if_available == pytest.approx(5.0)


def test_at_or_above_30_trades_produces_fail(tmp_path: Path) -> None:
    journal = tmp_path / "signals.csv"
    _write_csv(journal, _rows_spaced(30, net_move=-5.0, outcome="LOSS"))
    reports = run_monitor(journal)
    ustech = next(r for r in reports if r.candidate_id == "USTECHCASH_SELL_H12")
    assert ustech.completed_non_overlapping_trades == 30
    assert ustech.status == "FAIL"
    assert ustech.avg_net_atr_if_available == pytest.approx(-5.0)


# ---------------------------------------------------------------------------
# Read-only source file.
# ---------------------------------------------------------------------------


def test_original_csv_is_never_modified(tmp_path: Path) -> None:
    journal = tmp_path / "signals.csv"
    _write_csv(journal, _rows_spaced(40, net_move=3.0))
    before_bytes = journal.read_bytes()
    before_mtime = journal.stat().st_mtime_ns

    run_monitor(journal)
    run_monitor(journal)  # run twice: still never touched.

    assert journal.read_bytes() == before_bytes
    assert journal.stat().st_mtime_ns == before_mtime


def test_read_journal_snapshot_does_not_mutate_rows_across_calls(tmp_path: Path) -> None:
    journal = tmp_path / "signals.csv"
    rows = _rows_spaced(5)
    _write_csv(journal, rows)
    first = read_journal_snapshot(journal)
    second = read_journal_snapshot(journal)
    assert first == second


# ---------------------------------------------------------------------------
# No trading/network side effects: the module imports nothing broker- or
# network-shaped, and its only I/O primitive is a read-only CSV open.
# ---------------------------------------------------------------------------


def test_module_has_no_broker_or_network_imports() -> None:
    import ast

    import trademind.mt5_prospective_monitor as module

    source = Path(module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_names.add(node.module)

    forbidden_substrings = ["socket", "requests", "urllib", "metatrader5", "http.client", "mt5"]
    lowered_imports = {name.lower() for name in imported_names}
    for name in lowered_imports:
        for term in forbidden_substrings:
            assert term not in name, f"unexpected network/broker-shaped import: {name!r}"


def test_cli_main_prints_json_and_returns_zero(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    import json

    from trademind.mt5_prospective_monitor import main

    journal = tmp_path / "signals.csv"
    _write_csv(journal, _rows_spaced(5))
    exit_code = main(["--journal", str(journal)])
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert len(payload["candidates"]) == 3
