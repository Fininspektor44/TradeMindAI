"""TradeMind v1.25 crypto adapter enriched with native market structure.

The module wraps the v1.24 local Bybit adapter, adds point-in-time structure
snapshots and writes a separate evidence namespace. It reads local CSV files
only and never calls Bybit, publishes signals or sends orders.
"""

from __future__ import annotations

import argparse
import csv
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from trademind import crypto_signal_adapter as base
from trademind.crypto_market_structure import MarketStructureEngine
from trademind.signal_evidence import similarity_dimensions, similarity_key
from trademind.signal_intelligence import SignalCandidate, candidate_from_dict

VERSION = "1.25.0"
STRUCTURE_SETUP_FAMILY = "CRYPTO_MTF_STRUCTURE_FLOW"


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _sequence(value: Any) -> list[Any]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return list(value)
    return []


def _blend(original: Any, native: Any, native_weight: float) -> float:
    return max(
        0.0,
        min(
            1.0,
            (1.0 - native_weight) * base._number({"value": original}, "value")
            + native_weight * base._number({"value": native}, "value"),
        ),
    )


def _structure_market(
    original: Mapping[str, Any],
    snapshot: Mapping[str, Any],
) -> dict[str, Any]:
    market = {key: _mapping(value) for key, value in original.items()}
    timeframes = _mapping(snapshot.get("timeframes"))
    h1 = _mapping(timeframes.get("H1"))
    m15 = _mapping(timeframes.get("M15"))
    liquidity = _mapping(snapshot.get("liquidity"))
    fvg = _mapping(snapshot.get("fvg"))
    fibonacci = _mapping(snapshot.get("fibonacci"))
    volatility = _mapping(snapshot.get("volatility"))

    structure = _mapping(market.get("structure"))
    structure.update(
        {
            "swing_bias": h1.get("bias", structure.get("swing_bias", "NEUTRAL")),
            "internal_bias": m15.get(
                "bias",
                structure.get("internal_bias", "NEUTRAL"),
            ),
            "swing_break": h1.get("break", structure.get("swing_break", "NONE")),
            "internal_break": m15.get(
                "break",
                structure.get("internal_break", "NONE"),
            ),
            "swing_break_level": h1.get("break_level"),
            "internal_break_level": m15.get("break_level"),
            "last_swing_high": h1.get("last_swing_high"),
            "last_swing_low": h1.get("last_swing_low"),
        }
    )
    market["structure"] = structure

    liquidity_market = _mapping(market.get("liquidity"))
    liquidity_market.update(
        {
            "ssl_sweep": bool(liquidity.get("ssl_sweep")),
            "bsl_sweep": bool(liquidity.get("bsl_sweep")),
            "sweep_type": liquidity.get("sweep_type", "NONE"),
            "sweep_level": liquidity.get("sweep_level"),
            "sweep_depth_atr": liquidity.get("sweep_depth_atr", 0.0),
            "fvg": fvg.get("type", "NONE"),
            "fvg_lower": fvg.get("lower"),
            "fvg_upper": fvg.get("upper"),
            "fvg_size_atr": fvg.get("size_atr", 0.0),
        }
    )
    market["liquidity"] = liquidity_market

    fibonacci_market = _mapping(market.get("fibonacci"))
    fibonacci_market.update(
        {
            "retracement": fibonacci.get("retracement"),
            "ote_low": fibonacci.get("ote_low", 0.618),
            "ote_mid": fibonacci.get("ote_mid", 0.705),
            "ote_high": fibonacci.get("ote_high", 0.790),
            "ote_hit": bool(fibonacci.get("ote_hit")),
            "level_618": fibonacci.get("level_618"),
            "level_705": fibonacci.get("level_705"),
            "level_790": fibonacci.get("level_790"),
            "impulse_start": fibonacci.get("impulse_start"),
            "impulse_end": fibonacci.get("impulse_end"),
            "source_status": "NATIVE_BYBIT_M5_RESAMPLE",
        }
    )
    market["fibonacci"] = fibonacci_market

    volatility_market = _mapping(market.get("volatility"))
    volatility_market.update(
        {
            "atr": volatility.get("atr_m5"),
            "atr_m5": volatility.get("atr_m5"),
            "atr_m15": volatility.get("atr_m15"),
            "atr_h1": volatility.get("atr_h1"),
        }
    )
    market["volatility"] = volatility_market

    confirmation = _mapping(market.get("confirmation"))
    confirmation.update(
        {
            "fvg": fvg.get("type", "NONE"),
            "native_structure_state": snapshot.get("state"),
            "future_bars_used": False,
        }
    )
    market["confirmation"] = confirmation

    custom = _mapping(market.get("custom"))
    custom.update(
        {
            "structure_engine_version": VERSION,
            "structure_state": snapshot.get("state"),
            "structure_as_of": snapshot.get("as_of"),
            "structure_bar_counts": snapshot.get("bar_counts"),
        }
    )
    market["custom"] = custom
    return market


def build_candidate(
    row: Mapping[str, Any],
    snapshot: Mapping[str, Any] | None = None,
) -> SignalCandidate:
    original = base.build_candidate(row)
    if not snapshot or snapshot.get("state") not in {"OK", "DEGRADED"}:
        return original

    payload = original.as_dict()
    payload["setup_family"] = STRUCTURE_SETUP_FAMILY
    payload["scenario"] = (
        "Крипто: нативная H1/M15/M5 структура, ликвидность, FVG, OTE и поток Bybit"
    )
    payload["market_features"] = _structure_market(
        _mapping(payload.get("market_features")),
        snapshot,
    )

    scores = _mapping(payload.get("factor_scores"))
    native_scores = _mapping(snapshot.get("factor_scores"))
    scores["structure"] = _blend(scores.get("structure"), native_scores.get("structure"), 0.70)
    scores["liquidity"] = _blend(scores.get("liquidity"), native_scores.get("liquidity"), 0.65)
    scores["fibonacci"] = _blend(scores.get("fibonacci"), native_scores.get("fibonacci"), 1.00)
    scores["confirmation"] = _blend(
        scores.get("confirmation"),
        native_scores.get("confirmation"),
        0.65,
    )
    payload["factor_scores"] = scores

    reasons = {
        key: tuple(str(item) for item in _sequence(value))
        for key, value in _mapping(payload.get("factor_reasons")).items()
    }
    native_reasons = _mapping(snapshot.get("factor_reasons"))
    for key in ("structure", "liquidity", "fibonacci", "confirmation"):
        reasons[key] = (
            *reasons.get(key, ()),
            *(str(item) for item in _sequence(native_reasons.get(key))),
        )
    payload["factor_reasons"] = reasons
    return candidate_from_dict(payload)


def _build_candidates(
    rows: Sequence[Mapping[str, Any]],
    engine: MarketStructureEngine,
) -> tuple[
    list[tuple[str, SignalCandidate, Mapping[str, Any], Mapping[str, Any]]],
    list[dict[str, str]],
]:
    candidates: dict[
        str,
        tuple[str, SignalCandidate, Mapping[str, Any], Mapping[str, Any]],
    ] = {}
    errors: list[dict[str, str]] = []
    for index, row in enumerate(rows, start=1):
        source_id = base._text(row, "decision_id")
        try:
            action = base._text(row, "action").upper()
            signal_time = base._parse_time(base._text(row, "signal_time"))
            snapshot = engine.snapshot(
                base._text(row, "symbol").upper(),
                signal_time,
                action,
            )
            candidate = build_candidate(row, snapshot)
        except (OSError, TypeError, ValueError) as exc:
            errors.append(
                {
                    "row": str(index),
                    "source_decision_id": source_id,
                    "reason": str(exc),
                }
            )
            continue
        candidates[candidate.signal_id] = (source_id, candidate, row, snapshot)
    ordered = sorted(candidates.values(), key=lambda item: item[1].observed_at)
    return ordered, errors


@dataclass(frozen=True, slots=True)
class AdapterRun:
    candidates: int
    outcomes: int
    rejected_rows: int
    structure_ok: int
    structure_degraded: int
    output_dir: Path


def run_adapter(
    decisions_path: Path,
    signals_path: Path,
    bars_path: Path,
    output_dir: Path,
    *,
    cost_r: float = 0.04,
    limit: int = 0,
    now: datetime | None = None,
) -> AdapterRun:
    if cost_r < 0:
        raise ValueError("cost_r cannot be negative")
    if limit < 0:
        raise ValueError("limit cannot be negative")

    decisions = base._read_csv(decisions_path)
    signals = base._read_csv(signals_path)
    if limit > 0:
        decisions = decisions[-limit:]
    engine = MarketStructureEngine.from_csv(bars_path)
    built, candidate_errors = _build_candidates(decisions, engine)
    by_source_id = {
        source_id: candidate
        for source_id, candidate, _, _ in built
        if source_id
    }
    outcomes, outcome_errors = base.build_outcomes(
        signals,
        by_source_id,
        cost_r=cost_r,
    )

    candidate_lines: list[str] = []
    structure_lines: list[str] = []
    structure_ok = 0
    structure_degraded = 0
    for source_id, candidate, source_row, snapshot in built:
        state = str(snapshot.get("state") or "")
        structure_ok += state == "OK"
        structure_degraded += state == "DEGRADED"
        payload = {
            **candidate.as_dict(),
            "similarity_key": similarity_key(candidate),
            "similarity_dimensions": similarity_dimensions(candidate),
            "asset_class": "CRYPTO",
            "venue": "BYBIT",
            "source_decision_id": source_id,
            "source_gate_status": base._text(source_row, "gate_status"),
            "source_quality_score": base._number(source_row, "quality_score"),
            "structure_state": state,
            "shadow_only": True,
            "orders_enabled": False,
            "publication_enabled": False,
        }
        candidate_lines.append(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        structure_lines.append(
            json.dumps(
                {
                    "source_decision_id": source_id,
                    "signal_id": candidate.signal_id,
                    **dict(snapshot),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )

    outcome_lines = [
        json.dumps(item.as_dict(), ensure_ascii=False, sort_keys=True)
        for item in outcomes
    ]
    root = output_dir.expanduser().resolve()
    base._atomic_text(
        root / "candidates.jsonl",
        "\n".join(candidate_lines) + ("\n" if candidate_lines else ""),
    )
    base._atomic_text(
        root / "outcomes.jsonl",
        "\n".join(outcome_lines) + ("\n" if outcome_lines else ""),
    )
    base._atomic_text(
        root / "structure_snapshots.jsonl",
        "\n".join(structure_lines) + ("\n" if structure_lines else ""),
    )
    errors = [*candidate_errors, *outcome_errors]
    base._atomic_json(root / "errors.json", {"errors": errors})
    captured_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    base._atomic_json(
        root / "status.json",
        {
            "schema_version": VERSION,
            "state": "OK",
            "updated_at": captured_at.isoformat(),
            "asset_class": "CRYPTO",
            "venue": "BYBIT",
            "setup_family": STRUCTURE_SETUP_FAMILY,
            "decisions_source": str(decisions_path),
            "signals_source": str(signals_path),
            "bars_source": str(bars_path),
            "symbols_with_bars": len(engine.symbols()),
            "candidates": len(built),
            "outcomes": len(outcomes),
            "rejected_rows": len(errors),
            "structure_ok": structure_ok,
            "structure_degraded": structure_degraded,
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
    return AdapterRun(
        candidates=len(built),
        outcomes=len(outcomes),
        rejected_rows=len(errors),
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
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="TradeMind v1.25 Crypto Market Structure Adapter"
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
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args(argv)
    try:
        result = run_adapter(
            args.decisions.expanduser().resolve(),
            args.signals.expanduser().resolve(),
            args.bars.expanduser().resolve(),
            args.output_dir,
            cost_r=args.cost_r,
            limit=args.limit,
        )
    except (OSError, ValueError, TypeError, csv.Error, json.JSONDecodeError) as exc:
        print(f"Crypto market structure adapter failed: {exc}")
        return 1

    print("TradeMind v1.25 Crypto Market Structure Core")
    print("Closed Bybit M5 -> M15/H1 -> BOS/CHoCH -> sweep/FVG -> OTE.")
    print("Read-only. Orders OFF. Publication OFF. Exchange API not called.")
    print(f"Candidates: {result.candidates}")
    print(f"Outcomes: {result.outcomes}")
    print(f"Structure OK/degraded: {result.structure_ok}/{result.structure_degraded}")
    print(f"Rejected rows: {result.rejected_rows}")
    print(f"Output: {result.output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
