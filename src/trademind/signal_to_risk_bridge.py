"""TradeMind Signal-to-Risk Bridge 1.0.

The bridge connects an evidence-approved signal passport to fresh read-only MT5
snapshots and Risk Manager Core 1.0. It creates one account-specific decision
package without importing a broker API, publishing a signal, or sending orders.

A passport is never trusted by label alone. The bridge verifies candidate
identity, setup-evidence identity, stored gate checks, and a fresh independent
gate evaluation before the account risk calculation is allowed to run.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from trademind.mt5_risk_adapter import AdapterBundle, adapt_mt5_exports
from trademind.risk_manager import (
    RiskDecision,
    RiskProfile,
    evaluate_risk,
    profile_from_dict,
)
from trademind.signal_evidence import similarity_key
from trademind.signal_intelligence import (
    HistoricalEvidence,
    PublicationPolicy,
    SignalCandidate,
    append_journal_event,
    candidate_from_dict,
    evidence_from_dict,
    evaluate_candidate,
)

BRIDGE_VERSION = "1.0.0"
WAITING_STATE = "WAITING_NO_PUBLISHABLE_PASSPORT"
DECISION_STATE = "DECISION_READY"


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


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non-finite numeric value")
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
    if hasattr(value, "as_dict"):
        return _json_safe(value.as_dict())
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


def _close(left: Any, right: Any, tolerance: float = 1e-6) -> bool:
    first = _number(left, math.nan)
    second = _number(right, math.nan)
    return math.isfinite(first) and math.isfinite(second) and abs(first - second) <= tolerance


@dataclass(frozen=True, slots=True)
class ValidatedPassport:
    path: Path
    payload: Mapping[str, Any]
    candidate: SignalCandidate
    evidence: HistoricalEvidence
    passport_hash: str
    gate_quality_score: float
    conservative_probability: float
    expected_value_r: float

    @property
    def signal_id(self) -> str:
        return self.candidate.signal_id

    def summary(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "passport_hash": self.passport_hash,
            "signal_id": self.signal_id,
            "created_at": _iso(self.candidate.created_at),
            "symbol": self.candidate.symbol,
            "timeframe": self.candidate.timeframe,
            "action": self.candidate.plan.action,
            "setup_family": self.candidate.setup_family,
            "setup_key": self.evidence.setup_key,
            "gate_state": "PUBLISHABLE",
            "quality_score": self.gate_quality_score,
            "conservative_probability": self.conservative_probability,
            "completed_sample": self.evidence.completed,
            "expected_value_r": self.expected_value_r,
        }


@dataclass(frozen=True, slots=True)
class BridgeRun:
    status: Mapping[str, Any]
    package: Mapping[str, Any] | None = None

    @property
    def state(self) -> str:
        return _text(self.status.get("state"))


def validate_publishable_passport(
    path: Path,
    *,
    now: datetime,
    cost_r: float,
    policy: PublicationPolicy | None = None,
) -> ValidatedPassport:
    payload = _read_json(path)
    candidate_payload = payload.get("candidate")
    evidence_payload = payload.get("historical_evidence")
    gate_payload = payload.get("gate_decision")
    safety_payload = payload.get("safety", {})
    if not isinstance(candidate_payload, Mapping):
        raise ValueError("passport candidate must be an object")
    if not isinstance(evidence_payload, Mapping):
        raise ValueError("passport historical_evidence must be an object")
    if not isinstance(gate_payload, Mapping):
        raise ValueError("passport gate_decision must be an object")
    if not isinstance(safety_payload, Mapping):
        raise ValueError("passport safety must be an object")

    candidate = candidate_from_dict(candidate_payload)
    evidence = evidence_from_dict(evidence_payload)
    root_signal_id = _text(payload.get("signal_id"))
    embedded_signal_id = _text(candidate_payload.get("signal_id"))
    if root_signal_id != candidate.signal_id:
        raise ValueError("passport signal_id does not match immutable candidate")
    if embedded_signal_id and embedded_signal_id != candidate.signal_id:
        raise ValueError("embedded candidate signal_id does not match candidate")

    expected_setup_key = similarity_key(candidate)
    if evidence.setup_key != expected_setup_key:
        raise ValueError("historical evidence setup_key does not match candidate setup")

    stored_state = _text(gate_payload.get("state")).upper()
    stored_publishable = bool(gate_payload.get("publishable"))
    stored_checks = gate_payload.get("checks", {})
    if stored_state != "PUBLISHABLE" or not stored_publishable:
        raise ValueError(f"passport gate state is {stored_state or '<empty>'}, not PUBLISHABLE")
    if not isinstance(stored_checks, Mapping) or not stored_checks:
        raise ValueError("passport gate checks are missing")
    failed_stored = sorted(str(name) for name, passed in stored_checks.items() if not bool(passed))
    if failed_stored:
        raise ValueError(f"passport contains failed gate checks: {', '.join(failed_stored)}")
    if gate_payload.get("reasons"):
        raise ValueError("publishable passport unexpectedly contains gate rejection reasons")

    if bool(safety_payload.get("orders_enabled", False)):
        raise ValueError("passport safety boundary reports orders enabled")
    if bool(safety_payload.get("robot_signals_used_as_primary_trigger", False)):
        raise ValueError("passport uses robot monitoring as a primary signal trigger")

    rules = policy or PublicationPolicy()
    recalculated = evaluate_candidate(
        candidate,
        evidence,
        rules,
        cost_r=cost_r,
        now=now,
    )
    if recalculated.state != "PUBLISHABLE":
        details = "; ".join(recalculated.reasons) or "gate checks failed"
        raise ValueError(f"independent gate revalidation is {recalculated.state}: {details}")
    if dict(recalculated.checks) != {str(key): bool(value) for key, value in stored_checks.items()}:
        raise ValueError("stored gate checks differ from independent gate revalidation")
    if not _close(gate_payload.get("quality_score"), recalculated.quality_score):
        raise ValueError("stored gate quality differs from independent calculation")
    if not _close(
        gate_payload.get("conservative_probability"),
        recalculated.conservative_probability,
    ):
        raise ValueError("stored conservative probability differs from calculation")
    if not _close(gate_payload.get("expected_value_r"), recalculated.expected_value_r):
        raise ValueError("stored expected value differs from independent calculation")

    return ValidatedPassport(
        path=path,
        payload=payload,
        candidate=candidate,
        evidence=evidence,
        passport_hash=_sha256(payload),
        gate_quality_score=recalculated.quality_score,
        conservative_probability=recalculated.conservative_probability,
        expected_value_r=recalculated.expected_value_r,
    )


def _select_passport(
    *,
    explicit_path: Path | None,
    passports_dir: Path | None,
    now: datetime,
    cost_r: float,
) -> tuple[ValidatedPassport | None, list[dict[str, str]], int]:
    if explicit_path is not None:
        selected = validate_publishable_passport(
            explicit_path,
            now=now,
            cost_r=cost_r,
        )
        return selected, [], 1
    if passports_dir is None:
        raise ValueError("passport or passports directory is required")
    passports_dir.mkdir(parents=True, exist_ok=True)
    paths = sorted(
        (path for path in passports_dir.rglob("*.json") if path.is_file()),
        key=lambda path: (path.stat().st_mtime_ns, str(path)),
        reverse=True,
    )
    valid: list[ValidatedPassport] = []
    skipped: list[dict[str, str]] = []
    for path in paths:
        try:
            valid.append(
                validate_publishable_passport(
                    path,
                    now=now,
                    cost_r=cost_r,
                )
            )
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            skipped.append({"path": str(path), "reason": str(exc)})
    if not valid:
        return None, skipped, len(paths)
    valid.sort(key=lambda item: (item.candidate.created_at, item.signal_id), reverse=True)
    return valid[0], skipped, len(paths)


def _waiting_status(
    *,
    now: datetime,
    login: str,
    passport_source: str,
    scanned: int,
    skipped: Sequence[Mapping[str, str]],
) -> dict[str, Any]:
    return {
        "schema_version": BRIDGE_VERSION,
        "state": WAITING_STATE,
        "updated_at": _iso(now),
        "account_login": login,
        "passport_source": passport_source,
        "passports_scanned": scanned,
        "passports_rejected": len(skipped),
        "rejected_passports": list(skipped[:50]),
        "message": "No independently validated PUBLISHABLE passport is available.",
        "safety": {
            "read_only": True,
            "orders_enabled": False,
            "broker_api_called": False,
            "signal_publication_enabled": False,
            "manual_approval_requested": False,
        },
    }


def _decision_package(
    *,
    now: datetime,
    passport: ValidatedPassport,
    adapter: AdapterBundle,
    decision: RiskDecision,
    profile: RiskProfile,
) -> dict[str, Any]:
    return {
        "schema_version": BRIDGE_VERSION,
        "state": DECISION_STATE,
        "generated_at": _iso(now),
        "passport": passport.summary(),
        "account": adapter.account.as_dict(),
        "portfolio": adapter.portfolio.as_dict(),
        "instrument": adapter.instrument.as_dict(),
        "mt5_adapter_status": dict(adapter.status),
        "risk_profile": profile.as_dict(),
        "risk_decision": decision.as_dict(),
        "trader_summary": {
            "decision": decision.state,
            "symbol": decision.symbol,
            "action": decision.action,
            "requested_risk_pct": decision.requested_risk_pct,
            "actual_risk_pct": decision.actual_risk_pct,
            "actual_risk_money": decision.actual_risk_money,
            "portfolio_risk_after_pct": decision.portfolio_risk_after_pct,
            "margin_required": decision.margin_required,
            "free_margin_after": decision.free_margin_after,
            "orders": [item.as_dict() for item in decision.orders],
            "block_reasons": [item.as_dict() for item in decision.reasons],
            "warnings": [item.as_dict() for item in decision.warnings],
        },
        "safety": {
            "read_only": True,
            "orders_enabled": False,
            "broker_api_called": False,
            "signal_publication_enabled": False,
            "manual_approval_requested": False,
            "allow_means_risk_checks_only": True,
        },
    }


def run_bridge(
    *,
    login: str,
    account_csv: Path,
    positions_csv: Path,
    symbols_csv: Path,
    profile_path: Path,
    output_dir: Path,
    passport_path: Path | None = None,
    passports_dir: Path | None = None,
    correlations: Path | None = None,
    journal: Path | None = None,
    requested_risk_pct: float | None = None,
    cost_r: float = 0.04,
    maximum_mt5_age_seconds: float = 120.0,
    now: datetime | None = None,
) -> BridgeRun:
    captured_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    account_login = _text(login)
    if not account_login:
        raise ValueError("account login is required")
    if cost_r < 0:
        raise ValueError("cost_r cannot be negative")
    profile = profile_from_dict(_read_json(profile_path))

    selected, skipped, scanned = _select_passport(
        explicit_path=passport_path,
        passports_dir=passports_dir,
        now=captured_at,
        cost_r=cost_r,
    )
    source = str(passport_path or passports_dir or "")
    if selected is None:
        status = _waiting_status(
            now=captured_at,
            login=account_login,
            passport_source=source,
            scanned=scanned,
            skipped=skipped,
        )
        _atomic_json(output_dir / "status.json", status)
        return BridgeRun(status=status)

    adapter = adapt_mt5_exports(
        account_csv=account_csv,
        positions_csv=positions_csv,
        symbols_csv=symbols_csv,
        symbol=selected.candidate.symbol,
        action=selected.candidate.plan.action,
        correlations=correlations,
        maximum_age_seconds=maximum_mt5_age_seconds,
        maximum_clock_skew_seconds=profile.maximum_clock_skew_seconds,
        now=captured_at,
    )
    if adapter.account.account_id != account_login:
        raise ValueError(
            f"requested login {account_login} does not match MT5 snapshot "
            f"{adapter.account.account_id}"
        )

    decision = evaluate_risk(
        selected.candidate,
        gate_state="PUBLISHABLE",
        account=adapter.account,
        instrument=adapter.instrument,
        profile=profile,
        portfolio=adapter.portfolio,
        requested_risk_pct=requested_risk_pct,
        now=captured_at,
    )
    package = _decision_package(
        now=captured_at,
        passport=selected,
        adapter=adapter,
        decision=decision,
        profile=profile,
    )
    status = {
        "schema_version": BRIDGE_VERSION,
        "state": DECISION_STATE,
        "updated_at": _iso(captured_at),
        "account_login": account_login,
        "signal_id": selected.signal_id,
        "decision_id": decision.decision_id,
        "risk_state": decision.state,
        "symbol": decision.symbol,
        "action": decision.action,
        "passport_hash": selected.passport_hash,
        "passports_scanned": scanned,
        "passports_rejected": len(skipped),
        "latest_decision": str(output_dir / "latest_decision.json"),
        "history_decision": str(output_dir / "history" / f"{decision.decision_id}.json"),
        "safety": dict(package["safety"]),
    }
    _atomic_json(output_dir / "latest_decision.json", package)
    _atomic_json(output_dir / "history" / f"{decision.decision_id}.json", package)
    _atomic_json(output_dir / "status.json", status)
    if journal is not None:
        append_journal_event(
            journal,
            signal_id=selected.signal_id,
            event_type="SIGNAL_TO_RISK_DECISION",
            payload={
                "bridge_version": BRIDGE_VERSION,
                "passport_hash": selected.passport_hash,
                "account_id": adapter.account.account_id,
                "decision": decision.as_dict(),
            },
            recorded_at=captured_at,
        )
    return BridgeRun(status=status, package=package)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="TradeMind Signal-to-Risk Bridge 1.0"
    )
    parser.add_argument("--login", required=True)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--passport", type=Path)
    source.add_argument("--passports-dir", type=Path)
    parser.add_argument("--account-csv", type=Path, required=True)
    parser.add_argument("--positions-csv", type=Path, required=True)
    parser.add_argument("--symbols-csv", type=Path, required=True)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--correlations", type=Path)
    parser.add_argument("--journal", type=Path)
    parser.add_argument("--requested-risk-pct", type=float)
    parser.add_argument("--cost-r", type=float, default=0.04)
    parser.add_argument("--maximum-mt5-age-seconds", type=float, default=120.0)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)

    try:
        result = run_bridge(
            login=args.login,
            passport_path=args.passport,
            passports_dir=args.passports_dir,
            account_csv=args.account_csv,
            positions_csv=args.positions_csv,
            symbols_csv=args.symbols_csv,
            profile_path=args.profile,
            correlations=args.correlations,
            journal=args.journal,
            requested_risk_pct=args.requested_risk_pct,
            cost_r=args.cost_r,
            maximum_mt5_age_seconds=args.maximum_mt5_age_seconds,
            output_dir=args.output_dir,
        )
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        print(f"Signal-to-risk bridge failed: {exc}")
        return 1

    print("TradeMind Signal-to-Risk Bridge 1.0")
    print("Validated passport + UTC MT5 snapshots + Risk Manager.")
    print("Orders OFF. Publication OFF. Broker API not called.")
    print(f"State: {result.state}")
    if result.package is None:
        print(
            "Passports scanned/rejected: "
            f"{result.status.get('passports_scanned', 0)}/"
            f"{result.status.get('passports_rejected', 0)}"
        )
        print(f"Status: {args.output_dir / 'status.json'}")
        return 0

    decision = result.package["risk_decision"]
    print(f"Signal: {decision['signal_id']}")
    print(f"Account: {decision['account_id']} ({decision['venue']})")
    print(f"Instrument: {decision['symbol']} {decision['action']}")
    print(f"Risk state: {decision['state']}")
    print(
        "Risk requested/actual: "
        f"{decision['requested_risk_pct']:.3f}%/"
        f"{decision['actual_risk_pct']:.3f}% "
        f"({decision['actual_risk_money']:.2f})"
    )
    print(
        "Portfolio risk after: "
        f"{decision['portfolio_risk_after_pct']:.3f}% "
        f"({decision['portfolio_risk_after_money']:.2f})"
    )
    for order in decision["orders"]:
        print(
            f"Entry {order['entry_index']}: {order['order_type']} "
            f"price={order['planned_price']:.10g} volume={order['volume']:.8g} "
            f"risk={order['risk_money']:.2f}"
        )
    for reason in decision["reasons"]:
        print(f"BLOCK {reason['code']}: {reason['message']}")
    for warning in decision["warnings"]:
        print(f"WARN {warning['code']}: {warning['message']}")
    print(f"Output: {args.output_dir / 'latest_decision.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
