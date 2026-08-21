#!/usr/bin/env python3
"""Read-only verification and listing for TradeMind project checkpoints."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from trademind.project_checkpoint import (  # noqa: E402
    RESULT_SCHEMA_VERSION,
    CheckpointError,
    list_checkpoints,
    verify_checkpoint,
)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=REPO_ROOT)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--checkpoint")
    mode.add_argument("--list", action="store_true", dest="list_all")
    parser.add_argument("--bundle-dir", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    try:
        result = (
            list_checkpoints(args.repo)
            if args.list_all
            else verify_checkpoint(args.repo, args.checkpoint, bundle_dir=args.bundle_dir)
        )
    except CheckpointError as exc:
        result = {
            "schema_version": RESULT_SCHEMA_VERSION,
            "verification_status": "FAILED",
            "error_code": exc.code,
            "error": str(exc),
        }
        print(json.dumps(result, indent=2, sort_keys=True))
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    if args.list_all and any(
        item["verification_status"] != "VERIFIED" for item in result["checkpoints"]
    ):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
