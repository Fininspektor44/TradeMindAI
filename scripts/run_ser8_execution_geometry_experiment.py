#!/usr/bin/env python3
"""Deterministic SER8 execution-geometry A/B experiment over every
HISTORICAL_DATA_READY FX symbol's already-computed historical replay.

RESEARCH/SCREENING ONLY: this never creates, matches, or accepts a
hypothesis; never reads or consumes a protected final holdout; and never
grants any symbol execution authority. It reuses, unmodified, the existing
build_research_readiness_inventory / create_replay engine (the same one
replay_ser8_historical_data.py and run_ser8_historical_multisymbol_
screening.py already call) to locate/refresh each symbol's replay
artifacts, then re-evaluates the SAME already-published candidates under
four execution-geometry variants using the existing, unmodified
trademind.signal_shadow.evaluate_shadow_candidate. No signals are
regenerated; no historical data is reacquired.

No live MT5 calls, no broker mutation, no network access: every input is a
real, already-published, content-addressed, on-disk artifact.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from trademind.ser8_execution_geometry_experiment import (  # noqa: E402
    build_multisymbol_geometry_experiment_report,
    compact_report_lines,
    write_multisymbol_geometry_experiment_report,
)
from trademind.ser8_historical_data import (  # noqa: E402
    HistoricalDataError,
    load_inventory,
    verify_inventory_account_identities,
)
from trademind.ser8_historical_replay import (  # noqa: E402
    build_research_readiness_inventory,
    load_research_policy,
)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execution-account", required=True)
    parser.add_argument("--market-data-account", required=True)
    parser.add_argument(
        "--historical-inventory",
        type=Path,
        default=REPO_ROOT / "data" / "ser8_historical_market_data" / "historical_inventory.json",
    )
    parser.add_argument(
        "--replay-root",
        type=Path,
        default=REPO_ROOT / "data" / "ser8_historical_replay",
    )
    parser.add_argument(
        "--readiness-output",
        type=Path,
        default=REPO_ROOT / "data" / "ser8_historical_replay" / "research_readiness.json",
    )
    parser.add_argument(
        "--policy",
        type=Path,
        default=REPO_ROOT / "config" / "research" / "ser8_historical_research_policy_v1.json",
    )
    parser.add_argument(
        "--experiment-output",
        type=Path,
        default=REPO_ROOT / "data" / "ser8_execution_geometry_experiment" / "experiment_report.json",
    )
    parser.add_argument(
        "--stability-window-count",
        type=int,
        default=3,
        help="Number of consecutive chronological windows for stability reporting",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the full experiment report as JSON instead of the compact report",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    try:
        policy = load_research_policy(args.policy.expanduser().resolve())
        historical_inventory_path = args.historical_inventory.expanduser().resolve()
        historical_inventory = load_inventory(historical_inventory_path)
        verify_inventory_account_identities(
            historical_inventory,
            execution_account_login=args.execution_account,
            market_data_account_login=args.market_data_account,
        )
        captured_at = datetime.now(timezone.utc)
        readiness_payload = build_research_readiness_inventory(
            historical_inventory_path=historical_inventory_path,
            replay_root=args.replay_root.expanduser().resolve(),
            policy=policy,
            output_path=args.readiness_output.expanduser().resolve(),
            captured_at=captured_at,
        )
        report = build_multisymbol_geometry_experiment_report(
            historical_inventory=historical_inventory,
            readiness_payload=readiness_payload,
            stability_window_count=args.stability_window_count,
            captured_at=captured_at,
        )
        experiment_output = args.experiment_output.expanduser().resolve()
        write_multisymbol_geometry_experiment_report(experiment_output, report)
    except (HistoricalDataError, OSError, ValueError, TypeError) as exc:
        code = exc.code if isinstance(exc, HistoricalDataError) else "EXPERIMENT_FAILED"
        print(json.dumps({"status": "FAILED", "error_code": code, "error": str(exc)}, sort_keys=True))
        return 1

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        for line in compact_report_lines(report, experiment_report_path=str(experiment_output)):
            print(line)
    return 0 if report["experiment_valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
