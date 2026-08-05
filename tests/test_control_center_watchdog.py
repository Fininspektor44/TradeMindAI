from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone
from pathlib import Path

from trademind.control_center_watchdog import (
    WATCHDOG_START,
    evaluate_watchdog,
    inject_dashboard,
)


def _write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = list(rows[0])
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _control_status(report_dir: Path, now: datetime, worst_dd: float = 10.0) -> dict:
    return {
        "dashboard": str(report_dir.parent / "dashboard" / "index.html"),
        "robots": [
            {
                "robot": "AOExtremum",
                "account_login": "123",
                "report_dir": str(report_dir),
                "open_baskets": 1,
                "position_snapshot_rows": 1,
                "unmatched_position_snapshot_rows": 0,
                "collector_latest_at": now.isoformat(),
                "worst_account_drawdown_money": worst_dd,
            }
        ],
    }


def test_watchdog_detects_leg_age_and_new_dd_record(tmp_path: Path) -> None:
    now = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)
    report = tmp_path / "report"
    opened_at = now - timedelta(hours=96)
    _write_csv(
        report / "basket_history.csv",
        [
            {
                "basket_id": "B1",
                "completed": 0,
                "symbol": "EURCAD",
                "side": "SELL",
                "max_legs": 6,
                "opened_at": opened_at.isoformat(),
            }
        ],
    )
    _write_csv(
        report / "snapshots" / "basket_snapshot_drawdown.csv",
        [
            {
                "basket_id": "B1",
                "basket_age_minutes": 96 * 60,
                "latest_positions": 6,
            }
        ],
    )

    control = _control_status(report, now, worst_dd=10.0)
    first, state = evaluate_watchdog(control, {}, now=now)
    codes = {row["code"] for row in first["active_alerts"]}
    assert "DANGEROUS_LEG" in codes
    assert "BASKET_STUCK_WARNING" in codes
    assert not first["recent_events"]

    control["robots"][0]["worst_account_drawdown_money"] = 12.5
    second, _ = evaluate_watchdog(control, state, now=now + timedelta(minutes=5))
    assert second["recent_events"][-1]["code"] == "NEW_ACCOUNT_DD_RECORD"


def test_watchdog_detects_stale_and_missing_snapshots(tmp_path: Path) -> None:
    now = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)
    report = tmp_path / "report"
    _write_csv(
        report / "basket_history.csv",
        [
            {
                "basket_id": "B1",
                "completed": 0,
                "symbol": "AUDCAD",
                "side": "BUY",
                "max_legs": 2,
                "opened_at": (now - timedelta(hours=1)).isoformat(),
            }
        ],
    )
    control = _control_status(report, now - timedelta(minutes=30))
    control["robots"][0]["position_snapshot_rows"] = 0
    status, _ = evaluate_watchdog(control, {}, now=now, stale_minutes=15)
    codes = {row["code"] for row in status["active_alerts"]}
    assert "MONITOR_STALE" in codes
    assert "OPEN_BASKETS_WITHOUT_POSITION_SNAPSHOTS" in codes
    assert "OPEN_BASKET_SNAPSHOT_MISSING" in codes
    assert status["state"] == "CRITICAL"


def test_dashboard_injection_is_idempotent(tmp_path: Path) -> None:
    dashboard = tmp_path / "index.html"
    dashboard.write_text(
        "<!doctype html><html><head>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "</head><body><div class='robot-grid'></div></body></html>",
        encoding="utf-8",
    )
    watch_status = {
        "state": "OK",
        "active_alerts": [],
        "recent_events": [],
    }
    inject_dashboard(dashboard, watch_status)
    inject_dashboard(dashboard, watch_status)
    rendered = dashboard.read_text(encoding="utf-8")
    assert rendered.count(WATCHDOG_START) == 1
    assert rendered.count("http-equiv='refresh'") == 1
    assert "активных предупреждений нет" in rendered
