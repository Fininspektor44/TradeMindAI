from __future__ import annotations

import hashlib
import math
from collections.abc import Iterator, Mapping
from pathlib import Path

import pytest

from trademind.signal_statistics_provenance import (
    CANDIDATE_CONTENT_SCHEMA_VERSION,
    CANDIDATE_DEFINITION_SCHEMA_VERSION,
    CODE_PROVENANCE_SCHEMA_VERSION,
    MAX_CANONICAL_JSON_BYTES,
    MAX_JSON_DEPTH,
    MAX_JSON_INTEGER_ABS,
    MAX_JSON_MAPPING_ENTRIES,
    MAX_JSON_NODES,
    MAX_JSON_SEQUENCE_LENGTH,
    MAX_JSON_STRING_LENGTH,
    MAX_JSON_TOTAL_STRING_BYTES,
    NUMERIC_CANONICALIZATION_VERSION,
    CandidateContentV2,
    CandidateDefinitionV2,
    CodeProvenance,
    PacketContentHashProjection,
    ProvenanceError,
    ReportContentHashProjection,
    canonical_json_bytes,
    freeze_json,
    freeze_json_object,
    packet_content_hash,
    parse_json,
    report_content_hash,
    sha256_bytes,
    task_id_from_packet_hash,
    validate_sha256_ref,
)

_GIT_COMMIT = "1" * 40
_POLICY_HASH = f"sha256:{'2' * 64}"


class _OneShotMapping(Mapping[str, object]):
    def __init__(self) -> None:
        self.items_calls = 0

    def __getitem__(self, key: str) -> object:
        raise AssertionError("snapshot must use items()")

    def __iter__(self) -> Iterator[str]:
        raise AssertionError("snapshot must use items()")

    def __len__(self) -> int:
        return 2

    def items(self) -> object:
        self.items_calls += 1
        if self.items_calls != 1:
            raise AssertionError("mapping was observed more than once")
        return (("z", [2, 3]), ("a", {"nested": True}))


def _definition(**changes: object) -> CandidateDefinitionV2:
    values: dict[str, object] = {
        "source_kind": "signal_journal",
        "source_namespace": "trademind_signal_journal",
        "symbol": "XAUUSD",
        "timeframe": "M5",
        "feature": "BULLISH_BOS",
        "horizon": 3,
        "action_scope": "BUY_SELL_DIRECTIONAL",
        "evaluation_method_version": "signal-statistics-v2",
    }
    values.update(changes)
    return CandidateDefinitionV2(**values)


def _content(**changes: object) -> CandidateContentV2:
    values: dict[str, object] = {
        "candidate_definition": _definition(),
        "evaluation_policy_hash": _POLICY_HASH,
        "metrics": {"trades": 60, "avg_net_atr": 0.2, "ci95": [0.1, 0.3]},
        "status": "RESEARCH_CANDIDATE",
        "reason_codes": ("POSITIVE_SPLITS", "BELOW_RESEARCH_MINIMUM"),
    }
    values.update(changes)
    return CandidateContentV2(**values)


def test_canonical_json_is_key_order_independent_and_nested() -> None:
    left = {"z": [1, {"b": False, "a": None}], "a": "тест"}
    right = {"a": "тест", "z": [1, {"a": None, "b": False}]}

    assert canonical_json_bytes(left) == canonical_json_bytes(right)
    assert canonical_json_bytes(left) == ('{"a":"тест","z":[1,{"a":null,"b":false}]}'.encode())


def test_freeze_detaches_nested_dict_and_list_mutations() -> None:
    values = [1, 2]
    nested = {"values": values}
    source = {"nested": nested}
    frozen = freeze_json_object(source)
    before = canonical_json_bytes(frozen)

    values.append(3)
    nested["new"] = True
    source["other"] = "changed"

    assert canonical_json_bytes(frozen) == before
    assert canonical_json_bytes(frozen) == b'{"nested":{"values":[1,2]}}'
    with pytest.raises(TypeError):
        frozen["changed"] = True  # type: ignore[index]


def test_custom_mapping_is_snapshotted_exactly_once() -> None:
    source = _OneShotMapping()

    frozen = freeze_json_object(source)

    assert source.items_calls == 1
    assert canonical_json_bytes(frozen) == b'{"a":{"nested":true},"z":[2,3]}'


def test_recursive_containers_are_rejected() -> None:
    recursive_list: list[object] = []
    recursive_list.append(recursive_list)
    recursive_dict: dict[str, object] = {}
    recursive_dict["self"] = recursive_dict

    with pytest.raises(ProvenanceError, match="recursive sequence"):
        freeze_json(recursive_list)
    with pytest.raises(ProvenanceError, match="recursive mapping"):
        freeze_json(recursive_dict)


def test_shared_non_recursive_reference_is_accepted() -> None:
    shared = {"metric": [1, 2]}

    assert canonical_json_bytes({"left": shared, "right": shared}) == (
        b'{"left":{"metric":[1,2]},"right":{"metric":[1,2]}}'
    )


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_numbers_are_rejected(value: float) -> None:
    with pytest.raises(ProvenanceError, match="non-finite"):
        canonical_json_bytes({"metric": value})


@pytest.mark.parametrize("value", [b"bytes", Path("journal.json"), object(), {1: "bad"}])
def test_non_json_values_are_rejected_without_coercion(value: object) -> None:
    with pytest.raises(ProvenanceError):
        canonical_json_bytes({"value": value})


def test_bool_and_int_remain_distinct_json_values() -> None:
    assert canonical_json_bytes({"value": True}) == b'{"value":true}'
    assert canonical_json_bytes({"value": 1}) == b'{"value":1}'


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0.0, b"0.0"),
        (-0.0, b"0.0"),
        (1, b"1"),
        (1.0, b"1.0"),
        (0.5, b"0.5"),
        (0.1, b"0.1000000000000000055511151231257827021181583404541015625"),
        (1e-7, b"0.0000000999999999999999954748111825886258685613938723690807819366455078125"),
        (1e20, b"100000000000000000000.0"),
    ],
)
def test_numeric_canonicalization_v1_golden_vectors(
    value: int | float,
    expected: bytes,
) -> None:
    assert canonical_json_bytes(value) == expected
    assert b"e" not in expected.lower()


def test_numeric_canonicalization_v1_is_explicit_and_signed_zero_is_normalized() -> None:
    assert NUMERIC_CANONICALIZATION_VERSION == "trademind-provenance-numeric-v1"
    assert canonical_json_bytes(-0.0) == canonical_json_bytes(0.0)
    assert canonical_json_bytes(1) != canonical_json_bytes(1.0)
    frozen = freeze_json({"metric": -0.0})
    assert math.copysign(1.0, frozen["metric"]) == 1.0


def test_strict_json_parser_rejects_duplicates_and_non_finite_extensions() -> None:
    with pytest.raises(ProvenanceError, match="duplicate JSON key"):
        parse_json('{"metric":1,"metric":2}')
    with pytest.raises(ProvenanceError, match="non-standard JSON constant"):
        parse_json('{"metric":NaN}')
    with pytest.raises(ProvenanceError, match="non-standard JSON constant"):
        parse_json('{"metric":Infinity}')


def test_strict_json_parser_returns_frozen_deterministic_utf8() -> None:
    parsed = parse_json('{"ключ":"значение","items":[true,null]}')

    assert canonical_json_bytes(parsed) == ('{"items":[true,null],"ключ":"значение"}'.encode())
    with pytest.raises(TypeError):
        parsed["extra"] = 1  # type: ignore[index]


def test_invalid_utf8_and_unpaired_surrogate_fail_closed() -> None:
    with pytest.raises(ProvenanceError, match="valid UTF-8"):
        parse_json(b"\xff")
    with pytest.raises(ProvenanceError, match="valid UTF-8"):
        canonical_json_bytes({"text": "\ud800"})
    with pytest.raises(ProvenanceError, match="valid UTF-8"):
        parse_json('{"text":"\ud800"}')


def test_depth_limit_accepts_boundary_and_rejects_overflow() -> None:
    boundary: object = None
    for _ in range(MAX_JSON_DEPTH):
        boundary = [boundary]
    canonical_json_bytes(boundary)

    overflow = [boundary]
    with pytest.raises(ProvenanceError, match="maximum JSON depth"):
        canonical_json_bytes(overflow)


def test_node_limit_accepts_boundary_and_rejects_overflow() -> None:
    leaf_count = MAX_JSON_NODES - 4
    sizes = [leaf_count // 3] * 3
    sizes[-1] += leaf_count - sum(sizes)
    boundary = [[None] * size for size in sizes]
    canonical_json_bytes(boundary)

    overflow = [list(group) for group in boundary]
    overflow[-1].append(None)
    with pytest.raises(ProvenanceError, match="maximum JSON node count"):
        canonical_json_bytes(overflow)


def test_mapping_entry_limit_accepts_boundary_and_rejects_overflow() -> None:
    boundary = {str(index): index for index in range(MAX_JSON_MAPPING_ENTRIES)}
    canonical_json_bytes(boundary)

    overflow = dict(boundary)
    overflow["overflow"] = 1
    with pytest.raises(ProvenanceError, match="maximum mapping entries"):
        canonical_json_bytes(overflow)


def test_sequence_limit_accepts_boundary_and_rejects_overflow() -> None:
    canonical_json_bytes([None] * MAX_JSON_SEQUENCE_LENGTH)

    with pytest.raises(ProvenanceError, match="maximum sequence length"):
        canonical_json_bytes([None] * (MAX_JSON_SEQUENCE_LENGTH + 1))


def test_string_limit_accepts_boundary_and_rejects_overflow() -> None:
    canonical_json_bytes("x" * MAX_JSON_STRING_LENGTH)

    with pytest.raises(ProvenanceError, match="maximum JSON string length"):
        canonical_json_bytes("x" * (MAX_JSON_STRING_LENGTH + 1))


def test_aggregate_string_limit_accepts_boundary_and_rejects_overflow() -> None:
    string_count = MAX_JSON_TOTAL_STRING_BYTES // MAX_JSON_STRING_LENGTH
    boundary = ["x" * MAX_JSON_STRING_LENGTH for _ in range(string_count)]
    remainder = MAX_JSON_TOTAL_STRING_BYTES % MAX_JSON_STRING_LENGTH
    if remainder:
        boundary.append("x" * remainder)
    canonical_json_bytes(boundary)

    overflow = list(boundary)
    overflow.append("x")
    with pytest.raises(ProvenanceError, match="aggregate JSON string bytes"):
        canonical_json_bytes(overflow)


def test_multibyte_utf8_aggregate_limit_accepts_boundary_and_rejects_overflow() -> None:
    boundary = "€" * MAX_JSON_STRING_LENGTH
    assert len(boundary.encode("utf-8")) == MAX_JSON_TOTAL_STRING_BYTES
    canonical_json_bytes(boundary)

    with pytest.raises(ProvenanceError, match="aggregate JSON string bytes"):
        canonical_json_bytes([boundary, "x"])


def test_integer_limit_accepts_boundary_and_rejects_bool_and_overflow() -> None:
    assert canonical_json_bytes(MAX_JSON_INTEGER_ABS) == str(MAX_JSON_INTEGER_ABS).encode()
    assert canonical_json_bytes(-MAX_JSON_INTEGER_ABS) == str(-MAX_JSON_INTEGER_ABS).encode()
    with pytest.raises(ProvenanceError, match="integer magnitude"):
        canonical_json_bytes(MAX_JSON_INTEGER_ABS + 1)
    with pytest.raises(ProvenanceError, match="positive integer"):
        _definition(horizon=True)


def test_canonical_byte_limit_accepts_exact_boundary_and_rejects_overflow() -> None:
    # A control character occupies one source byte but six canonical JSON bytes.
    # This isolates the serialized-size limit from the aggregate input-string limit.
    escaped_count = (MAX_CANONICAL_JSON_BYTES - len(b'["",""]') - 3) // 6
    boundary = ["\x01" * escaped_count, "xxx"]
    assert len(canonical_json_bytes(boundary)) == MAX_CANONICAL_JSON_BYTES

    overflow = list(boundary)
    overflow[-1] += "x"
    with pytest.raises(ProvenanceError, match="maximum canonical JSON bytes"):
        canonical_json_bytes(overflow)


def test_expanded_fixed_point_floats_cannot_bypass_canonical_byte_limit() -> None:
    smallest_binary64 = float.fromhex("0x0.0000000000001p-1022")
    assert b"e" not in canonical_json_bytes(smallest_binary64).lower()

    with pytest.raises(ProvenanceError, match="maximum canonical JSON bytes"):
        canonical_json_bytes([smallest_binary64] * 300)


def test_sha256_helpers_emit_and_validate_exact_lowercase_refs() -> None:
    expected = f"sha256:{hashlib.sha256(b'payload').hexdigest()}"

    assert sha256_bytes(b"payload") == expected
    assert validate_sha256_ref(expected) == expected


@pytest.mark.parametrize(
    "value",
    [
        f"sha256:{'A' * 64}",
        f"sha256:{'a' * 63}",
        f"sha256:{'a' * 65}",
        f"sha1:{'a' * 64}",
        "not-a-hash",
        f" sha256:{'a' * 64}",
        f"sha256:{'a' * 64}\n",
        f"sha256：{'a' * 64}",
        f"sha256:{'а' * 64}",
        b"sha256:bytes",
    ],
)
def test_sha256_validation_rejects_malformed_refs(value: object) -> None:
    with pytest.raises(ProvenanceError, match="64 lowercase hex"):
        validate_sha256_ref(value)


def test_code_provenance_accepts_clean_injected_git_and_embedded_revisions() -> None:
    worktree = CodeProvenance(
        producer_name="trademind.signal_statistics",
        producer_version="2.0.0",
        git_commit=_GIT_COMMIT,
        revision_source="git_worktree",
    )
    embedded = CodeProvenance(
        producer_name="trademind.signal_statistics",
        producer_version="2.0.0",
        git_commit="a" * 40,
        revision_source="embedded_build",
    )

    assert worktree.schema_version == CODE_PROVENANCE_SCHEMA_VERSION
    assert worktree.tree_state == "CLEAN"
    assert embedded.revision_source == "embedded_build"
    assert CodeProvenance.from_payload(worktree.to_payload()) == worktree


def test_code_provenance_is_a_claim_not_an_attestation() -> None:
    claim = CodeProvenance(
        producer_name="caller_claim",
        producer_version="1",
        git_commit="0" * 40,
        revision_source="embedded_build",
    )

    assert claim.git_commit == "0" * 40
    assert "not an attestation" in CodeProvenance.__doc__
    assert "trusted external" in CodeProvenance.__doc__


@pytest.mark.parametrize("tree_state", ["DIRTY", "UNKNOWN", "clean", ""])
def test_code_provenance_rejects_non_clean_tree_state(tree_state: str) -> None:
    with pytest.raises(ProvenanceError, match="tree_state must be CLEAN"):
        CodeProvenance(
            producer_name="producer",
            producer_version="2",
            git_commit=_GIT_COMMIT,
            revision_source="git_worktree",
            tree_state=tree_state,
        )


@pytest.mark.parametrize("revision", ["1" * 7, "main", "A" * 40, "1" * 41])
def test_code_provenance_rejects_short_branch_uppercase_and_long_revision(
    revision: str,
) -> None:
    with pytest.raises(ProvenanceError, match="40 lowercase"):
        CodeProvenance(
            producer_name="producer",
            producer_version="2",
            git_commit=revision,
            revision_source="git_worktree",
        )


def test_code_provenance_rejects_unknown_revision_source_and_schema() -> None:
    with pytest.raises(ProvenanceError, match="revision_source"):
        CodeProvenance("producer", "2", _GIT_COMMIT, "environment")
    with pytest.raises(ProvenanceError, match="schema_version"):
        CodeProvenance(
            "producer",
            "2",
            _GIT_COMMIT,
            "git_worktree",
            schema_version="unknown",
        )


def test_candidate_id_is_deterministic_and_uses_full_definition_digest() -> None:
    first = _definition()
    second = _definition()

    assert first.schema_version == CANDIDATE_DEFINITION_SCHEMA_VERSION
    assert first.candidate_id == second.candidate_id
    assert len(first.candidate_id) == len("ssc-v2-") + 64


def test_candidate_definition_has_independent_golden_bytes_and_domain_hash() -> None:
    expected_bytes = (
        b'{"action_scope":"BUY_SELL_DIRECTIONAL",'
        b'"evaluation_method_version":"signal-statistics-v2",'
        b'"feature":"BULLISH_BOS","horizon":3,'
        b'"schema_version":"signal-statistics-candidate-definition-v2",'
        b'"source_kind":"signal_journal",'
        b'"source_namespace":"trademind_signal_journal",'
        b'"symbol":"XAUUSD","timeframe":"M5"}'
    )
    expected_candidate_id = (
        "ssc-v2-1f4995c3c42669c3a5e041dcd493bf9ce3f57d7235fc6e05f0194dc10126f017"
    )
    expected_hash = "sha256:1f4995c3c42669c3a5e041dcd493bf9ce3f57d7235fc6e05f0194dc10126f017"
    independent_hash = (
        "sha256:"
        + hashlib.sha256(
            b"trademind:signal-statistics:candidate-definition:v2\x00" + expected_bytes
        ).hexdigest()
    )

    definition = _definition()

    assert independent_hash == expected_hash
    assert canonical_json_bytes(definition.to_payload()) == expected_bytes
    assert definition.candidate_id == expected_candidate_id


def test_candidate_id_is_independent_of_definition_mapping_order() -> None:
    payload = _definition().to_payload()
    reordered = dict(reversed(tuple(payload.items())))

    assert CandidateDefinitionV2.from_payload(reordered).candidate_id == _definition().candidate_id


@pytest.mark.parametrize(
    "changes",
    [
        {"timeframe": "M15"},
        {"source_namespace": "another_journal"},
        {"feature": "BEARISH_BOS"},
        {"horizon": 6},
    ],
)
def test_candidate_id_changes_with_semantic_definition(changes: dict[str, object]) -> None:
    assert _definition(**changes).candidate_id != _definition().candidate_id


def test_candidate_definition_rejects_empty_identifiers_and_unknown_fields() -> None:
    with pytest.raises(ProvenanceError, match="symbol"):
        _definition(symbol="")
    payload = _definition().to_payload()
    payload["unexpected"] = True
    with pytest.raises(ProvenanceError, match="unknown fields"):
        CandidateDefinitionV2.from_payload(payload)
    with pytest.raises(ProvenanceError, match="ASCII machine identifier"):
        _definition(source_namespace="C:\\journal\\signals.json")
    with pytest.raises(ProvenanceError, match="ASCII machine identifier"):
        _definition(source_namespace="/var/data/signals.json")


def test_metrics_do_not_affect_candidate_identity() -> None:
    first = _content(metrics={"avg": 0.1})
    second = _content(metrics={"avg": 999.0})

    assert first.candidate_id == second.candidate_id
    assert first.content_hash != second.content_hash


@pytest.mark.parametrize(
    "changes",
    [
        {"metrics": {"trades": 61, "avg_net_atr": 0.2}},
        {"status": "VALIDATED"},
        {"evaluation_policy_hash": f"sha256:{'3' * 64}"},
        {"reason_codes": ("POSITIVE_SPLITS", "FULL_SAMPLE_REACHED")},
        {"candidate_definition": _definition(timeframe="M15")},
    ],
)
def test_candidate_content_hash_changes_with_every_semantic_component(
    changes: dict[str, object],
) -> None:
    assert _content(**changes).content_hash != _content().content_hash


def test_candidate_content_hash_is_order_independent_and_immutable() -> None:
    metrics = {"trades": 60, "nested": {"z": 2, "a": [1, 2]}}
    first = _content(metrics=metrics)
    before = first.content_hash
    metrics["trades"] = 999
    nested = metrics["nested"]
    assert isinstance(nested, dict)
    nested["changed"] = True
    second = _content(metrics={"nested": {"a": [1, 2], "z": 2}, "trades": 60})

    assert first.content_hash == before == second.content_hash
    assert CandidateContentV2.from_payload(first.to_payload()).content_hash == before


def test_candidate_content_has_independent_golden_bytes_and_domain_hash() -> None:
    expected_bytes = (
        b'{"candidate_definition":{"action_scope":"BUY_SELL_DIRECTIONAL",'
        b'"evaluation_method_version":"signal-statistics-v2",'
        b'"feature":"BULLISH_BOS","horizon":3,'
        b'"schema_version":"signal-statistics-candidate-definition-v2",'
        b'"source_kind":"signal_journal",'
        b'"source_namespace":"trademind_signal_journal",'
        b'"symbol":"XAUUSD","timeframe":"M5"},'
        b'"evaluation_policy_hash":"sha256:'
        b'2222222222222222222222222222222222222222222222222222222222222222",'
        b'"metrics":{"avg_net_atr":'
        b"0.200000000000000011102230246251565404236316680908203125,"
        b'"ci95":['
        b"0.1000000000000000055511151231257827021181583404541015625,"
        b"0.299999999999999988897769753748434595763683319091796875],"
        b'"trades":60},'
        b'"reason_codes":["BELOW_RESEARCH_MINIMUM","POSITIVE_SPLITS"],'
        b'"schema_version":"signal-statistics-candidate-content-v2",'
        b'"status":"RESEARCH_CANDIDATE"}'
    )
    expected_hash = "sha256:692a69e09cdd713097cb6046d43b7deef3c4573b33b71caf6b2a80d6ca453511"
    independent_hash = (
        "sha256:"
        + hashlib.sha256(
            b"trademind:signal-statistics:candidate-content:v2\x00" + expected_bytes
        ).hexdigest()
    )

    content = _content()

    assert independent_hash == expected_hash
    assert canonical_json_bytes(content.to_payload()) == expected_bytes
    assert content.content_hash == expected_hash


def test_reason_code_order_does_not_create_content_hash_ambiguity() -> None:
    first = _content(reason_codes=("POSITIVE_SPLITS", "BELOW_RESEARCH_MINIMUM"))
    second = _content(reason_codes=("BELOW_RESEARCH_MINIMUM", "POSITIVE_SPLITS"))

    assert first.reason_codes == second.reason_codes
    assert first.content_hash == second.content_hash


@pytest.mark.parametrize(
    "reason_codes",
    [
        ("",),
        ("localized prose is not a machine code",),
        ("VALID", "VALID"),
        ("INVALID/PLATFORM/PATH",),
    ],
)
def test_candidate_content_rejects_invalid_or_duplicate_reason_codes(
    reason_codes: tuple[str, ...],
) -> None:
    with pytest.raises(ProvenanceError):
        _content(reason_codes=reason_codes)


def test_candidate_content_rejects_non_finite_metrics_and_invalid_policy_hash() -> None:
    with pytest.raises(ProvenanceError, match="non-finite"):
        _content(metrics={"avg": float("nan")})
    with pytest.raises(ProvenanceError, match="64 lowercase hex"):
        _content(evaluation_policy_hash="sha256:not-a-digest")


def test_typed_report_and_packet_hash_helpers_are_explicit_and_deterministic() -> None:
    report_left = ReportContentHashProjection({"candidates": ["a"], "policy": _POLICY_HASH})
    report_right = ReportContentHashProjection({"policy": _POLICY_HASH, "candidates": ["a"]})
    packet_left = PacketContentHashProjection({"report_hash": report_content_hash(report_left)})
    packet_right = PacketContentHashProjection({"report_hash": report_content_hash(report_right)})

    assert report_content_hash(report_left) == report_content_hash(report_right)
    assert packet_content_hash(packet_left) == packet_content_hash(packet_right)
    with pytest.raises(ProvenanceError, match="ReportContentHashProjection"):
        report_content_hash({"generated_at": "caller-must-project"})  # type: ignore[arg-type]
    with pytest.raises(ProvenanceError, match="PacketContentHashProjection"):
        packet_content_hash({"artifact_ref": "caller-must-project"})  # type: ignore[arg-type]


def test_report_and_packet_hash_domains_are_distinct_for_identical_content() -> None:
    content = {"schema_version": "same-test-projection-v1", "value": [1, 2.0]}

    report_hash = report_content_hash(ReportContentHashProjection(content))
    packet_hash = packet_content_hash(PacketContentHashProjection(content))

    assert report_hash != packet_hash


def test_strict_restart_roundtrips_all_persisted_contracts() -> None:
    objects = (
        CodeProvenance(
            producer_name="trademind.signal_statistics",
            producer_version="2.0.0",
            git_commit=_GIT_COMMIT,
            revision_source="git_worktree",
        ),
        _definition(),
        _content(),
    )

    for original in objects:
        original_bytes = canonical_json_bytes(original.to_payload())
        parsed = parse_json(original_bytes)
        restored = type(original).from_payload(parsed)

        assert canonical_json_bytes(restored.to_payload()) == original_bytes
        if isinstance(original, CandidateDefinitionV2):
            assert restored.candidate_id == original.candidate_id
        if isinstance(original, CandidateContentV2):
            assert restored.content_hash == original.content_hash


def test_task_id_is_deterministic_safe_ascii_and_preserves_full_digest() -> None:
    packet_hash = f"sha256:{'a' * 64}"
    expected = f"signal-stats-v2-{'a' * 64}"

    assert task_id_from_packet_hash(packet_hash) == expected
    assert task_id_from_packet_hash(packet_hash) == expected
    assert expected.isascii()
    assert expected.endswith("a" * 64)
    with pytest.raises(ProvenanceError, match="64 lowercase hex"):
        task_id_from_packet_hash(f"sha256:{'A' * 64}")


def test_contract_versions_are_exact_and_unknown_versions_fail_closed() -> None:
    assert CODE_PROVENANCE_SCHEMA_VERSION == "signal-statistics-code-provenance-v1"
    assert CANDIDATE_DEFINITION_SCHEMA_VERSION == "signal-statistics-candidate-definition-v2"
    assert CANDIDATE_CONTENT_SCHEMA_VERSION == "signal-statistics-candidate-content-v2"
    with pytest.raises(ProvenanceError, match="schema_version"):
        _definition(schema_version="signal-statistics-candidate-definition-v3")
    with pytest.raises(ProvenanceError, match="schema_version"):
        _content(schema_version="signal-statistics-candidate-content-v3")
