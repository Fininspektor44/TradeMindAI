from trademind.orchestrator.agent_protocol import AgentEnvelope, AgentResult, SCHEMA_VERSION
from trademind.orchestrator.models import Role


def test_agent_envelope_payload_is_versioned_and_structured():
    envelope = AgentEnvelope(
        task_id="T1",
        revision=2,
        role=Role.AUDITOR,
        goal="review",
        scope=("src/example.py",),
        forbidden_actions=("BROKER_ORDER_PLACE",),
        acceptance_criteria=("tests pass",),
        artifact_refs=("sha256:abc",),
        required_output_schema="audit-v1",
    )
    payload = envelope.to_payload()

    assert payload["schema_version"] == SCHEMA_VERSION
    assert payload["role"] == "AUDITOR"
    assert payload["scope"] == ["src/example.py"]
    assert payload["artifacts"] == ["sha256:abc"]
    assert payload["required_output_schema"] == "audit-v1"


def test_agent_result_rejects_invalid_usage_and_success_error_mix():
    try:
        AgentResult(success=True, summary="bad", tokens=-1)
    except ValueError:
        pass
    else:
        raise AssertionError("negative token usage must be rejected")

    try:
        AgentResult(success=True, summary="bad", error="should not coexist")
    except ValueError:
        pass
    else:
        raise AssertionError("successful result cannot contain an error")
