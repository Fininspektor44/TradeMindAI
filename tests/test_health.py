from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone

from trademind.health import _gap_statistics, inspect_journal, inspect_market_file


def _write_market_csv(path, rows: list[dict[str, str]]) -> None:
    fieldnames = [
        "time",
        "symbol",
        "timeframe",
        "open",
        "high",
        "low",
        "close",
        "tick_volume",
        "spread",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def test_market_health_detects_fresh_file_and_zero_spread(tmp_path) -> None:
    now = datetime(2026, 7, 15, 12, 10, tzinfo=timezone.utc)
    path = tmp_path / "XAUUSD_M5.csv"
    _write_market_csv(
        path,
        [
            {
                "time": str(int((now - timedelta(minutes=5)).timestamp())),
                "symbol": "XAUUSD",
                "timeframe": "M5",
                "open": "4000",
                "high": "4002",
                "low": "3999",
                "close": "4001",
                "tick_volume": "1200",
                "spread": "0",
            }
        ],
    )

    health = inspect_market_file(
        path,
        "XAUUSD",
        "M5",
        now=now,
        max_age_minutes=20,
    )

    assert health.status == "WARN"
    assert health.rows == 1
    assert health.spread == 0
    assert "non-positive spread" in health.message


def test_market_health_marks_missing_and_stale_files(tmp_path) -> None:
    now = datetime(2026, 7, 15, 12, 10, tzinfo=timezone.utc)
    missing = inspect_market_file(
        tmp_path / "missing.csv",
        "XAUUSD",
        "M5",
        now=now,
        max_age_minutes=20,
    )
    assert missing.status == "ERROR"

    path = tmp_path / "XAUUSD_M5.csv"
    _write_market_csv(
        path,
        [
            {
                "time": str(int((now - timedelta(hours=2)).timestamp())),
                "symbol": "XAUUSD",
                "timeframe": "M5",
                "open": "4000",
                "high": "4002",
                "low": "3999",
                "close": "4001",
                "tick_volume": "1200",
                "spread": "5",
            }
        ],
    )
    stale = inspect_market_file(
        path,
        "XAUUSD",
        "M5",
        now=now,
        max_age_minutes=20,
    )
    assert stale.status == "ERROR"
    assert "older than 20 minutes" in stale.message


def test_gap_statistics_ignore_weekend_but_count_weekday_gap() -> None:
    friday = datetime(2026, 7, 17, 21, 55, tzinfo=timezone.utc)
    monday = datetime(2026, 7, 20, 0, 5, tzinfo=timezone.utc)
    monday_late = datetime(2026, 7, 20, 1, 0, tzinfo=timezone.utc)

    count, maximum = _gap_statistics([friday, monday, monday_late], 5, 6)

    assert count == 1
    assert maximum == 55.0


def test_journal_health_counts_schema_rows_and_duplicates(tmp_path) -> None:
    now = datetime(2026, 7, 15, 12, 10, tzinfo=timezone.utc)
    path = tmp_path / "signals.csv"
    fieldnames = ["schema_version", "signal_id", "signal_time", "symbol"]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(
            [
                {
                    "schema_version": "1.1",
                    "signal_id": "XAUUSD:M5:1",
                    "signal_time": (now - timedelta(minutes=5)).isoformat(),
                    "symbol": "XAUUSD",
                },
                {
                    "schema_version": "1.1",
                    "signal_id": "XAUUSD:M5:1",
                    "signal_time": (now - timedelta(minutes=5)).isoformat(),
                    "symbol": "XAUUSD",
                },
            ]
        )

    health = inspect_journal(
        path,
        ["XAUUSD", "BRENT"],
        schema_version="1.1",
        now=now,
        max_age_minutes=20,
    )

    assert health.status == "ERROR"
    assert health.schema_rows == 2
    assert health.duplicate_ids == 1
    assert health.counts == {"XAUUSD": 2, "BRENT": 0}
