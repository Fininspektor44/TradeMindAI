from __future__ import annotations

import csv
from pathlib import Path

from trademind.watchdog_unified import inspect_unified_states


def test_inspect_unified_states_counts_statuses(tmp_path: Path) -> None:
    path = tmp_path / "latest.csv"
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=("status", "scenario"))
        writer.writeheader()
        writer.writerow({"status": "CANDIDATE", "scenario": "BOS_PLUS_SWEEP"})
        writer.writerow({"status": "INSUFFICIENT_SAMPLE", "scenario": "OTE_705"})
    check, counts = inspect_unified_states(path)
    assert check.status == "OK"
    assert counts == {"CANDIDATE": 1, "INSUFFICIENT_SAMPLE": 1}


def test_inspect_unified_states_rejects_missing_status(tmp_path: Path) -> None:
    path = tmp_path / "latest.csv"
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=("status", "scenario"))
        writer.writeheader()
        writer.writerow({"status": "", "scenario": "FVG"})
    check, _counts = inspect_unified_states(path)
    assert check.status == "ERROR"
    assert "without status" in check.message


def test_unified_watchdog_is_read_only() -> None:
    root = Path(__file__).resolve().parents[1]
    paths = [
        root / "src" / "trademind" / "watchdog_unified.py",
        root / "scripts" / "run_v144_watchdog.ps1",
    ]
    forbidden = (
        "CTrade",
        "OrderSend",
        "PositionClose",
        "TRADE_ACTION_DEAL",
        ".Buy(",
        ".Sell(",
    )
    for path in paths:
        text = path.read_text(encoding="utf-8")
        for token in forbidden:
            assert token not in text
