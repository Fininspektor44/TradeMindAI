"""Deterministic, isolated SER8 replay over verified MT5 historical bars.

Replay reuses the production observation engines, FX candidate adapter, and
conservative shadow outcome evaluator. Candidate construction receives only
bars at or before its timestamp. Outcomes receive only strictly later bars.
Artifacts are written beneath ``data/ser8_historical_replay`` and are never
opened by or appended to the live runtime journals.
"""

from __future__ import annotations

import bisect
import hashlib
import inspect
import json
import math
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Sequence

import trademind.fx_research as fx_research_module
import trademind.fx_signal_adapter as fx_adapter_module
import trademind.signal_shadow as shadow_module
import trademind.signals.engine as signal_engine_module
import trademind.structure.engine as structure_engine_module
from trademind.fx_research import build_fx_observations, validate_fx_observations
from trademind.fx_signal_adapter import ADAPTER_VERSION, build_candidate
from trademind.ser8_historical_data import (
    HistoricalBarV1,
    HistoricalDataError,
    load_canonical_bars,
    load_inventory,
    verify_dataset,
)
from trademind.ser8_symbol_universe import VerifiedHistoricalResearchEvidenceV1
from trademind.signal_evidence import similarity_dimensions, similarity_key
from trademind.signal_intelligence import candidate_from_dict
from trademind.signal_shadow import Bar, evaluate_shadow_candidate
from trademind.signal_statistics_provenance import canonical_json_bytes, sha256_bytes
from trademind.signals import SignalEngine
from trademind.structure import MarketStructureEngine

REPLAY_SCHEMA_VERSION = "ser8-historical-replay-v1"
READINESS_SCHEMA_VERSION = "ser8-historical-research-readiness-v1"
POLICY_SCHEMA_VERSION = "ser8-historical-research-policy-v1"
REPLAY_VERSION = "1.0.0"
_REPLAY_HASH_DOMAIN = b"trademind:ser8:historical-replay:v1"
_READINESS_HASH_DOMAIN = b"trademind:ser8:historical-research-readiness:v1"
_OUTCOME_HASH_DOMAIN = b"trademind:ser8:historical-replay-outcome:v1"
_LIVE_ARTIFACT_PATH_PARTS = frozenset({
    "live_signal_runtime_v1",
    "signal_intelligence_v1_16",
    "paper_signals",
})


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _number(value: float) -> str:
    return format(float(value), ".17g")


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _percentile_rank(previous: Sequence[int], current: int) -> float:
    if not previous:
        return 0.0
    return 100.0 * sum(value <= current for value in previous) / len(previous)


def assert_replay_artifact_isolation(replay_root: Path, output_path: Path) -> tuple[Path, Path]:
    root = replay_root.expanduser().resolve()
    output = output_path.expanduser().resolve()
    lowered = {part.lower() for part in (*root.parts, *output.parts)}
    if lowered & _LIVE_ARTIFACT_PATH_PARTS or any(
        part.startswith("live_signal_runtime") for part in lowered
    ):
        raise HistoricalDataError(
            "LIVE_REPLAY_PATH_FORBIDDEN",
            "historical replay artifacts cannot target a live runtime/journal path",
        )
    try:
        output.relative_to(root)
    except ValueError as exc:
        raise HistoricalDataError(
            "REPLAY_OUTPUT_OUTSIDE_ROOT",
            "research readiness output must be inside replay_root",
        ) from exc
    if output.name != "research_readiness.json":
        raise HistoricalDataError(
            "REPLAY_OUTPUT_NAME_INVALID",
            "replay output must be named research_readiness.json",
        )
    return root, output


def load_research_policy(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HistoricalDataError("REPLAY_POLICY_READ_FAILED", f"cannot read replay policy: {path}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != POLICY_SCHEMA_VERSION:
        raise HistoricalDataError("REPLAY_POLICY_INVALID", "unsupported replay policy")
    expected_research_minimum = inspect.signature(validate_fx_observations).parameters[
        "research_minimum"
    ].default
    if payload.get("research_minimum_completed_outcomes") != expected_research_minimum:
        raise HistoricalDataError(
            "REPLAY_POLICY_DRIFT",
            "policy research minimum no longer matches fx_research.validate_fx_observations",
        )
    engine_minimum = max(SignalEngine().minimum_candles, MarketStructureEngine().minimum_candles)
    expected_attempt_rows = engine_minimum + int(payload.get("shadow_max_bars", 0))
    if payload.get("minimum_rows_for_replay_attempt") != expected_attempt_rows:
        raise HistoricalDataError(
            "REPLAY_POLICY_DRIFT",
            "minimum_rows_for_replay_attempt no longer matches engine history + shadow horizon",
        )
    if payload.get("initial_timeframe") != "M5" or payload.get("expected_interval_seconds") != 300:
        raise HistoricalDataError("REPLAY_POLICY_INVALID", "V1 initial timeframe contract must be M5/300s")
    if int(payload.get("shadow_max_bars", 0)) < 1 or float(payload.get("shadow_cost_r", -1)) < 0:
        raise HistoricalDataError("REPLAY_POLICY_INVALID", "shadow replay parameters are invalid")
    return payload


def _derived_volume_rows(
    bars: Sequence[HistoricalBarV1],
    *,
    point: float,
) -> list[dict[str, str]]:
    """Map only broker rates and deterministic past-only derivatives.

    Quote-direction microstructure is not available from copy_rates_range and
    therefore contributes a conservative zero. This limitation is explicit in
    replay provenance; no bar, price, or gap is synthesized.
    """
    if not math.isfinite(point) or point <= 0:
        raise HistoricalDataError("REPLAY_POINT_INVALID", "symbol point is required for replay")
    output: list[dict[str, str]] = []
    for index, bar in enumerate(bars):
        prior = bars[max(0, index - 100) : index]
        prior_20 = prior[-20:]
        prior_volumes = [item.tick_volume for item in prior_20 if item.tick_volume >= 0]
        mean_volume = _mean([float(value) for value in prior_volumes])
        tick_count = max(0, bar.tick_volume)
        bar_range_points = (bar.high - bar.low) / point
        body_points = abs(bar.close - bar.open) / point
        output.append(
            {
                "time": str(int(bar.time_utc.timestamp())),
                "symbol": bar.symbol,
                "timeframe": bar.timeframe,
                "open": _number(bar.open),
                "high": _number(bar.high),
                "low": _number(bar.low),
                "close": _number(bar.close),
                "bar_tick_volume": str(bar.tick_volume),
                "tick_count": str(tick_count),
                "tick_rate_per_sec": _number(tick_count / 300.0),
                "bid_up": "0",
                "bid_down": "0",
                "ask_up": "0",
                "ask_down": "0",
                "mid_up": "0",
                "mid_down": "0",
                "direction_imbalance": "0",
                "delta_proxy": "0",
                "rvol_20": _number(tick_count / mean_volume if mean_volume > 0 else 0.0),
                "volume_percentile_100": _number(_percentile_rank(prior_volumes, tick_count)),
                "spread_mean_points": str(bar.spread),
                "spread_min_points": "0",
                "spread_max_points": "0",
                "spread_last_points": "0",
                "spread_expansion_points": "0",
                "point": _number(point),
                "realized_abs_move_points": _number(body_points),
                "range_per_tick_points": _number(bar_range_points / tick_count if tick_count else 0.0),
                "body_per_tick_points": _number(body_points / tick_count if tick_count else 0.0),
                "tick_copy_status": "MT5_HISTORICAL_RATES_ONLY",
            }
        )
    return output


def code_provenance() -> dict[str, str]:
    modules = {
        "fx_research": fx_research_module,
        "fx_signal_adapter": fx_adapter_module,
        "signal_engine": signal_engine_module,
        "market_structure_engine": structure_engine_module,
        "signal_shadow": shadow_module,
        "historical_replay": __import__(__name__, fromlist=["unused"]),
    }
    return {
        name: sha256_bytes(Path(module.__file__).read_bytes())
        for name, module in sorted(modules.items())
    }


def build_replay_payloads(
    *,
    bars: Sequence[HistoricalBarV1],
    dataset_manifest: Mapping[str, object],
    max_bars: int,
    cost_r: float,
) -> tuple[list[dict[str, object]], list[dict[str, object]], int]:
    """Build candidates from past-only windows, then outcomes from future bars."""
    symbol = str(dataset_manifest["symbol"])
    volume_rows = _derived_volume_rows(bars, point=float(dataset_manifest["symbol_point"]))
    observations = build_fx_observations(
        volume_rows,
        symbols=(symbol,),
        include_forward_outcomes=False,
    )
    candidates_by_id: dict[str, object] = {}
    candidate_payloads: list[dict[str, object]] = []
    rejected = 0
    provenance = (
        f"SER8_HISTORICAL_REPLAY_{REPLAY_VERSION}",
        "MT5_COPY_RATES_RANGE_VERIFIED_SOURCE",
        "FX_RESEARCH_SIGNAL_ENGINE_AND_STRUCTURE",
        f"FX_SIGNAL_ADAPTER_{ADAPTER_VERSION}",
        "MT5_COPY_RATES_MICROSTRUCTURE_UNAVAILABLE_CONSERVATIVE_ZERO_CONTRIBUTION",
    )
    for observation in observations:
        try:
            candidate = build_candidate(observation, provenance=provenance)
        except ValueError:
            rejected += 1
            continue
        if candidate.signal_id in candidates_by_id:
            raise HistoricalDataError("REPLAY_CANDIDATE_DUPLICATE", candidate.signal_id)
        candidates_by_id[candidate.signal_id] = candidate
        candidate_payloads.append(
            {
                **candidate.as_dict(),
                "similarity_key": similarity_key(candidate),
                "similarity_dimensions": similarity_dimensions(candidate),
                "shadow_only": True,
                "orders_enabled": False,
                "publication_enabled": False,
                "replay": {
                    "schema_version": REPLAY_SCHEMA_VERSION,
                    "dataset_sha256": dataset_manifest["dataset_sha256"],
                    "live_journal": False,
                    "future_fields_available_to_candidate": False,
                },
            }
        )
    candidate_payloads.sort(key=lambda item: (str(item["observed_at"]), str(item["signal_id"])))

    shadow_bars = [
        Bar(
            time=bar.time_utc,
            symbol=bar.symbol,
            timeframe=bar.timeframe,
            open=bar.open,
            high=bar.high,
            low=bar.low,
            close=bar.close,
        )
        for bar in bars
    ]
    bar_times = [bar.time for bar in shadow_bars]
    outcome_payloads: list[dict[str, object]] = []
    for candidate_payload in candidate_payloads:
        candidate = candidates_by_id[str(candidate_payload["signal_id"])]
        first_future = bisect.bisect_right(bar_times, candidate.observed_at.astimezone(timezone.utc))
        future_window = shadow_bars[first_future : first_future + max_bars]
        outcome = evaluate_shadow_candidate(
            candidate,
            future_window,
            max_bars=max_bars,
            target_index=0,
            cost_r=cost_r,
        )
        if outcome is None:
            continue
        base = outcome.as_dict()
        if base["signal_id"] not in candidates_by_id:
            raise HistoricalDataError("REPLAY_OUTCOME_ORPHAN", str(base["signal_id"]))
        digest = hashlib.sha256()
        digest.update(_OUTCOME_HASH_DOMAIN)
        digest.update(b"\x00")
        digest.update(canonical_json_bytes(base))
        outcome_payloads.append(
            {
                **base,
                "replay_outcome_id": f"sha256:{digest.hexdigest()}",
                "dataset_sha256": dataset_manifest["dataset_sha256"],
                "live_journal": False,
            }
        )
    outcome_payloads.sort(key=lambda item: (str(item["completed_at"]), str(item["signal_id"])))
    return candidate_payloads, outcome_payloads, rejected


def _jsonl_bytes(rows: Sequence[Mapping[str, object]]) -> bytes:
    return b"".join(canonical_json_bytes(dict(row)) + b"\n" for row in rows)


def _write_synced(path: Path, content: bytes) -> None:
    with path.open("wb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def create_replay(
    *,
    dataset_dir: Path,
    replay_root: Path,
    policy: Mapping[str, object],
    captured_at: datetime,
) -> tuple[dict[str, object], Path]:
    replay_root, _ = assert_replay_artifact_isolation(
        replay_root,
        replay_root / "research_readiness.json",
    )
    dataset_manifest = verify_dataset(dataset_dir)
    if dataset_manifest.get("accepted_historical_data") is not True:
        raise HistoricalDataError("REPLAY_DATASET_NOT_ACCEPTED", "dataset failed historical integrity")
    bars = load_canonical_bars(dataset_dir / "bars.csv")
    max_bars = int(policy["shadow_max_bars"])
    cost_r = float(policy["shadow_cost_r"])
    candidates, outcomes, rejected = build_replay_payloads(
        bars=bars,
        dataset_manifest=dataset_manifest,
        max_bars=max_bars,
        cost_r=cost_r,
    )
    candidates_bytes = _jsonl_bytes(candidates)
    outcomes_bytes = _jsonl_bytes(outcomes)
    provenance = code_provenance()
    identity = {
        "schema_version": REPLAY_SCHEMA_VERSION,
        "dataset_id": dataset_manifest["dataset_id"],
        "dataset_sha256": dataset_manifest["dataset_sha256"],
        "execution_account_login": dataset_manifest["execution_account_login"],
        "market_data_account_login": dataset_manifest["market_data_account_login"],
        "execution_universe_source": dataset_manifest["execution_universe_source"],
        "market_data_source_type": dataset_manifest["market_data_source_type"],
        "market_data_account_server": dataset_manifest["market_data_account_server"],
        "symbol": dataset_manifest["symbol"],
        "timeframe": dataset_manifest["timeframe"],
        "policy_schema_version": policy["schema_version"],
        "policy_sha256": sha256_bytes(canonical_json_bytes(dict(policy))),
        "shadow_max_bars": max_bars,
        "shadow_cost_r": cost_r,
        "research_minimum_completed_outcomes": policy["research_minimum_completed_outcomes"],
        "code_provenance": provenance,
        "candidates_sha256": sha256_bytes(candidates_bytes),
        "outcomes_sha256": sha256_bytes(outcomes_bytes),
    }
    digest = hashlib.sha256()
    digest.update(_REPLAY_HASH_DOMAIN)
    digest.update(b"\x00")
    digest.update(canonical_json_bytes(identity))
    replay_sha256 = digest.hexdigest()
    eligible = (
        dataset_manifest.get("asset_class") in policy["research_asset_classes"]
        and dataset_manifest.get("broker_trade_mode") in policy["research_trade_modes"]
        and dataset_manifest.get("risk_model_supported") is True
    )
    minimum = int(policy["research_minimum_completed_outcomes"])
    research_ready = eligible and len(outcomes) >= minimum
    if not eligible:
        readiness_reason = "asset class, trade mode, or risk-model boundary is not eligible"
    elif len(outcomes) < minimum:
        readiness_reason = f"completed replay outcomes {len(outcomes)} below authoritative minimum {minimum}"
    else:
        readiness_reason = "verified replay outcomes meet the authoritative research minimum"
    manifest = {
        **identity,
        "replay_id": f"ser8-replay:sha256:{replay_sha256}",
        "replay_sha256": replay_sha256,
        "source_capture_utc": _utc_text(captured_at),
        "candidate_count": len(candidates),
        "candidate_rejected_count": rejected,
        "completed_outcome_count": len(outcomes),
        "incomplete_outcome_count": len(candidates) - len(outcomes),
        "candidate_future_access": False,
        "outcome_requires_candidate": True,
        "live_runtime_artifacts_written": False,
        "existing_hypotheses_modified": False,
        "protected_holdout_consumed": False,
        "research_ready": research_ready,
        "readiness_reason": readiness_reason,
    }
    manifest["manifest_sha256"] = hashlib.sha256(canonical_json_bytes(manifest)).hexdigest()
    replay_root.mkdir(parents=True, exist_ok=True)
    destination = replay_root / replay_sha256
    if destination.exists():
        existing = verify_replay(destination)
        if (
            existing.get("replay_sha256") != replay_sha256
            or (destination / "candidates.jsonl").read_bytes() != candidates_bytes
            or (destination / "outcomes.jsonl").read_bytes() != outcomes_bytes
        ):
            raise HistoricalDataError("REPLAY_COLLISION", "different replay exists under this identity")
        return existing, destination
    temporary = Path(tempfile.mkdtemp(prefix=".ser8-replay-", dir=replay_root))
    try:
        _write_synced(temporary / "candidates.jsonl", candidates_bytes)
        _write_synced(temporary / "outcomes.jsonl", outcomes_bytes)
        _write_synced(temporary / "manifest.json", canonical_json_bytes(manifest) + b"\n")
        temporary.replace(destination)
    except BaseException:
        if temporary.exists():
            shutil.rmtree(temporary)
        raise
    return manifest, destination


def verify_replay(replay_dir: Path) -> dict[str, object]:
    manifest_path = replay_dir / "manifest.json"
    candidates_path = replay_dir / "candidates.jsonl"
    outcomes_path = replay_dir / "outcomes.jsonl"
    if not all(path.is_file() for path in (manifest_path, candidates_path, outcomes_path)):
        raise HistoricalDataError("REPLAY_FILES_MISSING", f"replay files missing: {replay_dir}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise HistoricalDataError("REPLAY_MANIFEST_INVALID", f"invalid replay manifest: {replay_dir}") from exc
    if manifest.get("schema_version") != REPLAY_SCHEMA_VERSION:
        raise HistoricalDataError("REPLAY_SCHEMA_INVALID", f"unsupported replay schema: {replay_dir}")
    supplied_manifest_hash = manifest.get("manifest_sha256")
    semantic_manifest = dict(manifest)
    semantic_manifest.pop("manifest_sha256", None)
    if supplied_manifest_hash != hashlib.sha256(canonical_json_bytes(semantic_manifest)).hexdigest():
        raise HistoricalDataError("REPLAY_MANIFEST_HASH_MISMATCH", f"manifest hash mismatch: {replay_dir}")
    candidates_bytes = candidates_path.read_bytes()
    outcomes_bytes = outcomes_path.read_bytes()
    if manifest.get("candidates_sha256") != sha256_bytes(candidates_bytes):
        raise HistoricalDataError("REPLAY_CANDIDATES_HASH_MISMATCH", f"candidate hash mismatch: {replay_dir}")
    if manifest.get("outcomes_sha256") != sha256_bytes(outcomes_bytes):
        raise HistoricalDataError("REPLAY_OUTCOMES_HASH_MISMATCH", f"outcome hash mismatch: {replay_dir}")
    identity_keys = (
        "schema_version", "dataset_id", "dataset_sha256", "execution_account_login",
        "market_data_account_login", "execution_universe_source", "market_data_source_type",
        "market_data_account_server", "symbol", "timeframe",
        "policy_schema_version", "policy_sha256", "shadow_max_bars", "shadow_cost_r",
        "research_minimum_completed_outcomes", "code_provenance", "candidates_sha256",
        "outcomes_sha256",
    )
    identity = {key: manifest.get(key) for key in identity_keys}
    digest = hashlib.sha256()
    digest.update(_REPLAY_HASH_DOMAIN)
    digest.update(b"\x00")
    digest.update(canonical_json_bytes(identity))
    expected_replay = digest.hexdigest()
    if manifest.get("replay_sha256") != expected_replay or replay_dir.name != expected_replay:
        raise HistoricalDataError("REPLAY_IDENTITY_MISMATCH", f"replay identity mismatch: {replay_dir}")
    candidate_ids: set[str] = set()
    for line in candidates_bytes.splitlines():
        if not line:
            continue
        candidate = candidate_from_dict(json.loads(line))
        if candidate.signal_id in candidate_ids:
            raise HistoricalDataError("REPLAY_CANDIDATE_DUPLICATE", candidate.signal_id)
        candidate_ids.add(candidate.signal_id)
    outcome_ids: set[str] = set()
    for line in outcomes_bytes.splitlines():
        if not line:
            continue
        outcome = json.loads(line)
        signal_id = str(outcome.get("signal_id", ""))
        if signal_id not in candidate_ids:
            raise HistoricalDataError("REPLAY_OUTCOME_ORPHAN", signal_id)
        if signal_id in outcome_ids:
            raise HistoricalDataError("REPLAY_OUTCOME_DUPLICATE", signal_id)
        base_keys = (
            "schema_version", "signal_id", "setup_key", "completed_at", "outcome", "net_r",
            "exit_reason", "exit_price", "filled_entries", "allocation_filled", "average_entry",
            "mfe_r", "mae_r", "bars_observed",
        )
        base = {key: outcome.get(key) for key in base_keys}
        outcome_digest = hashlib.sha256()
        outcome_digest.update(_OUTCOME_HASH_DOMAIN)
        outcome_digest.update(b"\x00")
        outcome_digest.update(canonical_json_bytes(base))
        if outcome.get("replay_outcome_id") != f"sha256:{outcome_digest.hexdigest()}":
            raise HistoricalDataError("REPLAY_OUTCOME_IDENTITY_MISMATCH", signal_id)
        outcome_ids.add(signal_id)
    if manifest.get("candidate_count") != len(candidate_ids):
        raise HistoricalDataError("REPLAY_CANDIDATE_COUNT_MISMATCH", str(replay_dir))
    if manifest.get("completed_outcome_count") != len(outcome_ids):
        raise HistoricalDataError("REPLAY_OUTCOME_COUNT_MISMATCH", str(replay_dir))
    if manifest.get("candidate_future_access") is not False:
        raise HistoricalDataError("REPLAY_FUTURE_ACCESS_INVALID", str(replay_dir))
    if manifest.get("outcome_requires_candidate") is not True:
        raise HistoricalDataError("REPLAY_OUTCOME_BINDING_INVALID", str(replay_dir))
    return manifest


def readiness_hash(payload: Mapping[str, object]) -> str:
    semantic = dict(payload)
    semantic.pop("readiness_inventory_sha256", None)
    digest = hashlib.sha256()
    digest.update(_READINESS_HASH_DOMAIN)
    digest.update(b"\x00")
    digest.update(canonical_json_bytes(semantic))
    return digest.hexdigest()


def build_research_readiness_inventory(
    *,
    historical_inventory_path: Path,
    replay_root: Path,
    policy: Mapping[str, object],
    output_path: Path,
    captured_at: datetime,
) -> dict[str, object]:
    replay_root, output_path = assert_replay_artifact_isolation(replay_root, output_path)
    historical = load_inventory(historical_inventory_path)
    entries: list[dict[str, object]] = []
    for source_entry in historical["entries"]:
        entry = {
            "symbol": source_entry["symbol"],
            "execution_account_login": historical["execution_account_login"],
            "market_data_account_login": historical["market_data_account_login"],
            "asset_class": source_entry["asset_class"],
            "broker_trade_mode": source_entry["broker_trade_mode"],
            "risk_model_supported": source_entry["risk_model_supported"],
            "historical_rows": source_entry["row_count"],
            "accepted_historical_data": source_entry["accepted_historical_data"],
            "dataset_sha256": source_entry.get("dataset_sha256"),
            "dataset_dir": source_entry.get("dataset_dir"),
            "replay_sha256": None,
            "candidate_count": 0,
            "completed_outcome_count": 0,
            "research_minimum": int(policy["research_minimum_completed_outcomes"]),
            "research_ready": False,
            "readiness_reason": source_entry.get("status_reason") or source_entry["status"],
        }
        replay_eligible = (
            source_entry.get("accepted_historical_data") is True
            and source_entry.get("asset_class") in policy["research_asset_classes"]
            and source_entry.get("broker_trade_mode") in policy["research_trade_modes"]
            and source_entry.get("risk_model_supported") is True
        )
        if replay_eligible:
            dataset_dir = Path(str(source_entry["dataset_dir"]))
            replay_manifest, replay_dir = create_replay(
                dataset_dir=dataset_dir,
                replay_root=replay_root,
                policy=policy,
                captured_at=captured_at,
            )
            entry.update(
                {
                    "replay_sha256": replay_manifest["replay_sha256"],
                    "replay_dir": str(replay_dir),
                    "candidate_count": replay_manifest["candidate_count"],
                    "completed_outcome_count": replay_manifest["completed_outcome_count"],
                    "research_ready": replay_manifest["research_ready"],
                    "readiness_reason": replay_manifest["readiness_reason"],
                }
            )
        elif source_entry.get("accepted_historical_data") is True:
            entry["readiness_reason"] = (
                "historical data retained for inventory; asset/trade/risk boundary is not replay eligible"
            )
        entries.append(entry)
    entries.sort(key=lambda item: str(item["symbol"]))
    payload: dict[str, object] = {
        "schema_version": READINESS_SCHEMA_VERSION,
        "captured_at_utc": _utc_text(captured_at),
        "execution_account_login": historical["execution_account_login"],
        "market_data_account_login": historical["market_data_account_login"],
        "execution_universe_source": historical["execution_universe_source"],
        "market_data_source_type": historical["market_data_source_type"],
        "market_data_account_server": historical["market_data_account_server"],
        "historical_inventory_sha256": historical["inventory_sha256"],
        "policy": dict(policy),
        "historical_inventory_path": str(historical_inventory_path),
        "replay_root": str(replay_root),
        "entries": entries,
        "research_ready_count": sum(entry["research_ready"] is True for entry in entries),
        "live_runtime_artifacts_written": False,
        "hypotheses_created": 0,
        "holdouts_consumed": 0,
    }
    payload["readiness_inventory_sha256"] = readiness_hash(payload)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_name(f".{output_path.name}.tmp")
    _write_synced(temporary, canonical_json_bytes(payload) + b"\n")
    temporary.replace(output_path)
    return payload


def load_verified_research_readiness(
    path: Path,
    *,
    execution_account_login: str | None = None,
    market_data_account_login: str | None = None,
) -> tuple[dict[str, int], dict[str, VerifiedHistoricalResearchEvidenceV1]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HistoricalDataError("READINESS_INVENTORY_READ_FAILED", f"cannot read {path}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != READINESS_SCHEMA_VERSION:
        raise HistoricalDataError("READINESS_INVENTORY_INVALID", "unsupported readiness inventory")
    if payload.get("readiness_inventory_sha256") != readiness_hash(payload):
        raise HistoricalDataError("READINESS_INVENTORY_HASH_MISMATCH", "readiness inventory hash mismatch")
    historical_inventory_path = payload.get("historical_inventory_path")
    if not isinstance(historical_inventory_path, str):
        raise HistoricalDataError("READINESS_HISTORICAL_INVENTORY_MISSING", str(path))
    historical_inventory = load_inventory(Path(historical_inventory_path))
    if historical_inventory.get("inventory_sha256") != payload.get("historical_inventory_sha256"):
        raise HistoricalDataError(
            "READINESS_HISTORICAL_INVENTORY_MISMATCH",
            "readiness inventory does not match its historical inventory",
        )
    if execution_account_login is not None and payload.get("execution_account_login") != str(
        execution_account_login
    ).strip():
        raise HistoricalDataError(
            "READINESS_EXECUTION_ACCOUNT_MISMATCH",
            "readiness inventory belongs to a different execution account",
        )
    if market_data_account_login is not None and payload.get("market_data_account_login") != str(
        market_data_account_login
    ).strip():
        raise HistoricalDataError(
            "READINESS_MARKET_DATA_ACCOUNT_MISMATCH",
            "readiness inventory belongs to a different market-data account",
        )
    for field in (
        "execution_account_login",
        "market_data_account_login",
        "execution_universe_source",
        "market_data_source_type",
        "market_data_account_server",
    ):
        if payload.get(field) != historical_inventory.get(field):
            raise HistoricalDataError(
                "READINESS_ACCOUNT_PROVENANCE_MISMATCH",
                f"readiness {field} does not match its historical inventory",
            )
    policy = payload.get("policy")
    if not isinstance(policy, dict):
        raise HistoricalDataError("READINESS_POLICY_MISSING", str(path))
    policy_sha256 = sha256_bytes(canonical_json_bytes(policy))
    rows: dict[str, int] = {}
    evidence: dict[str, VerifiedHistoricalResearchEvidenceV1] = {}
    seen: set[str] = set()
    for entry in payload.get("entries", []):
        if not isinstance(entry, dict):
            raise HistoricalDataError("READINESS_ENTRY_INVALID", "readiness entry must be an object")
        symbol = str(entry.get("symbol", "")).upper()
        if not symbol or symbol in seen:
            raise HistoricalDataError("READINESS_ENTRY_INVALID", f"invalid/duplicate symbol: {symbol}")
        seen.add(symbol)
        historical_rows = int(entry.get("historical_rows", 0))
        if entry.get("accepted_historical_data") is True:
            rows[symbol] = historical_rows
        dataset_sha = entry.get("dataset_sha256")
        replay_sha = entry.get("replay_sha256")
        if isinstance(dataset_sha, str) and isinstance(replay_sha, str):
            dataset_dir = entry.get("dataset_dir")
            replay_dir = entry.get("replay_dir")
            if not isinstance(dataset_dir, str) or not isinstance(replay_dir, str):
                raise HistoricalDataError("READINESS_ARTIFACT_PATH_MISSING", symbol)
            dataset_manifest = verify_dataset(Path(dataset_dir))
            replay_manifest = verify_replay(Path(replay_dir))
            if (
                dataset_manifest.get("dataset_sha256") != dataset_sha
                or dataset_manifest.get("execution_account_login")
                != payload.get("execution_account_login")
                or dataset_manifest.get("market_data_account_login")
                != payload.get("market_data_account_login")
                or replay_manifest.get("replay_sha256") != replay_sha
                or replay_manifest.get("dataset_sha256") != dataset_sha
                or replay_manifest.get("execution_account_login")
                != payload.get("execution_account_login")
                or replay_manifest.get("market_data_account_login")
                != payload.get("market_data_account_login")
                or replay_manifest.get("completed_outcome_count") != entry.get("completed_outcome_count")
                or replay_manifest.get("research_minimum_completed_outcomes") != entry.get("research_minimum")
                or replay_manifest.get("research_ready") is not entry.get("research_ready")
                or replay_manifest.get("policy_sha256") != policy_sha256
            ):
                raise HistoricalDataError("READINESS_ARTIFACT_MISMATCH", symbol)
            minimum = int(entry.get("research_minimum", 0))
            completed = int(entry.get("completed_outcome_count", 0))
            ready = entry.get("research_ready") is True
            eligible = (
                entry.get("asset_class") == "FX"
                and entry.get("broker_trade_mode") == "FULL"
                and entry.get("risk_model_supported") is True
            )
            if ready and (not eligible or completed < minimum):
                raise HistoricalDataError("READINESS_ENTRY_FALSE_READY", f"false readiness claim: {symbol}")
            evidence[symbol] = VerifiedHistoricalResearchEvidenceV1(
                dataset_sha256=dataset_sha,
                replay_sha256=replay_sha,
                historical_rows=historical_rows,
                completed_outcomes=completed,
                research_minimum=minimum,
                research_ready=ready,
                readiness_reason=str(entry.get("readiness_reason") or "historical replay incomplete"),
            )
    return rows, evidence


__all__ = [
    "POLICY_SCHEMA_VERSION",
    "READINESS_SCHEMA_VERSION",
    "REPLAY_SCHEMA_VERSION",
    "build_replay_payloads",
    "build_research_readiness_inventory",
    "code_provenance",
    "create_replay",
    "load_research_policy",
    "load_verified_research_readiness",
    "readiness_hash",
    "assert_replay_artifact_isolation",
    "verify_replay",
]
