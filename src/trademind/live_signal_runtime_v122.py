"""TradeMind Live Signal Runtime v1.22.1 incremental safety patch.

This orchestration layer fixes the v1.21 bootstrap boundary: after the initial
lookback, only observations whose source bar is strictly newer than the stored
per-symbol watermark may enter the live archive. A newly appearing symbol gets
the same bounded bootstrap window instead of a historical backfill.

The module remains read-only. It does not publish signals, import a broker
client, or send, modify, or close orders.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from trademind.fx_research import build_fx_observations, load_volume_rows
from trademind.live_signal_runtime import (
    RUN_COMPLETE,
    WAITING_NO_NEW_BARS,
    WAITING_SOURCE_EMPTY,
    RuntimeRun,
    _atomic_json,
    _atomic_text,
    _bar_close_time,
    _integer,
    _iso,
    _observation_ids,
    _prepare_live_observation,
    _read_json,
    _text,
    append_candidates,
    append_observations,
    closed_volume_rows,
    evaluate_live_outcomes,
    latest_closed_watermarks,
    merge_evidence_outcomes,
)
from trademind.signal_passport_factory import run_factory
from trademind.signal_to_risk_bridge import run_bridge
from trademind.volume import VolumeCollectSummary, collect_volume_files

RUNTIME_PATCH_VERSION = "1.22.1"


def select_incremental_observations(
    built: Sequence[Mapping[str, str]],
    source_rows_by_key: Mapping[tuple[str, int], Mapping[str, str]],
    *,
    previous_watermarks: Mapping[str, int],
    known_ids: set[str],
    captured_at: datetime,
    bootstrap_lookback_seconds: float,
    server_utc_offset_hours: int,
) -> list[dict[str, str]]:
    """Select only genuinely new observations using per-symbol watermarks.

    Existing symbols must be strictly newer than their stored source-bar epoch.
    Symbols with no prior watermark are limited to the bounded bootstrap window.
    """
    if bootstrap_lookback_seconds <= 0:
        raise ValueError("bootstrap_lookback_seconds must be positive")
    cutoff = captured_at.astimezone(timezone.utc) - timedelta(
        seconds=bootstrap_lookback_seconds
    )
    selected: list[dict[str, str]] = []
    for raw in built:
        observation_id = _text(raw.get("observation_id"))
        symbol = _text(raw.get("symbol")).upper()
        source_epoch = _integer(raw.get("source_bar_time"))
        if not observation_id or observation_id in known_ids or not symbol or source_epoch <= 0:
            continue

        prepared = _prepare_live_observation(
            raw,
            source_rows_by_key,
            server_utc_offset_hours=server_utc_offset_hours,
        )
        signal_time = datetime.fromisoformat(prepared["signal_time"]).astimezone(
            timezone.utc
        )
        previous = previous_watermarks.get(symbol)
        if previous is None:
            if signal_time < cutoff:
                continue
        elif source_epoch <= previous:
            continue
        selected.append(prepared)

    selected.sort(
        key=lambda item: (
            _text(item.get("signal_time")),
            _text(item.get("symbol")),
        )
    )
    return selected


def _watermark_advances(
    current: Mapping[str, int], previous: Mapping[str, int]
) -> dict[str, int]:
    regressed = {
        symbol: value
        for symbol, value in current.items()
        if symbol in previous and value < previous[symbol]
    }
    if regressed:
        details = ", ".join(
            f"{symbol}:{value}<{previous[symbol]}"
            for symbol, value in sorted(regressed.items())
        )
        raise ValueError(f"closed-bar watermark regressed: {details}")
    return {
        symbol: value
        for symbol, value in current.items()
        if value > previous.get(symbol, 0)
    }


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
        str(key).upper(): _integer(value)
        for key, value in dict(previous_state.get("closed_bar_watermarks", {})).items()
    }
    advanced_symbols = _watermark_advances(current_watermarks, previous_watermarks)
    has_new_bar = bool(advanced_symbols)

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
        new_observations = select_incremental_observations(
            built,
            source_by_key,
            previous_watermarks=previous_watermarks,
            known_ids=_observation_ids(observations_path),
            captured_at=captured_at,
            bootstrap_lookback_seconds=bootstrap_lookback_seconds,
            server_utc_offset_hours=server_utc_offset_hours,
        )
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
        "schema_version": RUNTIME_PATCH_VERSION,
        "state": state,
        "updated_at": _iso(captured_at),
        "account_login": _text(login),
        "server_utc_offset_hours": server_utc_offset_hours,
        "source_files": collect_summary.source_files,
        "canonical_rows": collect_summary.canonical_rows,
        "closed_fx_m5_rows": len(closed_rows),
        "latest_closed_bar_at": _iso(latest_close) if latest_close else None,
        "new_closed_bar_detected": has_new_bar,
        "advanced_symbols": dict(sorted(advanced_symbols.items())),
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
        "incremental_guard": {
            "version": RUNTIME_PATCH_VERSION,
            "per_symbol_watermark_required": True,
            "new_symbol_bootstrap_seconds": bootstrap_lookback_seconds,
            "historical_backfill_allowed": False,
        },
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
            "schema_version": RUNTIME_PATCH_VERSION,
            "updated_at": _iso(captured_at),
            "closed_bar_watermarks": current_watermarks,
            "latest_closed_bar_at": status["latest_closed_bar_at"],
            "historical_backfill_allowed": False,
        },
    )
    return RuntimeRun(status=status, factory=factory, bridge=bridge)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="TradeMind Live Signal Runtime v1.22.1 incremental patch"
    )
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
    parser.add_argument("--canonical-volume", type=Path, default=Path("data/volume_v1_4/volume_bars.csv"))
    parser.add_argument("--historical-outcomes", type=Path, default=Path("data/signal_intelligence_v1_16/outcomes.jsonl"))
    parser.add_argument("--runtime-root", type=Path, default=Path("data/live_signal_runtime_v1"))
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
            correlations_path=args.correlations.expanduser().resolve() if args.correlations else None,
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
        print(f"Live signal runtime v1.22.1 failed: {exc}")
        return 1

    status = result.status
    print("TradeMind Live Signal Runtime v1.22.1")
    print("Strict per-symbol watermarks. Historical backfill OFF.")
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
