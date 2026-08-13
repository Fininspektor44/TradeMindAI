import json
from collections.abc import Iterator, Mapping
from datetime import datetime
from pathlib import Path
from types import MappingProxyType

import pytest

from trademind.orchestrator.agent_protocol import (
    AgentDecision,
    AgentEnvelope,
    AgentProtocolError,
    AgentResult,
    JsonPayloadError,
    MAX_JSON_INTEGER_ABS,
    MAX_JSON_MAPPING_ENTRIES,
    MAX_JSON_NODES,
    MAX_JSON_SEQUENCE_LENGTH,
    MAX_JSON_STRING_LENGTH,
    SCHEMA_VERSION,
    V1_SCHEMA_VERSION,
    V2_SCHEMA_VERSION,
    WirePayloadError,
    canonical_json_dumps,
    canonical_json_loads,
)
from trademind.orchestrator.models import Role


def _envelope(**overrides):
    options = {
        "task_id": "T1",
        "revision": 2,
        "role": Role.AUDITOR,
        "goal": "review",
        "scope": ("src/example.py",),
        "forbidden_actions": ("BROKER_ORDER_PLACE",),
        "acceptance_criteria": ("tests pass",),
        "artifact_refs": ("sha256:abc",),
        "required_output_schema": "audit-v1",
    }
    options.update(overrides)
    return AgentEnvelope(**options)


def test_agent_envelope_payload_is_versioned_and_structured():
    envelope = _envelope()
    payload = envelope.to_payload()

    assert payload["schema_version"] == SCHEMA_VERSION
    assert payload["role"] == "AUDITOR"
    assert payload["scope"] == ["src/example.py"]
    assert payload["artifacts"] == ["sha256:abc"]
    assert payload["required_output_schema"] == "audit-v1"
    assert "structured_input" not in payload
    assert "input_schema" not in payload
    assert AgentEnvelope.from_payload(json.loads(json.dumps(payload))) == envelope


def test_v1_envelope_and_legacy_positional_constructor_remain_compatible():
    envelope = AgentEnvelope(
        "T-v1",
        1,
        Role.ARCHITECT,
        "legacy request",
        (),
        (),
        (),
        (),
        "result-v1",
        V1_SCHEMA_VERSION,
    )

    assert envelope.schema_version == V1_SCHEMA_VERSION
    assert AgentEnvelope.from_payload(envelope.to_payload()) == envelope


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda payload: payload.pop("schema_version"), "missing required field"),
        (lambda payload: payload.update(schema_version="agent-v99"), "unsupported"),
        (lambda payload: payload.update(unknown=True), "unknown fields"),
    ],
)
def test_envelope_wire_rejects_missing_unknown_version_and_unknown_fields(
    mutation, message
):
    payload = _envelope().to_payload()
    mutation(payload)

    with pytest.raises(WirePayloadError, match=message):
        AgentEnvelope.from_payload(payload)


def test_v1_envelope_rejects_v2_structured_fields():
    payload = _envelope(schema_version=V1_SCHEMA_VERSION).to_payload()
    payload.update(structured_input={"candidate": 1}, input_schema="candidate-v1")

    with pytest.raises(WirePayloadError, match="unknown fields"):
        AgentEnvelope.from_payload(payload)


def test_structured_input_is_deeply_frozen_and_roundtrips_with_schema():
    source = {
        "candidate": {"symbol": "EURUSD", "horizon": 12},
        "features": ["breakout", True, None, 1.25],
    }
    envelope = _envelope(
        structured_input=source,
        input_schema="discovery-candidate-v0",
    )
    source["candidate"]["symbol"] = "MUTATED"
    source["features"].append("MUTATED")

    payload = envelope.to_payload()
    restored = AgentEnvelope.from_payload(json.loads(json.dumps(payload)))

    assert payload["input_schema"] == "discovery-candidate-v0"
    assert payload["structured_input"] == {
        "candidate": {"horizon": 12, "symbol": "EURUSD"},
        "features": ["breakout", True, None, 1.25],
    }
    assert restored == envelope
    with pytest.raises(TypeError):
        envelope.structured_input["candidate"] = {}


def test_mapping_proxy_and_shared_non_cyclic_references_are_supported():
    shared = ["same"]
    envelope = _envelope(
        structured_input=MappingProxyType({"left": shared, "right": shared}),
        input_schema="shared-v1",
    )

    assert envelope.to_payload()["structured_input"] == {
        "left": ["same"],
        "right": ["same"],
    }


class _AdversarialMapping(Mapping):
    def __init__(self) -> None:
        self.item_calls = 0

    def __getitem__(self, key):
        return "value"

    def __iter__(self) -> Iterator:
        return iter(("safe",))

    def __len__(self) -> int:
        return 1

    def items(self):
        self.item_calls += 1
        return iter((("safe", "value"),)) if self.item_calls == 1 else iter(((1, "bad"),))


def test_custom_mapping_is_snapshotted_once_before_validation_and_freeze():
    source = _AdversarialMapping()

    envelope = _envelope(structured_input=source, input_schema="mapping-v1")

    assert source.item_calls == 1
    assert envelope.to_payload()["structured_input"] == {"safe": "value"}


def test_structured_result_roundtrips_without_parsing_summary():
    source = {
        "hypothesis": {"statement": "effect is positive"},
        "observations": [100, 200],
    }
    result = AgentResult(
        success=True,
        summary="diagnostic prose only",
        output_schema="hypothesis-v0",
        structured_output=source,
    )
    source["hypothesis"]["statement"] = "MUTATED"
    source["observations"].append(300)

    restored = AgentResult.from_payload(json.loads(json.dumps(result.to_payload())))

    assert restored == result
    assert restored.output_schema == "hypothesis-v0"
    assert restored.structured_output["hypothesis"]["statement"] == "effect is positive"
    assert restored.summary == "diagnostic prose only"


def test_legacy_v1_result_cache_shape_is_exact_and_v2_is_explicit():
    legacy_payload = {
        "success": True,
        "summary": "legacy",
        "artifact_refs": [],
        "output_schema": "result-v1",
        "tokens": 1,
        "cost": 0.0,
        "error": None,
        "decision": "CONTINUE",
    }

    legacy = AgentResult.from_payload(legacy_payload)
    current = AgentResult(success=True, summary="current", output_schema="result-v2")

    assert legacy.schema_version == V1_SCHEMA_VERSION
    assert current.to_payload()["schema_version"] == V2_SCHEMA_VERSION
    with pytest.raises(WirePayloadError, match="unknown fields"):
        AgentResult.from_payload(legacy_payload | {"structured_output": {"value": 1}})
    with pytest.raises(WirePayloadError, match="unknown fields"):
        AgentResult.from_payload(legacy_payload | {"unknown": True})


@pytest.mark.parametrize(
    "field,value",
    [
        ("success", "false"),
        ("summary", {}),
        ("artifact_refs", [1]),
        ("output_schema", 1),
        ("tokens", "1"),
        ("tokens", True),
        ("cost", "0.1"),
        ("cost", float("nan")),
        ("cost", float("inf")),
        ("error", 1),
        ("decision", "UNKNOWN"),
    ],
)
def test_result_wire_rejects_type_coercion_and_invalid_values(field, value):
    payload = AgentResult(
        success=True,
        summary="valid",
        output_schema="result-v2",
    ).to_payload()
    payload[field] = value

    with pytest.raises((WirePayloadError, AgentProtocolError)):
        AgentResult.from_payload(payload)


def test_failed_result_cannot_carry_structured_output():
    with pytest.raises(ValueError, match="cannot contain structured_output"):
        AgentResult(
            success=False,
            summary="failed",
            error="provider failure",
            structured_output={"decision": "APPROVE"},
        )


def test_json_serialization_is_deterministic_across_mapping_order():
    left = {"outer": {"z": 3, "a": 1}, "value": 2}
    right = {"value": 2, "outer": {"a": 1, "z": 3}}

    assert canonical_json_dumps(left) == canonical_json_dumps(right)
    assert _envelope(
        structured_input=left,
        input_schema="ordered-v1",
    ).to_payload() == _envelope(
        structured_input=right,
        input_schema="ordered-v1",
    ).to_payload()


def test_protocol_version_and_input_schema_change_request_identity():
    v1 = _envelope(schema_version=V1_SCHEMA_VERSION).to_payload()
    v2 = _envelope().to_payload()
    input_a = _envelope(structured_input={"value": 1}, input_schema="input-a").to_payload()
    input_b = _envelope(structured_input={"value": 1}, input_schema="input-b").to_payload()

    assert canonical_json_dumps(v1) != canonical_json_dumps(v2)
    assert canonical_json_dumps(input_a) != canonical_json_dumps(input_b)


@pytest.mark.parametrize(
    "encoded",
    [
        "{malformed",
        '{"value":NaN}',
        '{"value":Infinity}',
        '{"value":1,"value":2}',
    ],
)
def test_strict_json_parser_rejects_malformed_extensions_and_duplicate_keys(encoded):
    with pytest.raises(JsonPayloadError):
        canonical_json_loads(encoded)


@pytest.mark.parametrize("invalid", (float("nan"), float("inf"), float("-inf")))
def test_structured_payload_rejects_non_finite_float(invalid):
    with pytest.raises(JsonPayloadError, match="non-finite"):
        _envelope(structured_input={"value": invalid}, input_schema="input-v1")
    with pytest.raises(JsonPayloadError, match="non-finite"):
        AgentResult(
            success=True,
            summary="bad",
            output_schema="output-v1",
            structured_output={"value": invalid},
        )


@pytest.mark.parametrize(
    "invalid",
    (Path("secret.json"), b"bytes", datetime(2025, 1, 1), object()),
)
def test_structured_payload_rejects_non_json_objects(invalid):
    with pytest.raises(JsonPayloadError, match="unsupported JSON type"):
        _envelope(structured_input={"value": invalid}, input_schema="input-v1")


def test_structured_payload_rejects_non_string_mapping_keys():
    with pytest.raises(JsonPayloadError, match="keys must be strings"):
        _envelope(structured_input={1: "value"}, input_schema="input-v1")


def test_structured_payload_rejects_recursive_structure():
    cyclic = []
    cyclic.append(cyclic)

    with pytest.raises(JsonPayloadError, match="recursive"):
        AgentResult(
            success=True,
            summary="bad cycle",
            output_schema="output-v1",
            structured_output={"cycle": cyclic},
        )


def test_structured_payload_rejects_excessive_depth():
    nested = {"leaf": True}
    for _ in range(33):
        nested = {"next": nested}

    with pytest.raises(JsonPayloadError, match="maximum JSON depth"):
        _envelope(structured_input=nested, input_schema="input-v1")


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"value": MAX_JSON_INTEGER_ABS + 1}, "integer magnitude"),
        ({"value": "x" * (MAX_JSON_STRING_LENGTH + 1)}, "string length"),
        ({"value": [None] * (MAX_JSON_SEQUENCE_LENGTH + 1)}, "sequence length"),
        (
            {str(index): index for index in range(MAX_JSON_MAPPING_ENTRIES + 1)},
            "mapping entries",
        ),
    ],
)
def test_structured_payload_rejects_resource_limit_overflow(payload, message):
    with pytest.raises(JsonPayloadError, match=message):
        _envelope(structured_input=payload, input_schema="bounded-v1")


def test_structured_payload_rejects_total_node_limit():
    payload = {
        "groups": [
            [None] * 4_000,
            [None] * 4_000,
            [None] * (MAX_JSON_NODES - 8_000),
        ]
    }

    with pytest.raises(JsonPayloadError, match="node count"):
        _envelope(structured_input=payload, input_schema="bounded-v1")


def test_structured_payload_rejects_canonical_size_limit():
    payload = {"groups": [["x" * 30] * 4_000, ["y" * 30] * 4_000]}

    with pytest.raises(JsonPayloadError, match="canonical JSON bytes"):
        _envelope(structured_input=payload, input_schema="bounded-v1")


def test_object_root_is_an_explicit_structured_protocol_contract():
    with pytest.raises(JsonPayloadError, match="must be a JSON object"):
        _envelope(structured_input=["not", "an", "object"], input_schema="input-v1")


def test_json_edge_primitives_remain_exact_and_serializable():
    envelope = _envelope(
        structured_input={
            "boolean": True,
            "integer": 1,
            "negative_zero": -0.0,
            "tuple": ("a", "b"),
            "max_integer": MAX_JSON_INTEGER_ABS,
        },
        input_schema="edges-v1",
    )

    encoded = canonical_json_dumps(envelope.to_payload())
    assert '"boolean":true' in encoded
    assert '"integer":1' in encoded
    assert '"negative_zero":-0.0' in encoded


def test_accepted_payload_depth_survives_protocol_wrapper_serialization():
    nested = {"leaf": True}
    for _ in range(31):
        nested = {"next": nested}

    envelope = _envelope(structured_input=nested, input_schema="input-v1")

    assert canonical_json_dumps({"envelope": envelope.to_payload()})


def test_structured_input_and_schema_must_be_declared_together():
    with pytest.raises(ValueError, match="requires input_schema"):
        _envelope(structured_input={"value": 1})
    with pytest.raises(ValueError, match="requires structured_input"):
        _envelope(input_schema="input-v1")


def test_agent_result_rejects_invalid_usage_and_success_error_mix():
    try:
        AgentResult(success=True, summary="bad", output_schema="result-v1", tokens=-1)
    except ValueError:
        pass
    else:
        raise AssertionError("negative token usage must be rejected")

    try:
        AgentResult(
            success=True,
            summary="bad",
            output_schema="result-v1",
            error="should not coexist",
        )
    except ValueError:
        pass
    else:
        raise AssertionError("successful result cannot contain an error")


def test_agent_result_requires_schema_on_success_and_error_on_failure():
    try:
        AgentResult(success=True, summary="missing schema")
    except ValueError:
        pass
    else:
        raise AssertionError("successful result must declare output schema")

    try:
        AgentResult(success=False, summary="failed")
    except ValueError:
        pass
    else:
        raise AssertionError("failed result must explain its error")


def test_failed_transport_result_cannot_fake_auditor_rejection():
    try:
        AgentResult(
            success=False,
            summary="transport failed",
            error="timeout",
            decision=AgentDecision.REJECT,
        )
    except ValueError:
        pass
    else:
        raise AssertionError("failed provider call cannot masquerade as an auditor decision")
