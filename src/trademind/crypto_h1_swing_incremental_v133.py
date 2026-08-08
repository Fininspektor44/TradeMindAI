"""Incremental runtime wrapper for the TradeMind v1.33 crypto swing filter."""

from __future__ import annotations

import argparse
import csv
import json
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Sequence

from trademind import crypto_h1_swing_incremental as base
from trademind.crypto_h1_swing_filter_v133 import SETUP_FAMILY, VERSION, evaluate_row


@contextmanager
def _patched_v133_filter() -> Iterator[None]:
    previous_evaluate_row = base.evaluate_row
    previous_version = base.VERSION
    previous_setup_family = base.SETUP_FAMILY
    try:
        base.evaluate_row = evaluate_row
        base.VERSION = VERSION
        base.SETUP_FAMILY = SETUP_FAMILY
        yield
    finally:
        base.evaluate_row = previous_evaluate_row
        base.VERSION = previous_version
        base.SETUP_FAMILY = previous_setup_family


def run_incremental(
    decisions_path: Path,
    bars_path: Path,
    output_dir: Path,
    *,
    batch_size: int = 400,
    now=None,
):
    with _patched_v133_filter():
        return base.run_incremental(
            decisions_path,
            bars_path,
            output_dir,
            batch_size=batch_size,
            now=now,
        )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="TradeMind v1.33 incremental H1 swing opportunity monitor"
    )
    parser.add_argument(
        "--decisions",
        type=Path,
        default=Path("data/bybit_shadow_v1_10/decisions.csv"),
    )
    parser.add_argument(
        "--bars",
        type=Path,
        default=Path("data/bybit_v1_9/bybit_bars.csv"),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("data/crypto_signal_intelligence_v1_26"),
    )
    parser.add_argument("--batch-size", type=int, default=400)
    args = parser.parse_args(argv)
    try:
        result = run_incremental(
            args.decisions.expanduser().resolve(),
            args.bars.expanduser().resolve(),
            args.output_dir,
            batch_size=args.batch_size,
        )
    except (OSError, ValueError, TypeError, csv.Error, json.JSONDecodeError) as exc:
        print(f"H1 swing opportunity runtime v1.33 failed: {exc}")
        return 1

    print("TradeMind v1.33 H1 Swing Opportunity Filter")
    print(
        "H1 direction -> M15 opposite-break veto -> recent 3-bar M5 breakout/hold "
        "-> volume/delta -> H1 space."
    )
    print("Read-only. Orders OFF. Publication OFF. Exchange API not called.")
    print(f"Processed batch: {result.processed_batch}")
    print(f"Eligible opportunities: {result.eligible_total}")
    print(f"Rejected decisions: {result.rejected_total}")
    print(f"Errors: {result.error_total}")
    print(f"Remaining history: {result.remaining_decisions}")
    print(f"Output: {result.output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
