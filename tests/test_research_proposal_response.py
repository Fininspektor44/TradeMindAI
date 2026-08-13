from __future__ import annotations

import copy
import re
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from trademind.orchestrator.artifact_store import ArtifactStore
from trademind.research_proposal_response import (
    MAX_FALSIFIABLE_CLAIM_LENGTH,
    MAX_PROPOSAL_RATIONALE_LENGTH,
    MAX_PROPOSAL_TITLE_LENGTH,
    MAX_PROPOSED_TEST_LENGTH,
    MAX_REJECTION_CONDITION_LENGTH,
    MAX_RESEARCH_PROPOSALS,
    RESEARCH_PROPOSAL_RESPONSE_KIND,
    RESEARCH_PROPOSAL_RESPONSE_SCHEMA_VERSION,
    ResearchProposalConfidence,
    ResearchProposalResponseError,
    ResearchProposalResponseV1,
    ResearchProposalV1,
    parse_research_proposal_response_v1,
    research_proposal_response_json_schema,
    validate_research_proposals_for_packet,
)
from trademind.signal_statistics_agent_packet import (
    build_packet_v2_from_artifact,
    persist_packet_v2,
)
from trademind.signal_statistics_provenance import (
    CandidateContentV2,
    CandidateDefinitionV2,
    CodeProvenance,
)
from trademind.signal_statistics_report import build_report_v2, persist_report_v2


_POLICY_HASH = f"sha256:{'2' * 64}"
_SOURCE_HASH = f"sha256:{'3' * 64}"


def _candidate(*, feature: str) -> CandidateContentV2:
    return CandidateContentV2(
        candidate_definition=CandidateDefinitionV2(
            source_kind="signal_journal",
            source_namespace="trademind_signal_journal",
            symbol="XAUUSD",
            timeframe="M5",
            feature=feature,
            horizon=3,
            action_scope="BUY_SELL_DIRECTIONAL",
            evaluation_method_version="signal-statistics-v2",
        ),
        evaluation_policy_hash=_POLICY_HASH,
        metrics={"trades": 24, "win_rate": 0.5},
        status="RESEARCH_CANDIDATE",
        reason_codes=("BELOW_RESEARCH_MINIMUM",),
    )


def _verified_packet(store: ArtifactStore, *, features: tuple[str, ...]):
    report = build_report_v2(
        tuple(_candidate(feature=feature) for feature in features),
        source_snapshot_hash_ref=_SOURCE_HASH,
        source_schema_version="1.1",
        code_provenance=CodeProvenance(
            producer_name="trademind",
            producer_version="research-proposal-response-test",
            git_commit="1" * 40,
            revision_source="git_worktree",
        ),
        journal_rows=24,
        generated_at="2026-08-13T12:00:00+00:00",
    )
    report_artifact = persist_report_v2(report, artifact_store=store)
    packet = build_packet_v2_from_artifact(report_artifact.hash_ref, artifact_store=store)
    packet_artifact = persist_packet_v2(packet, artifact_store=store)
    assert packet_artifact.hash_ref
    return packet


def _proposal_payload(candidate_id: str, **changes: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "candidate_id": candidate_id,
        "title": "Regime-conditioned continuation",
        "rationale": "The candidate may depend on volatility regime.",
        "falsifiable_claim": "The effect remains positive in high-volatility periods.",
        "proposed_test": "Compare predefined high- and low-volatility public-data subsets.",
        "rejection_condition": "Reject if the high-volatility effect is non-positive.",
        "confidence": "MEDIUM",
    }
    payload.update(changes)
    return payload


def _response_payload(*proposals: dict[str, object]) -> dict[str, object]:
    return {
        "schema_version": RESEARCH_PROPOSAL_RESPONSE_SCHEMA_VERSION,
        "response_kind": RESEARCH_PROPOSAL_RESPONSE_KIND,
        "proposals": list(proposals),
    }


def _candidate_id(index: int) -> str:
    return f"ssc-v2-{index:064x}"


def test_one_valid_proposal_is_immutable_and_unreviewed() -> None:
    response = ResearchProposalResponseV1.from_payload(
        _response_payload(_proposal_payload(_candidate_id(1)))
    )

    assert response.schema_version == "research-proposal-response-v1"
    assert response.response_kind == "falsifiable_research_hypothesis_proposals"
    assert response.proposals[0].confidence is ResearchProposalConfidence.MEDIUM
    assert "unreviewed" in ResearchProposalResponseV1.__doc__.lower()
    with pytest.raises(FrozenInstanceError):
        response.response_kind = "accepted"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        response.proposals[0].title = "changed"  # type: ignore[misc]


def test_multiple_independent_proposals_may_refer_to_the_same_candidate() -> None:
    response = ResearchProposalResponseV1.from_payload(
        _response_payload(
            _proposal_payload(_candidate_id(1)),
            _proposal_payload(
                _candidate_id(1),
                title="Second bounded proposal",
                falsifiable_claim="The effect is absent in low-volatility periods.",
            ),
        )
    )

    assert len(response.proposals) == 2
    assert response.proposals[0].candidate_id == response.proposals[1].candidate_id


def test_same_and_other_candidate_proposals_pass_authoritative_packet_binding(
    tmp_path: Path,
) -> None:
    packet = _verified_packet(
        ArtifactStore(tmp_path / "artifacts"),
        features=("BULLISH_BOS", "BEARISH_BOS"),
    )
    first_candidate = packet.candidate_bindings[0]["candidate_id"]
    second_candidate = packet.candidate_bindings[1]["candidate_id"]
    assert type(first_candidate) is str
    assert type(second_candidate) is str
    first = _proposal_payload(first_candidate)
    response = ResearchProposalResponseV1.from_payload(
        _response_payload(
            first,
            _proposal_payload(first_candidate, title="Independent proposal"),
            _proposal_payload(second_candidate, title="Other candidate proposal"),
            dict(first),
        )
    )

    assert [proposal.candidate_id for proposal in response.proposals] == [
        first_candidate,
        first_candidate,
        second_candidate,
        first_candidate,
    ]
    assert validate_research_proposals_for_packet(response, packet) is response


def test_empty_proposals_is_a_legitimate_abstention() -> None:
    response = ResearchProposalResponseV1.from_payload(_response_payload())

    assert response.proposals == ()
    assert parse_research_proposal_response_v1(response.canonical_bytes()) == response


def test_canonical_wire_golden_and_restart_roundtrip() -> None:
    response = ResearchProposalResponseV1.from_payload(
        _response_payload(
            _proposal_payload(
                _candidate_id(1),
                title="Bounded title",
                rationale="Bounded rationale.",
                falsifiable_claim="Effect is positive on the predefined subset.",
                proposed_test="Evaluate the predefined subset.",
                rejection_condition="Reject when effect is non-positive.",
                confidence="LOW",
            )
        )
    )
    expected = (
        b'{"proposals":[{"candidate_id":"ssc-v2-0000000000000000000000000000000000000000'
        b'000000000000000000000001","confidence":"LOW","falsifiable_claim":"Effect is '
        b'positive on the predefined subset.","proposed_test":"Evaluate the predefined '
        b'subset.","rationale":"Bounded rationale.","rejection_condition":"Reject when '
        b'effect is non-positive.","title":"Bounded title"}],"response_kind":"falsifiable_'
        b'research_hypothesis_proposals","schema_version":"research-proposal-response-v1"}'
    )

    assert response.canonical_bytes() == expected
    restored = parse_research_proposal_response_v1(expected)
    assert restored == response
    assert restored.canonical_bytes() == expected


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("schema_version", "research-proposal-response-v2", "unsupported"),
        ("response_kind", "accepted_hypotheses", "unsupported"),
        ("unknown", True, "unknown fields"),
    ),
)
def test_top_level_version_kind_and_unknown_fields_fail_closed(
    field: str,
    value: object,
    message: str,
) -> None:
    payload = _response_payload()
    payload[field] = value

    with pytest.raises(ResearchProposalResponseError, match=message):
        ResearchProposalResponseV1.from_payload(payload)


def test_missing_top_level_and_proposal_fields_are_rejected() -> None:
    top = _response_payload()
    top.pop("response_kind")
    proposal = _proposal_payload(_candidate_id(1))
    proposal.pop("falsifiable_claim")

    with pytest.raises(ResearchProposalResponseError, match="missing required fields"):
        ResearchProposalResponseV1.from_payload(top)
    with pytest.raises(ResearchProposalResponseError, match="missing required fields"):
        ResearchProposalResponseV1.from_payload(_response_payload(proposal))


@pytest.mark.parametrize(
    "field",
    [
        "order",
        "action",
        "execution",
        "broker",
        "tool",
        "status",
        "accepted",
        "validated",
    ],
)
def test_execution_trading_and_model_status_fields_are_unknown_and_rejected(
    field: str,
) -> None:
    proposal = _proposal_payload(_candidate_id(1))
    proposal[field] = "BUY"

    with pytest.raises(ResearchProposalResponseError, match="unknown fields"):
        ResearchProposalResponseV1.from_payload(_response_payload(proposal))


def test_strict_wire_rejects_duplicate_keys_nan_infinity_and_noncanonical_json() -> None:
    canonical = ResearchProposalResponseV1.from_payload(_response_payload()).canonical_bytes()
    duplicate = (
        '{"proposals":[],"response_kind":"falsifiable_research_hypothesis_proposals",'
        '"schema_version":"research-proposal-response-v1",'
        '"schema_version":"research-proposal-response-v1"}'
    )

    with pytest.raises(ResearchProposalResponseError, match="duplicate JSON key"):
        parse_research_proposal_response_v1(duplicate)
    with pytest.raises(ResearchProposalResponseError, match="non-standard JSON constant"):
        parse_research_proposal_response_v1(canonical[:-1] + b',"confidence":NaN}')
    with pytest.raises(ResearchProposalResponseError, match="non-standard JSON constant"):
        parse_research_proposal_response_v1(canonical[:-1] + b',"confidence":Infinity}')
    with pytest.raises(ResearchProposalResponseError, match="canonical JSON"):
        parse_research_proposal_response_v1(
            b'{"schema_version": "research-proposal-response-v1", "response_kind":"falsifiable_research_hypothesis_proposals","proposals":[]}'
        )


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("candidate_id", True),
        ("title", True),
        ("rationale", 1),
        ("falsifiable_claim", None),
        ("proposed_test", []),
        ("rejection_condition", {}),
        ("confidence", 1),
        ("confidence", "1"),
    ),
)
def test_exact_types_without_bool_or_numeric_string_coercion(field: str, value: object) -> None:
    proposal = _proposal_payload(_candidate_id(1))
    proposal[field] = value
    with pytest.raises(ResearchProposalResponseError):
        ResearchProposalResponseV1.from_payload(_response_payload(proposal))


@pytest.mark.parametrize(
    ("field", "limit"),
    (
        ("title", MAX_PROPOSAL_TITLE_LENGTH),
        ("rationale", MAX_PROPOSAL_RATIONALE_LENGTH),
        ("falsifiable_claim", MAX_FALSIFIABLE_CLAIM_LENGTH),
        ("proposed_test", MAX_PROPOSED_TEST_LENGTH),
        ("rejection_condition", MAX_REJECTION_CONDITION_LENGTH),
    ),
)
def test_domain_string_limits_accept_boundary_and_reject_overflow(
    field: str,
    limit: int,
) -> None:
    boundary = ResearchProposalResponseV1.from_payload(
        _response_payload(_proposal_payload(_candidate_id(1), **{field: "x" * limit}))
    )
    assert len(getattr(boundary.proposals[0], field)) == limit

    with pytest.raises(ResearchProposalResponseError, match="exceeds maximum length"):
        ResearchProposalResponseV1.from_payload(
            _response_payload(_proposal_payload(_candidate_id(1), **{field: "x" * (limit + 1)}))
        )


def test_proposal_cardinality_limit_accepts_boundary_and_rejects_overflow() -> None:
    boundary = ResearchProposalResponseV1.from_payload(
        _response_payload(
            *(
                _proposal_payload(_candidate_id(index + 1))
                for index in range(MAX_RESEARCH_PROPOSALS)
            )
        )
    )
    assert len(boundary.proposals) == MAX_RESEARCH_PROPOSALS

    with pytest.raises(ResearchProposalResponseError, match="maximum items"):
        ResearchProposalResponseV1.from_payload(
            _response_payload(
                *(
                    _proposal_payload(_candidate_id(index + 1))
                    for index in range(MAX_RESEARCH_PROPOSALS + 1)
                )
            )
        )


def test_all_domain_limits_fit_shared_multibyte_json_boundary() -> None:
    response = ResearchProposalResponseV1.from_payload(
        _response_payload(
            *(
                _proposal_payload(
                    _candidate_id(index + 1),
                    title="😀" * MAX_PROPOSAL_TITLE_LENGTH,
                    rationale="😀" * MAX_PROPOSAL_RATIONALE_LENGTH,
                    falsifiable_claim="😀" * MAX_FALSIFIABLE_CLAIM_LENGTH,
                    proposed_test="😀" * MAX_PROPOSED_TEST_LENGTH,
                    rejection_condition="😀" * MAX_REJECTION_CONDITION_LENGTH,
                )
                for index in range(MAX_RESEARCH_PROPOSALS)
            )
        )
    )

    assert len(response.proposals) == MAX_RESEARCH_PROPOSALS
    assert parse_research_proposal_response_v1(response.canonical_bytes()) == response


@pytest.mark.parametrize("confidence", ["low", "UNKNOWN", "0.5", 0.5, True])
def test_confidence_is_a_strict_qualitative_self_assessment(confidence: object) -> None:
    with pytest.raises(ResearchProposalResponseError):
        ResearchProposalResponseV1.from_payload(
            _response_payload(_proposal_payload(_candidate_id(1), confidence=confidence))
        )
    assert "not probability" in ResearchProposalConfidence.__doc__.lower()


@pytest.mark.parametrize(
    "value",
    [
        "",
        "   ",
        "line\nbreak",
        "tab\ttext",
        "null\x00text",
        "\u0085",
        "\ufeff",
        "\ufeff   ",
        "\ufeff \t",
    ],
)
def test_required_text_rejects_empty_whitespace_and_control_characters(value: str) -> None:
    with pytest.raises(ResearchProposalResponseError):
        ResearchProposalResponseV1.from_payload(
            _response_payload(_proposal_payload(_candidate_id(1), title=value))
        )


@pytest.mark.parametrize("value", ["x", "Привет", " x ", "\ufeffresearch"])
def test_required_text_preserves_valid_normal_unicode_exactly(value: str) -> None:
    response = ResearchProposalResponseV1.from_payload(
        _response_payload(_proposal_payload(_candidate_id(1), title=value))
    )

    assert response.proposals[0].title == value


@pytest.mark.parametrize(
    "candidate_id",
    [
        "ssc-v2-" + "A" * 64,
        "ssc-v2-" + "a" * 63,
        "ssc-v2-" + "a" * 65,
        "ssc-v2-" + "а" * 64,
        "ssc-v2-" + "a" * 64 + "\n",
        " " + "ssc-v2-" + "a" * 64,
        "ssc-v2-" + "a" * 64 + " ",
        "sha256:" + "a" * 64,
        True,
    ],
)
def test_candidate_id_format_is_strict(candidate_id: object) -> None:
    with pytest.raises(ResearchProposalResponseError, match="candidate_id"):
        ResearchProposalResponseV1.from_payload(
            _response_payload(_proposal_payload(candidate_id))  # type: ignore[arg-type]
        )


def test_packet_bound_validation_accepts_only_candidates_in_authoritative_packet(
    tmp_path: Path,
) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    packet = _verified_packet(store, features=("BULLISH_BOS", "BEARISH_BOS"))
    first_id = packet.candidate_bindings[0]["candidate_id"]
    response = ResearchProposalResponseV1.from_payload(
        _response_payload(_proposal_payload(first_id))  # type: ignore[arg-type]
    )

    assert validate_research_proposals_for_packet(response, packet) is response
    assert (
        validate_research_proposals_for_packet(
            ResearchProposalResponseV1.from_payload(_response_payload()),
            packet,
        ).proposals
        == ()
    )


def test_packet_bound_validation_rejects_unknown_and_other_packet_candidate(
    tmp_path: Path,
) -> None:
    first_store = ArtifactStore(tmp_path / "first")
    second_store = ArtifactStore(tmp_path / "second")
    packet = _verified_packet(first_store, features=("BULLISH_BOS",))
    other_packet = _verified_packet(second_store, features=("DIFFERENT_FEATURE",))
    other_candidate = other_packet.candidate_bindings[0]["candidate_id"]
    response = ResearchProposalResponseV1.from_payload(
        _response_payload(_proposal_payload(other_candidate))  # type: ignore[arg-type]
    )

    with pytest.raises(ResearchProposalResponseError, match="not present"):
        validate_research_proposals_for_packet(response, packet)


def test_json_schema_matches_local_contract_limits_and_is_a_fresh_value() -> None:
    schema = research_proposal_response_json_schema()
    proposal = schema["properties"]["proposals"]  # type: ignore[index]
    item = proposal["items"]  # type: ignore[index]
    properties = item["properties"]  # type: ignore[index]
    candidate_id_schema = properties["candidate_id"]

    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {
        "schema_version",
        "response_kind",
        "proposals",
    }
    assert schema["properties"]["schema_version"]["const"] == (  # type: ignore[index]
        RESEARCH_PROPOSAL_RESPONSE_SCHEMA_VERSION
    )
    assert schema["properties"]["response_kind"]["const"] == (  # type: ignore[index]
        RESEARCH_PROPOSAL_RESPONSE_KIND
    )
    assert proposal["minItems"] == 0
    assert proposal["maxItems"] == MAX_RESEARCH_PROPOSALS
    assert item["additionalProperties"] is False
    assert set(item["required"]) == {
        "candidate_id",
        "title",
        "rationale",
        "falsifiable_claim",
        "proposed_test",
        "rejection_condition",
        "confidence",
    }
    assert properties["title"]["maxLength"] == MAX_PROPOSAL_TITLE_LENGTH
    assert candidate_id_schema == {
        "type": "string",
        "pattern": r"^ssc-v2-[0-9a-f]{64}$",
        "minLength": 71,
        "maxLength": 71,
    }
    assert properties["rationale"]["maxLength"] == MAX_PROPOSAL_RATIONALE_LENGTH
    assert properties["falsifiable_claim"]["maxLength"] == MAX_FALSIFIABLE_CLAIM_LENGTH
    assert properties["proposed_test"]["maxLength"] == MAX_PROPOSED_TEST_LENGTH
    assert properties["rejection_condition"]["maxLength"] == (MAX_REJECTION_CONDITION_LENGTH)
    assert properties["confidence"]["enum"] == ["LOW", "MEDIUM", "HIGH"]

    changed = copy.deepcopy(schema)
    changed["properties"]["proposals"]["maxItems"] = 0  # type: ignore[index]
    assert (
        research_proposal_response_json_schema()["properties"]["proposals"]["maxItems"]  # type: ignore[index]
        == MAX_RESEARCH_PROPOSALS
    )


def _schema_string_accepts(schema: dict[str, object], value: object) -> bool:
    if type(value) is not str:
        return False
    minimum = schema.get("minLength")
    maximum = schema.get("maxLength")
    pattern = schema.get("pattern")
    if type(minimum) is int and len(value) < minimum:
        return False
    if type(maximum) is int and len(value) > maximum:
        return False
    return type(pattern) is str and re.search(pattern, value) is not None


@pytest.mark.parametrize(
    ("value", "accepted"),
    [
        ("ssc-v2-" + "a" * 64, True),
        ("ssc-v2-" + "a" * 64 + "\n", False),
        (" " + "ssc-v2-" + "a" * 64, False),
        ("ssc-v2-" + "a" * 64 + " ", False),
        ("ssc-v2-" + "A" * 64, False),
        ("ssc-v2-" + "a" * 63, False),
        ("ssc-v2-" + "a" * 65, False),
        ("ssc-v2-" + "а" * 64, False),
    ],
)
def test_candidate_id_json_schema_semantics_are_exact(value: str, accepted: bool) -> None:
    schema = research_proposal_response_json_schema()
    proposal_schema = schema["properties"]["proposals"]["items"]  # type: ignore[index]
    candidate_schema = proposal_schema["properties"]["candidate_id"]  # type: ignore[index]

    assert _schema_string_accepts(candidate_schema, value) is accepted


@pytest.mark.parametrize(
    ("value", "accepted"),
    [
        ("x", True),
        ("Привет", True),
        ("Исследование 😀", True),
        (" x ", True),
        ("\ufeffresearch", True),
        ("", False),
        ("   ", False),
        ("valid\n", False),
        ("\u0000x", False),
        ("\u0085", False),
        ("\ud800", False),
        ("\ufeff", False),
        ("\ufeff   ", False),
        ("\ufeff \t", False),
    ],
)
@pytest.mark.parametrize(
    ("field", "max_length"),
    [
        ("title", MAX_PROPOSAL_TITLE_LENGTH),
        ("rationale", MAX_PROPOSAL_RATIONALE_LENGTH),
        ("falsifiable_claim", MAX_FALSIFIABLE_CLAIM_LENGTH),
        ("proposed_test", MAX_PROPOSED_TEST_LENGTH),
        ("rejection_condition", MAX_REJECTION_CONDITION_LENGTH),
    ],
)
def test_text_json_schema_semantics_match_local_contract(
    field: str,
    max_length: int,
    value: str,
    accepted: bool,
) -> None:
    schema = research_proposal_response_json_schema()
    proposal_schema = schema["properties"]["proposals"]["items"]  # type: ignore[index]
    text_schema = proposal_schema["properties"][field]  # type: ignore[index]

    assert text_schema["minLength"] == 1
    assert text_schema["maxLength"] == max_length
    assert _schema_string_accepts(text_schema, value) is accepted
    payload = _response_payload(_proposal_payload(_candidate_id(1), **{field: value}))
    if accepted:
        proposal = ResearchProposalResponseV1.from_payload(payload).proposals[0]
        assert getattr(proposal, field) == value
    else:
        with pytest.raises(ResearchProposalResponseError):
            ResearchProposalResponseV1.from_payload(payload)


def test_direct_construction_requires_domain_types_and_remains_canonical() -> None:
    proposal = ResearchProposalV1(
        candidate_id=_candidate_id(1),
        title="Title",
        rationale="Rationale.",
        falsifiable_claim="Claim can fail.",
        proposed_test="Test the claim.",
        rejection_condition="Reject on predefined failure.",
        confidence=ResearchProposalConfidence.HIGH,
    )
    response = ResearchProposalResponseV1((proposal,))

    assert parse_research_proposal_response_v1(response.canonical_bytes()) == response
    with pytest.raises(ResearchProposalResponseError, match="immutable tuple"):
        ResearchProposalResponseV1([proposal])  # type: ignore[arg-type]
