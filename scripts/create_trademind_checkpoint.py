#!/usr/bin/env python3
"""Create, push, and verify one immutable TradeMind project checkpoint."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from trademind.project_checkpoint import (  # noqa: E402
    DEFAULT_BUNDLE_ROOT,
    CheckpointError,
    CheckpointMetadata,
    create_checkpoint,
    failure_result,
)


def _key_value(value: str) -> tuple[str, str]:
    key, separator, item = value.partition("=")
    if not separator or not key.strip() or not item.strip():
        raise argparse.ArgumentTypeError("expected NAME=VERSION")
    return key.strip(), item.strip()


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=REPO_ROOT)
    parser.add_argument("--layer-name", required=True)
    parser.add_argument("--checkpoint-id")
    parser.add_argument("--final-status", required=True)
    parser.add_argument("--layer-status", required=True)
    parser.add_argument("--full-pytest-status")
    parser.add_argument("--full-pytest-summary")
    parser.add_argument("--ea-version")
    parser.add_argument("--runtime-version", action="append", type=_key_value, default=[])
    parser.add_argument("--active-hypothesis-id", action="append", default=[])
    parser.add_argument("--protected-holdout-id", action="append", default=[])
    parser.add_argument("--accepted-research-id", action="append", default=[])
    parser.add_argument("--demo-account", action="append", default=[])
    parser.add_argument("--magic-number", type=int)
    parser.add_argument("--task-name", action="append", default=[])
    parser.add_argument("--config", action="append", default=[])
    parser.add_argument("--artifact", action="append", default=[])
    parser.add_argument("--note", action="append", default=[])
    parser.add_argument("--remote", default="origin")
    parser.add_argument("--create-bundle", action="store_true")
    parser.add_argument("--bundle-root", type=Path, default=DEFAULT_BUNDLE_ROOT)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    metadata = CheckpointMetadata(
        layer_name=args.layer_name,
        checkpoint_id=args.checkpoint_id,
        final_status=args.final_status,
        layer_status=args.layer_status,
        full_pytest_status=args.full_pytest_status,
        full_pytest_summary=args.full_pytest_summary,
        ea_version=args.ea_version,
        runtime_versions=tuple(args.runtime_version),
        active_hypothesis_ids=tuple(args.active_hypothesis_id),
        protected_holdout_ids=tuple(args.protected_holdout_id),
        accepted_research_ids=tuple(args.accepted_research_id),
        demo_account_allowlist=tuple(args.demo_account),
        magic_number=args.magic_number,
        task_names=tuple(args.task_name),
        config_paths=tuple(args.config),
        artifact_paths=tuple(args.artifact),
        notes=tuple(args.note),
    )
    try:
        result = create_checkpoint(
            repo=args.repo,
            metadata=metadata,
            remote=args.remote,
            create_bundle=args.create_bundle,
            bundle_root=args.bundle_root,
        )
    except CheckpointError as exc:
        print(json.dumps(failure_result(exc), indent=2, sort_keys=True))
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
