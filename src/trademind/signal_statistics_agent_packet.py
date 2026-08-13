"""Build a read-only research packet from signal statistics for AI-agent review."""

from __future__ import annotations

import argparse
import io
import json
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Mapping, Self, Sequence

from trademind.orchestrator.artifact_store import ArtifactRef, ArtifactStore
from trademind.signal_statistics_provenance import (
    CandidateContentV2,
    PacketContentHashProjection,
    ProvenanceError,
    canonical_json_bytes,
    freeze_json_object,
    packet_content_hash,
    parse_json,
    sha256_bytes,
    validate_sha256_ref,
)
from trademind.signal_statistics_report import (
    REPORT_SCHEMA_VERSION,
    REPORT_V2_SCHEMA_VERSION,
    SignalStatisticsReportV2,
    load_report_v2,
)

AGENT_PACKET_SCHEMA_VERSION = "signal-statistics-agent-packet-v1"
PACKET_V2_SCHEMA_VERSION = "signal-statistics-packet-v2"
PACKET_V2_MEDIA_TYPE = "application/vnd.trademind.signal-statistics-packet-v2+json"

_ELIGIBLE_STATUSES = ("RESEARCH_CANDIDATE", "VALIDATED")
_SELECTION_METHOD = "report-candidate-identity-order-v1"
_DECISION_SCOPE = "research_hypotheses_only"
_PROHIBITED_ACTIONS = (
    "change_signal_weights",
    "change_signal_generation_logic",
    "enable_orders",
    "call_broker",
    "execute_trades",
    "authorize_live_trading",
    "publish_or_sell_signals",
)
_ANALYSIS_QUESTIONS = (
    "Which market conditions strengthen or weaken each candidate?",
    "Does the effect persist across time, symbols, horizons, and market regimes?",
    "What falsifiable hypothesis should be tested next on untouched data?",
    "What evidence would cause this candidate to be rejected?",
)
_EXPECTED_OUTPUT = MappingProxyType(
    {
        "kind": "falsifiable_research_hypothesis_proposals",
        "machine_readable_required": True,
        "trading_authorization": False,
    }
)


def _eligible(pattern: Mapping[str, object]) -> bool:
    status = str(pattern.get("status", ""))
    if status not in {"RESEARCH_CANDIDATE", "VALIDATED"}:
        return False

    ci95 = pattern.get("mean_ci95")
    early = pattern.get("early")
    late = pattern.get("late")
    if not isinstance(ci95, list) or len(ci95) != 2:
        return False
    if not isinstance(early, dict) or not isinstance(late, dict):
        return False

    try:
        ci_low = float(ci95[0])
        profit_factor = float(pattern.get("profit_factor_atr", 0.0))
        avg_net_atr = float(pattern.get("avg_net_atr", 0.0))
        early_avg = float(early.get("avg_net_atr", 0.0))
        late_avg = float(late.get("avg_net_atr", 0.0))
    except (TypeError, ValueError):
        return False

    return (
        ci_low > 0.0
        and profit_factor > 1.0
        and avg_net_atr > 0.0
        and early_avg > 0.0
        and late_avg > 0.0
    )


def _rank_key(pattern: Mapping[str, object]) -> tuple[int, float, float, int]:
    status_rank = 1 if str(pattern.get("status")) == "VALIDATED" else 0
    ci95 = pattern.get("mean_ci95")
    ci_low = float(ci95[0]) if isinstance(ci95, list) and ci95 else float("-inf")
    profit_factor = float(pattern.get("profit_factor_atr", 0.0))
    trades = int(pattern.get("trades", 0))
    return status_rank, ci_low, profit_factor, trades


def build_agent_packet(
    report: Mapping[str, object],
    *,
    max_candidates: int = 10,
) -> dict[str, object]:
    """Return a conservative, machine-readable packet for research-only AI analysis."""
    if max_candidates < 1:
        raise ValueError("max_candidates must be positive")
    if report.get("schema_version") != REPORT_SCHEMA_VERSION:
        raise ValueError("unsupported signal statistics report schema")
    if report.get("read_only") is not True or report.get("orders_enabled") is not False:
        raise ValueError("signal statistics report must be read-only with orders disabled")

    raw_patterns = report.get("patterns", [])
    if not isinstance(raw_patterns, list):
        raise ValueError("patterns must be a list")

    eligible = [item for item in raw_patterns if isinstance(item, dict) and _eligible(item)]
    eligible.sort(key=_rank_key, reverse=True)
    selected = eligible[:max_candidates]

    return {
        "schema_version": AGENT_PACKET_SCHEMA_VERSION,
        "source_report_schema_version": report.get("schema_version"),
        "source_generated_at": report.get("generated_at"),
        "read_only": True,
        "orders_enabled": False,
        "decision_scope": "research_hypotheses_only",
        "candidate_count": len(selected),
        "selection_policy": {
            "allowed_statuses": ["RESEARCH_CANDIDATE", "VALIDATED"],
            "require_ci95_lower_above_zero": True,
            "require_positive_early_and_late_avg_net_atr": True,
            "require_profit_factor_atr_above_one": True,
            "max_candidates": max_candidates,
        },
        "prohibited_actions": [
            "change_signal_weights",
            "change_signal_generation_logic",
            "enable_orders",
            "publish_or_sell_signals",
        ],
        "analysis_questions": [
            "Which market conditions strengthen or weaken each candidate?",
            "Does the effect persist across time, symbols, horizons, and market regimes?",
            "What falsifiable hypothesis should be tested next on untouched data?",
            "What evidence would cause this candidate to be rejected?",
        ],
        "candidates": selected,
    }


class SignalStatisticsPacketV2Error(ProvenanceError):
    """Raised when a Packet v2 violates its strict research-only contract."""


def _require_exact_fields(
    payload: Mapping[str, object],
    *,
    required: frozenset[str],
    name: str,
) -> None:
    fields = frozenset(payload)
    missing = required - fields
    if missing:
        raise SignalStatisticsPacketV2Error(
            f"{name} is missing fields: {', '.join(sorted(missing))}"
        )
    unknown = fields - required
    if unknown:
        raise SignalStatisticsPacketV2Error(
            f"{name} contains unknown fields: {', '.join(sorted(unknown))}"
        )


def _machine_identifier(value: object, *, field_name: str) -> str:
    if type(value) is not str or not value or len(value) > 128:
        raise SignalStatisticsPacketV2Error(
            f"{field_name} must be a non-empty bounded exact string"
        )
    try:
        encoded = value.encode("ascii")
    except UnicodeEncodeError as exc:
        raise SignalStatisticsPacketV2Error(f"{field_name} must use ASCII") from exc
    if any(
        not (
            ord("A") <= byte <= ord("Z")
            or ord("a") <= byte <= ord("z")
            or ord("0") <= byte <= ord("9")
            or byte in b"_.:-"
        )
        for byte in encoded
    ):
        raise SignalStatisticsPacketV2Error(f"{field_name} is not a stable machine identifier")
    return value


def _candidate_binding(candidate: CandidateContentV2) -> dict[str, object]:
    definition = candidate.candidate_definition
    return {
        "candidate_id": candidate.candidate_id,
        "candidate_content_hash": candidate.content_hash,
        "symbol": definition.symbol,
        "timeframe": definition.timeframe,
        "feature": definition.feature,
        "horizon": definition.horizon,
        "status": candidate.status,
    }


@dataclass(frozen=True, slots=True, init=False)
class SignalStatisticsPacketV2:
    """Structured, immutable and provenance-bound AI research envelope.

    Every field in Packet v2 is semantic. The packet carries only candidate
    identities and bounded descriptors derived from a verified Report v2; it
    never duplicates mutable candidate statistics, filesystem paths, credentials,
    provider prompts, or trading authorization.
    """

    report_semantic_hash: str
    report_artifact_hash_ref: str
    candidate_bindings: tuple[Mapping[str, object], ...]
    schema_version: str = PACKET_V2_SCHEMA_VERSION
    packet_semantic_hash: str = field(init=False)

    def __init__(self) -> None:
        raise SignalStatisticsPacketV2Error(
            "use build_packet_v2_from_artifact() or verify_packet_v2()"
        )

    @classmethod
    def _from_validated_claims(
        cls,
        *,
        report_semantic_hash: str,
        report_artifact_hash_ref: str,
        candidate_bindings: tuple[Mapping[str, object], ...],
        schema_version: str = PACKET_V2_SCHEMA_VERSION,
    ) -> Self:
        packet = object.__new__(cls)
        object.__setattr__(packet, "report_semantic_hash", report_semantic_hash)
        object.__setattr__(packet, "report_artifact_hash_ref", report_artifact_hash_ref)
        object.__setattr__(packet, "candidate_bindings", candidate_bindings)
        object.__setattr__(packet, "schema_version", schema_version)
        packet.__post_init__()
        return packet

    def __post_init__(self) -> None:
        if type(self.schema_version) is not str or self.schema_version != PACKET_V2_SCHEMA_VERSION:
            raise SignalStatisticsPacketV2Error("unsupported signal statistics packet version")
        validate_sha256_ref(self.report_semantic_hash)
        validate_sha256_ref(self.report_artifact_hash_ref)
        if type(self.candidate_bindings) is not tuple:
            raise SignalStatisticsPacketV2Error("candidate_bindings must be an immutable tuple")
        if not self.candidate_bindings:
            raise SignalStatisticsPacketV2Error("Packet v2 requires at least one candidate")

        parsed: list[Mapping[str, object]] = []
        seen: set[str] = set()
        for index, binding in enumerate(self.candidate_bindings):
            parsed_binding = self._parse_candidate_binding(binding, index=index)
            candidate_id = parsed_binding["candidate_id"]
            if candidate_id in seen:
                raise SignalStatisticsPacketV2Error(f"duplicate candidate identity: {candidate_id}")
            seen.add(candidate_id)
            parsed.append(parsed_binding)
        parsed.sort(key=lambda item: item["candidate_id"])
        object.__setattr__(self, "candidate_bindings", tuple(parsed))

        projection = PacketContentHashProjection(self.semantic_projection())
        object.__setattr__(self, "packet_semantic_hash", packet_content_hash(projection))
        canonical_json_bytes(self.to_payload())

    @staticmethod
    def _parse_candidate_binding(
        value: object,
        *,
        index: int,
    ) -> Mapping[str, object]:
        if not isinstance(value, Mapping):
            raise SignalStatisticsPacketV2Error(
                f"candidate_bindings[{index}] must be a JSON object"
            )
        binding = freeze_json_object(value, field_name=f"candidate_bindings[{index}]")
        _require_exact_fields(
            binding,
            required=frozenset(
                {
                    "candidate_id",
                    "candidate_content_hash",
                    "symbol",
                    "timeframe",
                    "feature",
                    "horizon",
                    "status",
                }
            ),
            name=f"candidate_bindings[{index}]",
        )
        candidate_id = binding["candidate_id"]
        if (
            type(candidate_id) is not str
            or not candidate_id.startswith("ssc-v2-")
            or len(candidate_id) != len("ssc-v2-") + 64
        ):
            raise SignalStatisticsPacketV2Error(
                f"candidate_bindings[{index}].candidate_id is malformed"
            )
        validate_sha256_ref(f"sha256:{candidate_id.removeprefix('ssc-v2-')}")
        validate_sha256_ref(binding["candidate_content_hash"])
        for field_name in ("symbol", "timeframe", "feature", "status"):
            _machine_identifier(
                binding[field_name],
                field_name=f"candidate_bindings[{index}].{field_name}",
            )
        if binding["status"] not in _ELIGIBLE_STATUSES:
            raise SignalStatisticsPacketV2Error(
                f"candidate_bindings[{index}].status is not allowed by selection policy"
            )
        if type(binding["horizon"]) is not int or binding["horizon"] < 1:
            raise SignalStatisticsPacketV2Error(
                f"candidate_bindings[{index}].horizon must be an exact positive integer"
            )
        return binding

    def semantic_projection(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "read_only": True,
            "orders_enabled": False,
            "broker_calls_allowed": False,
            "execution_allowed": False,
            "live_trading_authorized": False,
            "decision_scope": _DECISION_SCOPE,
            "report_binding": {
                "report_schema_version": REPORT_V2_SCHEMA_VERSION,
                "report_semantic_hash": self.report_semantic_hash,
                "report_artifact_hash_ref": self.report_artifact_hash_ref,
            },
            "selection_policy": {
                "method": _SELECTION_METHOD,
                "allowed_statuses": list(_ELIGIBLE_STATUSES),
            },
            "candidate_bindings": [dict(binding) for binding in self.candidate_bindings],
            "prohibited_actions": list(_PROHIBITED_ACTIONS),
            "analysis_questions": list(_ANALYSIS_QUESTIONS),
            "expected_output": dict(_EXPECTED_OUTPUT),
        }

    def to_payload(self) -> dict[str, object]:
        payload = self.semantic_projection()
        payload["packet_semantic_hash"] = self.packet_semantic_hash
        return payload

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_payload())

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> SignalStatisticsPacketV2:
        frozen = freeze_json_object(payload, field_name="signal_statistics_packet_v2")
        _require_exact_fields(
            frozen,
            required=frozenset(
                {
                    "schema_version",
                    "packet_semantic_hash",
                    "read_only",
                    "orders_enabled",
                    "broker_calls_allowed",
                    "execution_allowed",
                    "live_trading_authorized",
                    "decision_scope",
                    "report_binding",
                    "selection_policy",
                    "candidate_bindings",
                    "prohibited_actions",
                    "analysis_questions",
                    "expected_output",
                }
            ),
            name="signal statistics packet v2",
        )
        if frozen["schema_version"] != PACKET_V2_SCHEMA_VERSION:
            raise SignalStatisticsPacketV2Error("unsupported signal statistics packet version")
        for field_name, expected in (
            ("read_only", True),
            ("orders_enabled", False),
            ("broker_calls_allowed", False),
            ("execution_allowed", False),
            ("live_trading_authorized", False),
        ):
            if frozen[field_name] is not expected:
                raise SignalStatisticsPacketV2Error(f"packet safety invariant failed: {field_name}")
        if frozen["decision_scope"] != _DECISION_SCOPE:
            raise SignalStatisticsPacketV2Error("packet decision_scope is unsupported")

        report_binding = frozen["report_binding"]
        selection_policy = frozen["selection_policy"]
        candidate_bindings = frozen["candidate_bindings"]
        prohibited_actions = frozen["prohibited_actions"]
        analysis_questions = frozen["analysis_questions"]
        expected_output = frozen["expected_output"]
        for name, nested in (
            ("report_binding", report_binding),
            ("selection_policy", selection_policy),
            ("expected_output", expected_output),
        ):
            if not isinstance(nested, Mapping):
                raise SignalStatisticsPacketV2Error(f"{name} must be a JSON object")
        if type(candidate_bindings) is not tuple:
            raise SignalStatisticsPacketV2Error("candidate_bindings must be a JSON array")
        if type(prohibited_actions) is not tuple or type(analysis_questions) is not tuple:
            raise SignalStatisticsPacketV2Error(
                "packet instruction collections must be JSON arrays"
            )

        _require_exact_fields(
            report_binding,
            required=frozenset(
                {
                    "report_schema_version",
                    "report_semantic_hash",
                    "report_artifact_hash_ref",
                }
            ),
            name="report_binding",
        )
        _require_exact_fields(
            selection_policy,
            required=frozenset({"method", "allowed_statuses"}),
            name="selection_policy",
        )
        _require_exact_fields(
            expected_output,
            required=frozenset({"kind", "machine_readable_required", "trading_authorization"}),
            name="expected_output",
        )
        if report_binding["report_schema_version"] != REPORT_V2_SCHEMA_VERSION:
            raise SignalStatisticsPacketV2Error("report binding schema version is unsupported")
        if selection_policy != freeze_json_object(
            {
                "method": _SELECTION_METHOD,
                "allowed_statuses": list(_ELIGIBLE_STATUSES),
            }
        ):
            raise SignalStatisticsPacketV2Error("selection policy does not match Packet v2")
        if prohibited_actions != _PROHIBITED_ACTIONS:
            raise SignalStatisticsPacketV2Error("prohibited actions do not match Packet v2")
        if analysis_questions != _ANALYSIS_QUESTIONS:
            raise SignalStatisticsPacketV2Error("analysis questions do not match Packet v2")
        if expected_output != freeze_json_object(_EXPECTED_OUTPUT):
            raise SignalStatisticsPacketV2Error("expected output does not match Packet v2")

        claimed_packet_hash = validate_sha256_ref(frozen["packet_semantic_hash"])
        packet = cls._from_validated_claims(
            schema_version=frozen["schema_version"],
            report_semantic_hash=report_binding["report_semantic_hash"],
            report_artifact_hash_ref=report_binding["report_artifact_hash_ref"],
            candidate_bindings=tuple(candidate_bindings),
        )
        if packet.packet_semantic_hash != claimed_packet_hash:
            raise SignalStatisticsPacketV2Error("packet semantic hash mismatch")
        if canonical_json_bytes(frozen) != packet.canonical_bytes():
            raise SignalStatisticsPacketV2Error("packet payload is not canonical Packet v2")
        return packet


def _eligible_report_candidates(
    report: SignalStatisticsReportV2,
) -> tuple[CandidateContentV2, ...]:
    return tuple(
        candidate for candidate in report.candidates if candidate.status in _ELIGIBLE_STATUSES
    )


def build_packet_v2_from_artifact(
    report_artifact_hash_ref: str,
    *,
    artifact_store: ArtifactStore,
) -> SignalStatisticsPacketV2:
    """Build only from a Report v2 artifact fully verified by its CAS-backed loader."""
    validated_ref = validate_sha256_ref(report_artifact_hash_ref)
    report = load_report_v2(validated_ref, artifact_store=artifact_store)
    candidates = _eligible_report_candidates(report)
    if not candidates:
        raise SignalStatisticsPacketV2Error("verified Report v2 has no eligible candidates")
    return SignalStatisticsPacketV2._from_validated_claims(
        report_semantic_hash=report.report_semantic_hash,
        report_artifact_hash_ref=validated_ref,
        candidate_bindings=tuple(_candidate_binding(candidate) for candidate in candidates),
    )


def verify_packet_v2(encoded: str | bytes) -> SignalStatisticsPacketV2:
    """Verify canonical wire data and Packet v2 semantic provenance claims only.

    This function does not prove Packet or Report CAS persistence. Use
    :func:`load_packet_v2` for full Packet CAS integrity plus upstream Report v2
    artifact and semantic binding verification.
    """
    if type(encoded) is bytes:
        exact_bytes = encoded
    elif type(encoded) is str:
        try:
            exact_bytes = encoded.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise SignalStatisticsPacketV2Error("packet must be valid UTF-8") from exc
    else:
        raise SignalStatisticsPacketV2Error("packet wire payload must be exact str or bytes")
    parsed = parse_json(exact_bytes)
    if not isinstance(parsed, Mapping):
        raise SignalStatisticsPacketV2Error("Packet v2 root must be a JSON object")
    if canonical_json_bytes(parsed) != exact_bytes:
        raise SignalStatisticsPacketV2Error("Packet v2 wire payload must use canonical JSON bytes")
    return SignalStatisticsPacketV2.from_payload(parsed)


def persist_packet_v2(
    packet: SignalStatisticsPacketV2,
    *,
    artifact_store: ArtifactStore,
) -> ArtifactRef:
    """Persist exact canonical Packet v2 bytes through Verified CAS v1."""
    if type(packet) is not SignalStatisticsPacketV2:
        raise SignalStatisticsPacketV2Error("packet must be SignalStatisticsPacketV2")
    _verify_report_binding(packet, artifact_store=artifact_store)
    exact_bytes = packet.canonical_bytes()
    artifact = artifact_store.import_snapshot(
        io.BytesIO(exact_bytes),
        media_type=PACKET_V2_MEDIA_TYPE,
    )
    if artifact.hash_ref != sha256_bytes(exact_bytes):
        raise SignalStatisticsPacketV2Error("Verified CAS returned an unexpected packet hash")
    return artifact


def _verify_report_binding(
    packet: SignalStatisticsPacketV2,
    *,
    artifact_store: ArtifactStore,
) -> None:
    report = load_report_v2(packet.report_artifact_hash_ref, artifact_store=artifact_store)
    if report.report_semantic_hash != packet.report_semantic_hash:
        raise SignalStatisticsPacketV2Error("Packet v2 report semantic binding mismatch")
    expected_candidates = tuple(
        _candidate_binding(candidate) for candidate in _eligible_report_candidates(report)
    )
    if tuple(packet.candidate_bindings) != tuple(
        sorted(expected_candidates, key=lambda item: item["candidate_id"])
    ):
        raise SignalStatisticsPacketV2Error("Packet v2 candidate binding mismatch")


def load_packet_v2(
    packet_artifact_hash_ref: str,
    *,
    artifact_store: ArtifactStore,
) -> SignalStatisticsPacketV2:
    """Fully verify Packet CAS bytes, semantics, and its Report v2 binding."""
    validated_ref = validate_sha256_ref(packet_artifact_hash_ref)
    resolved = artifact_store.resolve_verified(
        validated_ref,
        expected_media_type=PACKET_V2_MEDIA_TYPE,
    )
    if resolved.hash_ref != validated_ref:
        raise SignalStatisticsPacketV2Error("resolved Packet v2 identity mismatch")
    exact_bytes = artifact_store.read_verified(
        validated_ref,
        expected_media_type=PACKET_V2_MEDIA_TYPE,
    )
    packet = verify_packet_v2(exact_bytes)
    _verify_report_binding(packet, artifact_store=artifact_store)
    return packet


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Create a conservative read-only AI research packet from a signal statistics report"
    )
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-candidates", type=int, default=10)
    args = parser.parse_args(argv)

    if not args.report.is_file():
        parser.error(f"report not found: {args.report}")

    report = json.loads(args.report.read_text(encoding="utf-8"))
    packet = build_agent_packet(report, max_candidates=args.max_candidates)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(packet, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
