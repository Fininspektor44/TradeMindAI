from __future__ import annotations

import csv
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from trademind.watchdog import run_watchdog


def _touch(path: Path, now: datetime, content: str = "ok") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    os.utime(path, (now.timestamp(), now.timestamp()))


def _source(path: Path, symbol: str, now: datetime) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "schema_version",
        "time",
        "symbol",
        "timeframe",
        "tick_count",
        "tick_copy_status",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
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


def _states(path: Path, now: datetime) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["status", "captured_at"])
        writer.writeheader()
        writer.writerow({"status": "INSUFFICIENT_SAMPLE", "captured_at": now.isoformat()})
        writer.writerow({"status": "RESEARCH_CANDIDATE", "captured_at": now.isoformat()})
    os.utime(path, (now.timestamp(), now.timestamp()))


def _tasks(path: Path, now: datetime) -> None:
    payload = [
        {
            "name": "TradeMindAI-v1.4-VolumeCollector",
            "exists": True,
            "enabled": True,
            "state": "Ready",
            "last_task_result": 0,
            "last_run_time": now.isoformat(),
            "next_run_time": (now + timedelta(minutes=5)).isoformat(),
        },
        {
            "name": "TradeMindAI-v1.4.2-FXResearch",
            "exists": True,
            "enabled": True,
            "state": "Ready",
            "last_task_result": 0,
            "last_run_time": now.isoformat(),
            "next_run_time": (now + timedelta(minutes=5)).isoformat(),
        },
    ]
    _touch(path, now, json.dumps(payload))


def _paths(tmp_path: Path, now: datetime) -> dict[str, Path]:
    source_dir = tmp_path / "common"
    _source(source_dir / "volume_EURUSD_M5.csv", "EURUSD", now)
    _source(source_dir / "volume_GBPUSD_M5.csv", "GBPUSD", now)
    volume = tmp_path / "data" / "volume.csv"
    observations = tmp_path / "data" / "observations.csv"
    states = tmp_path / "data" / "latest.csv"
    dashboard = tmp_path / "data" / "dashboard.html"
    tasks = tmp_path / "data" / "tasks.json"
    _touch(volume, now, "header\nrow\n")
    _touch(observations, now, "header\nrow\n")
    _states(states, now)
    _touch(dashboard, now, "<html>ok</html>")
    _tasks(tasks, now)
    return {
        "source_dir": source_dir,
        "volume": volume,
        "observations": observations,
        "states": states,
        "dashboard": dashboard,
        "tasks": tasks,
        "status": tmp_path / "watchdog" / "status.json",
        "report": tmp_path / "watchdog" / "report.txt",
        "html": tmp_path / "watchdog" / "index.html",
    }


def _run(paths: dict[str, Path], now: datetime):
    return run_watchdog(
        source_dir=paths["source_dir"],
        volume_path=paths["volume"],
        observations_path=paths["observations"],
        states_path=paths["states"],
        dashboard_path=paths["dashboard"],
        task_snapshot_path=paths["tasks"],
        status_path=paths["status"],
        report_path=paths["report"],
        html_path=paths["html"],
        expected_symbols=("EURUSD", "GBPUSD"),
        now=now,
    )


def test_watchdog_healthy_pipeline_is_ok(tmp_path: Path) -> None:
    now = datetime(2026, 7, 31, 18, 0, tzinfo=timezone.utc)
    paths = _paths(tmp_path, now)

    snapshot = _run(paths, now)

    assert snapshot.overall_status == "OK"
    assert snapshot.notify_required is False
    assert snapshot.validation_counts == {
        "INSUFFICIENT_SAMPLE": 1,
        "RESEARCH_CANDIDATE": 1,
    }
    assert paths["status"].is_file()
    assert "Overall status: OK" in paths["report"].read_text(encoding="utf-8")
    assert "TradeMind Watchdog" in paths["html"].read_text(encoding="utf-8")


def test_watchdog_notifies_only_on_error_transition(tmp_path: Path) -> None:
    now = datetime(2026, 7, 31, 18, 0, tzinfo=timezone.utc)
    paths = _paths(tmp_path, now)
    paths["dashboard"].unlink()

    first = _run(paths, now)
    second = _run(paths, now + timedelta(minutes=1))

    assert first.overall_status == "ERROR"
    assert first.notify_required is True
    assert second.overall_status == "ERROR"
    assert second.notify_required is False


def test_watchdog_reports_stale_streams_as_error_on_weekday(tmp_path: Path) -> None:
    now = datetime(2026, 7, 31, 18, 0, tzinfo=timezone.utc)
    paths = _paths(tmp_path, now)
    stale = now - timedelta(minutes=45)
    for path in paths["source_dir"].glob("*.csv"):
        os.utime(path, (stale.timestamp(), stale.timestamp()))

    snapshot = _run(paths, now)

    assert snapshot.overall_status == "ERROR"
    source_check = next(check for check in snapshot.checks if check.name == "MT5 source streams")
    assert source_check.status == "ERROR"
    assert "stale" in source_check.message


def test_watchdog_rejects_failed_windows_task(tmp_path: Path) -> None:
    now = datetime(2026, 7, 31, 18, 0, tzinfo=timezone.utc)
    paths = _paths(tmp_path, now)
    payload = json.loads(paths["tasks"].read_text(encoding="utf-8"))
    payload[1]["last_task_result"] = 1
    _touch(paths["tasks"], now, json.dumps(payload))

    snapshot = _run(paths, now)

    assert snapshot.overall_status == "ERROR"
    task = next(check for check in snapshot.checks if "FXResearch" in check.name)
    assert task.status == "ERROR"
    assert "last result=1" in task.message


def test_watchdog_contract_is_read_only() -> None:
    text = "\n".join(
        Path(path).read_text(encoding="utf-8")
        for path in (
            "src/trademind/watchdog.py",
            "scripts/run_v144_watchdog.ps1",
            "scripts/install_v144_watchdog_task.ps1",
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
