"""Incremental runner for TradeMind v1.25 crypto market structure.

The live scheduler processes a bounded batch of unseen Bybit shadow decisions,
merges the resulting immutable candidates with the local evidence archive and
rebuilds outcomes for every already-known candidate. New decisions are handled
first while older history is backfilled over successive runs. No exchange,
publication or order API is used.
"""

from __future__ import annotations

import argparse
import csv
import json
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from trademind import crypto_signal_adapter as base
from trademind import crypto_signal_adapter_v125 as structure
from trademind.signal_intelligence import candidate_from_dict

VERSION = "1.25.1"


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


def _write_csv(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields: list[str] = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(str(key))
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


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


def _candidate_key(row: Mapping[str, Any]) -> str:
    return str(row.get("source_decision_id") or row.get("signal_id") or "")


def _structure_key(row: Mapping[str, Any]) -> str:
    return str(row.get("source_decision_id") or row.get("signal_id") or "")


def _outcome_key(row: Mapping[str, Any]) -> str:
    return str(row.get("signal_id") or "")


def _error_key(row: Mapping[str, Any]) -> str:
    return "|".join(
        (
            str(row.get("source_decision_id") or ""),
            str(row.get("row") or ""),
            str(row.get("reason") or ""),
        )
    )


def _observed_sort(row: Mapping[str, Any]) -> str:
    return str(row.get("observed_at") or row.get("signal_time") or "")


def _attempted_ids(
    candidates: Sequence[Mapping[str, Any]],
    errors: Sequence[Mapping[str, Any]],
) -> set[str]:
    result = {
        str(row.get("source_decision_id") or "")
        for row in candidates
        if str(row.get("source_decision_id") or "")
    }
    result.update(
        str(row.get("source_decision_id") or "")
        for row in errors
        if str(row.get("source_decision_id") or "")
    )
    return result


@dataclass(frozen=True, slots=True)
class IncrementalRun:
    processed_batch: int
    total_candidates: int
    total_outcomes: int
    remaining_decisions: int
    structure_ok: int
    structure_degraded: int
    output_dir: Path


def run_incremental(
    decisions_path: Path,
    signals_path: Path,
    bars_path: Path,
    output_dir: Path,
    *,
    cost_r: float = 0.04,
    batch_size: int = 400,
    now: datetime | None = None,
) -> IncrementalRun:
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    if cost_r < 0:
        raise ValueError("cost_r cannot be negative")

    root = output_dir.expanduser().resolve()
    decisions = base._read_csv(decisions_path)
    signals = base._read_csv(signals_path)
    existing_candidates = _read_jsonl(root / "candidates.jsonl")
    existing_structures = _read_jsonl(root / "structure_snapshots.jsonl")
    existing_error_payload = _read_json(root / "errors.json")
    existing_errors = [
        dict(row)
        for row in existing_error_payload.get("errors", [])
        if isinstance(row, Mapping)
    ]

    attempted = _attempted_ids(existing_candidates, existing_errors)
    unseen = [
        row
        for row in decisions
        if str(row.get("decision_id") or "") not in attempted
    ]
    batch = unseen[-batch_size:]

    incoming_candidates: list[dict[str, Any]] = []
    incoming_structures: list[dict[str, Any]] = []
    incoming_errors: list[dict[str, Any]] = []
    batch_ok = 0
    batch_degraded = 0

    if batch:
        with tempfile.TemporaryDirectory(prefix="trademind_v125_") as temporary:
            temporary_root = Path(temporary)
            batch_decisions = temporary_root / "decisions.csv"
            batch_output = temporary_root / "output"
            _write_csv(batch_decisions, batch)
            result = structure.run_adapter(
                batch_decisions,
                signals_path,
                bars_path,
                batch_output,
                cost_r=cost_r,
                now=now,
            )
            incoming_candidates = _read_jsonl(batch_output / "candidates.jsonl")
            incoming_structures = _read_jsonl(
                batch_output / "structure_snapshots.jsonl"
            )
            batch_errors = _read_json(batch_output / "errors.json")
            incoming_errors = [
                dict(row)
                for row in batch_errors.get("errors", [])
                if isinstance(row, Mapping)
            ]
            batch_ok = result.structure_ok
            batch_degraded = result.structure_degraded

    candidates = _merge_rows(
        existing_candidates,
        incoming_candidates,
        _candidate_key,
        _observed_sort,
    )
    structures = _merge_rows(
        existing_structures,
        incoming_structures,
        _structure_key,
        lambda row: str(row.get("as_of") or ""),
    )
    candidate_errors = _merge_rows(
        existing_errors,
        incoming_errors,
        _error_key,
        lambda row: str(row.get("row") or ""),
    )

    by_source_id = {
        str(row.get("source_decision_id") or ""): candidate_from_dict(row)
        for row in candidates
        if str(row.get("source_decision_id") or "")
    }
    outcomes, outcome_errors = base.build_outcomes(
        signals,
        by_source_id,
        cost_r=cost_r,
    )
    outcome_rows = [item.as_dict() for item in outcomes]
    errors = _merge_rows(
        candidate_errors,
        [dict(row) for row in outcome_errors],
        _error_key,
        lambda row: str(row.get("row") or ""),
    )

    base._atomic_text(root / "candidates.jsonl", _jsonl_text(candidates))
    base._atomic_text(root / "outcomes.jsonl", _jsonl_text(outcome_rows))
    base._atomic_text(root / "structure_snapshots.jsonl", _jsonl_text(structures))
    base._atomic_json(root / "errors.json", {"errors": errors})

    attempted_after = _attempted_ids(candidates, errors)
    remaining = sum(
        str(row.get("decision_id") or "") not in attempted_after for row in decisions
    )
    structure_ok = sum(str(row.get("state") or "") == "OK" for row in structures)
    structure_degraded = sum(
        str(row.get("state") or "") == "DEGRADED" for row in structures
    )
    captured_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    state = "BACKFILLING" if remaining else "OK"
    base._atomic_json(
        root / "status.json",
        {
            "schema_version": VERSION,
            "state": state,
            "updated_at": captured_at.isoformat(),
            "asset_class": "CRYPTO",
            "venue": "BYBIT",
            "setup_family": structure.STRUCTURE_SETUP_FAMILY,
            "decisions_source": str(decisions_path),
            "signals_source": str(signals_path),
            "bars_source": str(bars_path),
            "processed_batch": len(batch),
            "batch_size": batch_size,
            "remaining_decisions": remaining,
            "candidates": len(candidates),
            "outcomes": len(outcome_rows),
            "rejected_rows": len(errors),
            "structure_ok": structure_ok,
            "structure_degraded": structure_degraded,
            "last_batch_structure_ok": batch_ok,
            "last_batch_structure_degraded": batch_degraded,
            "safety": {
                "read_only": True,
                "orders_enabled": False,
                "publication_enabled": False,
                "broker_api_called": False,
                "source_files_modified": False,
                "future_bars_used": False,
            },
        },
    )
    return IncrementalRun(
        processed_batch=len(batch),
        total_candidates=len(candidates),
        total_outcomes=len(outcome_rows),
        remaining_decisions=remaining,
        structure_ok=structure_ok,
        structure_degraded=structure_degraded,
        output_dir=root,
    )


def safety_contract() -> Mapping[str, Any]:
    return {
        "read_only": True,
        "orders_enabled": False,
        "publication_enabled": False,
        "broker_api_called": False,
        "future_bars_used": False,
        "source_files_modified": False,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="TradeMind v1.25.1 incremental crypto structure runtime"
    )
    parser.add_argument(
        "--decisions",
        type=Path,
        default=Path("data/bybit_shadow_v1_10/decisions.csv"),
    )
    parser.add_argument(
        "--signals",
        type=Path,
        default=Path("data/bybit_shadow_v1_10/signals.csv"),
    )
    parser.add_argument(
        "--bars",
        type=Path,
        default=Path("data/bybit_v1_9/bybit_bars.csv"),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("data/crypto_signal_intelligence_v1_25"),
    )
    parser.add_argument("--cost-r", type=float, default=0.04)
    parser.add_argument("--batch-size", type=int, default=400)
    args = parser.parse_args(argv)
    try:
        result = run_incremental(
            args.decisions.expanduser().resolve(),
            args.signals.expanduser().resolve(),
            args.bars.expanduser().resolve(),
            args.output_dir,
            cost_r=args.cost_r,
            batch_size=args.batch_size,
        )
    except (OSError, ValueError, TypeError, csv.Error, json.JSONDecodeError) as exc:
        print(f"Incremental crypto structure runtime failed: {exc}")
        return 1

    print("TradeMind v1.25.1 Incremental Crypto Structure")
    print("Newest unseen decisions first; older history backfills in bounded batches.")
    print("Read-only. Orders OFF. Publication OFF. Exchange API not called.")
    print(f"Processed batch: {result.processed_batch}")
    print(f"Candidates total: {result.total_candidates}")
    print(f"Outcomes total: {result.total_outcomes}")
    print(f"Remaining history: {result.remaining_decisions}")
    print(
        "Structure OK/degraded: "
        f"{result.structure_ok}/{result.structure_degraded}"
    )
    print(f"Output: {result.output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
