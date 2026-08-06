"""TradeMind v1.24 read-only Bybit crypto signal adapter.

The adapter converts the existing forward-only Bybit shadow decisions into the
same immutable SignalCandidate contract used by the FX Passport Factory. It
also converts completed paper outcomes into evidence observations. It reads
local CSV files only, does not call Bybit or a broker, and never sends orders.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from trademind.signal_evidence import OutcomeObservation, similarity_dimensions, similarity_key
from trademind.signal_intelligence import EntryOrder, SignalCandidate, TradePlan

VERSION = "1.24.0"
SOURCE_ID = "BYBIT_LINEAR_SHADOW"
SETUP_FAMILY = "CRYPTO_MTF_FLOW_ALIGNMENT"
VALID_ACTIONS = {"BUY", "SELL"}


def _text(row: Mapping[str, Any], key: str, default: str = "") -> str:
    return str(row.get(key, default) or default).strip()


def _number(row: Mapping[str, Any], key: str, default: float = 0.0) -> float:
    try:
        value = float(str(row.get(key, default) or default).replace(",", "."))
    except (TypeError, ValueError):
        return default
    return value if math.isfinite(value) else default


def _integer(row: Mapping[str, Any], key: str, default: int = 0) -> int:
    try:
        return int(float(str(row.get(key, default) or default).replace(",", ".")))
    except (TypeError, ValueError):
        return default


def _clamp(value: float, lower: float = 0.0, upper: float = 1.0) -> float:
    return max(lower, min(upper, value))


def _parse_time(value: Any) -> datetime:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("signal_time is required")
    parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("signal_time must include timezone information")
    return parsed.astimezone(timezone.utc)


def _aligned(action: str, value: float, epsilon: float = 0.0) -> bool:
    return value > epsilon if action == "BUY" else value < -epsilon


def _bias(value: float) -> str:
    if value > 0:
        return "BULLISH"
    if value < 0:
        return "BEARISH"
    return "NEUTRAL"


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        raise ValueError(f"CSV source not found: {path}")
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [
            {str(key): str(value or "").strip() for key, value in dict(row).items()}
            for row in csv.DictReader(handle)
        ]


def _atomic_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    _atomic_text(
        path,
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )


def _build_plan(row: Mapping[str, Any], action: str) -> TradePlan:
    entry = _number(row, "entry_price")
    stop = _number(row, "stop_price")
    target = _number(row, "target_price")
    if min(entry, stop, target) <= 0:
        raise ValueError("entry, stop and target must be positive")
    if action == "BUY" and not stop < entry < target:
        raise ValueError("BUY geometry is invalid")
    if action == "SELL" and not target < entry < stop:
        raise ValueError("SELL geometry is invalid")

    risk = abs(entry - stop)
    second_target = entry + (2.0 * risk if action == "BUY" else -2.0 * risk)
    targets = (target, second_target)
    return TradePlan(
        action=action,
        entries=(
            EntryOrder(
                price=entry,
                allocation=1.0,
                rationale="M5 триггер после согласования H1 и M15 потоков Bybit",
                order_type="MARKET",
            ),
        ),
        stop_price=stop,
        targets=targets,
        invalidation="Сценарий отменяется при пробое защитного стопа MTF-сетапа",
        target_rationale=("Первая цель 1,5R", "Вторая цель 2R"),
    )


def _factor_scores(
    row: Mapping[str, Any], action: str
) -> tuple[dict[str, float], dict[str, tuple[str, ...]]]:
    h1_return = _number(row, "h1_return_pct")
    m15_return = _number(row, "m15_return_pct")
    h1_delta = _number(row, "h1_delta_turnover")
    m15_delta = _number(row, "m15_delta_turnover")
    m5_delta = _number(row, "m5_delta_turnover")
    m15_book = _number(row, "m15_book_imbalance_10")
    m5_book = _number(row, "m5_book_imbalance_10")
    m5_spread = max(0.0, _number(row, "m5_spread_bps"))
    source_quality = _clamp(_number(row, "quality_score") / 100.0)
    trade_count = max(0, _integer(row, "m5_trade_count"))

    structure = 0.55 * _aligned(action, h1_return) + 0.45 * _aligned(action, m15_return)
    liquidity = 0.50 * _aligned(action, m15_book, 0.03) + 0.50 * _aligned(
        action, m5_book, 0.03
    )
    volume = (
        0.40 * _aligned(action, m15_delta)
        + 0.45 * _aligned(action, m5_delta)
        + 0.15 * (trade_count > 0)
    )
    momentum = (
        0.40 * _aligned(action, h1_return)
        + 0.30 * _aligned(action, m15_return)
        + 0.30 * source_quality
    )
    spread_score = _clamp(1.0 - m5_spread / 10.0)
    confirmation = 0.50 * _aligned(action, m15_delta) + 0.50 * _aligned(
        action, m5_delta
    )

    scores = {
        "structure": _clamp(structure),
        "liquidity": _clamp(liquidity),
        "fibonacci": 0.0,
        "volume": _clamp(volume),
        "momentum": _clamp(momentum),
        "volatility": spread_score,
        "confirmation": _clamp(confirmation),
        "session": 0.70,
        "execution": spread_score,
        "portfolio": 0.50,
    }
    reasons = {
        "structure": (
            f"H1 уклон: {_bias(h1_return).lower()}",
            f"M15 уклон: {_bias(m15_return).lower()}",
        ),
        "liquidity": (
            f"Дисбаланс стакана M15: {m15_book:.3f}",
            f"Дисбаланс стакана M5: {m5_book:.3f}",
        ),
        "fibonacci": ("Крипто-источник пока не передаёт независимую OTE-геометрию",),
        "volume": (
            f"Delta M15: {m15_delta:.2f}",
            f"Delta M5: {m5_delta:.2f}",
        ),
        "momentum": (
            f"Доходность H1: {100.0 * h1_return:.3f}%",
            f"Исходная оценка Bybit: {100.0 * source_quality:.1f}",
        ),
        "volatility": (f"Спред M5: {m5_spread:.2f} б.п.",),
        "confirmation": (
            f"Поток M15 согласован: {int(_aligned(action, m15_delta))}",
            f"Поток M5 согласован: {int(_aligned(action, m5_delta))}",
        ),
        "session": ("Крипторынок работает 24/7",),
        "execution": (f"Спред M5: {m5_spread:.2f} б.п.",),
        "portfolio": ("Корреляционный риск криптопортфеля пока не подключён",),
    }
    return scores, reasons


def _market_features(row: Mapping[str, Any], action: str) -> dict[str, dict[str, Any]]:
    h1_return = _number(row, "h1_return_pct")
    m15_return = _number(row, "m15_return_pct")
    risk_pct = max(0.0, _number(row, "risk_pct"))
    spread_bps = max(0.0, _number(row, "m5_spread_bps"))
    spread_cost_atr = spread_bps / (risk_pct * 10_000.0) if risk_pct > 0 else 0.0
    return {
        "structure": {
            "swing_bias": _bias(h1_return),
            "internal_bias": _bias(m15_return),
            "swing_break": "NONE",
            "internal_break": "NONE",
        },
        "liquidity": {
            "ssl_sweep": False,
            "bsl_sweep": False,
            "ssl_sweep_depth_atr": 0.0,
            "bsl_sweep_depth_atr": 0.0,
        },
        "fibonacci": {
            "retracement": 0.0,
            "ote_low": 0.618,
            "ote_mid": 0.705,
            "ote_high": 0.790,
            "source_status": "NOT_AVAILABLE_FROM_BYBIT_SHADOW_V1_10",
        },
        "volume": {
            "rvol_20": 0.0,
            "volume_percentile_100": 0.0,
            "direction_imbalance": _number(row, "m5_book_imbalance_10"),
            "delta_proxy": _number(row, "m5_delta_turnover"),
            "tick_rate_ratio_20": 0.0,
            "m5_trade_count": _integer(row, "m5_trade_count"),
            "m15_delta_turnover": _number(row, "m15_delta_turnover"),
            "m5_delta_turnover": _number(row, "m5_delta_turnover"),
        },
        "momentum": {
            "h1_return_pct": h1_return,
            "m15_return_pct": m15_return,
            "ema_fast": None,
            "ema_slow": None,
            "rsi": None,
        },
        "volatility": {
            "atr": None,
            "risk_distance_pct": risk_pct,
            "spread_bps": spread_bps,
            "spread_cost_atr": spread_cost_atr,
        },
        "confirmation": {
            "fvg": "NONE",
            "action": action,
            "source_components": _text(row, "components"),
            "source_reasons": _text(row, "reasons"),
        },
        "session": {"name": "CRYPTO_24_7"},
        "execution": {
            "spread_bps": spread_bps,
            "funding_rate": _number(row, "m5_funding_rate"),
            "basis_bps": _number(row, "m5_basis_bps"),
        },
        "sentiment": {
            "funding_rate": _number(row, "m5_funding_rate"),
            "basis_bps": _number(row, "m5_basis_bps"),
            "h1_open_interest_change_pct": _number(row, "h1_oi_change_pct"),
            "m15_open_interest_change_pct": _number(row, "m15_oi_change_pct"),
        },
        "portfolio": {"correlation_feed_status": "NOT_CONNECTED"},
        "custom": {
            "asset_class": "CRYPTO",
            "venue": "BYBIT",
            "source_decision_id": _text(row, "decision_id"),
            "source_gate_status": _text(row, "gate_status"),
            "source_quality_score": _number(row, "quality_score"),
            "h1_delta_turnover": _number(row, "h1_delta_turnover"),
            "m15_book_imbalance_10": _number(row, "m15_book_imbalance_10"),
            "m5_book_imbalance_10": _number(row, "m5_book_imbalance_10"),
        },
    }


def build_candidate(row: Mapping[str, Any]) -> SignalCandidate:
    action = _text(row, "action").upper()
    if action not in VALID_ACTIONS:
        raise ValueError("decision action is not BUY/SELL")
    signal_time = _parse_time(_text(row, "signal_time"))
    plan = _build_plan(row, action)
    scores, reasons = _factor_scores(row, action)
    components = [item for item in _text(row, "components").split("|") if item]
    scenario = "Крипто: H1 контекст, M15 подтверждение и M5 триггер"
    if components:
        scenario += "; " + ", ".join(components)
    return SignalCandidate(
        observed_at=signal_time,
        created_at=signal_time,
        symbol=_text(row, "symbol").upper(),
        timeframe="M5",
        setup_family=SETUP_FAMILY,
        scenario=scenario,
        plan=plan,
        market_features=_market_features(row, action),
        factor_scores=scores,
        factor_reasons=reasons,
        provenance=(
            "BYBIT_PUBLIC_COLLECTOR_V1_9",
            "BYBIT_SHADOW_RESEARCH_V1_10",
            f"CRYPTO_SIGNAL_ADAPTER_{VERSION}",
        ),
        generated_from_market_data=True,
        robot_context_only={},
    )


def build_candidates(
    rows: Sequence[Mapping[str, Any]],
) -> tuple[list[tuple[str, SignalCandidate, Mapping[str, Any]]], list[dict[str, str]]]:
    candidates: dict[str, tuple[str, SignalCandidate, Mapping[str, Any]]] = {}
    errors: list[dict[str, str]] = []
    for index, row in enumerate(rows, start=1):
        try:
            candidate = build_candidate(row)
        except ValueError as exc:
            errors.append(
                {
                    "row": str(index),
                    "source_decision_id": _text(row, "decision_id"),
                    "reason": str(exc),
                }
            )
            continue
        candidates[candidate.signal_id] = (_text(row, "decision_id"), candidate, row)
    ordered = sorted(candidates.values(), key=lambda item: item[1].observed_at)
    return ordered, errors


def build_outcomes(
    signals: Sequence[Mapping[str, Any]],
    by_source_id: Mapping[str, SignalCandidate],
    *,
    cost_r: float,
) -> tuple[list[OutcomeObservation], list[dict[str, str]]]:
    outcomes: dict[str, OutcomeObservation] = {}
    errors: list[dict[str, str]] = []
    for index, row in enumerate(signals, start=1):
        if _integer(row, "completed") != 1:
            continue
        source_id = _text(row, "paper_signal_id")
        candidate = by_source_id.get(source_id)
        if candidate is None:
            errors.append(
                {
                    "row": str(index),
                    "source_decision_id": source_id,
                    "reason": "completed signal has no adapted candidate",
                }
            )
            continue
        raw_r = _number(row, "result_r")
        net_r = raw_r - max(0.0, cost_r)
        if net_r > 1e-12:
            outcome = "WIN"
        elif net_r < -1e-12:
            outcome = "LOSS"
        else:
            outcome = "FLAT"
            net_r = 0.0
        try:
            completed_at = _parse_time(_text(row, "updated_at") or _text(row, "signal_time"))
            observation = OutcomeObservation(
                signal_id=candidate.signal_id,
                setup_key=similarity_key(candidate),
                completed_at=completed_at,
                outcome=outcome,
                net_r=net_r,
            )
        except ValueError as exc:
            errors.append(
                {
                    "row": str(index),
                    "source_decision_id": source_id,
                    "reason": str(exc),
                }
            )
            continue
        outcomes[observation.signal_id] = observation
    return sorted(outcomes.values(), key=lambda item: item.completed_at), errors


@dataclass(frozen=True, slots=True)
class AdapterRun:
    candidates: int
    outcomes: int
    rejected_rows: int
    output_dir: Path


def run_adapter(
    decisions_path: Path,
    signals_path: Path,
    output_dir: Path,
    *,
    cost_r: float = 0.04,
    limit: int = 0,
    bars_path: Path | None = None,
    now: datetime | None = None,
) -> AdapterRun:
    if cost_r < 0:
        raise ValueError("cost_r cannot be negative")
    if limit < 0:
        raise ValueError("limit cannot be negative")
    decisions = _read_csv(decisions_path)
    signals = _read_csv(signals_path)
    if limit > 0:
        decisions = decisions[-limit:]

    built, candidate_errors = build_candidates(decisions)
    by_source_id = {source_id: candidate for source_id, candidate, _ in built if source_id}
    outcomes, outcome_errors = build_outcomes(signals, by_source_id, cost_r=cost_r)

    candidate_lines: list[str] = []
    for source_id, candidate, source_row in built:
        payload = {
            **candidate.as_dict(),
            "similarity_key": similarity_key(candidate),
            "similarity_dimensions": similarity_dimensions(candidate),
            "asset_class": "CRYPTO",
            "venue": "BYBIT",
            "source_decision_id": source_id,
            "source_gate_status": _text(source_row, "gate_status"),
            "source_quality_score": _number(source_row, "quality_score"),
            "shadow_only": True,
            "orders_enabled": False,
            "publication_enabled": False,
        }
        candidate_lines.append(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    outcome_lines = [
        json.dumps(item.as_dict(), ensure_ascii=False, sort_keys=True) for item in outcomes
    ]

    root = output_dir.expanduser().resolve()
    _atomic_text(
        root / "candidates.jsonl",
        "\n".join(candidate_lines) + ("\n" if candidate_lines else ""),
    )
    _atomic_text(
        root / "outcomes.jsonl",
        "\n".join(outcome_lines) + ("\n" if outcome_lines else ""),
    )
    errors = [*candidate_errors, *outcome_errors]
    _atomic_json(root / "errors.json", {"errors": errors})
    captured_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    _atomic_json(
        root / "status.json",
        {
            "schema_version": VERSION,
            "state": "OK",
            "updated_at": captured_at.isoformat(),
            "asset_class": "CRYPTO",
            "venue": "BYBIT",
            "decisions_source": str(decisions_path),
            "signals_source": str(signals_path),
            "bars_source": str(bars_path) if bars_path else "",
            "candidates": len(built),
            "outcomes": len(outcomes),
            "rejected_rows": len(errors),
            "safety": {
                "read_only": True,
                "orders_enabled": False,
                "publication_enabled": False,
                "broker_api_called": False,
                "source_files_modified": False,
            },
        },
    )
    return AdapterRun(
        candidates=len(built),
        outcomes=len(outcomes),
        rejected_rows=len(errors),
        output_dir=root,
    )


def safety_contract() -> Mapping[str, Any]:
    return {
        "read_only": True,
        "orders_enabled": False,
        "publication_enabled": False,
        "broker_api_called": False,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="TradeMind v1.24 Crypto Signal Adapter")
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
        default=Path("data/crypto_signal_intelligence_v1_24"),
    )
    parser.add_argument("--cost-r", type=float, default=0.04)
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args(argv)
    try:
        result = run_adapter(
            args.decisions.expanduser().resolve(),
            args.signals.expanduser().resolve(),
            args.output_dir,
            cost_r=args.cost_r,
            limit=args.limit,
            bars_path=args.bars.expanduser().resolve(),
        )
    except (OSError, ValueError, TypeError, csv.Error, json.JSONDecodeError) as exc:
        print(f"Crypto signal adapter failed: {exc}")
        return 1

    print("TradeMind v1.24 Crypto Signal Adapter")
    print("Bybit shadow decisions -> immutable candidates -> evidence outcomes.")
    print("Read-only. Orders OFF. Publication OFF. Broker API not called.")
    print(f"Candidates: {result.candidates}")
    print(f"Outcomes: {result.outcomes}")
    print(f"Rejected rows: {result.rejected_rows}")
    print(f"Output: {result.output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
