"""TradeMind Live Signal Runtime v1.21.

This read-only runtime turns newly closed canonical MT5 volume bars into fresh
FX observations and signal candidates, evaluates their shadow outcomes, builds
statistically gated passports, and asks the account-specific Risk Manager for
an ALLOW/BLOCK decision.

Historical v1.16 candidates and outcomes remain immutable. Live observations,
candidates, and outcomes are stored in an isolated v1.21 workspace. Historical
and live outcomes are merged only into a derived evidence file, preventing the
live runtime from rewriting the research archive.

No broker client is imported. No signal is published. No order is sent,
modified, or closed.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from trademind.fx_research import (
    FX_MAJORS,
    _OBSERVATION_FIELDS,
    build_fx_observations,
    load_volume_rows,
    session_for_time,
)
from trademind.fx_signal_adapter import build_candidates
from trademind.signal_evidence import load_outcomes, similarity_dimensions, similarity_key
from trademind.signal_intelligence import SignalCandidate, candidate_from_dict
from trademind.signal_passport_factory import FactoryRun, run_factory
from trademind.signal_shadow import Bar, append_outcomes, evaluate_shadow_candidate
from trademind.signal_to_risk_bridge import BridgeRun, run_bridge
from trademind.volume import VolumeCollectSummary, collect_volume_files

RUNTIME_VERSION = "1.21.0"
RUN_COMPLETE = "RUN_COMPLETE"
WAITING_NO_NEW_BARS = "WAITING_NO_NEW_CLOSED_BARS"
WAITING_SOURCE_EMPTY = "WAITING_SOURCE_EMPTY"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _integer(value: Any, default: int = 0) -> int:
    try:
        return int(str(value or "").strip())
    except (TypeError, ValueError):
        return default


def _number(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _server_epoch_to_utc(epoch_seconds: int, server_utc_offset_hours: int) -> datetime:
    server_clock = datetime.fromtimestamp(epoch_seconds, tz=timezone.utc)
    return server_clock - timedelta(hours=server_utc_offset_hours)


def _bar_close_time(row: Mapping[str, Any], server_utc_offset_hours: int) -> datetime:
    opened = _server_epoch_to_utc(_integer(row.get("time")), server_utc_offset_hours)
    return opened + timedelta(seconds=max(1, _integer(row.get("bar_seconds"), 300)))


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    temporary.replace(path)


def _atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def _read_json(path: Path) -> Mapping[str, Any]:
    if not path.is_file():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, Mapping):
        raise ValueError(f"JSON root must be an object: {path}")
    return payload


def closed_volume_rows(
    rows: Sequence[Mapping[str, str]],
    *,
    now: datetime,
    server_utc_offset_hours: int,
    close_grace_seconds: float,
) -> list[dict[str, str]]:
    """Return only healthy bars whose close is safely in the past."""
    if close_grace_seconds < 0:
        raise ValueError("close_grace_seconds cannot be negative")
    cutoff = now.astimezone(timezone.utc) - timedelta(seconds=close_grace_seconds)
    closed: list[dict[str, str]] = []
    for source in rows:
        row = dict(source)
        if row.get("symbol", "").upper() not in FX_MAJORS:
            continue
        if row.get("timeframe", "").upper() != "M5":
            continue
        if row.get("tick_copy_status", "").upper() != "OK":
            continue
        if _integer(row.get("time")) <= 0:
            continue
        if _bar_close_time(row, server_utc_offset_hours) <= cutoff:
            closed.append(row)
    closed.sort(key=lambda item: (item["symbol"], _integer(item["time"])))
    return closed


def latest_closed_watermarks(rows: Sequence[Mapping[str, str]]) -> dict[str, int]:
    result: dict[str, int] = {}
    for row in rows:
        symbol = row.get("symbol", "").upper()
        result[symbol] = max(result.get(symbol, 0), _integer(row.get("time")))
    return dict(sorted(result.items()))


def _observation_ids(path: Path) -> set[str]:
    if not path.is_file():
        return set()
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return {
            _text(row.get("observation_id"))
            for row in csv.DictReader(handle)
            if _text(row.get("observation_id"))
        }


def _prepare_live_observation(
    row: Mapping[str, str],
    source_rows_by_key: Mapping[tuple[str, int], Mapping[str, str]],
    *,
    server_utc_offset_hours: int,
) -> dict[str, str]:
    prepared = {field: _text(row.get(field)) for field in _OBSERVATION_FIELDS}
    source_key = (
        prepared.get("symbol", "").upper(),
        _integer(prepared.get("source_bar_time")),
    )
    source = source_rows_by_key.get(source_key)
    if source is None:
        raise ValueError(f"source bar missing for observation {prepared.get('observation_id')}")
    available_at = _bar_close_time(source, server_utc_offset_hours)
    prepared["signal_time"] = _iso(available_at)
    prepared["session"] = session_for_time(available_at)
    prepared["server_utc_offset_hours"] = str(server_utc_offset_hours)
    return prepared


def append_observations(path: Path, rows: Sequence[Mapping[str, str]]) -> int:
    """Append immutable live observations while preserving a stable CSV schema."""
    existing_ids = _observation_ids(path)
    additions = [dict(row) for row in rows if _text(row.get("observation_id")) not in existing_ids]
    if not additions:
        return 0

    existing_rows: list[dict[str, str]] = []
    if path.is_file():
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            existing_rows = [dict(row) for row in csv.DictReader(handle)]

    combined = existing_rows + additions
    combined.sort(key=lambda item: (_text(item.get("signal_time")), _text(item.get("symbol"))))
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=_OBSERVATION_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(combined)
    temporary.replace(path)
    return len(additions)


def _candidate_payload(candidate: SignalCandidate, observation_id: str) -> dict[str, Any]:
    return {
        **candidate.as_dict(),
        "similarity_key": similarity_key(candidate),
        "similarity_dimensions": similarity_dimensions(candidate),
        "shadow_only": True,
        "orders_enabled": False,
        "publication_enabled": False,
        "runtime": {
            "version": RUNTIME_VERSION,
            "observation_id": observation_id,
            "read_only": True,
        },
    }


def _load_candidate_payloads(path: Path) -> dict[str, Mapping[str, Any]]:
    payloads: dict[str, Mapping[str, Any]] = {}
    if not path.is_file():
        return payloads
    for line_number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"candidate line {line_number}: invalid JSON") from exc
        if not isinstance(payload, Mapping):
            raise ValueError(f"candidate line {line_number}: root must be an object")
        candidate = candidate_from_dict(payload)
        current = payloads.get(candidate.signal_id)
        if current is not None and dict(current) != dict(payload):
            raise ValueError(f"conflicting live candidate: {candidate.signal_id}")
        payloads[candidate.signal_id] = payload
    return payloads


def append_candidates(
    path: Path,
    observations: Sequence[Mapping[str, str]],
) -> tuple[int, tuple[str, ...], int]:
    """Build and append only valid BUY/SELL candidates from new observations."""
    candidates, errors = build_candidates(observations)
    observation_by_time_symbol = {
        (_text(row.get("signal_time")), _text(row.get("symbol")).upper()): _text(
            row.get("observation_id")
        )
        for row in observations
    }
    existing = _load_candidate_payloads(path)
    added_ids: list[str] = []
    for candidate in candidates:
        observation_id = observation_by_time_symbol.get(
            (_iso(candidate.observed_at), candidate.symbol.upper()),
            "",
        )
        payload = _candidate_payload(candidate, observation_id)
        current = existing.get(candidate.signal_id)
        if current is not None:
            if dict(current) != payload:
                raise ValueError(f"immutable live candidate mutation: {candidate.signal_id}")
            continue
        existing[candidate.signal_id] = payload
        added_ids.append(candidate.signal_id)

    ordered = sorted(
        existing.values(),
        key=lambda payload: (
            _text(payload.get("observed_at")),
            _text(payload.get("signal_id")),
        ),
    )
    _atomic_text(
        path,
        "".join(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + "\n"
            for payload in ordered
        ),
    )
    return len(added_ids), tuple(added_ids), len(errors)


def _live_candidates(path: Path) -> list[SignalCandidate]:
    candidates: list[SignalCandidate] = []
    for payload in _load_candidate_payloads(path).values():
        runtime = payload.get("runtime", {})
        if not isinstance(runtime, Mapping):
            continue
        if _text(runtime.get("version")) != RUNTIME_VERSION:
            continue
        candidates.append(candidate_from_dict(payload))
    candidates.sort(key=lambda item: item.observed_at)
    return candidates


def _utc_bars(
    rows: Sequence[Mapping[str, str]],
    *,
    server_utc_offset_hours: int,
) -> list[Bar]:
    bars: list[Bar] = []
    for row in rows:
        bars.append(
            Bar(
                time=_bar_close_time(row, server_utc_offset_hours),
                symbol=_text(row.get("symbol")).upper(),
                timeframe=_text(row.get("timeframe")).upper(),
                open=_number(row.get("open")),
                high=_number(row.get("high")),
                low=_number(row.get("low")),
                close=_number(row.get("close")),
            )
        )
    bars.sort(key=lambda item: (item.symbol, item.timeframe, item.time))
    return bars


def evaluate_live_outcomes(
    *,
    candidates_path: Path,
    outcomes_path: Path,
    closed_rows: Sequence[Mapping[str, str]],
    server_utc_offset_hours: int,
    max_bars: int,
    cost_r: float,
) -> int:
    existing_ids = {row.signal_id for row in load_outcomes(outcomes_path)}
    bars = _utc_bars(closed_rows, server_utc_offset_hours=server_utc_offset_hours)
    completed = []
    for candidate in _live_candidates(candidates_path):
        if candidate.signal_id in existing_ids:
            continue
        outcome = evaluate_shadow_candidate(
            candidate,
            bars,
            max_bars=max_bars,
            target_index=0,
            cost_r=cost_r,
        )
        if outcome is not None:
            completed.append(outcome)
    return append_outcomes(outcomes_path, completed)


def merge_evidence_outcomes(
    historical_path: Path,
    live_path: Path,
    output_path: Path,
) -> int:
    """Build a derived immutable-by-signal-id evidence journal."""
    merged: dict[str, Mapping[str, Any]] = {}
    for source in (historical_path, live_path):
        if not source.is_file():
            continue
        for line_number, line in enumerate(
            source.read_text(encoding="utf-8-sig").splitlines(), start=1
        ):
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{source} line {line_number}: invalid JSON") from exc
            if not isinstance(payload, Mapping):
                raise ValueError(f"{source} line {line_number}: root must be an object")
            signal_id = _text(payload.get("signal_id"))
            if not signal_id:
                raise ValueError(f"{source} line {line_number}: signal_id missing")
            current = merged.get(signal_id)
            if current is not None and dict(current) != dict(payload):
                raise ValueError(f"conflicting outcome across archives: {signal_id}")
            merged[signal_id] = payload
    ordered = sorted(
        merged.values(),
        key=lambda payload: (
            _text(payload.get("completed_at")),
            _text(payload.get("signal_id")),
        ),
    )
    _atomic_text(
        output_path,
        "".join(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + "\n"
            for payload in ordered
        ),
    )
    return len(ordered)


@dataclass(frozen=True, slots=True)
class RuntimeRun:
    status: Mapping[str, Any]
    factory: FactoryRun
    bridge: BridgeRun

    @property
    def state(self) -> str:
        return _text(self.status.get("state"))


def run_live_runtime(
    *,
    login: str,
    volume_source_dir: Path,
    canonical_volume_path: Path,
    historical_outcomes_path: Path,
    runtime_root: Path,
    account_csv: Path,
    positions_csv: Path,
    symbols_csv: Path,
    risk_profile_path: Path,
    correlations_path: Path | None = None,
    journal_path: Path | None = None,
    server_utc_offset_hours: int = 0,
    close_grace_seconds: float = 5.0,
    bootstrap_lookback_seconds: float = 900.0,
    maximum_candidate_age_seconds: float = 900.0,
    maximum_mt5_age_seconds: float = 120.0,
    max_bars: int = 72,
    cost_r: float = 0.04,
    now: datetime | None = None,
) -> RuntimeRun:
    captured_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if not -14 <= server_utc_offset_hours <= 14:
        raise ValueError("server_utc_offset_hours must be between -14 and 14")
    if bootstrap_lookback_seconds <= 0:
        raise ValueError("bootstrap_lookback_seconds must be positive")
    if max_bars < 1:
        raise ValueError("max_bars must be positive")
    if cost_r < 0:
        raise ValueError("cost_r cannot be negative")

    runtime_root.mkdir(parents=True, exist_ok=True)
    observations_path = runtime_root / "observations.csv"
    candidates_path = runtime_root / "candidates.jsonl"
    live_outcomes_path = runtime_root / "outcomes.jsonl"
    evidence_outcomes_path = runtime_root / "evidence_outcomes.jsonl"
    factory_root = runtime_root / "factory"
    passports_dir = factory_root / "passports"
    bridge_root = runtime_root / "bridge" / _text(login)
    state_path = runtime_root / "state.json"
    status_path = runtime_root / "status.json"

    collect_summary: VolumeCollectSummary = collect_volume_files(
        volume_source_dir,
        canonical_volume_path,
    )
    volume_rows, _ = load_volume_rows(canonical_volume_path)
    closed_rows = closed_volume_rows(
        volume_rows,
        now=captured_at,
        server_utc_offset_hours=server_utc_offset_hours,
        close_grace_seconds=close_grace_seconds,
    )
    current_watermarks = latest_closed_watermarks(closed_rows)
    previous_state = _read_json(state_path)
    previous_watermarks = {
        str(key): _integer(value)
        for key, value in dict(previous_state.get("closed_bar_watermarks", {})).items()
    }
    has_new_bar = current_watermarks != previous_watermarks

    new_observations: list[dict[str, str]] = []
    new_observation_count = 0
    new_candidate_count = 0
    rejected_candidate_rows = 0
    if closed_rows and has_new_bar:
        built = build_fx_observations(
            list(closed_rows),
            server_utc_offset_hours=server_utc_offset_hours,
        )
        source_by_key = {
            (_text(row.get("symbol")).upper(), _integer(row.get("time"))): row
            for row in closed_rows
        }
        known_ids = _observation_ids(observations_path)
        bootstrap_cutoff = captured_at - timedelta(seconds=bootstrap_lookback_seconds)
        for raw in built:
            observation_id = _text(raw.get("observation_id"))
            if not observation_id or observation_id in known_ids:
                continue
            prepared = _prepare_live_observation(
                raw,
                source_by_key,
                server_utc_offset_hours=server_utc_offset_hours,
            )
            signal_time = datetime.fromisoformat(prepared["signal_time"])
            if not previous_state and signal_time < bootstrap_cutoff:
                continue
            new_observations.append(prepared)
        new_observation_count = append_observations(observations_path, new_observations)
        new_candidate_count, _, rejected_candidate_rows = append_candidates(
            candidates_path,
            new_observations,
        )
    elif not candidates_path.exists():
        _atomic_text(candidates_path, "")

    if not live_outcomes_path.exists():
        _atomic_text(live_outcomes_path, "")
    new_outcomes = evaluate_live_outcomes(
        candidates_path=candidates_path,
        outcomes_path=live_outcomes_path,
        closed_rows=closed_rows,
        server_utc_offset_hours=server_utc_offset_hours,
        max_bars=max_bars,
        cost_r=cost_r,
    )
    evidence_count = merge_evidence_outcomes(
        historical_outcomes_path,
        live_outcomes_path,
        evidence_outcomes_path,
    )

    factory = run_factory(
        candidates_path=candidates_path,
        outcomes_path=evidence_outcomes_path,
        output_dir=factory_root,
        passports_dir=passports_dir,
        journal=journal_path,
        cost_r=cost_r,
        maximum_candidate_age_seconds=maximum_candidate_age_seconds,
        now=captured_at,
    )
    bridge = run_bridge(
        login=_text(login),
        passports_dir=passports_dir,
        account_csv=account_csv,
        positions_csv=positions_csv,
        symbols_csv=symbols_csv,
        profile_path=risk_profile_path,
        correlations=correlations_path,
        journal=journal_path,
        cost_r=cost_r,
        maximum_mt5_age_seconds=maximum_mt5_age_seconds,
        output_dir=bridge_root,
        now=captured_at,
    )

    if not closed_rows:
        state = WAITING_SOURCE_EMPTY
    elif has_new_bar:
        state = RUN_COMPLETE
    else:
        state = WAITING_NO_NEW_BARS
    latest_close = (
        max(_bar_close_time(row, server_utc_offset_hours) for row in closed_rows)
        if closed_rows
        else None
    )
    status = {
        "schema_version": RUNTIME_VERSION,
        "state": state,
        "updated_at": _iso(captured_at),
        "account_login": _text(login),
        "server_utc_offset_hours": server_utc_offset_hours,
        "source_files": collect_summary.source_files,
        "canonical_rows": collect_summary.canonical_rows,
        "closed_fx_m5_rows": len(closed_rows),
        "latest_closed_bar_at": _iso(latest_close) if latest_close else None,
        "new_closed_bar_detected": has_new_bar,
        "new_observations": new_observation_count,
        "new_candidates": new_candidate_count,
        "candidate_rows_rejected": rejected_candidate_rows,
        "new_shadow_outcomes": new_outcomes,
        "evidence_outcomes": evidence_count,
        "factory_state": factory.state,
        "factory_publishable": factory.status.get("publishable", 0),
        "bridge_state": bridge.state,
        "risk_state": (
            bridge.package.get("risk_decision", {}).get("state")
            if bridge.package is not None
            else None
        ),
        "paths": {
            "canonical_volume": str(canonical_volume_path),
            "observations": str(observations_path),
            "candidates": str(candidates_path),
            "live_outcomes": str(live_outcomes_path),
            "evidence_outcomes": str(evidence_outcomes_path),
            "factory": str(factory_root),
            "bridge": str(bridge_root),
        },
        "safety": {
            "read_only": True,
            "orders_enabled": False,
            "signal_publication_enabled": False,
            "broker_api_called": False,
            "historical_archive_mutated": False,
            "grid_robots_used_as_signal_source": False,
        },
    }
    _atomic_json(status_path, status)
    _atomic_json(
        state_path,
        {
            "schema_version": RUNTIME_VERSION,
            "updated_at": _iso(captured_at),
            "closed_bar_watermarks": current_watermarks,
            "latest_closed_bar_at": status["latest_closed_bar_at"],
        },
    )
    return RuntimeRun(status=status, factory=factory, bridge=bridge)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="TradeMind Live Signal Runtime v1.21")
    parser.add_argument("--login", required=True)
    parser.add_argument(
        "--volume-source-dir",
        type=Path,
        default=(
            Path(os.getenv("APPDATA", ""))
            / "MetaQuotes"
            / "Terminal"
            / "Common"
            / "Files"
            / "TradeMindAI_Volume_v1_4"
        ),
    )
    parser.add_argument(
        "--canonical-volume",
        type=Path,
        default=Path("data/volume_v1_4/volume_bars.csv"),
    )
    parser.add_argument(
        "--historical-outcomes",
        type=Path,
        default=Path("data/signal_intelligence_v1_16/outcomes.jsonl"),
    )
    parser.add_argument(
        "--runtime-root",
        type=Path,
        default=Path("data/live_signal_runtime_v1"),
    )
    parser.add_argument("--account-csv", type=Path, required=True)
    parser.add_argument("--positions-csv", type=Path, required=True)
    parser.add_argument("--symbols-csv", type=Path, required=True)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--correlations", type=Path)
    parser.add_argument("--journal", type=Path)
    parser.add_argument("--server-utc-offset-hours", type=int, default=0)
    parser.add_argument("--close-grace-seconds", type=float, default=5.0)
    parser.add_argument("--bootstrap-lookback-seconds", type=float, default=900.0)
    parser.add_argument("--maximum-candidate-age-seconds", type=float, default=900.0)
    parser.add_argument("--maximum-mt5-age-seconds", type=float, default=120.0)
    parser.add_argument("--max-bars", type=int, default=72)
    parser.add_argument("--cost-r", type=float, default=0.04)
    args = parser.parse_args(argv)

    try:
        result = run_live_runtime(
            login=args.login,
            volume_source_dir=args.volume_source_dir.expanduser().resolve(),
            canonical_volume_path=args.canonical_volume.expanduser().resolve(),
            historical_outcomes_path=args.historical_outcomes.expanduser().resolve(),
            runtime_root=args.runtime_root.expanduser().resolve(),
            account_csv=args.account_csv.expanduser().resolve(),
            positions_csv=args.positions_csv.expanduser().resolve(),
            symbols_csv=args.symbols_csv.expanduser().resolve(),
            risk_profile_path=args.profile.expanduser().resolve(),
            correlations_path=(
                args.correlations.expanduser().resolve() if args.correlations else None
            ),
            journal_path=args.journal.expanduser().resolve() if args.journal else None,
            server_utc_offset_hours=args.server_utc_offset_hours,
            close_grace_seconds=args.close_grace_seconds,
            bootstrap_lookback_seconds=args.bootstrap_lookback_seconds,
            maximum_candidate_age_seconds=args.maximum_candidate_age_seconds,
            maximum_mt5_age_seconds=args.maximum_mt5_age_seconds,
            max_bars=args.max_bars,
            cost_r=args.cost_r,
        )
    except (OSError, ValueError, TypeError, csv.Error, json.JSONDecodeError) as exc:
        print(f"Live signal runtime failed: {exc}")
        return 1

    status = result.status
    print("TradeMind Live Signal Runtime v1.21")
    print("Closed bars -> fresh candidates -> passports -> MT5 risk decision.")
    print("Orders OFF. Publication OFF. Broker API not called.")
    print(f"State: {result.state}")
    print(
        "Closed rows / new observations / new candidates / new outcomes: "
        f"{status['closed_fx_m5_rows']}/{status['new_observations']}/"
        f"{status['new_candidates']}/{status['new_shadow_outcomes']}"
    )
    print(
        "Factory / Bridge / Risk: "
        f"{status['factory_state']} / {status['bridge_state']} / "
        f"{status.get('risk_state') or 'NONE'}"
    )
    print(f"Latest closed bar: {status.get('latest_closed_bar_at') or 'NONE'}")
    print(f"Status: {args.runtime_root / 'status.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
