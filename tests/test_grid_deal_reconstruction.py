from __future__ import annotations

import csv
from pathlib import Path

from trademind.grid_basket_audit import run_grid_audit
from trademind.grid_deal_reconstruction import run_reconstruction


FIELDS = [
    "ticket",
    "position_id",
    "time_msc",
    "symbol",
    "magic",
    "robot",
    "deal_type",
    "entry",
    "volume",
    "price",
    "profit",
    "commission",
    "swap",
    "fee",
    "comment",
    "reason",
]


def _write(path: Path) -> None:
    rows = [
        [1, 1001, 1_700_000_000_000, "EURUSD", 77, "Grid", "BUY", "IN", 0.01, 1.10, 0, -0.2, 0, 0, "leg1", "EXPERT"],
        [2, 1002, 1_700_000_300_000, "EURUSD", 77, "Grid", "BUY", "IN", 0.02, 1.09, 0, -0.3, 0, 0, "leg2", "EXPERT"],
        [3, 1001, 1_700_000_600_000, "EURUSD", 77, "Grid", "SELL", "OUT", 0.01, 1.105, 5, -0.1, 0, 0, "close1", "TP"],
        [4, 1002, 1_700_000_900_000, "EURUSD", 77, "Grid", "SELL", "OUT", 0.02, 1.105, 10, -0.1, -0.2, 0, "close2", "TP"],
        [5, 2001, 1_700_001_000_000, "GBPUSD", 88, "Grid2", "SELL", "IN", 0.01, 1.25, 0, -0.2, 0, 0, "open", "EXPERT"],
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(FIELDS)
        writer.writerows(rows)


def test_reconstructs_closed_and_open_baskets(tmp_path: Path) -> None:
    deals = tmp_path / "deals.csv"
    output = tmp_path / "basket_legs.csv"
    _write(deals)
    before = deals.read_bytes()

    summary = run_reconstruction(deals, output)

    assert summary.stats["baskets"] == 2
    assert summary.stats["completed_baskets"] == 1
    assert summary.stats["open_baskets"] == 1
    assert summary.stats["legs"] == 3
    assert deals.read_bytes() == before

    with output.open(encoding="utf-8", newline="") as handle:
        legs = list(csv.DictReader(handle))
    closed = [row for row in legs if row["symbol"] == "EURUSD"]
    assert [row["leg_no"] for row in closed] == ["1", "2"]
    assert all(row["closed_at"] for row in closed)
    assert all(float(row["net_profit"]) == 14.1 for row in closed)
    opened = [row for row in legs if row["symbol"] == "GBPUSD"]
    assert opened[0]["closed_at"] == ""
    assert opened[0]["net_profit"] == ""

    analytics_dir = tmp_path / "analytics"
    analytics = run_grid_audit(output, analytics_dir)
    assert analytics.status["drawdown_coverage"] == 0.0
    assert analytics.status["worst_drawdown_money"] == 0.0
    assert analytics.status["drawdown_missing_is_zero"] is False
    dashboard = (analytics_dir / "dashboard" / "index.html").read_text(encoding="utf-8")
    assert "НЕ ИЗМЕРЕНА" in dashboard
    assert "Покрытие DD" in dashboard
