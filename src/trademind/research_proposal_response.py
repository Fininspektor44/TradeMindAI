"""Strict provider-neutral contract for unreviewed research proposals.

This module validates external model output only. A validated response is not a
hypothesis, a registry entry, an experiment manifest, a scientific result, or a
trading authorization. A later trusted execution layer must attach provider and
request provenance and decide whether any proposal should proceed to review.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Self

from trademind.signal_statistics_agent_packet import SignalStatisticsPacketV2
from trademind.signal_statistics_orchestrator_bridge import RESEARCH_PROPOSAL_OUTPUT_KIND
from trademind.signal_statistics_provenance import (
    ProvenanceError,
    canonical_json_bytes,
    freeze_json_object,
    parse_json,
)


RESEARCH_PROPOSAL_RESPONSE_SCHEMA_VERSION = "research-proposal-response-v1"
RESEARCH_PROPOSAL_RESPONSE_KIND = RESEARCH_PROPOSAL_OUTPUT_KIND

# Domain limits are intentionally much smaller than the shared 256 KiB canonical
# provenance boundary. They cap model-authored prose before any future artifact or
# provider-result persistence.
MAX_RESEARCH_PROPOSALS = 8
MAX_PROPOSAL_TITLE_LENGTH = 160
MAX_PROPOSAL_RATIONALE_LENGTH = 1_000
MAX_FALSIFIABLE_CLAIM_LENGTH = 1_000
MAX_PROPOSED_TEST_LENGTH = 1_200
MAX_REJECTION_CONDITION_LENGTH = 800

_CANDIDATE_ID = re.compile(r"^ssc-v2-[0-9a-f]{64}$")
_CANDIDATE_ID_LENGTH = len("ssc-v2-") + 64
_TEXT_PATTERN = (
    r"^(?![\s\S]*[\u0000-\u001F\u007F-\u009F\uD800-\uDFFF])"
    r"(?=[\s\S]*[^\s\uFEFF])[\s\S]*$"
)
_TOP_LEVEL_FIELDS = frozenset({"schema_version", "response_kind", "proposals"})
_PROPOSAL_FIELDS = frozenset(
    {
        "candidate_id",
        "title",
        "rationale",
        "falsifiable_claim",
        "proposed_test",
        "rejection_condition",
        "confidence",
    }
)


class ResearchProposalResponseError(ValueError):
    """Raised when an unreviewed proposal response violates the v1 contract."""


class ResearchProposalConfidence(StrEnum):
    """External model self-assessment, not probability or statistical confidence."""

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


def _require_exact_fields(
    value: Mapping[str, object],
    *,
    required: frozenset[str],
    field_name: str,
) -> None:
    actual = frozenset(value)
    missing = sorted(required - actual)
    unknown = sorted(actual - required)
    if missing:
        raise ResearchProposalResponseError(
            f"{field_name} is missing required fields: {', '.join(missing)}"
        )
    if unknown:
        raise ResearchProposalResponseError(
            f"{field_name} contains unknown fields: {', '.join(unknown)}"
        )


def _required_text(value: object, *, field_name: str, max_length: int) -> str:
    if type(value) is not str:
        raise ResearchProposalResponseError(f"{field_name} must be an exact string")
    if len(value) > max_length:
        raise ResearchProposalResponseError(f"{field_name} exceeds maximum length {max_length}")
    if any(ord(character) <= 0x1F or 0x7F <= ord(character) <= 0x9F for character in value):
        raise ResearchProposalResponseError(f"{field_name} must not contain control characters")
    if not value or not any(
        not (character.isspace() or character == "\ufeff") for character in value
    ):
        raise ResearchProposalResponseError(f"{field_name} must contain non-whitespace text")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ResearchProposalResponseError(f"{field_name} must be valid UTF-8 text") from exc
    return value


def _candidate_id(value: object) -> str:
    if type(value) is not str or _CANDIDATE_ID.fullmatch(value) is None:
        raise ResearchProposalResponseError(
            "candidate_id must be ssc-v2- followed by 64 lowercase hexadecimal characters"
        )
    return value


def _frozen_object(value: object, *, field_name: str) -> Mapping[str, object]:
    try:
        frozen = freeze_json_object(value, field_name=field_name)
    except ProvenanceError as exc:
        raise ResearchProposalResponseError(str(exc)) from exc
    return frozen


@dataclass(frozen=True, slots=True)
class ResearchProposalV1:
    """One bounded, falsifiable, unreviewed proposal bound to a Packet candidate."""

    candidate_id: str
    title: str
    rationale: str
    falsifiable_claim: str
    proposed_test: str
    rejection_condition: str
    confidence: ResearchProposalConfidence

    def __post_init__(self) -> None:
        _candidate_id(self.candidate_id)
        _required_text(
            self.title,
            field_name="title",
            max_length=MAX_PROPOSAL_TITLE_LENGTH,
        )
        _required_text(
            self.rationale,
            field_name="rationale",
            max_length=MAX_PROPOSAL_RATIONALE_LENGTH,
        )
        _required_text(
            self.falsifiable_claim,
            field_name="falsifiable_claim",
            max_length=MAX_FALSIFIABLE_CLAIM_LENGTH,
        )
        _required_text(
            self.proposed_test,
            field_name="proposed_test",
            max_length=MAX_PROPOSED_TEST_LENGTH,
        )
        _required_text(
            self.rejection_condition,
            field_name="rejection_condition",
            max_length=MAX_REJECTION_CONDITION_LENGTH,
        )
        if not isinstance(self.confidence, ResearchProposalConfidence):
            raise ResearchProposalResponseError(
                "confidence must be an exact ResearchProposalConfidence value"
            )
        try:
            canonical_json_bytes(self.to_payload())
        except ProvenanceError as exc:
            raise ResearchProposalResponseError(str(exc)) from exc

    def to_payload(self) -> dict[str, str]:
        return {
            "candidate_id": self.candidate_id,
            "title": self.title,
            "rationale": self.rationale,
            "falsifiable_claim": self.falsifiable_claim,
            "proposed_test": self.proposed_test,
            "rejection_condition": self.rejection_condition,
            "confidence": self.confidence.value,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> Self:
        frozen = _frozen_object(payload, field_name="research_proposal_v1")
        _require_exact_fields(
            frozen,
            required=_PROPOSAL_FIELDS,
            field_name="research proposal v1",
        )
        confidence_value = frozen["confidence"]
        if type(confidence_value) is not str:
            raise ResearchProposalResponseError("confidence must be an exact string")
        try:
            confidence = ResearchProposalConfidence(confidence_value)
        except ValueError as exc:
            raise ResearchProposalResponseError(
                f"unsupported qualitative confidence: {confidence_value!r}"
            ) from exc
        return cls(
            candidate_id=_candidate_id(frozen["candidate_id"]),
            title=_required_text(
                frozen["title"],
                field_name="title",
                max_length=MAX_PROPOSAL_TITLE_LENGTH,
            ),
            rationale=_required_text(
                frozen["rationale"],
                field_name="rationale",
                max_length=MAX_PROPOSAL_RATIONALE_LENGTH,
            ),
            falsifiable_claim=_required_text(
                frozen["falsifiable_claim"],
                field_name="falsifiable_claim",
                max_length=MAX_FALSIFIABLE_CLAIM_LENGTH,
            ),
            proposed_test=_required_text(
                frozen["proposed_test"],
                field_name="proposed_test",
                max_length=MAX_PROPOSED_TEST_LENGTH,
            ),
            rejection_condition=_required_text(
                frozen["rejection_condition"],
                field_name="rejection_condition",
                max_length=MAX_REJECTION_CONDITION_LENGTH,
            ),
            confidence=confidence,
        )


@dataclass(frozen=True, slots=True)
class ResearchProposalResponseV1:
    """An immutable external-model payload of unreviewed research proposals.

    An empty proposal tuple is a legitimate abstention. The eight-item maximum is
    solely a resource bound: multiple proposals may refer to the same candidate,
    and v1 intentionally has no model-generated proposal ID or deduplication policy.
    """

    proposals: tuple[ResearchProposalV1, ...]
    schema_version: str = RESEARCH_PROPOSAL_RESPONSE_SCHEMA_VERSION
    response_kind: str = RESEARCH_PROPOSAL_RESPONSE_KIND

    def __post_init__(self) -> None:
        if type(self.schema_version) is not str or (
            self.schema_version != RESEARCH_PROPOSAL_RESPONSE_SCHEMA_VERSION
        ):
            raise ResearchProposalResponseError("unsupported research proposal response version")
        if type(self.response_kind) is not str or (
            self.response_kind != RESEARCH_PROPOSAL_RESPONSE_KIND
        ):
            raise ResearchProposalResponseError("unsupported research proposal response kind")
        if type(self.proposals) is not tuple or any(
            type(proposal) is not ResearchProposalV1 for proposal in self.proposals
        ):
            raise ResearchProposalResponseError(
                "proposals must be an immutable tuple of ResearchProposalV1 values"
            )
        if len(self.proposals) > MAX_RESEARCH_PROPOSALS:
            raise ResearchProposalResponseError(
                f"proposals exceeds maximum items {MAX_RESEARCH_PROPOSALS}"
            )
        try:
            canonical_json_bytes(self.to_payload())
        except ProvenanceError as exc:
            raise ResearchProposalResponseError(str(exc)) from exc

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "response_kind": self.response_kind,
            "proposals": [proposal.to_payload() for proposal in self.proposals],
        }

    def canonical_bytes(self) -> bytes:
        try:
            return canonical_json_bytes(self.to_payload())
        except ProvenanceError as exc:  # Defensive: construction already validated this payload.
            raise ResearchProposalResponseError(str(exc)) from exc

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> Self:
        frozen = _frozen_object(payload, field_name="research_proposal_response_v1")
        _require_exact_fields(
            frozen,
            required=_TOP_LEVEL_FIELDS,
            field_name="research proposal response v1",
        )
        schema_version = frozen["schema_version"]
        response_kind = frozen["response_kind"]
        proposals = frozen["proposals"]
        if type(schema_version) is not str:
            raise ResearchProposalResponseError("schema_version must be an exact string")
        if type(response_kind) is not str:
            raise ResearchProposalResponseError("response_kind must be an exact string")
        if type(proposals) is not tuple:
            raise ResearchProposalResponseError("proposals must be a JSON array")
        if len(proposals) > MAX_RESEARCH_PROPOSALS:
            raise ResearchProposalResponseError(
                f"proposals exceeds maximum items {MAX_RESEARCH_PROPOSALS}"
            )
        parsed: list[ResearchProposalV1] = []
        for index, proposal in enumerate(proposals):
            if not isinstance(proposal, Mapping):
                raise ResearchProposalResponseError(f"proposals[{index}] must be a JSON object")
            parsed.append(ResearchProposalV1.from_payload(proposal))
        return cls(
            proposals=tuple(parsed),
            schema_version=schema_version,
            response_kind=response_kind,
        )


def parse_research_proposal_response_v1(
    encoded: str | bytes,
) -> ResearchProposalResponseV1:
    """Parse one exact canonical wire payload and apply local authoritative validation."""
    if type(encoded) is bytes:
        exact_bytes = encoded
    elif type(encoded) is str:
        try:
            exact_bytes = encoded.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ResearchProposalResponseError("response must be valid UTF-8") from exc
    else:
        raise ResearchProposalResponseError("response wire payload must be exact str or bytes")
    try:
        parsed = parse_json(encoded)
    except ProvenanceError as exc:
        raise ResearchProposalResponseError(str(exc)) from exc
    if not isinstance(parsed, Mapping):
        raise ResearchProposalResponseError("research proposal response root must be a JSON object")
    response = ResearchProposalResponseV1.from_payload(parsed)
    if response.canonical_bytes() != exact_bytes:
        raise ResearchProposalResponseError(
            "research proposal response wire payload must use canonical JSON bytes"
        )
    return response


def validate_research_proposals_for_packet(
    response: ResearchProposalResponseV1,
    packet: SignalStatisticsPacketV2,
) -> ResearchProposalResponseV1:
    """Bind locally validated proposal candidate IDs to one authoritative Packet v2."""
    if type(response) is not ResearchProposalResponseV1:
        raise ResearchProposalResponseError("response must be ResearchProposalResponseV1")
    if type(packet) is not SignalStatisticsPacketV2:
        raise ResearchProposalResponseError("packet must be a verified SignalStatisticsPacketV2")

    projection = packet.semantic_projection()
    expected_output = projection.get("expected_output")
    if (
        projection.get("read_only") is not True
        or projection.get("orders_enabled") is not False
        or projection.get("broker_calls_allowed") is not False
        or projection.get("execution_allowed") is not False
        or projection.get("live_trading_authorized") is not False
        or not isinstance(expected_output, Mapping)
        or expected_output.get("kind") != RESEARCH_PROPOSAL_RESPONSE_KIND
        or expected_output.get("machine_readable_required") is not True
        or expected_output.get("trading_authorization") is not False
    ):
        raise ResearchProposalResponseError(
            "Packet v2 does not satisfy the research-only proposal output contract"
        )

    packet_candidate_ids = {binding["candidate_id"] for binding in packet.candidate_bindings}
    for proposal in response.proposals:
        if proposal.candidate_id not in packet_candidate_ids:
            raise ResearchProposalResponseError(
                f"proposal candidate_id is not present in verified Packet v2: "
                f"{proposal.candidate_id}"
            )
    return response


def _text_schema(max_length: int) -> dict[str, object]:
    return {
        "type": "string",
        "minLength": 1,
        "maxLength": max_length,
        "pattern": _TEXT_PATTERN,
    }


def research_proposal_response_json_schema() -> dict[str, object]:
    """Return a fresh deterministic provider-neutral JSON Schema for this contract.

    The text pattern uses ECMAScript-compatible lookaheads to reject C0/C1 controls
    and unpaired UTF-16 surrogates while requiring content other than whitespace or
    U+FEFF. Candidate length bounds make the full identity match independent of
    ``$`` terminal-newline behavior.
    """
    proposal_schema = {
        "type": "object",
        "additionalProperties": False,
        "required": sorted(_PROPOSAL_FIELDS),
        "properties": {
            "candidate_id": {
                "type": "string",
                "pattern": _CANDIDATE_ID.pattern,
                "minLength": _CANDIDATE_ID_LENGTH,
                "maxLength": _CANDIDATE_ID_LENGTH,
            },
            "title": _text_schema(MAX_PROPOSAL_TITLE_LENGTH),
            "rationale": _text_schema(MAX_PROPOSAL_RATIONALE_LENGTH),
            "falsifiable_claim": _text_schema(MAX_FALSIFIABLE_CLAIM_LENGTH),
            "proposed_test": _text_schema(MAX_PROPOSED_TEST_LENGTH),
            "rejection_condition": _text_schema(MAX_REJECTION_CONDITION_LENGTH),
            "confidence": {
                "type": "string",
                "enum": [confidence.value for confidence in ResearchProposalConfidence],
            },
        },
    }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "ResearchProposalResponseV1",
        "description": (
            "Unreviewed external-model research proposals; not hypotheses, validation, "
            "scientific facts, or trading authorization."
        ),
        "type": "object",
        "additionalProperties": False,
        "required": sorted(_TOP_LEVEL_FIELDS),
        "properties": {
            "schema_version": {
                "type": "string",
                "const": RESEARCH_PROPOSAL_RESPONSE_SCHEMA_VERSION,
            },
            "response_kind": {
                "type": "string",
                "const": RESEARCH_PROPOSAL_RESPONSE_KIND,
            },
            "proposals": {
                "type": "array",
                "minItems": 0,
                "maxItems": MAX_RESEARCH_PROPOSALS,
                "items": proposal_schema,
            },
        },
    }
