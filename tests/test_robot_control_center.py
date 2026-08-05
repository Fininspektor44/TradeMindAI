from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path

from trademind.robot_control_center import ReportSpec, run_control_center


def _write_csv(path: Path, fields: list[str], rows: list[list[object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(fields)
        writer.writerows(rows)


def _write_report(
    root: Path,
    *,
    name: str,
    account: str,
    measured: int,
    net: float,
    open_symbol: str,
) -> ReportSpec:
    report = root / name
    snapshot = report / "snapshots"
    snapshot.mkdir(parents=True)
    status = {
        "schema_version": "1.15.1",
        "state": "OK",
        "baskets": 12,
        "completed_baskets": 11,
        "open_baskets": 1,
        "wins": 11,
        "losses": 0,
        "net_profit": net,
        "profit_factor": 999,
        "drawdown_measured_baskets": measured,
        "drawdown_coverage": measured / 12,
        "collector_eligible_baskets": measured,
        "collector_measured_baskets": measured,
        "collector_drawdown_coverage": 1.0,
        "worst_drawdown_money": 47.1,
        "worst_drawdown_pct": 0.48,
        "latest_balance": 10_000,
        "latest_equity": 9_980,
        "latest_account_floating_drawdown_money": 20,
        "worst_account_floating_drawdown_money": 72.85,
        "worst_account_floating_drawdown_pct": 0.7285,
        "max_legs": 5,
        "max_concurrent_baskets": 4,
        "collector_started_at": "2026-08-05T04:35:00+00:00",
        "orders_enabled": False,
        "source_modified": False,
        "logic_changed": False,
    }
    (report / "status.json").write_text(
        json.dumps(status), encoding="utf-8"
    )
    snapshot_status = {
        "monitoring_latest_at": "2026-08-05T11:00:00+00:00",
        "position_snapshot_rows": 120,
        "unmatched_position_snapshot_rows": 0,
    }
    (snapshot / "status.json").write_text(
        json.dumps(snapshot_status), encoding="utf-8"
    )

    history_fields = [
        "basket_id",
        "symbol",
        "side",
        "opened_at",
        "closed_at",
        "completed",
        "max_legs",
        "duration_minutes",
        "max_drawdown_money",
        "max_drawdown_pct",
    ]
    rows: list[list[object]] = []
    for index in range(11):
        rows.append(
            [
                f"{name}-C-{index}",
                "EURUSD",
                "BUY",
                f"2026-08-{index + 1:02d}T00:00:00+00:00",
                f"2026-08-{index + 1:02d}T02:00:00+00:00",
                1,
                1 + index % 3,
                120,
                10,
                0.1,
            ]
        )
    rows.append(
        [
            f"{name}-OPEN",
            open_symbol,
            "SELL",
            "2026-08-05T06:00:00+00:00",
            "",
            0,
            2,
            0,
            47.1,
            0.48,
        ]
    )
    _write_csv(report / "basket_history.csv", history_fields, rows)

    _write_csv(
        report / "risk_by_leg.csv",
        [
            "leg_no",
            "baskets_reaching_leg",
            "next_leg_rate",
            "drawdown_sample_size",
            "average_max_drawdown_money",
            "worst_max_drawdown_money",
            "average_net_profit",
        ],
        [[1, 12, 0.5, measured, 20, 47.1, net / 11]],
    )
    _write_csv(
        snapshot / "basket_snapshot_drawdown.csv",
        [
            "basket_id",
            "max_legs",
            "basket_age_minutes",
            "latest_floating_money",
            "max_drawdown_money",
            "max_drawdown_pct",
            "latest_volume",
            "latest_positions",
        ],
        [[f"{name}-OPEN", 2, 300, -20, 47.1, 0.48, 0.24, 2]],
    )
    return ReportSpec(name=name, account_login=account, report_dir=report)


def test_control_center_builds_read_only_comparison(tmp_path: Path) -> None:
    ao = _write_report(
        tmp_path / "reports",
        name="AOExtremum",
        account="37365712",
        measured=3,
        net=50,
        open_symbol="EURCAD",
    )
    multi = _write_report(
        tmp_path / "reports",
        name="MultiRSI",
        account="37353316",
        measured=5,
        net=3222.95,
        open_symbol="AUDCAD",
    )
    tracked = [
        ao.report_dir / "status.json",
        ao.report_dir / "basket_history.csv",
        multi.report_dir / "status.json",
        multi.report_dir / "basket_history.csv",
    ]
    before = {path: path.read_bytes() for path in tracked}

    output = tmp_path / "control"
    status = run_control_center(
        [ao, multi],
        output,
        now=datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc),
    )

    assert status["state"] == "OK"
    assert status["comparison_ready"] is False
    assert status["orders_enabled"] is False
    assert status["source_modified"] is False
    assert {item["robot"] for item in status["robots"]} == {
        "AOExtremum",
        "MultiRSI",
    }
    assert all(path.read_bytes() == before[path] for path in tracked)

    dashboard = (output / "dashboard" / "index.html").read_text(encoding="utf-8")
    assert "TradeMind Control Center v1.15.2" in dashboard
    assert "AOExtremum" in dashboard
    assert "MultiRSI" in dashboard
    assert "минимум 30 измеренных корзин DD" in dashboard
    assert "EURCAD SELL" in dashboard
    assert "AUDCAD SELL" in dashboard
    assert "3222.95" in dashboard

    summary_rows = list(
        csv.DictReader((output / "robot_summary.csv").open(encoding="utf-8"))
    )
    assert len(summary_rows) == 2
    assert summary_rows[0]["account_login"] == "37365712"
    assert summary_rows[1]["account_login"] == "37353316"


def test_control_center_marks_preliminary_comparison_ready(tmp_path: Path) -> None:
    first = _write_report(
        tmp_path / "reports",
        name="First",
        account="1",
        measured=30,
        net=100,
        open_symbol="EURUSD",
    )
    second = _write_report(
        tmp_path / "reports",
        name="Second",
        account="2",
        measured=100,
        net=200,
        open_symbol="GBPUSD",
    )

    status = run_control_center([first, second], tmp_path / "control")

    assert status["comparison_ready"] is True
    dashboard = (tmp_path / "control" / "dashboard" / "index.html").read_text(
        encoding="utf-8"
    )
    assert "минимального порога для предварительного сравнения" in dashboard
