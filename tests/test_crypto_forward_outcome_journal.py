from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from trademind import crypto_forward_outcome_journal as journal
from trademind.crypto_h1_swing_filter import SETUP_FAMILY
from trademind.signal_evidence import load_outcomes


def _dt(hour: int, minute: int, second: int = 0, microsecond: int = 0) -> datetime:
    return datetime(2026, 8, 7, hour, minute, second, microsecond, tzinfo=timezone.utc)


def _ms(value: datetime) -> int:
    return int(value.timestamp() * 1000)


def _candidate(
    signal_id: str,
    observed_at: datetime,
    *,
    action: str = "BUY",
    entry: float = 100.0,
    stop: float = 95.0,
    target: float = 110.0,
) -> dict[str, object]:
    return {
        "signal_id": signal_id,
        "source_decision_id": f"decision-{signal_id}",
        "similarity_key": f"setup-key-{signal_id}",
        "symbol": "BTCUSDT",
        "setup_family": SETUP_FAMILY,
        "observed_at": observed_at.isoformat(),
        "plan": {
            "action": action,
            "entries": [{"price": entry, "allocation": 1.0}],
            "stop_price": stop,
            "targets": [target],
        },
    }


def _write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def _write_bars(path: Path, bars: list[tuple[datetime, float, float, float, float]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=(
                "symbol",
                "start_ms",
                "open",
                "high",
                "low",
                "close",
                "volume",
                "delta_turnover",
            ),
        )
        writer.writeheader()
        for start, open_price, high, low, close in bars:
            writer.writerow(
                {
                    "symbol": "BTCUSDT",
                    "start_ms": _ms(start),
                    "open": open_price,
                    "high": high,
                    "low": low,
                    "close": close,
                    "volume": 1000,
                    "delta_turnover": 100,
                }
            )


def test_first_run_sets_forward_boundary_without_backfill(tmp_path: Path) -> None:
    candidates = tmp_path / "candidates.jsonl"
    bars = tmp_path / "bars.csv"
    root = tmp_path / "out"
    _write_jsonl(candidates, [_candidate("old", _dt(9, 55))])
    _write_bars(bars, [])

    result = journal.run_forward_journal(candidates, bars, root, now=_dt(10, 0))

    assert result.initialized_now is True
    assert result.tracked_new == 0
    assert result.pending_total == 0
    assert result.outcomes_total == 0
    state = json.loads((root / "forward_journal_state.json").read_text(encoding="utf-8"))
    assert state["started_at"] == _dt(10, 0).isoformat()
    assert state["historical_candidates_backfilled"] is False


def test_target_hit_becomes_factory_compatible_win_and_mirror_restores(tmp_path: Path) -> None:
    candidates = tmp_path / "candidates.jsonl"
    bars = tmp_path / "bars.csv"
    root = tmp_path / "out"
    _write_jsonl(candidates, [_candidate("old", _dt(9, 55))])
    _write_bars(bars, [])
    journal.run_forward_journal(candidates, bars, root, now=_dt(10, 0))

    fresh = _candidate("fresh", _dt(10, 4, 59, 999999))
    _write_jsonl(candidates, [_candidate("old", _dt(9, 55)), fresh])
    _write_bars(bars, [(_dt(10, 5), 100.0, 111.0, 99.0, 109.0)])

    result = journal.run_forward_journal(candidates, bars, root, now=_dt(10, 10))

    assert result.tracked_new == 1
    assert result.resolved_new == 1
    assert result.pending_total == 0
    rows = load_outcomes(root / "outcomes.jsonl")
    assert len(rows) == 1
    assert rows[0].signal_id == "fresh"
    assert rows[0].outcome == "WIN"
    assert rows[0].net_r == pytest.approx(2.0)
    assert (root / "forward_outcomes.jsonl").read_text(encoding="utf-8")

    (root / "outcomes.jsonl").write_text("", encoding="utf-8")
    journal.run_forward_journal(candidates, bars, root, now=_dt(10, 15))
    assert len(load_outcomes(root / "outcomes.jsonl")) == 1


def test_stop_hit_becomes_loss(tmp_path: Path) -> None:
    candidates = tmp_path / "candidates.jsonl"
    bars = tmp_path / "bars.csv"
    root = tmp_path / "out"
    _write_jsonl(candidates, [])
    _write_bars(bars, [])
    journal.run_forward_journal(candidates, bars, root, now=_dt(10, 0))

    _write_jsonl(candidates, [_candidate("loss", _dt(10, 4, 59, 999999))])
    _write_bars(bars, [(_dt(10, 5), 100.0, 101.0, 94.0, 96.0)])
    result = journal.run_forward_journal(candidates, bars, root, now=_dt(10, 10))

    assert result.resolved_new == 1
    rows = load_outcomes(root / "outcomes.jsonl")
    assert rows[0].outcome == "LOSS"
    assert rows[0].net_r == -1.0


def test_same_bar_target_and_stop_is_ambiguous_not_evidence(tmp_path: Path) -> None:
    candidates = tmp_path / "candidates.jsonl"
    bars = tmp_path / "bars.csv"
    root = tmp_path / "out"
    _write_jsonl(candidates, [])
    _write_bars(bars, [])
    journal.run_forward_journal(candidates, bars, root, now=_dt(10, 0))

    _write_jsonl(candidates, [_candidate("amb", _dt(10, 4, 59, 999999))])
    _write_bars(bars, [(_dt(10, 5), 100.0, 111.0, 94.0, 100.0)])
    result = journal.run_forward_journal(candidates, bars, root, now=_dt(10, 10))

    assert result.ambiguous_new == 1
    assert result.outcomes_total == 0
    assert load_outcomes(root / "outcomes.jsonl") == []
    ambiguous = journal._read_jsonl(root / "forward_ambiguous.jsonl")
    assert ambiguous[0]["resolution"] == "AMBIGUOUS"
    assert ambiguous[0]["outcome"] == ""
    assert ambiguous[0]["net_r"] is None


def test_missing_m5_bar_blocks_later_resolution(tmp_path: Path) -> None:
    candidates = tmp_path / "candidates.jsonl"
    bars = tmp_path / "bars.csv"
    root = tmp_path / "out"
    _write_jsonl(candidates, [])
    _write_bars(bars, [])
    journal.run_forward_journal(candidates, bars, root, now=_dt(10, 0))

    _write_jsonl(candidates, [_candidate("gap", _dt(10, 4, 59, 999999))])
    _write_bars(bars, [(_dt(10, 10), 100.0, 120.0, 99.0, 115.0)])
    result = journal.run_forward_journal(candidates, bars, root, now=_dt(10, 20))

    assert result.resolved_new == 0
    assert result.pending_total == 1
    assert result.data_gap_pending == 1
    assert load_outcomes(root / "outcomes.jsonl") == []


def test_touch_logic_is_directionally_symmetric() -> None:
    assert journal._touch_outcome("BUY", 111.0, 99.0, 95.0, 110.0) == "TARGET_HIT"
    assert journal._touch_outcome("BUY", 101.0, 94.0, 95.0, 110.0) == "STOP_HIT"
    assert journal._touch_outcome("SELL", 101.0, 89.0, 105.0, 90.0) == "TARGET_HIT"
    assert journal._touch_outcome("SELL", 106.0, 99.0, 105.0, 90.0) == "STOP_HIT"


def test_safety_contract_is_strictly_read_only() -> None:
    safety = journal.safety_contract()
    assert safety["read_only"] is True
    assert safety["orders_enabled"] is False
    assert safety["publication_enabled"] is False
    assert safety["exchange_api_called"] is False
    assert safety["future_bars_used"] is False
    assert safety["source_files_modified"] is False
    assert safety["account_sizing_calculated"] is False
    assert safety["historical_candidates_backfilled"] is False
