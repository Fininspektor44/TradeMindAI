"""Adapt existing FX research observations into v1.16 shadow signal candidates.

The adapter reuses the market data already collected by TradeMind: structure,
liquidity, FVG, Fibonacci/OTE, volume, momentum, ATR, spread and session. It
creates deterministic candidate passports before any evidence gate is applied.
It does not use grid robots as signal sources and does not publish or trade.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from trademind.signal_evidence import similarity_dimensions, similarity_key
from trademind.signal_intelligence import EntryOrder, SignalCandidate, TradePlan

ADAPTER_VERSION = "1.16.0"
VALID_ACTIONS = {"BUY", "SELL"}
FIB_OTE_LEVELS = (0.618, 0.705, 0.790)


def _text(row: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        value = str(row.get(key, "") or "").strip()
        if value:
            return value
    return ""


def _number(row: Mapping[str, Any], *keys: str) -> float | None:
    text = _text(row, *keys).replace(",", ".")
    if not text:
        return None
    try:
        value = float(text)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _truthy(row: Mapping[str, Any], *keys: str) -> bool:
    return _text(row, *keys).lower() in {"1", "true", "yes", "y"}


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("signal_time must include timezone information")
    return parsed.astimezone(timezone.utc)


def _clamp(value: float, lower: float = 0.0, upper: float = 1.0) -> float:
    return max(lower, min(upper, value))


def _aligned(action: str, value: str) -> bool:
    normalized = value.upper()
    return normalized in ({"BULLISH", "BUY", "UP"} if action == "BUY" else {"BEARISH", "SELL", "DOWN"})


def _first_valid(values: Sequence[float | None], predicate: Any) -> float | None:
    for value in values:
        if value is not None and predicate(value):
            return value
    return None


def _market_geometry(row: Mapping[str, Any], action: str) -> tuple[float, float, float]:
    entry = _number(row, "entry_price", "close")
    atr = _number(row, "atr")
    if entry is None or entry <= 0 or atr is None or atr <= 0:
        raise ValueError("entry_price and ATR are required")

    lows = (
        _number(row, "swing_reference_low"),
        _number(row, "internal_reference_low"),
        _number(row, "liquidity_reference_low"),
        _number(row, "bar_low", "low"),
    )
    highs = (
        _number(row, "swing_reference_high"),
        _number(row, "internal_reference_high"),
        _number(row, "liquidity_reference_high"),
        _number(row, "bar_high", "high"),
    )
    protected_low = _first_valid(lows, lambda value: 0 < value < entry)
    protected_high = _first_valid(highs, lambda value: value > entry)
    if protected_low is None:
        protected_low = entry - 2.0 * atr
    if protected_high is None:
        protected_high = entry + 2.0 * atr
    if protected_high <= protected_low:
        raise ValueError("invalid structure range")
    return entry, protected_low, protected_high


def _build_plan(row: Mapping[str, Any], action: str) -> tuple[TradePlan, dict[str, float]]:
    entry, protected_low, protected_high = _market_geometry(row, action)
    atr = _number(row, "atr") or 0.0
    structure_range = protected_high - protected_low

    staged: list[EntryOrder] = [
        EntryOrder(
            price=entry,
            allocation=0.50,
            rationale="Market confirmation entry from the research signal close",
            order_type="MARKET",
        )
    ]
    allocations = (0.30, 0.20)
    ratios = FIB_OTE_LEVELS[1:]
    for ratio, allocation in zip(ratios, allocations, strict=True):
        level = (
            protected_high - ratio * structure_range
            if action == "BUY"
            else protected_low + ratio * structure_range
        )
        favorable = level < entry if action == "BUY" else level > entry
        if favorable and all(abs(level - item.price) > atr * 0.05 for item in staged):
            staged.append(
                EntryOrder(
                    price=level,
                    allocation=allocation,
                    rationale=f"Fibonacci/OTE {100 * ratio:.1f}% retracement",
                    order_type="LIMIT",
                )
            )

    allocated = sum(item.allocation for item in staged)
    average_entry = sum(item.price * item.allocation for item in staged) / allocated
    buffer = max(0.20 * atr, abs(entry) * 1e-6)
    if action == "BUY":
        stop = protected_low - buffer
        risk = average_entry - stop
        target_one = max(protected_high, average_entry + 1.5 * risk)
        target_two = max(target_one + 0.10 * atr, average_entry + 2.0 * risk)
        invalidation = "Protected swing low breaks after the liquidity/OTE setup"
        target_reasons = ("Prior/external high or 1.5R", "External liquidity or 2R")
    else:
        stop = protected_high + buffer
        risk = stop - average_entry
        target_one = min(protected_low, average_entry - 1.5 * risk)
        target_two = min(target_one - 0.10 * atr, average_entry - 2.0 * risk)
        invalidation = "Protected swing high breaks after the liquidity/OTE setup"
        target_reasons = ("Prior/external low or 1.5R", "External liquidity or 2R")
    if risk <= 0:
        raise ValueError("non-positive structural risk")

    plan = TradePlan(
        action=action,
        entries=tuple(staged),
        stop_price=stop,
        targets=(target_one, target_two),
        invalidation=invalidation,
        target_rationale=target_reasons,
    )
    geometry = {
        "protected_low": protected_low,
        "protected_high": protected_high,
        "structure_range": structure_range,
        "current_retracement": (
            (protected_high - entry) / structure_range
            if action == "BUY"
            else (entry - protected_low) / structure_range
        ),
    }
    return plan, geometry


def _factor_scores(
    row: Mapping[str, Any],
    action: str,
    geometry: Mapping[str, float],
) -> tuple[dict[str, float], dict[str, tuple[str, ...]]]:
    swing_bias = _text(row, "swing_bias")
    internal_bias = _text(row, "internal_bias")
    swing_break = _text(row, "swing_break")
    internal_break = _text(row, "internal_break")
    structure_parts = (
        0.30 * _aligned(action, swing_bias)
        + 0.25 * _aligned(action, internal_bias)
        + 0.25 * _aligned(action, swing_break)
        + 0.20 * _aligned(action, internal_break)
    )

    aligned_sweep = _truthy(row, "ssl_sweep") if action == "BUY" else _truthy(row, "bsl_sweep")
    sweep_depth = (
        _number(row, "ssl_sweep_depth_atr")
        if action == "BUY"
        else _number(row, "bsl_sweep_depth_atr")
    ) or 0.0
    liquidity_score = 0.70 * aligned_sweep + 0.30 * _clamp(sweep_depth / 0.50)

    retracement = geometry["current_retracement"]
    if 0.618 <= retracement <= 0.790:
        fibonacci_score = 1.0
    elif 0.50 <= retracement <= 0.89:
        fibonacci_score = 0.70
    else:
        fibonacci_score = 0.25

    rvol = _number(row, "rvol_20", "volume_ratio_20") or 0.0
    percentile = _number(row, "volume_percentile_100") or 0.0
    imbalance = _number(row, "direction_imbalance", "delta_proxy") or 0.0
    direction = 1.0 if action == "BUY" else -1.0
    pressure_aligned = direction * imbalance > 0.05
    volume_score = (
        0.45 * _clamp((rvol - 0.80) / 1.00)
        + 0.30 * _clamp(percentile / 100.0)
        + 0.25 * pressure_aligned
    )

    ema_fast = _number(row, "ema_fast")
    ema_slow = _number(row, "ema_slow")
    ema_aligned = False
    if ema_fast is not None and ema_slow is not None:
        ema_aligned = ema_fast > ema_slow if action == "BUY" else ema_fast < ema_slow
    body_efficiency = _number(row, "body_efficiency_ratio_20") or 0.0
    signal_confidence = (_number(row, "confidence") or 0.0) / 100.0
    if signal_confidence <= 0:
        signal_confidence = (_number(row, "score") or 0.0) / 100.0
    momentum_score = (
        0.35 * ema_aligned
        + 0.35 * _clamp(body_efficiency / 1.50)
        + 0.30 * _clamp(signal_confidence)
    )

    atr = _number(row, "atr") or 0.0
    spread_cost_atr = _number(row, "spread_cost_atr") or 1.0
    spread_ratio = _number(row, "spread_ratio_20") or 1.0
    volatility_score = (
        0.35 * (atr > 0)
        + 0.40 * _clamp(1.0 - spread_cost_atr / 0.15)
        + 0.25 * _clamp(1.5 - spread_ratio)
    )

    fvg_direction = _text(row, "fvg_direction")
    fvg_aligned = _aligned(action, fvg_direction)
    close_confirmed = _aligned(action, swing_break) or _aligned(action, internal_break)
    confirmation_score = 0.55 * fvg_aligned + 0.45 * close_confirmed

    session = _text(row, "session").upper()
    session_score = {
        "LONDON_NY_OVERLAP": 1.0,
        "LONDON": 0.90,
        "NEW_YORK": 0.85,
        "ASIA": 0.60,
        "OFF_HOURS": 0.25,
    }.get(session, 0.40)
    execution_score = (
        0.65 * _clamp(1.0 - spread_cost_atr / 0.12)
        + 0.35 * _clamp(1.5 - spread_ratio)
    )

    scores = {
        "structure": _clamp(structure_parts),
        "liquidity": _clamp(liquidity_score),
        "fibonacci": _clamp(fibonacci_score),
        "volume": _clamp(volume_score),
        "momentum": _clamp(momentum_score),
        "volatility": _clamp(volatility_score),
        "confirmation": _clamp(confirmation_score),
        "session": _clamp(session_score),
        "execution": _clamp(execution_score),
        "portfolio": 0.50,
    }
    reasons = {
        "structure": (
            f"swing_bias={swing_bias or 'UNKNOWN'}",
            f"internal_bias={internal_bias or 'UNKNOWN'}",
            f"swing_break={swing_break or 'NONE'}",
        ),
        "liquidity": (
            f"aligned_sweep={int(aligned_sweep)}",
            f"sweep_depth_atr={sweep_depth:.3f}",
        ),
        "fibonacci": (f"current_retracement={retracement:.3f}",),
        "volume": (
            f"rvol={rvol:.3f}",
            f"volume_percentile={percentile:.1f}",
            f"pressure_aligned={int(pressure_aligned)}",
        ),
        "momentum": (
            f"ema_aligned={int(ema_aligned)}",
            f"body_efficiency={body_efficiency:.3f}",
        ),
        "volatility": (
            f"atr={atr:.8f}",
            f"spread_cost_atr={spread_cost_atr:.4f}",
        ),
        "confirmation": (
            f"fvg={fvg_direction or 'NONE'}",
            f"break_confirmed={int(close_confirmed)}",
        ),
        "session": (session or "UNKNOWN",),
        "execution": (f"spread_ratio={spread_ratio:.3f}",),
        "portfolio": ("portfolio correlation feed not connected yet",),
    }
    return scores, reasons


def _market_features(
    row: Mapping[str, Any],
    action: str,
    geometry: Mapping[str, float],
) -> dict[str, dict[str, Any]]:
    historical_rates_only = _text(row, "tick_copy_status").upper() == "MT5_HISTORICAL_RATES_ONLY"
    features = {
        "structure": {
            "internal_bias": _text(row, "internal_bias"),
            "internal_break": _text(row, "internal_break"),
            "swing_bias": _text(row, "swing_bias"),
            "swing_break": _text(row, "swing_break"),
            "protected_low": geometry["protected_low"],
            "protected_high": geometry["protected_high"],
            "structure_event_count": _number(row, "structure_event_count") or 0.0,
        },
        "liquidity": {
            "bsl_sweep": _truthy(row, "bsl_sweep"),
            "ssl_sweep": _truthy(row, "ssl_sweep"),
            "bsl_sweep_depth_atr": _number(row, "bsl_sweep_depth_atr") or 0.0,
            "ssl_sweep_depth_atr": _number(row, "ssl_sweep_depth_atr") or 0.0,
            "reference_high": _number(row, "liquidity_reference_high"),
            "reference_low": _number(row, "liquidity_reference_low"),
        },
        "fibonacci": {
            "retracement": geometry["current_retracement"],
            "ote_low": 0.618,
            "ote_mid": 0.705,
            "ote_high": 0.790,
        },
        "volume": {
            "bar_tick_volume": _number(row, "bar_tick_volume") or 0.0,
            "rvol_20": _number(row, "rvol_20", "volume_ratio_20") or 0.0,
            "volume_percentile_100": _number(row, "volume_percentile_100") or 0.0,
            "direction_imbalance": (
                None if historical_rates_only else _number(row, "direction_imbalance") or 0.0
            ),
            "delta_proxy": None if historical_rates_only else _number(row, "delta_proxy") or 0.0,
            "tick_rate_ratio_20": _number(row, "tick_rate_ratio_20") or 0.0,
        },
        "momentum": {
            "ema_fast": _number(row, "ema_fast"),
            "ema_slow": _number(row, "ema_slow"),
            "rsi": _number(row, "rsi"),
            "body_efficiency_ratio_20": _number(row, "body_efficiency_ratio_20") or 0.0,
            "range_efficiency_ratio_20": _number(row, "range_efficiency_ratio_20") or 0.0,
            "source_score": _number(row, "score") or 0.0,
            "source_confidence": _number(row, "confidence") or 0.0,
        },
        "volatility": {
            "atr": _number(row, "atr") or 0.0,
            "spread_cost": _number(row, "spread_cost") or 0.0,
            "spread_cost_atr": _number(row, "spread_cost_atr") or 0.0,
            "spread_ratio_20": _number(row, "spread_ratio_20") or 0.0,
            "spread_expansion_points": (
                None if historical_rates_only else _number(row, "spread_expansion_points") or 0.0
            ),
        },
        "confirmation": {
            "fvg": _text(row, "fvg_direction"),
            "fvg_size_atr": _number(row, "fvg_size_atr") or 0.0,
            "signal_reasons": _text(row, "signal_reasons"),
            "action": action,
        },
        "session": {"name": _text(row, "session") or "UNKNOWN"},
        "execution": {
            "point": _number(row, "point"),
            "spread_mean_points": _number(row, "spread_mean_points") or 0.0,
            "spread_max_points": _number(row, "spread_max_points") or 0.0,
        },
        "portfolio": {"correlation_feed_status": "NOT_CONNECTED"},
        "custom": {
            "labels": [value for value in _text(row, "labels").split("|") if value],
            "observation_id": _text(row, "observation_id"),
        },
    }
    if historical_rates_only:
        features["custom"].update(
            {
                "source_capability": "MT5_HISTORICAL_RATES_ONLY",
                "unavailable_broker_features": [
                    "bid_up", "bid_down", "ask_up", "ask_down", "mid_up", "mid_down",
                    "direction_imbalance", "delta_proxy", "spread_min_points",
                    "spread_max_points", "spread_last_points", "spread_expansion_points",
                ],
                "unavailable_factor_contribution": 0.0,
            }
        )
    return features


def _setup_family(row: Mapping[str, Any], action: str, geometry: Mapping[str, float]) -> str:
    aligned_sweep = _truthy(row, "ssl_sweep") if action == "BUY" else _truthy(row, "bsl_sweep")
    fvg_aligned = _aligned(action, _text(row, "fvg_direction"))
    retracement = geometry["current_retracement"]
    if aligned_sweep and fvg_aligned and 0.618 <= retracement <= 0.790:
        return "SMC_OTE_LIQUIDITY_REVERSAL"
    if fvg_aligned and (
        _aligned(action, _text(row, "swing_bias"))
        or _aligned(action, _text(row, "internal_bias"))
    ):
        return "SMC_FVG_CONTINUATION"
    return "MULTIFACTOR_MARKET_SETUP"


def build_candidate(
    row: Mapping[str, Any],
    *,
    provenance: tuple[str, ...] | None = None,
) -> SignalCandidate:
    action = _text(row, "action").upper()
    if action not in VALID_ACTIONS:
        raise ValueError("observation action is not BUY/SELL")
    signal_time = _parse_time(_text(row, "signal_time"))
    plan, geometry = _build_plan(row, action)
    scores, reasons = _factor_scores(row, action, geometry)
    features = _market_features(row, action, geometry)
    setup_family = _setup_family(row, action, geometry)
    scenario = "; ".join(
        value
        for value in (
            _text(row, "signal_reasons"),
            _text(row, "labels").replace("|", ", "),
        )
        if value
    ) or setup_family
    return SignalCandidate(
        observed_at=signal_time,
        created_at=signal_time,
        symbol=_text(row, "symbol"),
        timeframe=_text(row, "timeframe") or "M5",
        setup_family=setup_family,
        scenario=scenario,
        plan=plan,
        market_features=features,
        factor_scores=scores,
        factor_reasons=reasons,
        provenance=provenance or (
            "FX_RESEARCH_V1_4_2",
            "SMC_STRUCTURE_ENGINE",
            "TICK_VOLUME_COLLECTOR",
            f"FX_SIGNAL_ADAPTER_{ADAPTER_VERSION}",
        ),
        generated_from_market_data=True,
        robot_context_only={},
    )


def load_observations(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        raise ValueError(f"observation CSV not found: {path}")
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [
            {key: str(value or "").strip() for key, value in dict(row).items()}
            for row in csv.DictReader(handle)
        ]


def _atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def build_candidates(
    rows: Sequence[Mapping[str, Any]],
) -> tuple[list[SignalCandidate], list[dict[str, str]]]:
    candidates: dict[str, SignalCandidate] = {}
    errors: list[dict[str, str]] = []
    for index, row in enumerate(rows, start=1):
        try:
            candidate = build_candidate(row)
        except ValueError as exc:
            errors.append(
                {
                    "row": str(index),
                    "observation_id": _text(row, "observation_id"),
                    "reason": str(exc),
                }
            )
            continue
        candidates[candidate.signal_id] = candidate
    return sorted(candidates.values(), key=lambda item: item.observed_at), errors


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Adapt TradeMind FX research observations into v1.16 shadow candidates"
    )
    parser.add_argument(
        "--observations",
        type=Path,
        default=Path("data/fx_research_v1_4_2/observations.csv"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("data/signal_intelligence_v1_16/candidates.jsonl"),
    )
    parser.add_argument("--errors", type=Path)
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args(argv)
    try:
        rows = load_observations(args.observations.expanduser().resolve())
        if args.limit > 0:
            rows = rows[-args.limit :]
        candidates, errors = build_candidates(rows)
        output_lines = []
        for candidate in candidates:
            payload = {
                **candidate.as_dict(),
                "similarity_key": similarity_key(candidate),
                "similarity_dimensions": similarity_dimensions(candidate),
                "shadow_only": True,
                "orders_enabled": False,
                "publication_enabled": False,
            }
            output_lines.append(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        _atomic_text(args.output, "\n".join(output_lines) + ("\n" if output_lines else ""))
        error_path = args.errors or args.output.with_name("candidate_errors.json")
        _atomic_text(
            error_path,
            json.dumps(errors, ensure_ascii=False, indent=2, sort_keys=True),
        )
    except (OSError, ValueError, csv.Error) as exc:
        print(f"FX signal adapter failed: {exc}")
        return 1

    print("TradeMind v1.16 FX Signal Adapter")
    print("Shadow candidates only. Orders OFF. Publication OFF.")
    print(f"Source rows: {len(rows)}")
    print(f"Candidates: {len(candidates)}")
    print(f"Rejected rows: {len(errors)}")
    print(f"Output: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
