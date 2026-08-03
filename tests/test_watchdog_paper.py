from __future__ import annotations

import csv
from pathlib import Path

from trademind.watchdog_paper import inspect_paper_states


def _write_states(path: Path, statuses: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=("gate_status", "symbol"))
        writer.writeheader()
        for index, status in enumerate(statuses):
            writer.writerow({"gate_status": status, "symbol": f"S{index}"})


def test_paper_state_watchdog_accepts_contract_statuses(tmp_path: Path) -> None:
    path = tmp_path / "latest.csv"
    _write_states(path, ["WATCH", "CANDIDATE", "VALIDATED", "REJECTED"])
    check, counts = inspect_paper_states(path)
    assert check.status == "OK"
    assert counts == {"WATCH": 1, "CANDIDATE": 1, "VALIDATED": 1, "REJECTED": 1}


def test_paper_state_watchdog_rejects_unknown_status(tmp_path: Path) -> None:
    path = tmp_path / "latest.csv"
    _write_states(path, ["WATCH", "MAGIC"])
    check, counts = inspect_paper_states(path)
    assert check.status == "ERROR"
    assert counts == {"WATCH": 1}
