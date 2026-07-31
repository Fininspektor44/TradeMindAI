from __future__ import annotations

import csv
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from trademind.watchdog import inspect_source_streams


def test_stale_stream_is_warning_during_weekend_closure(tmp_path: Path) -> None:
    now = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
    stale = now - timedelta(hours=3)
    path = tmp_path / "volume_EURUSD_M5.csv"
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["time", "symbol", "timeframe", "tick_count", "tick_copy_status"],
        )
        writer.writeheader()
        writer.writerow(
            {
                "time": str(int(stale.timestamp())),
                "symbol": "EURUSD",
                "timeframe": "M5",
                "tick_count": "100",
                "tick_copy_status": "OK",
            }
        )
    os.utime(path, (stale.timestamp(), stale.timestamp()))

    result = inspect_source_streams(
        tmp_path,
        ("EURUSD",),
        maximum_age=20,
        now=now,
    )

    assert result.status == "WARN"
    assert "stale" in result.message
