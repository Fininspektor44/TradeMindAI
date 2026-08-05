"""TradeMind Signal Passport Factory 1.0.

The factory converts fresh market candidates into immutable evidence-aware
signal passports. It uses only outcomes that were completed before each
candidate was created, preventing future-data leakage. Only independently
PUBLISHABLE passports enter the Signal-to-Risk Bridge inbox.

The module is research and orchestration only. It does not publish signals,
connect to a broker, calculate account sizing, or send orders.
"""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from trademind.signal_evidence import OutcomeObservation, aggregate_evidence, load_outcomes
from trademind.signal_intelligence import (
    PublicationPolicy,
    SignalCandidate,
    append_journal_event,
    build_passport,
    candidate_from_dict,
)
from trademind.signal_shadow import load_candidates

FACTORY_VERSION = "1.0.0"
READY_STATE = "PASSPORTS_READY"
WAITING_FRESH_STATE = "WAITING_NO_FRESH_CANDIDATES"
WAITING_PUBLISHABLE_STATE = "WAITING_NO_PUBLISHABLE_PASSPORT"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _number(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _read_json(path: Path) -> Mapping[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, Mapping):
        raise ValueError(f"JSON root must be an object: {path}")
    return payload


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    temporary.replace(path)


def _unique_destination(directory: Path, name: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / name
    if not destination.exists():
        return destination
    stem = destination.stem
    suffix = destination.suffix
    index = 1
    while True:
        candidate = directory / f"{stem}.{index}{suffix}"
        if not candidate.exists():
            return candidate
        index += 1


def _move_file(path: Path, directory: Path) -> Path:
    destination = _unique_destination(directory, path.name)
    path.replace(destination)
    return destination


def _candidate_from_passport(path: Path) -> SignalCandidate:
    payload = _read_json(path)
    candidate_payload = payload.get("candidate")
    if not isinstance(candidate_payload, Mapping):
        raise ValueError("passport candidate must be an object")
    candidate = candidate_from_dict(candidate_payload)
    root_signal_id = _text(payload.get("signal_id"))
    if root_signal_id != candidate.signal_id:
        raise ValueError("passport signal_id does not match immutable candidate")
    return candidate


def maintain_passport_inbox(
    passports_dir: Path,
    archive_dir: Path,
    quarantine_dir: Path,
    *,
    now: datetime,
    maximum_candidate_age_seconds: float,
    maximum_clock_skew_seconds: float,
) -> dict[str, Any]:
    """Archive expired passports and quarantine malformed/future passports."""
    passports_dir.mkdir(parents=True, exist_ok=True)
    archived: list[str] = []
    quarantined: list[dict[str, str]] = []
    active = 0
    for path in sorted(passports_dir.glob("*.json")):
        try:
            candidate = _candidate_from_passport(path)
            age = (now - candidate.created_at.astimezone(timezone.utc)).total_seconds()
            if age < -maximum_clock_skew_seconds:
                destination = _move_file(path, quarantine_dir)
                quarantined.append(
                    {
                        "source": str(path),
                        "destination": str(destination),
                        "reason": f"passport candidate is {-age:.1f}s in the future",
                    }
                )
            elif age > maximum_candidate_age_seconds:
                destination = _move_file(path, archive_dir)
                archived.append(str(destination))
            else:
                active += 1
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            destination = _move_file(path, quarantine_dir)
            quarantined.append(
                {
                    "source": str(path),
                    "destination": str(destination),
                    "reason": str(exc),
                }
            )
    return {
        "active": active,
        "archived": archived,
        "quarantined": quarantined,
    }


def _historical_outcomes(
    candidate: SignalCandidate,
    outcomes: Sequence[OutcomeObservation],
) -> list[OutcomeObservation]:
    cutoff = candidate.created_at.astimezone(timezone.utc)
    return [
        outcome
        for outcome in outcomes
        if outcome.signal_id != candidate.signal_id
        and outcome.completed_at.astimezone(timezone.utc) <= cutoff
    ]


def _policy_payload(policy: PublicationPolicy) -> dict[str, Any]:
    return {
        "minimum_quality_score": policy.minimum_quality_score,
        "minimum_completed": policy.minimum_completed,
        "minimum_wilson_lower": policy.minimum_wilson_lower,
        "minimum_profit_factor_r": policy.minimum_profit_factor_r,
        "minimum_expected_value_r": policy.minimum_expected_value_r,
        "minimum_first_target_rr": policy.minimum_first_target_rr,
        "minimum_market_groups": policy.minimum_market_groups,
        "maximum_evidence_age_hours": policy.maximum_evidence_age_hours,
        "minimum_drift_ratio": policy.minimum_drift_ratio,
        "factor_weights": dict(policy.factor_weights),
    }


def _factory_passport_payload(
    candidate: SignalCandidate,
    passport_payload: Mapping[str, Any],
    *,
    generated_at: datetime,
    evidence_outcomes: int,
    cost_r: float,
    recent_window: int,
    maximum_candidate_age_seconds: float,
    policy: PublicationPolicy,
) -> dict[str, Any]:
    return {
        **dict(passport_payload),
        "factory": {
            "factory_version": FACTORY_VERSION,
            "generated_at": _iso(generated_at),
            "candidate_created_at": _iso(candidate.created_at),
            "evidence_cutoff_at": _iso(candidate.created_at),
            "future_data_used": False,
            "historical_outcomes_available_at_cutoff": evidence_outcomes,
            "evaluation_cost_r": cost_r,
            "recent_window": recent_window,
            "maximum_candidate_age_seconds": maximum_candidate_age_seconds,
            "publication_policy": _policy_payload(policy),
        },
    }


def _existing_passport_matches(path: Path, candidate: SignalCandidate) -> bool:
    payload = _read_json(path)
    candidate_payload = payload.get("candidate")
    if not isinstance(candidate_payload, Mapping):
        raise ValueError(f"existing passport candidate is invalid: {path}")
    existing = candidate_from_dict(candidate_payload)
    if existing.signal_id != candidate.signal_id:
        raise ValueError(f"existing passport identity conflict: {path}")
    if dict(candidate_payload) != candidate.as_dict():
        raise ValueError(f"immutable candidate conflict in existing passport: {path}")
    return True


@dataclass(frozen=True, slots=True)
class FactoryRun:
    status: Mapping[str, Any]
    evaluations: tuple[Mapping[str, Any], ...]
    passport_paths: tuple[Path, ...]

    @property
    def state(self) -> str:
        return _text(self.status.get("state"))


def run_factory(
    *,
    candidates_path: Path,
    outcomes_path: Path,
    output_dir: Path,
    passports_dir: Path | None = None,
    journal: Path | None = None,
    cost_r: float = 0.04,
    recent_window: int = 30,
    maximum_candidate_age_seconds: float = 900.0,
    maximum_clock_skew_seconds: float = 30.0,
    candidate_limit: int = 0,
    now: datetime | None = None,
) -> FactoryRun:
    captured_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if cost_r < 0:
        raise ValueError("cost_r cannot be negative")
    if recent_window < 1:
        raise ValueError("recent_window must be positive")
    if maximum_candidate_age_seconds <= 0:
        raise ValueError("maximum_candidate_age_seconds must be positive")
    if maximum_clock_skew_seconds < 0:
        raise ValueError("maximum_clock_skew_seconds cannot be negative")
    if candidate_limit < 0:
        raise ValueError("candidate_limit cannot be negative")

    inbox = passports_dir or output_dir / "passports"
    archive = output_dir / "archive"
    quarantine = output_dir / "quarantine"
    maintenance = maintain_passport_inbox(
        inbox,
        archive,
        quarantine,
        now=captured_at,
        maximum_candidate_age_seconds=maximum_candidate_age_seconds,
        maximum_clock_skew_seconds=maximum_clock_skew_seconds,
    )

    candidates = load_candidates(candidates_path)
    outcomes = load_outcomes(outcomes_path)
    outcome_ids = {outcome.signal_id for outcome in outcomes}
    rules = PublicationPolicy()

    pending = [candidate for candidate in candidates if candidate.signal_id not in outcome_ids]
    pending.sort(key=lambda item: (item.created_at, item.signal_id), reverse=True)
    if candidate_limit > 0:
        pending = pending[:candidate_limit]

    counts = {
        "candidates_total": len(candidates),
        "outcomes_total": len(outcomes),
        "completed_candidates_skipped": len(candidates) - len(pending),
        "pending_candidates_scanned": len(pending),
        "fresh_candidates": 0,
        "stale_candidates": 0,
        "future_candidates": 0,
        "publishable": 0,
        "shadow_only": 0,
        "rejected": 0,
        "passports_created": 0,
        "passports_existing": 0,
    }
    evaluations: list[dict[str, Any]] = []
    passport_paths: list[Path] = []

    for candidate in pending:
        age = (captured_at - candidate.created_at.astimezone(timezone.utc)).total_seconds()
        if age < -maximum_clock_skew_seconds:
            counts["future_candidates"] += 1
            continue
        if age > maximum_candidate_age_seconds:
            counts["stale_candidates"] += 1
            continue

        counts["fresh_candidates"] += 1
        historical = _historical_outcomes(candidate, outcomes)
        evidence = aggregate_evidence(
            candidate,
            historical,
            captured_at=captured_at,
            recent_window=recent_window,
        )
        passport = build_passport(
            candidate,
            evidence,
            rules,
            cost_r=cost_r,
            now=captured_at,
        )
        decision = passport.decision
        evaluations.append(
            {
                "signal_id": candidate.signal_id,
                "created_at": _iso(candidate.created_at),
                "age_seconds": max(0.0, age),
                "symbol": candidate.symbol,
                "timeframe": candidate.timeframe,
                "action": candidate.plan.action,
                "setup_family": candidate.setup_family,
                "setup_key": evidence.setup_key,
                "historical_sample": evidence.completed,
                "state": decision.state,
                "quality_score": decision.quality_score,
                "conservative_probability": decision.conservative_probability,
                "expected_value_r": decision.expected_value_r,
                "reasons": list(decision.reasons),
                "checks": dict(decision.checks),
            }
        )

        if decision.state == "PUBLISHABLE":
            counts["publishable"] += 1
            output_path = inbox / f"{candidate.signal_id}.json"
            if output_path.exists():
                _existing_passport_matches(output_path, candidate)
                counts["passports_existing"] += 1
            else:
                payload = _factory_passport_payload(
                    candidate,
                    passport.as_dict(),
                    generated_at=captured_at,
                    evidence_outcomes=evidence.completed,
                    cost_r=cost_r,
                    recent_window=recent_window,
                    maximum_candidate_age_seconds=maximum_candidate_age_seconds,
                    policy=rules,
                )
                _atomic_json(output_path, payload)
                counts["passports_created"] += 1
                if journal is not None:
                    append_journal_event(
                        journal,
                        signal_id=candidate.signal_id,
                        event_type="SIGNAL_PASSPORT_CREATED",
                        payload={
                            "factory_version": FACTORY_VERSION,
                            "passport": payload,
                        },
                        recorded_at=captured_at,
                    )
            passport_paths.append(output_path)
        elif decision.state == "SHADOW_ONLY":
            counts["shadow_only"] += 1
        else:
            counts["rejected"] += 1

    if counts["publishable"] > 0:
        state = READY_STATE
    elif counts["fresh_candidates"] == 0:
        state = WAITING_FRESH_STATE
    else:
        state = WAITING_PUBLISHABLE_STATE

    status = {
        "schema_version": FACTORY_VERSION,
        "state": state,
        "updated_at": _iso(captured_at),
        **counts,
        "active_passports_before_run": maintenance["active"],
        "passports_archived": len(maintenance["archived"]),
        "passports_quarantined": len(maintenance["quarantined"]),
        "archive_paths": maintenance["archived"][:50],
        "quarantine_records": maintenance["quarantined"][:50],
        "passports_dir": str(inbox),
        "evaluations": str(output_dir / "evaluations.json"),
        "safety": {
            "read_only": True,
            "orders_enabled": False,
            "signal_publication_enabled": False,
            "broker_api_called": False,
            "future_data_used": False,
            "grid_robots_used_as_signal_source": False,
        },
    }
    _atomic_json(
        output_dir / "evaluations.json",
        {
            "schema_version": FACTORY_VERSION,
            "updated_at": _iso(captured_at),
            "evaluations": evaluations,
        },
    )
    _atomic_json(output_dir / "status.json", status)
    return FactoryRun(
        status=status,
        evaluations=tuple(evaluations),
        passport_paths=tuple(passport_paths),
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="TradeMind Signal Passport Factory 1.0")
    parser.add_argument(
        "--candidates",
        type=Path,
        default=Path("data/signal_intelligence_v1_16/candidates.jsonl"),
    )
    parser.add_argument(
        "--outcomes",
        type=Path,
        default=Path("data/signal_intelligence_v1_16/outcomes.jsonl"),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("data/signal_passport_factory_v1"),
    )
    parser.add_argument("--passports-dir", type=Path)
    parser.add_argument("--journal", type=Path)
    parser.add_argument("--cost-r", type=float, default=0.04)
    parser.add_argument("--recent-window", type=int, default=30)
    parser.add_argument("--maximum-candidate-age-seconds", type=float, default=900.0)
    parser.add_argument("--maximum-clock-skew-seconds", type=float, default=30.0)
    parser.add_argument("--candidate-limit", type=int, default=0)
    args = parser.parse_args(argv)

    try:
        result = run_factory(
            candidates_path=args.candidates.expanduser().resolve(),
            outcomes_path=args.outcomes.expanduser().resolve(),
            output_dir=args.output_dir.expanduser().resolve(),
            passports_dir=(
                args.passports_dir.expanduser().resolve()
                if args.passports_dir is not None
                else None
            ),
            journal=(args.journal.expanduser().resolve() if args.journal else None),
            cost_r=args.cost_r,
            recent_window=args.recent_window,
            maximum_candidate_age_seconds=args.maximum_candidate_age_seconds,
            maximum_clock_skew_seconds=args.maximum_clock_skew_seconds,
            candidate_limit=args.candidate_limit,
        )
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        print(f"Signal passport factory failed: {exc}")
        return 1

    status = result.status
    print("TradeMind Signal Passport Factory 1.0")
    print("Fresh candidates + as-of evidence + publication gate.")
    print("Orders OFF. Publication OFF. Broker API not called.")
    print(f"State: {result.state}")
    print(
        "Candidates fresh/stale/future: "
        f"{status['fresh_candidates']}/{status['stale_candidates']}/"
        f"{status['future_candidates']}"
    )
    print(
        "Gate publishable/shadow/rejected: "
        f"{status['publishable']}/{status['shadow_only']}/{status['rejected']}"
    )
    print(
        "Passports created/existing/archived: "
        f"{status['passports_created']}/{status['passports_existing']}/"
        f"{status['passports_archived']}"
    )
    for path in result.passport_paths[:20]:
        print(f"PASSPORT: {path}")
    print(f"Status: {args.output_dir / 'status.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
