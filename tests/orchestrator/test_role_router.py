from dataclasses import replace

from trademind.orchestrator.agent_protocol import AgentEnvelope, AgentProtocolError, AgentResult
from trademind.orchestrator.models import Role, Task
from trademind.orchestrator.policy import FORBIDDEN_ACTIONS
from trademind.orchestrator.role_router import RoleRouter, RoleRoutingError


class MockProvider:
    def __init__(self, name: str, *, output_schema: str | None = None) -> None:
        self._name = name
        self._output_schema = output_schema
        self.seen: list[AgentEnvelope] = []

    @property
    def provider_name(self) -> str:
        return f"mock-{self._name}"

    @property
    def model_name(self) -> str:
        return f"model-{self._name}"

    def execute(self, envelope: AgentEnvelope) -> AgentResult:
        self.seen.append(envelope)
        return AgentResult(
            success=True,
            summary=f"handled by {self._name}",
            output_schema=self._output_schema or envelope.required_output_schema,
        )


def _task() -> Task:
    return replace(
        Task.new(
            task_id="T1",
            goal="implement bounded change",
            scope=("src/example.py",),
            acceptance_criteria=("tests pass",),
        ),
        artifact_refs=("sha256:abc",),
    )


def test_routes_each_ai_role_to_its_configured_provider():
    architect = MockProvider("architect")
    developer = MockProvider("developer")
    auditor = MockProvider("auditor")
    router = RoleRouter(
        {
            Role.ARCHITECT: architect,
            Role.DEVELOPER: developer,
            Role.AUDITOR: auditor,
        }
    )

    for role, provider in (
        (Role.ARCHITECT, architect),
        (Role.DEVELOPER, developer),
        (Role.AUDITOR, auditor),
    ):
        result = router.execute(_task(), role=role, required_output_schema="result-v1")
        assert result.success
        assert len(provider.seen) == 1
        assert provider.seen[0].role is role


def test_operator_cannot_be_routed_to_model_provider():
    provider = MockProvider("operator")
    try:
        RoleRouter({Role.OPERATOR: provider})
    except ValueError:
        pass
    else:
        raise AssertionError("OPERATOR must remain deterministic local software")

    router = RoleRouter({})
    try:
        router.execute(_task(), role=Role.OPERATOR, required_output_schema="result-v1")
    except RoleRoutingError:
        pass
    else:
        raise AssertionError("OPERATOR must not be delegated to a model")


def test_missing_provider_hard_fails():
    router = RoleRouter({})
    try:
        router.execute(_task(), role=Role.AUDITOR, required_output_schema="audit-v1")
    except RoleRoutingError:
        pass
    else:
        raise AssertionError("missing provider must not silently fall back")


def test_mismatched_output_schema_is_rejected():
    router = RoleRouter({Role.AUDITOR: MockProvider("bad", output_schema="wrong-v9")})
    try:
        router.execute(_task(), role=Role.AUDITOR, required_output_schema="audit-v1")
    except AgentProtocolError:
        pass
    else:
        raise AssertionError("mismatched model output schema must be rejected")


def test_envelope_contains_only_structured_task_metadata_and_policy_boundary():
    provider = MockProvider("auditor")
    router = RoleRouter({Role.AUDITOR: provider})
    task = _task()

    router.execute(task, role=Role.AUDITOR, required_output_schema="audit-v1")
    envelope = provider.seen[0]

    assert envelope.task_id == task.task_id
    assert envelope.revision == task.revision
    assert envelope.goal == task.goal
    assert envelope.scope == task.scope
    assert envelope.acceptance_criteria == task.acceptance_criteria
    assert envelope.artifact_refs == task.artifact_refs
    assert set(envelope.forbidden_actions) == set(FORBIDDEN_ACTIONS)
    assert envelope.required_output_schema == "audit-v1"
