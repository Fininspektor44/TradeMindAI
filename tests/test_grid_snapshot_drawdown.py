from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path

from trademind.grid_snapshot_drawdown import enrich_grid_legs


LEG_FIELDS = [
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
    "source_ticket",
    "source_position_id",
    "source_comment",
]

POSITION_FIELDS = [
    "time_msc",
    "account_login",
    "server",
    "monitor_start",
    "position_ticket",
    "position_id",
    "position_time_msc",
    "symbol",
    "magic",
    "robot",
    "side",
    "volume",
    "open_price",
    "current_price",
    "sl",
    "tp",
    "profit",
    "swap",
    "comment",
]

ACCOUNT_FIELDS = [
    "time_msc",
    "account_login",
    "server",
    "currency",
    "monitor_start",
    "balance",
    "equity",
    "margin",
    "free_margin",
    "margin_level",
    "open_positions",
]


def _write(path: Path, fields: list[str], rows: list[list[object]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(fields)
        writer.writerows(rows)


def _position_row(
    time_msc: int,
    ticket: int,
    position_id: int,
    symbol: str,
    magic: int,
    side: str,
    volume: float,
    profit: float,
    swap: float,
) -> list[object]:
    return [
        time_msc,
        1,
        "srv",
        0,
        ticket,
        position_id,
        time_msc,
        symbol,
        magic,
        "Grid",
        side,
        volume,
        0.99,
        0.992,
        0,
        0,
        profit,
        swap,
        "",
    ]


def test_snapshot_enrichment_measures_basket_and_account_drawdown(
    tmp_path: Path,
) -> None:
    legs = tmp_path / "legs.csv"
    positions = tmp_path / "positions.csv"
    account = tmp_path / "account.csv"
    output = tmp_path / "enriched.csv"
    summary_dir = tmp_path / "snapshot"

    _write(
        legs,
        LEG_FIELDS,
        [
            [
                "CLOSED",
                "Grid",
                "8",
                "EURUSD",
                "SELL",
                1,
                "2026-08-05T03:00:00+00:00",
                1.10,
                0.1,
                "2026-08-05T03:30:00+00:00",
                10,
                0,
                0,
                10,
                "TP",
                "",
                "",
                "",
                1,
                11,
                "",
            ],
            [
                "OPEN",
                "Grid",
                "8",
                "AUDCAD",
                "SELL",
                1,
                "2026-08-05T04:00:00+00:00",
                0.99,
                0.1,
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                2,
                21,
                "",
            ],
            [
                "OPEN",
                "Grid",
                "8",
                "AUDCAD",
                "SELL",
                2,
                "2026-08-05T04:10:00+00:00",
                0.991,
                0.14,
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                3,
                22,
                "",
            ],
        ],
    )

    t1 = int(datetime(2026, 8, 5, 4, 20, tzinfo=timezone.utc).timestamp() * 1000)
    t2 = int(datetime(2026, 8, 5, 4, 21, tzinfo=timezone.utc).timestamp() * 1000)
    _write(
        positions,
        POSITION_FIELDS,
        [
            _position_row(t1, 101, 21, "AUDCAD", 8, "SELL", 0.10, -12, -1),
            _position_row(t1, 102, 22, "AUDCAD", 8, "SELL", 0.14, -18, -1),
            _position_row(t2, 101, 21, "AUDCAD", 8, "SELL", 0.10, -8, -1),
            _position_row(t2, 102, 22, "AUDCAD", 8, "SELL", 0.14, -10, -1),
            _position_row(t2, 999, 999, "GBPUSD", 77, "BUY", 0.10, 5, 0),
        ],
    )
    _write(
        account,
        ACCOUNT_FIELDS,
        [
            [t1, 1, "srv", "USD", 0, 1000, 968, 0, 0, 0, 2],
            [t2, 1, "srv", "USD", 0, 1000, 980, 0, 0, 0, 2],
        ],
    )

    before_legs = legs.read_bytes()
    before_positions = positions.read_bytes()
    summary = enrich_grid_legs(
        legs,
        positions,
        output,
        summary_dir,
        account_snapshots_path=account,
        now=datetime(2026, 8, 5, 5, 0, tzinfo=timezone.utc),
    )

    assert legs.read_bytes() == before_legs
    assert positions.read_bytes() == before_positions
    assert summary.status["snapshot_covered_baskets"] == 1
    assert summary.status["baskets"] == 2
    assert summary.status["snapshot_coverage"] == 0.5
    assert summary.status["matched_position_snapshot_rows"] == 4
    assert summary.status["unmatched_position_snapshot_rows"] == 1
    assert summary.status["worst_account_floating_drawdown_money"] == 32
    assert summary.status["latest_account_floating_drawdown_money"] == 20

    with output.open(encoding="utf-8") as handle:
        enriched = list(csv.DictReader(handle))
    open_rows = [row for row in enriched if row["basket_id"] == "OPEN"]
    assert len(open_rows) == 2
    assert all(float(row["max_drawdown_money"]) == 32 for row in open_rows)
    assert all(float(row["max_drawdown_pct"]) == 3.2 for row in open_rows)
    closed = [row for row in enriched if row["basket_id"] == "CLOSED"][0]
    assert closed["max_drawdown_money"] == ""

    snapshot_path = summary_dir / "basket_snapshot_drawdown.csv"
    with snapshot_path.open(encoding="utf-8") as handle:
        snapshot_rows = list(csv.DictReader(handle))
    assert len(snapshot_rows) == 1
    row = snapshot_rows[0]
    assert row["basket_id"] == "OPEN"
    assert float(row["max_drawdown_money"]) == 32
    assert float(row["max_drawdown_pct"]) == 3.2
    assert float(row["latest_floating_money"]) == -20
    assert int(row["latest_positions"]) == 2
    assert float(row["basket_age_minutes"]) == 21
    assert float(row["observed_minutes"]) == 1

    status = json.loads((summary_dir / "status.json").read_text(encoding="utf-8"))
    assert status["orders_enabled"] is False
    assert status["source_modified"] is False
    assert status["account_scope"] == "WHOLE_ACCOUNT_UNFILTERED"
