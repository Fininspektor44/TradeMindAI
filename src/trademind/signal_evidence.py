"""Versioned similarity keys and historical evidence aggregation for TradeMind.

The module groups completed shadow candidates by market context. It does not
create market setups and does not publish signals. Similarity definitions are
versioned so historical statistics cannot silently change when the feature
bucketing logic evolves.
"""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from trademind.signal_intelligence import (
    HistoricalEvidence,
    SignalCandidate,
    candidate_from_dict,
)

SIMILARITY_VERSION = "SIM_V1"
VALID_OUTCOMES = {"WIN", "LOSS", "FLAT"}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _parse_time(value: Any) -> datetime:
    text = _text(value)
    if not text:
        raise ValueError("completed_at is required")
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("completed_at must include timezone information")
    return parsed.astimezone(timezone.utc)


def _feature(
    candidate: SignalCandidate,
    group: str,
    *names: str,
    default: Any = "",
) -> Any:
    values = candidate.market_features.get(group, {})
    for name in names:
        value = values.get(name)
        if value is not None and _text(value):
            return value
    return default


def _bucket(value: float, boundaries: Sequence[tuple[float, str]], fallback: str) -> str:
    for upper, label in boundaries:
        if value < upper:
            return label
    return fallback


def _boolean_tag(value: Any, true_label: str, false_label: str = "NONE") -> str:
    if isinstance(value, bool):
        return true_label if value else false_label
    return true_label if _text(value).lower() in {"1", "true", "yes", "y"} else false_label


def _fibonacci_zone(candidate: SignalCandidate) -> str:
    raw = _feature(
        candidate,
        "fibonacci",
        "retracement",
        "entry_retracement",
        "ote_ratio",
        default=0.0,
    )
    value = _float(raw)
    if value <= 0:
        ratios = [
            _float(item)
            for key, item in candidate.market_features.get("fibonacci", {}).items()
            if key.startswith("entry_")
        ]
        value = sum(ratios) / len(ratios) if ratios else 0.0
    return _bucket(
        value,
        (
            (0.50, "SHALLOW"),
            (0.618, "DISCOUNT_50_618"),
            (0.705, "OTE_618_705"),
            (0.79, "OTE_705_790"),
            (0.89, "DEEP_790_890"),
        ),
        "EXTREME_OR_UNKNOWN",
    )


def _liquidity_tag(candidate: SignalCandidate) -> str:
    values = candidate.market_features.get("liquidity", {})
    bsl = _boolean_tag(values.get("bsl_sweep"), "BSL")
    ssl = _boolean_tag(values.get("ssl_sweep"), "SSL")
    if bsl != "NONE" and ssl != "NONE":
        return "BOTH"
    if bsl != "NONE":
        return bsl
    if ssl != "NONE":
        return ssl
    named = _text(values.get("sweep")) or _text(values.get("sweep_side"))
    return named.upper() or "NONE"


def similarity_dimensions(candidate: SignalCandidate) -> dict[str, str]:
    """Return the frozen SIM_V1 dimensions used to group comparable setups."""
    structure = candidate.market_features.get("structure", {})
    volume = candidate.market_features.get("volume", {})
    momentum = candidate.market_features.get("momentum", {})
    volatility = candidate.market_features.get("volatility", {})
    confirmation = candidate.market_features.get("confirmation", {})
    session = candidate.market_features.get("session", {})

    rvol = _float(volume.get("rvol_20", volume.get("relative_volume", 0.0)))
    impulse = _float(momentum.get("impulse_atr", momentum.get("displacement_atr", 0.0)))
    spread_cost_atr = _float(volatility.get("spread_cost_atr", 0.0))
    atr_percentile = _float(volatility.get("atr_percentile", 50.0), 50.0)

    return {
        "version": SIMILARITY_VERSION,
        "symbol": candidate.symbol,
        "timeframe": candidate.timeframe,
        "action": candidate.plan.action,
        "setup_family": candidate.setup_family,
        "swing_bias": _text(structure.get("swing_bias")).upper() or "UNKNOWN",
        "internal_bias": _text(structure.get("internal_bias")).upper() or "UNKNOWN",
        "liquidity": _liquidity_tag(candidate),
        "fibonacci": _fibonacci_zone(candidate),
        "fvg": _text(confirmation.get("fvg", confirmation.get("fvg_direction", ""))).upper()
        or "NONE",
        "volume_regime": _bucket(
            rvol,
            ((0.80, "LOW"), (1.20, "NORMAL"), (1.80, "HIGH")),
            "EXTREME",
        ),
        "momentum_regime": _bucket(
            impulse,
            ((0.50, "WEAK"), (1.00, "NORMAL"), (1.50, "STRONG")),
            "EXPLOSIVE",
        ),
        "volatility_regime": _bucket(
            atr_percentile,
            ((25.0, "LOW"), (75.0, "NORMAL"), (90.0, "HIGH")),
            "EXTREME",
        ),
        "cost_regime": _bucket(
            spread_cost_atr,
            ((0.03, "CHEAP"), (0.08, "NORMAL"), (0.15, "EXPENSIVE")),
            "PROHIBITIVE",
        ),
        "session": _text(session.get("name", session.get("session", ""))).upper()
        or "UNKNOWN",
    }


def similarity_key(candidate: SignalCandidate) -> str:
    dimensions = similarity_dimensions(candidate)
    ordered = (
        "version",
        "symbol",
        "timeframe",
        "action",
        "setup_family",
        "swing_bias",
        "internal_bias",
        "liquidity",
        "fibonacci",
        "fvg",
        "volume_regime",
        "momentum_regime",
        "volatility_regime",
        "cost_regime",
        "session",
    )
    return "|".join(f"{name}={dimensions[name]}" for name in ordered)


@dataclass(frozen=True, slots=True)
class OutcomeObservation:
    signal_id: str
    setup_key: str
    completed_at: datetime
    outcome: str
    net_r: float

    def __post_init__(self) -> None:
        outcome = self.outcome.upper()
        object.__setattr__(self, "outcome", outcome)
        if not _text(self.signal_id) or not _text(self.setup_key):
            raise ValueError("signal_id and setup_key are required")
        if self.completed_at.tzinfo is None:
            raise ValueError("completed_at must include timezone information")
        if outcome not in VALID_OUTCOMES:
            raise ValueError(f"unsupported outcome: {outcome}")
        if not math.isfinite(self.net_r):
            raise ValueError("net_r must be finite")
        if outcome == "WIN" and self.net_r <= 0:
            raise ValueError("WIN must have positive net_r")
        if outcome == "LOSS" and self.net_r >= 0:
            raise ValueError("LOSS must have negative net_r")

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> OutcomeObservation:
        return cls(
            signal_id=_text(payload.get("signal_id")),
            setup_key=_text(payload.get("setup_key")),
            completed_at=_parse_time(payload.get("completed_at")),
            outcome=_text(payload.get("outcome")),
            net_r=_float(payload.get("net_r")),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "signal_id": self.signal_id,
            "setup_key": self.setup_key,
            "completed_at": self.completed_at.astimezone(timezone.utc).isoformat(),
            "outcome": self.outcome,
            "net_r": self.net_r,
        }


def _win_rate(rows: Sequence[OutcomeObservation]) -> float:
    return sum(row.outcome == "WIN" for row in rows) / len(rows) if rows else 0.0


def _maximum_drawdown_r(rows: Sequence[OutcomeObservation]) -> float:
    equity = 0.0
    peak = 0.0
    maximum = 0.0
    for row in sorted(rows, key=lambda item: item.completed_at):
        equity += row.net_r
        peak = max(peak, equity)
        maximum = max(maximum, peak - equity)
    return maximum


def aggregate_evidence(
    candidate: SignalCandidate,
    observations: Sequence[OutcomeObservation],
    *,
    captured_at: datetime | None = None,
    recent_window: int = 30,
) -> HistoricalEvidence:
    key = similarity_key(candidate)
    matching = sorted(
        (row for row in observations if row.setup_key == key),
        key=lambda item: item.completed_at,
    )
    wins = [row for row in matching if row.outcome == "WIN"]
    losses = [row for row in matching if row.outcome == "LOSS"]
    flats = [row for row in matching if row.outcome == "FLAT"]
    gross_win = sum(row.net_r for row in wins)
    gross_loss = sum(abs(row.net_r) for row in losses)
    average_win = gross_win / len(wins) if wins else 0.0
    average_loss = -gross_loss / len(losses) if losses else -1.0
    recent = matching[-max(1, recent_window) :]
    return HistoricalEvidence(
        setup_key=key,
        captured_at=(captured_at or datetime.now(timezone.utc)).astimezone(timezone.utc),
        wins=len(wins),
        losses=len(losses),
        flats=len(flats),
        gross_win_r=gross_win,
        gross_loss_r=gross_loss,
        average_win_r=average_win,
        average_loss_r=average_loss,
        max_drawdown_r=_maximum_drawdown_r(matching),
        recent_win_rate=_win_rate(recent) if matching else None,
        baseline_win_rate=_win_rate(matching) if matching else None,
    )


def load_outcomes(path: Path) -> list[OutcomeObservation]:
    if not path.is_file():
        return []
    rows: list[OutcomeObservation] = []
    seen: set[str] = set()
    for line_number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = OutcomeObservation.from_dict(json.loads(line))
        except (ValueError, json.JSONDecodeError) as exc:
            raise ValueError(f"line {line_number}: {exc}") from exc
        if row.signal_id in seen:
            raise ValueError(f"duplicate outcome signal_id: {row.signal_id}")
        seen.add(row.signal_id)
        rows.append(row)
    return rows


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    temporary.replace(path)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="TradeMind v1.16 versioned similarity and evidence aggregator"
    )
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--outcomes", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--recent-window", type=int, default=30)
    args = parser.parse_args(argv)
    try:
        candidate = candidate_from_dict(
            json.loads(args.candidate.read_text(encoding="utf-8-sig"))
        )
        outcomes = load_outcomes(args.outcomes)
        evidence = aggregate_evidence(
            candidate,
            outcomes,
            recent_window=max(1, args.recent_window),
        )
        payload = {
            "similarity_version": SIMILARITY_VERSION,
            "similarity_dimensions": similarity_dimensions(candidate),
            **evidence.as_dict(),
        }
        _atomic_json(args.output, payload)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"Signal evidence aggregation failed: {exc}")
        return 1

    print("TradeMind v1.16 Signal Evidence")
    print("Read-only research. Orders OFF. Publication OFF.")
    print(f"Setup key: {evidence.setup_key}")
    print(f"Completed: {evidence.completed}")
    print(
        "Reliability, raw/conservative: "
        f"{100 * evidence.raw_win_rate:.1f}%/"
        f"{100 * evidence.wilson_lower_95:.1f}%"
    )
    print(f"Output: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
