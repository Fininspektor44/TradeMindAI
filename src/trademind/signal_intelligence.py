"""TradeMind v1.16 signal-intelligence core.

This module is the boundary between research data and any future public signal.
It does not scan a broker, place orders, or publish to Telegram. It validates an
immutable market setup, calculates evidence-aware reliability and expected
value, and decides whether the setup must remain shadow-only or may proceed to
a publication adapter.

Grid and robot monitoring data may be stored as context, but it is never a
valid primary trigger for a market signal.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from statistics import NormalDist
from typing import Any, Mapping, Sequence

SCHEMA_VERSION = "1.16.0"
VALID_ACTIONS = {"BUY", "SELL"}
VALID_GROUPS = {
    "structure",
    "liquidity",
    "fibonacci",
    "volume",
    "momentum",
    "volatility",
    "confirmation",
    "session",
    "execution",
    "portfolio",
    "macro",
    "sentiment",
    "robot_monitoring",
    "custom",
}
MARKET_GROUPS = VALID_GROUPS - {"robot_monitoring"}

DEFAULT_FACTOR_WEIGHTS: dict[str, float] = {
    "structure": 0.18,
    "liquidity": 0.14,
    "fibonacci": 0.10,
    "volume": 0.12,
    "momentum": 0.10,
    "volatility": 0.10,
    "confirmation": 0.10,
    "session": 0.06,
    "execution": 0.06,
    "portfolio": 0.04,
}


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
        raise ValueError("timestamp is required")
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include timezone information")
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non-finite numeric feature")
        return value
    if isinstance(value, datetime):
        return _iso(value)
    if isinstance(value, Mapping):
        return {
            str(key): _json_safe(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    raise ValueError(f"unsupported JSON value: {type(value).__name__}")


def _canonical_json(value: Any) -> str:
    return json.dumps(
        _json_safe(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _clamp01(value: Any) -> float:
    number = _float(value)
    if number < 0 or number > 1:
        raise ValueError(f"factor score must be within 0..1, got {number}")
    return number


def wilson_lower_bound(wins: int, observations: int, confidence: float = 0.95) -> float:
    """Return the Wilson lower confidence bound for a Bernoulli win rate."""
    if observations < 0 or wins < 0 or wins > observations:
        raise ValueError("invalid wins/observations")
    if observations == 0:
        return 0.0
    if not 0 < confidence < 1:
        raise ValueError("confidence must be between 0 and 1")
    z = NormalDist().inv_cdf(0.5 + confidence / 2.0)
    n = float(observations)
    p = wins / n
    denominator = 1.0 + z * z / n
    centre = p + z * z / (2.0 * n)
    margin = z * math.sqrt((p * (1.0 - p) + z * z / (4.0 * n)) / n)
    return max(0.0, (centre - margin) / denominator)


@dataclass(frozen=True, slots=True)
class EntryOrder:
    price: float
    allocation: float
    rationale: str
    order_type: str = "LIMIT"

    def __post_init__(self) -> None:
        if self.price <= 0 or not math.isfinite(self.price):
            raise ValueError("entry price must be positive and finite")
        if self.allocation <= 0 or self.allocation > 1:
            raise ValueError("entry allocation must be within (0, 1]")
        if not _text(self.rationale):
            raise ValueError("entry rationale is required")
        if self.order_type not in {"MARKET", "LIMIT", "STOP"}:
            raise ValueError("unsupported order_type")

    def as_dict(self) -> dict[str, Any]:
        return {
            "price": self.price,
            "allocation": self.allocation,
            "rationale": self.rationale,
            "order_type": self.order_type,
        }


@dataclass(frozen=True, slots=True)
class TradePlan:
    action: str
    entries: tuple[EntryOrder, ...]
    stop_price: float
    targets: tuple[float, ...]
    invalidation: str
    target_rationale: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        action = self.action.upper()
        object.__setattr__(self, "action", action)
        if action not in VALID_ACTIONS:
            raise ValueError("action must be BUY or SELL")
        if not self.entries:
            raise ValueError("at least one entry is required")
        if len(self.entries) > 5:
            raise ValueError("at most five staged entries are supported")
        allocation = sum(item.allocation for item in self.entries)
        if allocation > 1.000001:
            raise ValueError("entry allocations exceed 100%")
        if self.stop_price <= 0 or not math.isfinite(self.stop_price):
            raise ValueError("stop_price must be positive and finite")
        if not self.targets:
            raise ValueError("at least one target is required")
        if any(value <= 0 or not math.isfinite(value) for value in self.targets):
            raise ValueError("target prices must be positive and finite")
        if not _text(self.invalidation):
            raise ValueError("invalidation explanation is required")

        average_entry = self.average_entry
        if action == "BUY":
            if not self.stop_price < min(item.price for item in self.entries):
                raise ValueError("BUY stop must be below every entry")
            if not all(target > average_entry for target in self.targets):
                raise ValueError("BUY targets must be above average entry")
        else:
            if not self.stop_price > max(item.price for item in self.entries):
                raise ValueError("SELL stop must be above every entry")
            if not all(target < average_entry for target in self.targets):
                raise ValueError("SELL targets must be below average entry")

    @property
    def average_entry(self) -> float:
        allocated = sum(item.allocation for item in self.entries)
        return sum(item.price * item.allocation for item in self.entries) / allocated

    @property
    def risk_distance(self) -> float:
        return abs(self.average_entry - self.stop_price)

    @property
    def first_target_rr(self) -> float:
        return abs(self.targets[0] - self.average_entry) / self.risk_distance

    @property
    def best_target_rr(self) -> float:
        return max(abs(target - self.average_entry) / self.risk_distance for target in self.targets)

    def as_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "entries": [item.as_dict() for item in self.entries],
            "average_entry": self.average_entry,
            "stop_price": self.stop_price,
            "targets": list(self.targets),
            "invalidation": self.invalidation,
            "target_rationale": list(self.target_rationale),
            "risk_distance": self.risk_distance,
            "first_target_rr": self.first_target_rr,
            "best_target_rr": self.best_target_rr,
        }


@dataclass(frozen=True, slots=True)
class SignalCandidate:
    observed_at: datetime
    created_at: datetime
    symbol: str
    timeframe: str
    setup_family: str
    scenario: str
    plan: TradePlan
    market_features: Mapping[str, Mapping[str, Any]]
    factor_scores: Mapping[str, float]
    factor_reasons: Mapping[str, Sequence[str]]
    provenance: tuple[str, ...]
    generated_from_market_data: bool = True
    robot_context_only: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.observed_at.tzinfo is None or self.created_at.tzinfo is None:
            raise ValueError("candidate timestamps must include timezone information")
        if self.created_at.astimezone(timezone.utc) < self.observed_at.astimezone(timezone.utc):
            raise ValueError("created_at cannot precede observed_at")
        symbol = self.symbol.upper().strip()
        timeframe = self.timeframe.upper().strip()
        if not symbol or not timeframe or not _text(self.setup_family) or not _text(self.scenario):
            raise ValueError("candidate identity fields are required")
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "timeframe", timeframe)
        if not self.provenance:
            raise ValueError("at least one provenance source is required")

        unknown_features = set(self.market_features) - VALID_GROUPS
        unknown_scores = set(self.factor_scores) - VALID_GROUPS
        if unknown_features or unknown_scores:
            unknown = sorted(unknown_features | unknown_scores)
            raise ValueError(f"unknown feature groups: {', '.join(unknown)}")
        for group, score in self.factor_scores.items():
            _clamp01(score)
            if group == "robot_monitoring" and _float(score) > 0:
                raise ValueError("robot_monitoring cannot contribute to signal score")
        _json_safe(self.market_features)
        _json_safe(self.robot_context_only)

    @property
    def immutable_payload(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "observed_at": _iso(self.observed_at),
            "created_at": _iso(self.created_at),
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "setup_family": self.setup_family,
            "scenario": self.scenario,
            "plan": self.plan.as_dict(),
            "market_features": self.market_features,
            "factor_scores": self.factor_scores,
            "factor_reasons": {
                key: list(value) for key, value in self.factor_reasons.items()
            },
            "provenance": list(self.provenance),
            "generated_from_market_data": self.generated_from_market_data,
            "robot_context_only": self.robot_context_only,
        }

    @property
    def signal_id(self) -> str:
        prefix = self.observed_at.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        return f"TM-{prefix}-{self.symbol}-{self.plan.action}-{_sha256(self.immutable_payload)[:16]}"

    def as_dict(self) -> dict[str, Any]:
        return {"signal_id": self.signal_id, **self.immutable_payload}


@dataclass(frozen=True, slots=True)
class HistoricalEvidence:
    setup_key: str
    captured_at: datetime
    wins: int
    losses: int
    flats: int = 0
    gross_win_r: float = 0.0
    gross_loss_r: float = 0.0
    average_win_r: float = 0.0
    average_loss_r: float = -1.0
    max_drawdown_r: float = 0.0
    recent_win_rate: float | None = None
    baseline_win_rate: float | None = None

    def __post_init__(self) -> None:
        if not _text(self.setup_key):
            raise ValueError("setup_key is required")
        if self.captured_at.tzinfo is None:
            raise ValueError("evidence timestamp must include timezone information")
        if min(self.wins, self.losses, self.flats) < 0:
            raise ValueError("evidence counts cannot be negative")
        if self.gross_win_r < 0 or self.gross_loss_r < 0:
            raise ValueError("gross R values cannot be negative")
        if self.average_win_r < 0 or self.average_loss_r > 0:
            raise ValueError("average win/loss R signs are invalid")
        for value in (self.recent_win_rate, self.baseline_win_rate):
            if value is not None and not 0 <= value <= 1:
                raise ValueError("win rates must be within 0..1")

    @property
    def completed(self) -> int:
        return self.wins + self.losses + self.flats

    @property
    def raw_win_rate(self) -> float:
        return self.wins / self.completed if self.completed else 0.0

    @property
    def smoothed_win_rate(self) -> float:
        return (self.wins + 1.0) / (self.completed + 2.0)

    @property
    def wilson_lower_95(self) -> float:
        return wilson_lower_bound(self.wins, self.completed, 0.95)

    @property
    def profit_factor_r(self) -> float:
        if self.gross_loss_r <= 0:
            return math.inf if self.gross_win_r > 0 else 0.0
        return self.gross_win_r / self.gross_loss_r

    @property
    def drift_ratio(self) -> float | None:
        if (
            self.recent_win_rate is None
            or self.baseline_win_rate is None
            or self.baseline_win_rate <= 0
        ):
            return None
        return self.recent_win_rate / self.baseline_win_rate

    def as_dict(self) -> dict[str, Any]:
        return {
            "setup_key": self.setup_key,
            "captured_at": _iso(self.captured_at),
            "wins": self.wins,
            "losses": self.losses,
            "flats": self.flats,
            "completed": self.completed,
            "raw_win_rate": self.raw_win_rate,
            "smoothed_win_rate": self.smoothed_win_rate,
            "wilson_lower_95": self.wilson_lower_95,
            "gross_win_r": self.gross_win_r,
            "gross_loss_r": self.gross_loss_r,
            "average_win_r": self.average_win_r,
            "average_loss_r": self.average_loss_r,
            "profit_factor_r": self.profit_factor_r,
            "max_drawdown_r": self.max_drawdown_r,
            "recent_win_rate": self.recent_win_rate,
            "baseline_win_rate": self.baseline_win_rate,
            "drift_ratio": self.drift_ratio,
        }


@dataclass(frozen=True, slots=True)
class PublicationPolicy:
    factor_weights: Mapping[str, float] = field(
        default_factory=lambda: dict(DEFAULT_FACTOR_WEIGHTS)
    )
    minimum_quality_score: float = 75.0
    minimum_completed: int = 30
    minimum_wilson_lower: float = 0.60
    minimum_profit_factor_r: float = 1.20
    minimum_expected_value_r: float = 0.05
    minimum_first_target_rr: float = 1.20
    minimum_market_groups: int = 4
    maximum_evidence_age_hours: float = 24.0
    minimum_drift_ratio: float = 0.75

    def __post_init__(self) -> None:
        unknown = set(self.factor_weights) - VALID_GROUPS
        if unknown:
            raise ValueError(f"unknown policy groups: {', '.join(sorted(unknown))}")
        if _float(self.factor_weights.get("robot_monitoring")) != 0:
            raise ValueError("robot_monitoring policy weight must be zero")
        total = sum(_float(value) for value in self.factor_weights.values())
        if total <= 0:
            raise ValueError("factor weights must have a positive total")
        if self.minimum_completed < 1 or self.minimum_market_groups < 1:
            raise ValueError("minimum counts must be positive")


@dataclass(frozen=True, slots=True)
class GateDecision:
    state: str
    quality_score: float
    conservative_probability: float
    expected_value_r: float
    reasons: tuple[str, ...]
    checks: Mapping[str, bool]

    @property
    def publishable(self) -> bool:
        return self.state == "PUBLISHABLE"

    def as_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "publishable": self.publishable,
            "quality_score": self.quality_score,
            "conservative_probability": self.conservative_probability,
            "expected_value_r": self.expected_value_r,
            "reasons": list(self.reasons),
            "checks": dict(self.checks),
        }


@dataclass(frozen=True, slots=True)
class SignalPassport:
    candidate: SignalCandidate
    evidence: HistoricalEvidence
    policy: PublicationPolicy
    decision: GateDecision

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "signal_id": self.candidate.signal_id,
            "candidate": self.candidate.as_dict(),
            "historical_evidence": self.evidence.as_dict(),
            "gate_decision": self.decision.as_dict(),
            "safety": {
                "orders_enabled": False,
                "telegram_publication_enabled": False,
                "source_files_modified": False,
                "robot_signals_used_as_primary_trigger": False,
            },
        }


def quality_score(candidate: SignalCandidate, policy: PublicationPolicy) -> float:
    weighted = 0.0
    total_weight = 0.0
    for group, weight_value in policy.factor_weights.items():
        weight = _float(weight_value)
        if weight <= 0:
            continue
        weighted += weight * _clamp01(candidate.factor_scores.get(group, 0.0))
        total_weight += weight
    return round(100.0 * weighted / total_weight, 6) if total_weight > 0 else 0.0


def expected_value_r(
    candidate: SignalCandidate,
    evidence: HistoricalEvidence,
    *,
    cost_r: float = 0.0,
) -> float:
    probability = evidence.wilson_lower_95
    average_win = evidence.average_win_r or candidate.plan.first_target_rr
    average_loss = abs(evidence.average_loss_r) or 1.0
    return probability * average_win - (1.0 - probability) * average_loss - max(0.0, cost_r)


def evaluate_candidate(
    candidate: SignalCandidate,
    evidence: HistoricalEvidence,
    policy: PublicationPolicy | None = None,
    *,
    cost_r: float = 0.0,
    now: datetime | None = None,
) -> GateDecision:
    rules = policy or PublicationPolicy()
    captured_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    score = quality_score(candidate, rules)
    probability = evidence.wilson_lower_95
    expectancy = expected_value_r(candidate, evidence, cost_r=cost_r)
    market_groups = {
        group
        for group, value in candidate.factor_scores.items()
        if group in MARKET_GROUPS and _float(value) > 0
    }
    evidence_age = max(
        0.0,
        (captured_at - evidence.captured_at.astimezone(timezone.utc)).total_seconds() / 3600.0,
    )
    drift = evidence.drift_ratio
    checks = {
        "market_generated": candidate.generated_from_market_data,
        "quality": score >= rules.minimum_quality_score,
        "sample": evidence.completed >= rules.minimum_completed,
        "conservative_probability": probability >= rules.minimum_wilson_lower,
        "profit_factor": evidence.profit_factor_r >= rules.minimum_profit_factor_r,
        "expected_value": expectancy >= rules.minimum_expected_value_r,
        "rr": candidate.plan.first_target_rr >= rules.minimum_first_target_rr,
        "market_group_breadth": len(market_groups) >= rules.minimum_market_groups,
        "evidence_fresh": evidence_age <= rules.maximum_evidence_age_hours,
        "drift": drift is None or drift >= rules.minimum_drift_ratio,
    }

    reasons: list[str] = []
    labels = {
        "market_generated": "candidate is not generated from market data",
        "quality": f"quality {score:.1f} < {rules.minimum_quality_score:.1f}",
        "sample": f"completed sample {evidence.completed} < {rules.minimum_completed}",
        "conservative_probability": (
            f"Wilson lower {100 * probability:.1f}% < "
            f"{100 * rules.minimum_wilson_lower:.1f}%"
        ),
        "profit_factor": (
            f"profit factor {evidence.profit_factor_r:.2f} < "
            f"{rules.minimum_profit_factor_r:.2f}"
        ),
        "expected_value": (
            f"expected value {expectancy:.3f}R < "
            f"{rules.minimum_expected_value_r:.3f}R"
        ),
        "rr": (
            f"first-target RR {candidate.plan.first_target_rr:.2f} < "
            f"{rules.minimum_first_target_rr:.2f}"
        ),
        "market_group_breadth": (
            f"market factor groups {len(market_groups)} < {rules.minimum_market_groups}"
        ),
        "evidence_fresh": (
            f"evidence age {evidence_age:.1f}h > {rules.maximum_evidence_age_hours:.1f}h"
        ),
        "drift": (
            f"recent/baseline edge ratio {drift:.2f} < {rules.minimum_drift_ratio:.2f}"
            if drift is not None
            else "edge drift unavailable"
        ),
    }
    for name, passed in checks.items():
        if not passed:
            reasons.append(labels[name])

    hard_reject = any(
        not checks[name]
        for name in (
            "market_generated",
            "expected_value",
            "rr",
            "evidence_fresh",
            "drift",
        )
    )
    if all(checks.values()):
        state = "PUBLISHABLE"
    elif hard_reject:
        state = "REJECTED"
    else:
        state = "SHADOW_ONLY"
    return GateDecision(
        state=state,
        quality_score=score,
        conservative_probability=probability,
        expected_value_r=expectancy,
        reasons=tuple(reasons),
        checks=checks,
    )


def build_passport(
    candidate: SignalCandidate,
    evidence: HistoricalEvidence,
    policy: PublicationPolicy | None = None,
    *,
    cost_r: float = 0.0,
    now: datetime | None = None,
) -> SignalPassport:
    rules = policy or PublicationPolicy()
    decision = evaluate_candidate(
        candidate,
        evidence,
        rules,
        cost_r=cost_r,
        now=now,
    )
    return SignalPassport(
        candidate=candidate,
        evidence=evidence,
        policy=rules,
        decision=decision,
    )


def candidate_from_dict(payload: Mapping[str, Any]) -> SignalCandidate:
    plan_payload = payload.get("plan", {})
    if not isinstance(plan_payload, Mapping):
        raise ValueError("plan must be an object")
    entries_payload = plan_payload.get("entries", [])
    entries = tuple(
        EntryOrder(
            price=_float(item.get("price")),
            allocation=_float(item.get("allocation")),
            rationale=_text(item.get("rationale")),
            order_type=_text(item.get("order_type")) or "LIMIT",
        )
        for item in entries_payload
    )
    plan = TradePlan(
        action=_text(plan_payload.get("action")),
        entries=entries,
        stop_price=_float(plan_payload.get("stop_price")),
        targets=tuple(_float(value) for value in plan_payload.get("targets", [])),
        invalidation=_text(plan_payload.get("invalidation")),
        target_rationale=tuple(
            _text(value) for value in plan_payload.get("target_rationale", [])
        ),
    )
    factor_reasons = payload.get("factor_reasons", {})
    return SignalCandidate(
        observed_at=_parse_time(payload.get("observed_at")),
        created_at=_parse_time(payload.get("created_at")),
        symbol=_text(payload.get("symbol")),
        timeframe=_text(payload.get("timeframe")),
        setup_family=_text(payload.get("setup_family")),
        scenario=_text(payload.get("scenario")),
        plan=plan,
        market_features=payload.get("market_features", {}),
        factor_scores={
            str(key): _float(value)
            for key, value in dict(payload.get("factor_scores", {})).items()
        },
        factor_reasons={
            str(key): tuple(_text(item) for item in value)
            for key, value in dict(factor_reasons).items()
        },
        provenance=tuple(_text(value) for value in payload.get("provenance", [])),
        generated_from_market_data=bool(
            payload.get("generated_from_market_data", True)
        ),
        robot_context_only=payload.get("robot_context_only", {}),
    )


def evidence_from_dict(payload: Mapping[str, Any]) -> HistoricalEvidence:
    return HistoricalEvidence(
        setup_key=_text(payload.get("setup_key")),
        captured_at=_parse_time(payload.get("captured_at")),
        wins=int(payload.get("wins", 0)),
        losses=int(payload.get("losses", 0)),
        flats=int(payload.get("flats", 0)),
        gross_win_r=_float(payload.get("gross_win_r")),
        gross_loss_r=_float(payload.get("gross_loss_r")),
        average_win_r=_float(payload.get("average_win_r")),
        average_loss_r=_float(payload.get("average_loss_r"), -1.0),
        max_drawdown_r=_float(payload.get("max_drawdown_r")),
        recent_win_rate=(
            _float(payload.get("recent_win_rate"))
            if payload.get("recent_win_rate") is not None
            else None
        ),
        baseline_win_rate=(
            _float(payload.get("baseline_win_rate"))
            if payload.get("baseline_win_rate") is not None
            else None
        ),
    )


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    temporary.replace(path)


def append_journal_event(
    path: Path,
    *,
    signal_id: str,
    event_type: str,
    payload: Mapping[str, Any],
    recorded_at: datetime | None = None,
) -> dict[str, Any]:
    """Append one tamper-evident event and return the stored envelope."""
    if not _text(signal_id) or not _text(event_type):
        raise ValueError("signal_id and event_type are required")
    previous_hash = "GENESIS"
    existing: list[dict[str, Any]] = []
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            event = json.loads(line)
            existing.append(event)
            previous_hash = _text(event.get("event_hash"))

    event_core = {
        "schema_version": SCHEMA_VERSION,
        "recorded_at": _iso(recorded_at or datetime.now(timezone.utc)),
        "signal_id": signal_id,
        "event_type": event_type.upper(),
        "payload": _json_safe(payload),
        "previous_hash": previous_hash,
    }
    event_hash = _sha256(event_core)
    for event in existing:
        if event.get("event_hash") == event_hash:
            return event
        if (
            event_type.upper() == "CANDIDATE"
            and event.get("event_type") == "CANDIDATE"
            and event.get("signal_id") == signal_id
            and event.get("payload") != event_core["payload"]
        ):
            raise ValueError(f"immutable candidate mutation detected: {signal_id}")

    envelope = {**event_core, "event_hash": event_hash}
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(_canonical_json(envelope) + "\n")
    return envelope


def verify_journal(path: Path) -> tuple[bool, str]:
    if not path.is_file():
        return False, "journal not found"
    previous_hash = "GENESIS"
    seen_candidates: dict[str, Any] = {}
    for index, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return False, f"line {index}: invalid JSON"
        event_hash = _text(event.pop("event_hash", ""))
        if event.get("previous_hash") != previous_hash:
            return False, f"line {index}: broken previous_hash"
        if _sha256(event) != event_hash:
            return False, f"line {index}: event hash mismatch"
        if event.get("event_type") == "CANDIDATE":
            signal_id = _text(event.get("signal_id"))
            payload = event.get("payload")
            if signal_id in seen_candidates and seen_candidates[signal_id] != payload:
                return False, f"line {index}: candidate mutation"
            seen_candidates[signal_id] = payload
        previous_hash = event_hash
    return True, "OK"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="TradeMind v1.16 evidence-aware signal publication gate"
    )
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--journal", type=Path)
    parser.add_argument("--cost-r", type=float, default=0.0)
    args = parser.parse_args(argv)
    try:
        candidate = candidate_from_dict(
            json.loads(args.candidate.read_text(encoding="utf-8-sig"))
        )
        evidence = evidence_from_dict(
            json.loads(args.evidence.read_text(encoding="utf-8-sig"))
        )
        passport = build_passport(candidate, evidence, cost_r=args.cost_r)
        payload = passport.as_dict()
        _atomic_json(args.output, payload)
        if args.journal is not None:
            append_journal_event(
                args.journal,
                signal_id=candidate.signal_id,
                event_type="CANDIDATE",
                payload=candidate.as_dict(),
                recorded_at=candidate.created_at,
            )
            append_journal_event(
                args.journal,
                signal_id=candidate.signal_id,
                event_type="GATE_DECISION",
                payload=passport.decision.as_dict(),
            )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"Signal intelligence gate failed: {exc}")
        return 1

    decision = passport.decision
    print("TradeMind v1.16 Signal Intelligence Core")
    print("Research gate only. Orders OFF. Telegram publication OFF.")
    print(f"Signal ID: {candidate.signal_id}")
    print(f"State: {decision.state}")
    print(f"Quality: {decision.quality_score:.1f}/100")
    print(
        "Reliability, raw/conservative: "
        f"{100 * evidence.raw_win_rate:.1f}%/"
        f"{100 * decision.conservative_probability:.1f}% "
        f"(n={evidence.completed})"
    )
    print(f"Expected value: {decision.expected_value_r:.3f}R")
    for reason in decision.reasons:
        print(f"- {reason}")
    print(f"Output: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
