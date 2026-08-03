"""Canonical collector for TradeMind AI volume intelligence files."""

from __future__ import annotations

import argparse
import csv
import math
import os
from dataclasses import dataclass
from pathlib import Path

SCHEMA_VERSION = "1.4"
SOURCE_SCHEMA_VERSIONS = {"1.4", "1.7"}
FIELDNAMES = (
    "schema_version",
    "time",
    "symbol",
    "timeframe",
    "bar_seconds",
    "point",
    "open",
    "high",
    "low",
    "close",
    "bar_tick_volume",
    "tick_count",
    "tick_rate_per_sec",
    "bid_up",
    "bid_down",
    "ask_up",
    "ask_down",
    "mid_up",
    "mid_down",
    "buy_ticks",
    "sell_ticks",
    "trade_volume",
    "trade_volume_real",
    "spread_mean_points",
    "spread_min_points",
    "spread_max_points",
    "spread_last_points",
    "spread_expansion_points",
    "realized_abs_move_points",
    "direction_imbalance",
    "delta_proxy",
    "rvol_20",
    "volume_percentile_100",
    "range_per_tick_points",
    "body_per_tick_points",
    "tick_copy_status",
    "tick_copy_error",
)
_REQUIRED = set(FIELDNAMES)
_INTEGER_FIELDS = {
    "time",
    "bar_seconds",
    "bar_tick_volume",
    "tick_count",
    "bid_up",
    "bid_down",
    "ask_up",
    "ask_down",
    "mid_up",
    "mid_down",
    "buy_ticks",
    "sell_ticks",
    "trade_volume",
    "delta_proxy",
    "tick_copy_error",
}
_FLOAT_FIELDS = {
    "point",
    "open",
    "high",
    "low",
    "close",
    "tick_rate_per_sec",
    "trade_volume_real",
    "spread_mean_points",
    "spread_min_points",
    "spread_max_points",
    "spread_last_points",
    "spread_expansion_points",
    "realized_abs_move_points",
    "direction_imbalance",
    "rvol_20",
    "volume_percentile_100",
    "range_per_tick_points",
    "body_per_tick_points",
}
_STATUS_RANK = {"ERROR": 0, "PARTIAL": 1, "NO_TICKS": 2, "OK": 3}


@dataclass(frozen=True)
class VolumeCollectSummary:
    source_files: int
    rows_read: int
    canonical_rows: int
    duplicate_keys: int
    invalid_rows: int
    output_path: Path


def _normalize_row(row: dict[str, str]) -> dict[str, str]:
    missing = _REQUIRED - set(row)
    if missing:
        raise ValueError(f"missing columns: {','.join(sorted(missing))}")

    normalized = {name: str(row.get(name, "")).strip() for name in FIELDNAMES}
    source_schema = normalized["schema_version"]
    if source_schema not in SOURCE_SCHEMA_VERSIONS:
        raise ValueError(f"unsupported schema version: {source_schema!r}")

    # The v1.7 crypto exporter kept the exact v1.4 column contract but stamped
    # rows with its exporter release number. Normalize compatible source rows
    # to the stable canonical schema instead of discarding the crypto archive.
    normalized["schema_version"] = SCHEMA_VERSION
    normalized["symbol"] = normalized["symbol"].upper()
    normalized["timeframe"] = normalized["timeframe"].upper()
    normalized["tick_copy_status"] = normalized["tick_copy_status"].upper()
    if not normalized["symbol"] or not normalized["timeframe"]:
        raise ValueError("empty symbol or timeframe")
    if normalized["tick_copy_status"] not in _STATUS_RANK:
        raise ValueError(f"invalid tick_copy_status: {normalized['tick_copy_status']!r}")

    for name in _INTEGER_FIELDS:
        normalized[name] = str(int(normalized[name]))
    for name in _FLOAT_FIELDS:
        value = float(normalized[name])
        if not math.isfinite(value):
            raise ValueError(f"non-finite value in {name}")
        normalized[name] = f"{value:.12g}"

    if int(normalized["time"]) <= 0:
        raise ValueError("time must be positive")
    if int(normalized["bar_seconds"]) <= 0:
        raise ValueError("bar_seconds must be positive")
    if float(normalized["point"]) <= 0:
        raise ValueError("point must be positive")
    if int(normalized["tick_count"]) < 0 or int(normalized["bar_tick_volume"]) < 0:
        raise ValueError("volume counts cannot be negative")
    return normalized


def _key(row: dict[str, str]) -> tuple[str, str, int]:
    return row["symbol"], row["timeframe"], int(row["time"])


def _prefer(candidate: dict[str, str], current: dict[str, str]) -> bool:
    candidate_rank = _STATUS_RANK[candidate["tick_copy_status"]]
    current_rank = _STATUS_RANK[current["tick_copy_status"]]
    if candidate_rank != current_rank:
        return candidate_rank > current_rank
    return int(candidate["tick_count"]) >= int(current["tick_count"])


def _read_csv(path: Path) -> tuple[list[dict[str, str]], int]:
    rows: list[dict[str, str]] = []
    invalid = 0
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            if not reader.fieldnames:
                return rows, 1
            for raw in reader:
                try:
                    rows.append(_normalize_row(dict(raw)))
                except (TypeError, ValueError):
                    invalid += 1
    except OSError:
        return rows, 1
    return rows, invalid


def collect_volume_files(
    source_dir: Path,
    output_path: Path,
    *,
    pattern: str = "volume_*.csv",
) -> VolumeCollectSummary:
    source_dir = source_dir.expanduser().resolve()
    output_path = output_path.expanduser().resolve()
    files = sorted(path for path in source_dir.glob(pattern) if path.is_file())

    records: dict[tuple[str, str, int], dict[str, str]] = {}
    rows_read = 0
    duplicate_keys = 0
    invalid_rows = 0

    if output_path.is_file():
        existing_rows, invalid = _read_csv(output_path)
        invalid_rows += invalid
        for row in existing_rows:
            records[_key(row)] = row

    for path in files:
        rows, invalid = _read_csv(path)
        invalid_rows += invalid
        rows_read += len(rows)
        for row in rows:
            key = _key(row)
            current = records.get(key)
            if current is not None:
                duplicate_keys += 1
                if not _prefer(row, current):
                    continue
            records[key] = row

    ordered = sorted(records.values(), key=lambda row: _key(row))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_suffix(output_path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDNAMES, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(ordered)
    temporary.replace(output_path)

    return VolumeCollectSummary(
        source_files=len(files),
        rows_read=rows_read,
        canonical_rows=len(ordered),
        duplicate_keys=duplicate_keys,
        invalid_rows=invalid_rows,
        output_path=output_path,
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Collect and deduplicate TradeMind volume intelligence CSV files"
    )
    default_source = (
        Path(os.getenv("APPDATA", ""))
        / "MetaQuotes"
        / "Terminal"
        / "Common"
        / "Files"
        / "TradeMindAI_Volume_v1_4"
    )
    parser.add_argument("--source-dir", type=Path, default=default_source)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("data/volume_v1_4/volume_bars.csv"),
    )
    parser.add_argument("--pattern", default="volume_*.csv")
    args = parser.parse_args()

    summary = collect_volume_files(args.source_dir, args.output, pattern=args.pattern)
    print("TradeMind volume collector")
    print(f"Source directory: {args.source_dir.expanduser().resolve()}")
    print(f"Source files: {summary.source_files}")
    print(f"Rows read: {summary.rows_read}")
    print(f"Canonical rows: {summary.canonical_rows}")
    print(f"Duplicate keys ignored/replaced: {summary.duplicate_keys}")
    print(f"Invalid rows: {summary.invalid_rows}")
    print(f"Output: {summary.output_path}")
    return 0 if summary.source_files > 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
