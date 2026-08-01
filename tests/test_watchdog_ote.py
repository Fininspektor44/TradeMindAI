from __future__ import annotations

import csv
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from trademind.watchdog_ote import run_watchdog_ote


def _touch(path: Path, now: datetime, content: str = "ok") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    os.utime(path, (now.timestamp(), now.timestamp()))


def _source(path: Path, symbol: str, now: datetime) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=(
                "schema_version",
                "time",
                "symbol",
                "timeframe",
                "tick_count",
                "tick_copy_status",
            ),
        )
        writer.writeheader()
        writer.writerow(
            {
                "schema_version": "1.4",
                "time": str(int(now.timestamp())),
                "symbol": symbol,
                "timeframe": "M5",
                "tick_count": "120",
                "tick_copy_status": "OK",
            }
        )
    os.utime(path, (now.timestamp(), now.timestamp()))


def _state_csv(path: Path, now: datetime, statuses: tuple[str, ...]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=("status", "captured_at"))
        writer.writeheader()
        for status in statuses:
            writer.writerow({"status": status, "captured_at": now.isoformat()})
    os.utime(path, (now.timestamp(), now.timestamp()))


def _task_rows(now: datetime) -> list[dict[str, object]]:
    return [
        {
            "name": name,
            "exists": True,
            "enabled": True,
            "state": "Ready",
            "last_task_result": 0,
            "last_run_time": now.isoformat(),
            "next_run_time": (now + timedelta(minutes=5)).isoformat(),
        }
        for name in (
            "TradeMindAI-v1.4-VolumeCollector",
            "TradeMindAI-v1.4.2-FXResearch",
            "TradeMindAI-v1.5-SMC-OTE",
        )
    ]


def _paths(tmp_path: Path, now: datetime) -> dict[str, Path]:
    source_dir = tmp_path / "common"
    _source(source_dir / "volume_EURUSD_M5.csv", "EURUSD", now)
    _source(source_dir / "volume_GBPUSD_M5.csv", "GBPUSD", now)

    paths = {
        "source_dir": source_dir,
        "volume": tmp_path / "data" / "volume.csv",
        "observations": tmp_path / "data" / "observations.csv",
        "states": tmp_path / "data" / "fx_latest.csv",
        "dashboard": tmp_path / "data" / "fx_dashboard.html",
        "ote_signals": tmp_path / "data" / "ote_signals.csv",
        "ote_states": tmp_path / "data" / "ote_latest.csv",
        "ote_dashboard": tmp_path / "data" / "ote_dashboard.html",
        "tasks": tmp_path / "data" / "tasks.json",
        "status": tmp_path / "watchdog" / "status.json",
        "report": tmp_path / "watchdog" / "report.txt",
        "html": tmp_path / "watchdog" / "index.html",
    }
    _touch(paths["volume"], now, "header\nrow\n")
    _touch(paths["observations"], now, "header\nrow\n")
    _state_csv(paths["states"], now, ("INSUFFICIENT_SAMPLE", "RESEARCH_CANDIDATE"))
    _touch(paths["dashboard"], now, "<html>fx</html>")
    _touch(paths["ote_signals"], now, "header\nrow\n")
    _state_csv(paths["ote_states"], now, ("INSUFFICIENT_SAMPLE", "CANDIDATE"))
    _touch(paths["ote_dashboard"], now, "<html>ote</html>")
    _touch(paths["tasks"], now, json.dumps(_task_rows(now)))
    return paths


def _run(paths: dict[str, Path], now: datetime):
    return run_watchdog_ote(
        source_dir=paths["source_dir"],
        volume_path=paths["volume"],
        observations_path=paths["observations"],
        states_path=paths["states"],
        dashboard_path=paths["dashboard"],
        ote_signals_path=paths["ote_signals"],
        ote_states_path=paths["ote_states"],
        ote_dashboard_path=paths["ote_dashboard"],
        task_snapshot_path=paths["tasks"],
        status_path=paths["status"],
        report_path=paths["report"],
        html_path=paths["html"],
        expected_symbols=("EURUSD", "GBPUSD"),
        now=now,
    )


def test_watchdog_ote_healthy_pipeline_is_ok(tmp_path: Path) -> None:
    now = datetime(2026, 7, 31, 18, 0, tzinfo=timezone.utc)
    paths = _paths(tmp_path, now)

    snapshot = _run(paths, now)

    assert snapshot.overall_status == "OK"
    assert snapshot.notify_required is False
    assert snapshot.schema_version == "1.5.1"
    assert snapshot.validation_counts == {
        "FX_INSUFFICIENT_SAMPLE": 1,
        "FX_RESEARCH_CANDIDATE": 1,
        "OTE_INSUFFICIENT_SAMPLE": 1,
        "OTE_CANDIDATE": 1,
    }
    names = {check.name for check in snapshot.checks}
    assert "SMC OTE signals" in names
    assert "SMC OTE latest states" in names
    assert "SMC OTE dashboard" in names
    assert "Task: TradeMindAI-v1.5-SMC-OTE" in names
    assert "TradeMind AI v1.5.1 Watchdog" in paths["report"].read_text(encoding="utf-8")


def test_watchdog_ote_missing_dashboard_is_error(tmp_path: Path) -> None:
    now = datetime(2026, 7, 31, 18, 0, tzinfo=timezone.utc)
    paths = _paths(tmp_path, now)
    paths["ote_dashboard"].unlink()

    snapshot = _run(paths, now)

    assert snapshot.overall_status == "ERROR"
    assert snapshot.notify_required is True
    check = next(item for item in snapshot.checks if item.name == "SMC OTE dashboard")
    assert check.status == "ERROR"
    assert "missing file" in check.message


def test_watchdog_ote_rejects_failed_task(tmp_path: Path) -> None:
    now = datetime(2026, 7, 31, 18, 0, tzinfo=timezone.utc)
    paths = _paths(tmp_path, now)
    rows = _task_rows(now)
    rows[2]["last_task_result"] = 1
    _touch(paths["tasks"], now, json.dumps(rows))

    snapshot = _run(paths, now)

    assert snapshot.overall_status == "ERROR"
    task = next(item for item in snapshot.checks if "v1.5-SMC-OTE" in item.name)
    assert task.status == "ERROR"
    assert "last result=1" in task.message


def test_watchdog_ote_contract_is_read_only() -> None:
    text = "\n".join(
        Path(path).read_text(encoding="utf-8")
        for path in (
            "src/trademind/watchdog_ote.py",
            "scripts/run_v144_watchdog.ps1",
        )
    )
    forbidden = (
        "CTrade",
        "OrderSend(",
        ".Buy(",
        ".Sell(",
        "PositionClose(",
        "TRADE_ACTION_DEAL",
    )
    assert all(token not in text for token in forbidden)
