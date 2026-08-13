import json
import sys

from trademind.orchestrator.agent_protocol import AgentEnvelope
from trademind.orchestrator.mock_runner import DeterministicMockProvider, run_mock_cycle
from trademind.orchestrator.models import Role
from trademind.orchestrator.tool_runner import CommandTemplate


def _repo(path):
    path.mkdir()
    (path / "pyproject.toml").write_text("[project]\nname='mock'\n", encoding="utf-8")
    return path


def test_mock_runner_completes_with_local_deterministic_providers(tmp_path):
    repo = _repo(tmp_path / "repo")
    state_dir = tmp_path / "state"
    report = run_mock_cycle(
        repo_root=repo,
        state_dir=state_dir,
        test_template=CommandTemplate(
            executable=sys.executable,
            args=("-c", "print('operator tests pass')"),
            timeout_seconds=10,
        ),
    )

    assert report.verdict == "PASS"
    assert report.final_state == "COMPLETED"
    assert report.audit_chain_valid
    assert report.model_calls == 5
    assert report.artifact_count == 6

    persisted = json.loads((state_dir / "mock_run_report.json").read_text(encoding="utf-8"))
    assert persisted["verdict"] == "PASS"
    assert persisted["task_id"] == report.task_id


def test_mock_runner_fails_closed_when_operator_test_command_fails(tmp_path):
    repo = _repo(tmp_path / "repo")

    report = run_mock_cycle(
        repo_root=repo,
        state_dir=tmp_path / "state-fail",
        test_template=CommandTemplate(
            executable=sys.executable,
            args=("-c", "raise SystemExit(7)"),
            timeout_seconds=10,
        ),
    )

    assert report.verdict == "FAIL"
    assert report.final_state == "FAILED"
    assert report.audit_chain_valid


def test_deterministic_mock_provider_returns_structured_output():
    provider = DeterministicMockProvider(Role.ARCHITECT)
    envelope = AgentEnvelope(
        task_id="T-structured-mock",
        revision=1,
        role=Role.ARCHITECT,
        goal="produce structured output",
        scope=(),
        forbidden_actions=(),
        acceptance_criteria=(),
        artifact_refs=(),
        required_output_schema="mock-output-v1",
        structured_input={"candidate_id": "candidate-1"},
        input_schema="mock-input-v1",
    )

    result = provider.execute(envelope)

    assert result.success
    assert result.output_schema == "mock-output-v1"
    assert result.structured_output == {
        "input_schema": "mock-input-v1",
        "received_structured_input": True,
        "required_output_schema": "mock-output-v1",
        "role": "ARCHITECT",
    }


def test_deterministic_mock_provider_preserves_legacy_result_shape():
    provider = DeterministicMockProvider(Role.ARCHITECT)
    envelope = AgentEnvelope(
        task_id="T-legacy-mock",
        revision=1,
        role=Role.ARCHITECT,
        goal="legacy request",
        scope=(),
        forbidden_actions=(),
        acceptance_criteria=(),
        artifact_refs=(),
        required_output_schema="mock-output-v1",
    )

    result = provider.execute(envelope)

    assert result.structured_output is None
    assert "structured_output" not in result.to_payload()
