from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path

from trademind.grid_basket_analytics import run_grid_analytics


FIELDS = [
    "basket_id",
    "robot",
    "magic",
    "symbol",
    "side",
    "leg_no",
    "opened_at",
    "price",
    "volume",
    "closed_at",
    "gross_profit",
    "commission",
    "swap",
    "net_profit",
    "exit_reason",
    "max_drawdown_money",
    "max_drawdown_pct",
    "max_adverse_points",
]


def _write(path: Path) -> None:
    rows = [
        ["A", "Grid", "101", "EURUSD", "BUY", 1, "2026-01-01T00:00:00Z", 1.10, 0.01, "2026-01-01T02:00:00Z", 12, -1, 0, 11, "TP", 20, 2, 200],
        ["B", "Grid", "101", "EURUSD", "BUY", 1, "2026-01-01T01:00:00Z", 1.11, 0.01, "2026-01-01T04:00:00Z", -48, -2, 0, -50, "SL", 80, 8, 800],
        ["B", "Grid", "101", "EURUSD", "BUY", 2, "2026-01-01T02:00:00Z", 1.10, 0.02, "2026-01-01T04:00:00Z", -48, -2, 0, -50, "SL", 80, 8, 800],
        ["B", "Grid", "101", "EURUSD", "BUY", 3, "2026-01-01T03:00:00Z", 1.09, 0.04, "2026-01-01T04:00:00Z", -48, -2, 0, -50, "SL", 80, 8, 800],
        ["C", "Grid", "202", "GBPUSD", "SELL", 1, "2026-01-01T01:30:00Z", 1.30, 0.01, "2026-01-01T03:00:00Z", 8, -1, 0, 7, "TP", 15, 1.5, 150],
        ["C", "Grid", "202", "GBPUSD", "SELL", 2, "2026-01-01T02:30:00Z", 1.31, 0.02, "2026-01-01T03:00:00Z", 8, -1, 0, 7, "TP", 15, 1.5, 150],
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(FIELDS)
        writer.writerows(rows)


def test_grid_analytics_builds_separate_read_only_reports(tmp_path: Path) -> None:
    source = tmp_path / "basket_legs.csv"
    output = tmp_path / "out"
    _write(source)
    before = source.read_bytes()

    summary = run_grid_analytics(
        source,
        output,
        now=datetime(2026, 1, 2, tzinfo=timezone.utc),
    )

    assert summary.source_rows == 6
    assert summary.baskets == 3
    assert summary.completed_baskets == 3
    assert summary.status["orders_enabled"] is False
    assert summary.status["signal_generation_enabled"] is False
    assert summary.status["source_modified"] is False
    assert summary.status["max_legs"] == 3
    assert summary.status["max_concurrent_baskets"] == 3
    assert source.read_bytes() == before

    with (output / "risk_by_leg.csv").open(encoding="utf-8", newline="") as handle:
        risk = list(csv.DictReader(handle))
    assert risk[0]["baskets_reaching_leg"] == "3"
    assert risk[0]["next_leg_count"] == "2"
    assert risk[1]["baskets_reaching_leg"] == "2"
    assert risk[1]["next_leg_count"] == "1"
    assert risk[2]["baskets_reaching_leg"] == "1"
    assert risk[2]["stop_exits"] == "1"

    with (output / "portfolio_overlap.csv").open(encoding="utf-8", newline="") as handle:
        overlaps = list(csv.DictReader(handle))
    assert overlaps
    assert (output / "basket_history.csv").is_file()
    assert (output / "symbol_report.csv").is_file()
    assert (output / "status.json").is_file()
    assert (output / "dashboard" / "index.html").is_file()
