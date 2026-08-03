from __future__ import annotations

import csv
from pathlib import Path

from trademind.volume import FIELDNAMES, collect_volume_files


def _row(
    *,
    time: int,
    status: str = "OK",
    tick_count: int = 100,
    symbol: str = ".US30Cash",
    schema_version: str = "1.4",
) -> dict[str, str]:
    values = {name: "0" for name in FIELDNAMES}
    values.update(
        {
            "schema_version": schema_version,
            "time": str(time),
            "symbol": symbol,
            "timeframe": "M5",
            "bar_seconds": "300",
            "point": "0.1",
            "open": "100",
            "high": "102",
            "low": "99",
            "close": "101",
            "bar_tick_volume": str(tick_count),
            "tick_count": str(tick_count),
            "tick_rate_per_sec": "0.333",
            "trade_volume_real": "0",
            "direction_imbalance": "0.2",
            "rvol_20": "1.5",
            "volume_percentile_100": "80",
            "tick_copy_status": status,
            "tick_copy_error": "0",
        }
    )
    return values


def _write(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)


def test_collector_deduplicates_and_prefers_better_tick_copy(tmp_path: Path) -> None:
    source = tmp_path / "source"
    output = tmp_path / "data" / "volume.csv"
    _write(
        source / "volume_us30_m5.csv",
        [
            _row(time=1000, status="PARTIAL", tick_count=70),
            _row(time=1300, status="OK", tick_count=90),
        ],
    )
    _write(
        source / "volume_us30_restart_m5.csv",
        [
            _row(time=1000, status="OK", tick_count=110),
            _row(time=1600, status="OK", tick_count=120),
        ],
    )

    summary = collect_volume_files(source, output)

    assert summary.source_files == 2
    assert summary.rows_read == 4
    assert summary.canonical_rows == 3
    assert summary.duplicate_keys == 1
    with output.open("r", encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert [int(row["time"]) for row in rows] == [1000, 1300, 1600]
    assert rows[0]["tick_copy_status"] == "OK"
    assert rows[0]["tick_count"] == "110"
    assert rows[0]["symbol"] == ".US30CASH"


def test_collector_keeps_existing_history_and_rejects_bad_rows(tmp_path: Path) -> None:
    source = tmp_path / "source"
    output = tmp_path / "data" / "volume.csv"
    _write(output, [_row(time=1000)])
    bad = _row(time=1300)
    bad["point"] = "nan"
    _write(source / "volume_x.csv", [bad, _row(time=1600, symbol="XAUUSD")])

    summary = collect_volume_files(source, output)

    assert summary.invalid_rows == 1
    assert summary.canonical_rows == 2
    with output.open("r", encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert {(row["symbol"], row["time"]) for row in rows} == {
        (".US30CASH", "1000"),
        ("XAUUSD", "1600"),
    }


def test_collector_accepts_crypto_schema_17_and_normalizes_to_canonical(tmp_path: Path) -> None:
    source = tmp_path / "source"
    output = tmp_path / "data" / "volume.csv"
    _write(
        source / "volume_BTCUSD_M5.csv",
        [
            _row(time=1000, symbol="BTCUSD", schema_version="1.7", tick_count=250),
            _row(time=1300, symbol="ETHUSD", schema_version="1.7", tick_count=180),
        ],
    )

    summary = collect_volume_files(source, output)

    assert summary.rows_read == 2
    assert summary.invalid_rows == 0
    assert summary.canonical_rows == 2
    with output.open("r", encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert {row["symbol"] for row in rows} == {"BTCUSD", "ETHUSD"}
    assert {row["schema_version"] for row in rows} == {"1.4"}


def test_collector_still_rejects_unknown_schema(tmp_path: Path) -> None:
    source = tmp_path / "source"
    output = tmp_path / "data" / "volume.csv"
    _write(source / "volume_unknown.csv", [_row(time=1000, schema_version="9.9")])

    summary = collect_volume_files(source, output)

    assert summary.rows_read == 0
    assert summary.invalid_rows == 1
    assert summary.canonical_rows == 0
