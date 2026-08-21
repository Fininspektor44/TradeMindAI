#!/usr/bin/env python3
"""Build isolated deterministic SER8 replay and RESEARCH_READY inventory."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from trademind.ser8_historical_data import HistoricalDataError  # noqa: E402
from trademind.ser8_historical_replay import (  # noqa: E402
    build_research_readiness_inventory,
    load_research_policy,
)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
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
        "--output",
        type=Path,
        default=REPO_ROOT / "data" / "ser8_historical_replay" / "research_readiness.json",
    )
    parser.add_argument(
        "--policy",
        type=Path,
        default=REPO_ROOT / "config" / "research" / "ser8_historical_research_policy_v1.json",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    try:
        policy = load_research_policy(args.policy.expanduser().resolve())
        result = build_research_readiness_inventory(
            historical_inventory_path=args.historical_inventory.expanduser().resolve(),
            replay_root=args.replay_root.expanduser().resolve(),
            policy=policy,
            output_path=args.output.expanduser().resolve(),
            captured_at=datetime.now(timezone.utc),
        )
    except (HistoricalDataError, OSError, ValueError, TypeError) as exc:
        code = exc.code if isinstance(exc, HistoricalDataError) else "REPLAY_FAILED"
        print(json.dumps({"status": "FAILED", "error_code": code, "error": str(exc)}, sort_keys=True))
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
