"""Incremental runtime for TradeMind v1.26 H1 swing opportunities.

Newest unseen Bybit shadow decisions are evaluated first. Every decision becomes
one immutable audit record and either an eligible candidate or an explicit
rejection, so the minute scheduler never repeats old work. Outcomes remain empty
until a dedicated forward-only v1.26 paper journal is introduced.
"""

from __future__ import annotations

import argparse
import csv
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from trademind import crypto_signal_adapter as source
from trademind.crypto_h1_swing_filter import (
    SETUP_FAMILY,
    VERSION,
    FlowHistory,
    evaluate_row,
)
from trademind.crypto_market_structure import MarketStructureEngine
from trademind.signal_evidence import similarity_dimensions, similarity_key

LEGACY_ACTION_ERROR = "action must be BUY or SELL"
SUPPORTED_ACTIONS = frozenset({"BUY", "SELL"})


def _read_json(path: Path) -> Mapping[str, Any]:
    if not path.is_file():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    return payload if isinstance(payload, Mapping) else {}


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8-sig") as handle:
        for raw in handle:
            line = raw.strip()
            if not line:
                continue
            payload = json.loads(line)
            if isinstance(payload, Mapping):
                rows.append(dict(payload))
    return rows


def _jsonl_text(rows: Sequence[Mapping[str, Any]]) -> str:
    if not rows:
        return ""
    return "\n".join(
        json.dumps(dict(row), ensure_ascii=False, sort_keys=True) for row in rows
    ) + "\n"


def _merge_rows(
    existing: Sequence[Mapping[str, Any]],
    incoming: Sequence[Mapping[str, Any]],
    key: Callable[[Mapping[str, Any]], str],
    sort_key: Callable[[Mapping[str, Any]], str],
) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    anonymous: list[dict[str, Any]] = []
    for row in [*existing, *incoming]:
        normalized = dict(row)
        identity = key(normalized)
        if identity:
            merged[identity] = normalized
        else:
            anonymous.append(normalized)
    return sorted([*merged.values(), *anonymous], key=sort_key)


def _decision_key(row: Mapping[str, Any]) -> str:
    return str(row.get("source_decision_id") or row.get("signal_id") or "")


def _observed_sort(row: Mapping[str, Any]) -> str:
    return str(row.get("observed_at") or row.get("as_of") or "")


def _error_key(row: Mapping[str, Any]) -> str:
    return "|".join(
        (
            str(row.get("source_decision_id") or ""),
            str(row.get("reason") or ""),
        )
    )


def _is_legacy_action_error(row: Mapping[str, Any]) -> bool:
    return str(row.get("reason") or "").strip().casefold() == LEGACY_ACTION_ERROR.casefold()


def _attempted_ids(
    candidates: Sequence[Mapping[str, Any]],
    rejections: Sequence[Mapping[str, Any]],
    errors: Sequence[Mapping[str, Any]],
) -> set[str]:
    return {
        str(row.get("source_decision_id") or "")
        for row in [*candidates, *rejections, *errors]
        if str(row.get("source_decision_id") or "")
    }


def _unsupported_action_records(
    row: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    source_id = source._text(row, "decision_id")
    action = source._text(row, "action").upper()
    symbol = source._text(row, "symbol").upper()
    as_of = source._text(row, "signal_time")
    opportunity = {
        "eligible": False,
        "action": action,
        "reasons": ["ACTION_NOT_BUY_SELL"],
        "entry": None,
        "stop": None,
        "target": None,
        "breakout_level": None,
        "breakout_pivot_ms": None,
        "volume_ratio": 0.0,
        "current_volume": 0.0,
        "median_volume_20": 0.0,
        "delta_turnover": 0.0,
        "target_rr": 0.0,
        "target_atr_h1": 0.0,
    }
    audit = {
        "schema_version": VERSION,
        "source_decision_id": source_id,
        "symbol": symbol,
        "action": action,
        "as_of": as_of,
        "opportunity": opportunity,
        "snapshot_state": "SKIPPED_UNSUPPORTED_ACTION",
        "bar_counts": {},
        "safety": {
            "read_only": True,
            "orders_enabled": False,
            "publication_enabled": False,
            "exchange_api_called": False,
            "future_bars_used": False,
        },
    }
    rejection = {
        "schema_version": VERSION,
        "source_decision_id": source_id,
        "symbol": symbol,
        "action": action,
        "as_of": as_of,
        "reasons": ["ACTION_NOT_BUY_SELL"],
    }
    return audit, rejection


@dataclass(frozen=True, slots=True)
class IncrementalRun:
    processed_batch: int
    eligible_total: int
    rejected_total: int
    error_total: int
    remaining_decisions: int
    output_dir: Path


def run_incremental(
    decisions_path: Path,
    bars_path: Path,
    output_dir: Path,
    *,
    batch_size: int = 400,
    now: datetime | None = None,
) -> IncrementalRun:
    if batch_size < 1:
        raise ValueError("batch_size must be positive")

    root = output_dir.expanduser().resolve()
    decisions = source._read_csv(decisions_path)
    existing_candidates = _read_jsonl(root / "candidates.jsonl")
    existing_rejections = _read_jsonl(root / "rejections.jsonl")
    existing_audit = _read_jsonl(root / "opportunity_audit.jsonl")
    error_payload = _read_json(root / "errors.json")
    raw_existing_errors = [
        dict(row)
        for row in error_payload.get("errors", [])
        if isinstance(row, Mapping)
    ]
    recovered_legacy_action_errors = sum(
        _is_legacy_action_error(row) for row in raw_existing_errors
    )
    existing_errors = [
        row for row in raw_existing_errors if not _is_legacy_action_error(row)
    ]

    attempted = _attempted_ids(
        existing_candidates,
        existing_rejections,
        existing_errors,
    )
    unseen = [
        row
        for row in decisions
        if str(row.get("decision_id") or "") not in attempted
    ]
    batch = unseen[-batch_size:]

    incoming_candidates: list[dict[str, Any]] = []
    incoming_rejections: list[dict[str, Any]] = []
    incoming_audit: list[dict[str, Any]] = []
    incoming_errors: list[dict[str, Any]] = []
    tradable_rows: list[Mapping[str, Any]] = []

    for row in batch:
        action = source._text(row, "action").upper()
        if action in SUPPORTED_ACTIONS:
            tradable_rows.append(row)
            continue
        audit, rejection = _unsupported_action_records(row)
        incoming_audit.append(audit)
        incoming_rejections.append(rejection)

    if tradable_rows:
        engine = MarketStructureEngine.from_csv(bars_path)
        flow = FlowHistory.from_csv(bars_path)
        for row in tradable_rows:
            source_id = source._text(row, "decision_id")
            try:
                candidate, audit, rejection = evaluate_row(row, engine, flow)
            except (OSError, TypeError, ValueError, csv.Error) as exc:
                incoming_errors.append(
                    {
                        "schema_version": VERSION,
                        "source_decision_id": source_id,
                        "reason": str(exc),
                    }
                )
                continue

            incoming_audit.append(audit)
            if candidate is None:
                incoming_rejections.append(rejection)
                continue
            payload = {
                **candidate.as_dict(),
                "similarity_key": similarity_key(candidate),
                "similarity_dimensions": similarity_dimensions(candidate),
                "asset_class": "CRYPTO",
                "venue": "BYBIT",
                "source_decision_id": source_id,
                "source_gate_status": source._text(row, "gate_status"),
                "source_quality_score": source._number(row, "quality_score"),
                "opportunity_eligible": True,
                "shadow_only": True,
                "orders_enabled": False,
                "publication_enabled": False,
            }
            incoming_candidates.append(payload)

    candidates = _merge_rows(
        existing_candidates,
        incoming_candidates,
        _decision_key,
        _observed_sort,
    )
    rejections = _merge_rows(
        existing_rejections,
        incoming_rejections,
        _decision_key,
        _observed_sort,
    )
    audit = _merge_rows(
        existing_audit,
        incoming_audit,
        _decision_key,
        _observed_sort,
    )
    errors = _merge_rows(
        existing_errors,
        incoming_errors,
        _error_key,
        lambda row: str(row.get("source_decision_id") or ""),
    )

    source._atomic_text(root / "candidates.jsonl", _jsonl_text(candidates))
    source._atomic_text(root / "outcomes.jsonl", "")
    source._atomic_text(root / "rejections.jsonl", _jsonl_text(rejections))
    source._atomic_text(root / "opportunity_audit.jsonl", _jsonl_text(audit))
    source._atomic_json(root / "errors.json", {"errors": errors})

    attempted_after = _attempted_ids(candidates, rejections, errors)
    remaining = sum(
        str(row.get("decision_id") or "") not in attempted_after for row in decisions
    )
    captured_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    source._atomic_json(
        root / "status.json",
        {
            "schema_version": VERSION,
            "state": "BACKFILLING" if remaining else "OK",
            "updated_at": captured_at.isoformat(),
            "asset_class": "CRYPTO",
            "venue": "BYBIT",
            "setup_family": SETUP_FAMILY,
            "processed_batch": len(batch),
            "batch_size": batch_size,
            "remaining_decisions": remaining,
            "eligible_candidates": len(candidates),
            "rejected_decisions": len(rejections),
            "errors": len(errors),
            "recovered_legacy_action_errors": recovered_legacy_action_errors,
            "outcomes": 0,
            "evidence_state": "FORWARD_ONLY_JOURNAL_NOT_STARTED",
            "decision_chain": (
                "H1_DIRECTION>M15_VETO>M5_LAST_EXTREMUM_CLOSE_BREAK>"
                "M5_VOLUME_DELTA>H1_TARGET_SPACE"
            ),
            "thresholds": {
                "minimum_volume_ratio": 1.20,
                "minimum_target_rr": 1.80,
                "minimum_target_atr_h1": 0.70,
            },
            "safety": {
                "read_only": True,
                "orders_enabled": False,
                "publication_enabled": False,
                "exchange_api_called": False,
                "source_files_modified": False,
                "future_bars_used": False,
                "account_sizing_calculated": False,
            },
        },
    )
    return IncrementalRun(
        processed_batch=len(batch),
        eligible_total=len(candidates),
        rejected_total=len(rejections),
        error_total=len(errors),
        remaining_decisions=remaining,
        output_dir=root,
    )


def safety_contract() -> Mapping[str, Any]:
    return {
        "read_only": True,
        "orders_enabled": False,
        "publication_enabled": False,
        "exchange_api_called": False,
        "future_bars_used": False,
        "source_files_modified": False,
        "account_sizing_calculated": False,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="TradeMind v1.26 incremental H1 swing opportunity monitor"
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
        print(f"H1 swing opportunity runtime failed: {exc}")
        return 1

    print("TradeMind v1.26 H1 Swing Opportunity Filter")
    print("H1 direction -> M15 veto -> M5 extremum breakout -> volume/delta -> H1 space.")
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
