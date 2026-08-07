"""Forward-only outcome journal for TradeMind v1.27 crypto H1 swing candidates.

The canonical forward evidence is kept separately from the v1.26 placeholder
outcomes file. A compatibility mirror is rebuilt after every journal run so the
existing Passport Factory can consume the evidence without changing its schema.
"""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from trademind import crypto_signal_adapter as source
from trademind.crypto_h1_swing_filter import M5_MS, SETUP_FAMILY, read_flow_bars

VERSION = "1.27.0"
EVIDENCE_ACTIVE = "FORWARD_ONLY_JOURNAL_ACTIVE"
EVIDENCE_WAITING = "FORWARD_ONLY_WAITING_NEW_CANDIDATE"


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _sequence(value: Any) -> list[Any]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return list(value)
    return []


def _number(value: Any, default: float | None = None) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if math.isfinite(parsed) else default


def _parse_time(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso_from_ms(value: int) -> str:
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc).isoformat()


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    return dict(payload) if isinstance(payload, Mapping) else {}


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


def _candidate_id(candidate: Mapping[str, Any]) -> str:
    return str(candidate.get("signal_id") or candidate.get("source_decision_id") or "")


def _candidate_geometry(candidate: Mapping[str, Any]) -> dict[str, Any] | None:
    plan = _mapping(candidate.get("plan"))
    entries = _sequence(plan.get("entries"))
    targets = _sequence(plan.get("targets"))
    if not entries or not targets:
        return None
    entry = _number(_mapping(entries[0]).get("price"))
    stop = _number(plan.get("stop_price"))
    target = _number(targets[0])
    action = str(plan.get("action") or "").upper()
    if action not in {"BUY", "SELL"} or entry is None or stop is None or target is None:
        return None
    valid = stop < entry < target if action == "BUY" else target < entry < stop
    if not valid:
        return None
    return {"action": action, "entry": entry, "stop": stop, "target": target}


def _pending_record(candidate: Mapping[str, Any], tracked_at: datetime) -> dict[str, Any] | None:
    geometry = _candidate_geometry(candidate)
    observed_at = _parse_time(candidate.get("observed_at"))
    identity = _candidate_id(candidate)
    symbol = str(candidate.get("symbol") or "").upper()
    setup_key = str(candidate.get("similarity_key") or "")
    if not geometry or observed_at is None or not identity or not symbol or not setup_key:
        return None
    return {
        "schema_version": VERSION,
        "signal_id": identity,
        "source_decision_id": str(candidate.get("source_decision_id") or ""),
        "setup_key": setup_key,
        "symbol": symbol,
        "setup_family": str(candidate.get("setup_family") or ""),
        "signal_time": observed_at.isoformat(),
        "action": geometry["action"],
        "entry": geometry["entry"],
        "stop": geometry["stop"],
        "target": geometry["target"],
        "tracked_at": tracked_at.isoformat(),
    }


def _touch_outcome(action: str, high: float, low: float, stop: float, target: float) -> str | None:
    target_hit = high >= target if action == "BUY" else low <= target
    stop_hit = low <= stop if action == "BUY" else high >= stop
    if target_hit and stop_hit:
        return "AMBIGUOUS"
    if target_hit:
        return "TARGET_HIT"
    if stop_hit:
        return "STOP_HIT"
    return None


def _evaluate_pending(
    pending: Mapping[str, Any],
    bars_by_symbol: Mapping[str, Sequence[Any]],
    as_of_ms: int,
) -> tuple[dict[str, Any] | None, bool]:
    signal_time = _parse_time(pending.get("signal_time"))
    if signal_time is None:
        return None, False
    signal_ms = int(signal_time.timestamp() * 1000)
    expected_start = ((signal_ms // M5_MS) + 1) * M5_MS
    bars = bars_by_symbol.get(str(pending.get("symbol") or "").upper(), ())
    action = str(pending.get("action") or "").upper()
    entry = float(pending["entry"])
    stop = float(pending["stop"])
    target = float(pending["target"])
    bars_held = 0

    for bar in bars:
        if bar.start_ms < expected_start:
            continue
        if bar.end_ms > as_of_ms:
            break
        if bar.start_ms != expected_start:
            return None, True
        bars_held += 1
        resolution = _touch_outcome(action, bar.high, bar.low, stop, target)
        if resolution is None:
            expected_start += M5_MS
            continue
        risk = abs(entry - stop)
        net_r = None
        if resolution == "TARGET_HIT" and risk > 0:
            net_r = abs(target - entry) / risk
        elif resolution == "STOP_HIT":
            net_r = -1.0
        return {
            "schema_version": VERSION,
            "signal_id": str(pending.get("signal_id") or ""),
            "source_decision_id": str(pending.get("source_decision_id") or ""),
            "setup_key": str(pending.get("setup_key") or ""),
            "symbol": str(pending.get("symbol") or "").upper(),
            "setup_family": str(pending.get("setup_family") or ""),
            "signal_time": signal_time.isoformat(),
            "action": action,
            "entry": entry,
            "stop": stop,
            "target": target,
            "resolution": resolution,
            "completed_at": _iso_from_ms(bar.end_ms),
            "resolution_bar_start": _iso_from_ms(bar.start_ms),
            "bars_held": bars_held,
            "outcome": (
                "WIN" if resolution == "TARGET_HIT" else "LOSS" if resolution == "STOP_HIT" else ""
            ),
            "net_r": net_r,
            "safety": safety_contract(),
        }, False
    return None, False


def _append_jsonl(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    if not rows:
        if not path.exists():
            source._atomic_text(path, "")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(dict(row), ensure_ascii=False, sort_keys=True))
            handle.write("\n")
        handle.flush()


@dataclass(frozen=True, slots=True)
class JournalRun:
    initialized_now: bool
    tracked_new: int
    resolved_new: int
    ambiguous_new: int
    pending_total: int
    outcomes_total: int
    data_gap_pending: int
    output_dir: Path


def run_forward_journal(
    candidates_path: Path,
    bars_path: Path,
    output_dir: Path,
    *,
    now: datetime | None = None,
) -> JournalRun:
    root = output_dir.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    captured_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    state_path = root / "forward_journal_state.json"
    pending_path = root / "forward_pending.jsonl"
    canonical_outcomes_path = root / "forward_outcomes.jsonl"
    compatibility_outcomes_path = root / "outcomes.jsonl"
    ambiguous_path = root / "forward_ambiguous.jsonl"

    state = _read_json(state_path)
    initialized_now = not bool(state)
    if initialized_now:
        state = {
            "schema_version": VERSION,
            "started_at": captured_at.isoformat(),
            "setup_family": SETUP_FAMILY,
            "mode": "FORWARD_ONLY",
            "historical_candidates_backfilled": False,
            "canonical_outcomes": canonical_outcomes_path.name,
            "compatibility_outcomes": compatibility_outcomes_path.name,
            "safety": safety_contract(),
        }
        source._atomic_json(state_path, state)
        for path in (pending_path, canonical_outcomes_path, ambiguous_path):
            if not path.exists():
                source._atomic_text(path, "")

    started_at = _parse_time(state.get("started_at"))
    if started_at is None:
        raise ValueError("forward journal state has invalid started_at")

    candidates = _read_jsonl(candidates_path)
    pending = _read_jsonl(pending_path)
    outcomes = _read_jsonl(canonical_outcomes_path)
    ambiguous = _read_jsonl(ambiguous_path)
    finalized_ids = {
        str(row.get("signal_id") or "")
        for row in [*outcomes, *ambiguous]
        if row.get("signal_id")
    }
    pending_by_id = {
        str(row.get("signal_id") or ""): dict(row)
        for row in pending
        if row.get("signal_id")
    }

    tracked_new = 0
    if not initialized_now:
        for candidate in candidates:
            identity = _candidate_id(candidate)
            observed_at = _parse_time(candidate.get("observed_at"))
            if (
                not identity
                or identity in finalized_ids
                or identity in pending_by_id
                or observed_at is None
                or observed_at <= started_at
                or str(candidate.get("setup_family") or "") != SETUP_FAMILY
            ):
                continue
            record = _pending_record(candidate, captured_at)
            if record is not None:
                pending_by_id[identity] = record
                tracked_new += 1

    bars_by_symbol = read_flow_bars(bars_path)
    as_of_ms = int(captured_at.timestamp() * 1000)
    resolved: list[dict[str, Any]] = []
    ambiguous_new: list[dict[str, Any]] = []
    remaining: list[dict[str, Any]] = []
    gap_count = 0

    for identity, record in sorted(
        pending_by_id.items(),
        key=lambda item: str(item[1].get("signal_time") or ""),
    ):
        if identity in finalized_ids:
            continue
        result, gap_detected = _evaluate_pending(record, bars_by_symbol, as_of_ms)
        if result is None:
            current = dict(record)
            current["last_checked_at"] = captured_at.isoformat()
            current["data_gap_detected"] = gap_detected
            remaining.append(current)
            gap_count += int(gap_detected)
            continue
        if result["resolution"] == "AMBIGUOUS":
            ambiguous_new.append(result)
        else:
            resolved.append(result)
        finalized_ids.add(identity)

    _append_jsonl(canonical_outcomes_path, resolved)
    _append_jsonl(ambiguous_path, ambiguous_new)
    source._atomic_text(pending_path, _jsonl_text(remaining))

    all_outcomes = _read_jsonl(canonical_outcomes_path)
    all_ambiguous = _read_jsonl(ambiguous_path)
    source._atomic_text(compatibility_outcomes_path, _jsonl_text(all_outcomes))

    wins = sum(str(row.get("outcome") or "") == "WIN" for row in all_outcomes)
    losses = sum(str(row.get("outcome") or "") == "LOSS" for row in all_outcomes)
    evidence_state = (
        EVIDENCE_ACTIVE if pending_by_id or all_outcomes or all_ambiguous else EVIDENCE_WAITING
    )
    status = {
        "schema_version": VERSION,
        "state": "OK",
        "updated_at": captured_at.isoformat(),
        "started_at": started_at.isoformat(),
        "setup_family": SETUP_FAMILY,
        "evidence_state": evidence_state,
        "historical_candidates_backfilled": False,
        "tracked_new": tracked_new,
        "resolved_new": len(resolved),
        "ambiguous_new": len(ambiguous_new),
        "pending": len(remaining),
        "outcomes": len(all_outcomes),
        "wins": wins,
        "losses": losses,
        "ambiguous": len(all_ambiguous),
        "data_gap_pending": gap_count,
        "canonical_outcomes": str(canonical_outcomes_path),
        "compatibility_outcomes": str(compatibility_outcomes_path),
        "safety": safety_contract(),
    }
    source._atomic_json(root / "forward_journal_status.json", status)

    runtime_status = _read_json(root / "status.json")
    if runtime_status:
        runtime_status.update(
            {
                "outcomes": len(all_outcomes),
                "evidence_state": evidence_state,
                "forward_pending": len(remaining),
                "forward_ambiguous": len(all_ambiguous),
                "forward_journal_version": VERSION,
            }
        )
        source._atomic_json(root / "status.json", runtime_status)

    return JournalRun(
        initialized_now=initialized_now,
        tracked_new=tracked_new,
        resolved_new=len(resolved),
        ambiguous_new=len(ambiguous_new),
        pending_total=len(remaining),
        outcomes_total=len(all_outcomes),
        data_gap_pending=gap_count,
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
        "historical_candidates_backfilled": False,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="TradeMind v1.27 forward-only crypto outcome journal"
    )
    parser.add_argument(
        "--candidates",
        type=Path,
        default=Path("data/crypto_signal_intelligence_v1_26/candidates.jsonl"),
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
    args = parser.parse_args(argv)
    try:
        result = run_forward_journal(
            args.candidates.expanduser().resolve(),
            args.bars.expanduser().resolve(),
            args.output_dir,
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        print(f"Forward outcome journal failed: {exc}")
        return 1

    print("TradeMind v1.27 Forward Outcome Journal")
    print("Forward-only. Historical v1.26 candidates are not backfilled.")
    print("Read-only. Orders OFF. Publication OFF. Exchange API not called.")
    print(f"Initialized now: {result.initialized_now}")
    print(f"New candidates tracked: {result.tracked_new}")
    print(f"New outcomes resolved: {result.resolved_new}")
    print(f"New ambiguous resolutions: {result.ambiguous_new}")
    print(f"Open forward candidates: {result.pending_total}")
    print(f"Outcomes total: {result.outcomes_total}")
    print(f"Pending with data gap: {result.data_gap_pending}")
    print(f"Output: {result.output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
