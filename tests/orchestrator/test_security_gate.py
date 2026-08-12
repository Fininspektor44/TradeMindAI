import json
import sqlite3
import sys
from unittest.mock import patch

import pytest

import trademind.orchestrator.engine as engine_module
from trademind.orchestrator.agent_protocol import AgentDecision, AgentEnvelope, AgentResult
from trademind.orchestrator.artifact_store import ArtifactStore
from trademind.orchestrator.audit_log import AuditLog
from trademind.orchestrator.budget import BudgetManager
from trademind.orchestrator.control_plane import ControlPlane
from trademind.orchestrator.dispatcher import Dispatcher, route_to_generic_workflow
from trademind.orchestrator.engine import StageSpec, WorkflowEngine, WorkflowRoutingError
from trademind.orchestrator.models import PolicyDecision, Role, Task, TaskState
from trademind.orchestrator.role_router import RoleRouter
from trademind.orchestrator.service import OrchestratorService, ServiceStatus
from trademind.orchestrator.task_store import RevisionConflict, TaskStore
from trademind.orchestrator.tool_runner import CommandTemplate, ToolRunner


DISCOVERY_SCOPE = ("signal_statistics", "research_hypotheses_only")


class MockProvider:
    def __init__(self, role):
        self.role = role
        self.seen = []

    @property
    def provider_name(self):
        return "security-gate-mock"

    @property
    def model_name(self):
        return f"mock-{self.role.value.lower()}"

    def execute(self, envelope: AgentEnvelope) -> AgentResult:
        self.seen.append(envelope)
        decision = AgentDecision.APPROVE if self.role is Role.AUDITOR else AgentDecision.CONTINUE
        return AgentResult(
            success=True,
            summary="security gate fixture result",
            output_schema=envelope.required_output_schema,
            tokens=1,
            cost=0.0,
            decision=decision,
        )


def _engine(tmp_path, *, db_path=None, templates=None):
    db_path = db_path or tmp_path / "orchestrator.db"
    control = ControlPlane(db_path)
    registered_templates = (
        templates
        if templates is not None
        else {
            "orchestrator-tests": CommandTemplate(
                executable=sys.executable,
                args=("-c", "print('security gate operator test')"),
                timeout_seconds=10,
            )
        }
    )
    providers = {
        role: MockProvider(role)
        for role in (Role.ARCHITECT, Role.DEVELOPER, Role.AUDITOR)
    }
    engine = WorkflowEngine(
        control=control,
        router=RoleRouter(providers),
        budget=BudgetManager(
            db_path,
            daily_cost_ceiling=1.0,
            monthly_cost_ceiling=10.0,
            per_task_call_limit=10,
            per_role_call_limit=20,
            failure_cooldown_seconds=0,
            daily_token_ceiling=1_000,
            monthly_token_ceiling=10_000,
        ),
        artifacts=ArtifactStore(tmp_path / "artifacts"),
        tools=ToolRunner(
            allowed_roots=(tmp_path,),
            templates=registered_templates,
        ),
        working_directory=str(tmp_path),
        estimated_model_cost=0.0,
        estimated_model_tokens=1,
    )
    return engine, control, engine.budget, providers


def _advance_to_testing(engine, control, task):
    control.create_task(task)
    current = task
    for _ in range(5):
        current = engine.step(task.task_id)
    assert current.state is TaskState.TESTING
    return current


def _last_audit_payload(control):
    with sqlite3.connect(control.path) as db:
        payload = db.execute(
            "SELECT payload FROM audit_events ORDER BY id DESC LIMIT 1"
        ).fetchone()[0]
    return json.loads(payload)


def _audit_payloads(control):
    with sqlite3.connect(control.path) as db:
        rows = db.execute("SELECT payload FROM audit_events ORDER BY id").fetchall()
    return [json.loads(row[0]) for row in rows]


class NeverEngine:
    def __init__(self):
        self.calls = 0

    def step(self, task_id):
        self.calls += 1
        raise AssertionError(f"generic engine must not receive discovery task {task_id}")


def test_empty_allowed_tools_blocks_tool_runner_invocation(tmp_path):
    engine, control, _, _ = _engine(tmp_path)
    _advance_to_testing(
        engine,
        control,
        Task.new(task_id="T-no-tools", goal="deny every tool", budget_limit=1.0),
    )

    with (
        patch.object(engine.tools, "run_allowed", wraps=engine.tools.run_allowed) as gate,
        patch.object(engine.tools, "_run_template", wraps=engine.tools._run_template) as execute,
    ):
        failed = engine.step("T-no-tools")
        retried = engine.step("T-no-tools")

    assert gate.call_count == 1
    execute.assert_not_called()
    assert failed.state is TaskState.FAILED
    assert retried == failed
    assert control.task_store.get("T-no-tools") == failed
    audit = _last_audit_payload(control)
    assert audit["action"] == "TOOL_POLICY_DENIED"
    assert audit["policy_result"] == "FORBIDDEN"
    assert audit["from_state"] == "TESTING"
    assert audit["to_state"] == "FAILED"
    assert audit["metadata"] == {
        "allowed_tools": [],
        "requested_tool": "orchestrator-tests",
    }
    assert sum(item["action"] == "TOOL_POLICY_DENIED" for item in _audit_payloads(control)) == 1


def test_explicitly_allowed_tool_is_the_only_tool_invoked(tmp_path):
    engine, control, _, _ = _engine(tmp_path)
    _advance_to_testing(
        engine,
        control,
        Task.new(
            task_id="T-one-tool",
            goal="run one explicitly allowed tool",
            allowed_tools=("orchestrator-tests",),
            budget_limit=1.0,
        ),
    )

    with patch.object(engine.tools, "run_allowed", wraps=engine.tools.run_allowed) as run:
        audited = engine.step("T-one-tool")

    run.assert_called_once_with(
        "orchestrator-tests",
        allowed_templates=("orchestrator-tests",),
        cwd=str(tmp_path),
    )
    assert audited.state is TaskState.AUDITING


def test_stage_tool_not_granted_to_task_is_blocked_fail_closed(tmp_path, monkeypatch):
    templates = {
        name: CommandTemplate(
            executable=sys.executable,
            args=("-c", f"print({name!r})"),
        )
        for name in ("tool-a", "tool-b")
    }
    engine, control, _, _ = _engine(tmp_path, templates=templates)
    monkeypatch.setitem(
        engine_module._STAGES,
        TaskState.TESTING,
        StageSpec(
            TaskState.AUDITING,
            "RUN_TESTS",
            tool_template="tool-b",
        ),
    )
    _advance_to_testing(
        engine,
        control,
        Task.new(
            task_id="T-wrong-tool",
            goal="deny a tool not granted to this task",
            allowed_tools=("tool-a",),
            budget_limit=1.0,
        ),
    )

    with patch.object(
        engine.tools,
        "_run_template",
        wraps=engine.tools._run_template,
    ) as execute:
        failed = engine.step("T-wrong-tool")

    execute.assert_not_called()
    assert failed.state is TaskState.FAILED
    assert "not explicitly allowed" in _last_audit_payload(control)["error"]


def test_unknown_tool_name_is_blocked_before_tool_runner_invocation(tmp_path, monkeypatch):
    engine, control, _, _ = _engine(tmp_path)
    unknown_tool = "unknown-tool-template"
    monkeypatch.setitem(
        engine_module._STAGES,
        TaskState.TESTING,
        StageSpec(
            TaskState.AUDITING,
            "RUN_TESTS",
            tool_template=unknown_tool,
        ),
    )
    _advance_to_testing(
        engine,
        control,
        Task.new(
            task_id="T-unknown-tool",
            goal="deny unknown tool",
            allowed_tools=(unknown_tool,),
            budget_limit=1.0,
        ),
    )

    with patch.object(
        engine.tools,
        "_run_template",
        wraps=engine.tools._run_template,
    ) as execute:
        failed = engine.step("T-unknown-tool")

    execute.assert_not_called()
    assert failed.state is TaskState.FAILED
    assert _last_audit_payload(control)["error"] == f"unknown tool template: {unknown_tool}"


@pytest.mark.parametrize(
    "near_miss",
    (
        "Orchestrator-tests",
        " orchestrator-tests",
        "orchestrator-tests ",
        "orchestrator-test",
        "orchestrator-tests-extra",
    ),
)
def test_tool_permission_uses_exact_canonical_name(tmp_path, near_miss):
    engine, control, _, _ = _engine(tmp_path)
    _advance_to_testing(
        engine,
        control,
        Task.new(
            task_id="T-exact-name",
            goal="require exact tool name",
            allowed_tools=(near_miss,),
            budget_limit=1.0,
        ),
    )

    with patch.object(
        engine.tools,
        "_run_template",
        wraps=engine.tools._run_template,
    ) as execute:
        failed = engine.step("T-exact-name")

    execute.assert_not_called()
    assert failed.state is TaskState.FAILED


def test_tool_template_registry_is_immutable_and_copied(tmp_path):
    original = CommandTemplate(sys.executable, ("-c", "print('original')"))
    replacement = CommandTemplate(sys.executable, ("-c", "print('replacement')"))
    source = {"tool-a": original}
    runner = ToolRunner(allowed_roots=(tmp_path,), templates=source)

    with pytest.raises(TypeError):
        runner.templates["tool-a"] = replacement
    source["tool-a"] = replacement

    result = runner.run_allowed(
        "tool-a",
        allowed_templates=("tool-a",),
        cwd=tmp_path,
    )

    assert result.stdout.strip() == "original"


def test_dispatcher_skips_discovery_task_and_exposes_deterministic_reason(tmp_path):
    store = TaskStore(tmp_path / "tasks.db")
    discovery = Task.new(
        task_id="T-discovery",
        goal="create a research hypothesis",
        scope=DISCOVERY_SCOPE,
        priority=10,
    )
    generic = Task.new(task_id="T-generic", goal="run generic workflow", priority=1)
    store.save(discovery)
    store.save(generic)

    route = route_to_generic_workflow(discovery)

    assert not route.accepted
    assert route.reason == (
        "task scope signal_statistics/research_hypotheses_only belongs to the separate "
        "discovery workflow"
    )
    assert Dispatcher(store).next_runnable().task_id == "T-generic"


def test_generic_engine_rejects_discovery_task_without_state_transition(tmp_path):
    engine, control, budget, providers = _engine(tmp_path)
    discovery = Task.new(
        task_id="T-discovery",
        goal="create a research hypothesis",
        scope=DISCOVERY_SCOPE,
    )
    control.create_task(discovery)

    with pytest.raises(WorkflowRoutingError, match="belongs to the separate discovery workflow"):
        engine.step(discovery.task_id)

    persisted = control.task_store.get(discovery.task_id)
    assert persisted.state is TaskState.NEW
    assert persisted.assigned_role is Role.OPERATOR
    assert persisted.artifact_refs == ()
    assert budget.total_calls() == 0
    assert all(provider.seen == [] for provider in providers.values())
    assert control.audit_log.verify()


def test_only_discovery_task_leaves_generic_dispatcher_idle_without_mutation(tmp_path):
    store = TaskStore(tmp_path / "tasks.db")
    discovery = Task.new(
        task_id="T-discovery-only",
        goal="create a research hypothesis",
        scope=DISCOVERY_SCOPE,
    )
    store.save(discovery)

    assert Dispatcher(store).next_runnable() is None
    persisted = store.get(discovery.task_id)
    assert persisted.state is TaskState.NEW
    assert persisted.assigned_role is Role.OPERATOR


def test_service_reports_discovery_routing_diagnostic_without_engine_call(tmp_path):
    store = TaskStore(tmp_path / "tasks.db")
    discovery = Task.new(
        task_id="T-service-discovery",
        goal="create a research hypothesis",
        scope=DISCOVERY_SCOPE,
    )
    store.save(discovery)
    engine = NeverEngine()
    service = OrchestratorService(
        dispatcher=Dispatcher(store),
        engine=engine,
        idle_sleep_seconds=0,
    )

    tick = service.run_once()

    assert tick.status is ServiceStatus.IDLE
    assert tick.task_id is None
    assert len(tick.routing_diagnostics) == 1
    diagnostic = tick.routing_diagnostics[0]
    assert diagnostic.task_id == discovery.task_id
    assert diagnostic.reason == route_to_generic_workflow(discovery).reason
    assert engine.calls == 0
    assert store.get(discovery.task_id) == discovery

    empty_store = TaskStore(tmp_path / "empty.db")
    empty_tick = OrchestratorService(
        dispatcher=Dispatcher(empty_store),
        engine=NeverEngine(),
        idle_sleep_seconds=0,
    ).run_once()
    assert empty_tick.status is ServiceStatus.IDLE
    assert empty_tick.routing_diagnostics == ()


def test_stale_tool_denial_cannot_fail_a_newer_state_or_append_false_audit(tmp_path):
    engine, control, _, _ = _engine(tmp_path)
    testing = _advance_to_testing(
        engine,
        control,
        Task.new(task_id="T-stale-denial", goal="reject stale denial", budget_limit=1.0),
    )
    control.advance(
        testing.task_id,
        TaskState.AUDITING,
        revision=testing.revision,
        actor_role=Role.OPERATOR,
        action="RUN_TESTS",
        policy_result=PolicyDecision.AUTO_ALLOWED_WITH_AUDIT,
    )
    before = _audit_payloads(control)

    with pytest.raises(RevisionConflict, match="expected TESTING, got AUDITING"):
        control.system_halt(
            testing.task_id,
            TaskState.FAILED,
            revision=testing.revision,
            expected_state=TaskState.TESTING,
            action="TOOL_POLICY_DENIED",
            policy_result=PolicyDecision.FORBIDDEN,
            error="stale tool denial",
        )

    assert control.task_store.get(testing.task_id).state is TaskState.AUDITING
    assert _audit_payloads(control) == before


def test_denied_task_remains_terminal_after_engine_and_control_plane_restart(tmp_path):
    engine, control, _, _ = _engine(tmp_path)
    _advance_to_testing(
        engine,
        control,
        Task.new(task_id="T-restart-denial", goal="persist denial", budget_limit=1.0),
    )
    failed = engine.step("T-restart-denial")
    audit_before = _audit_payloads(control)

    restarted, restarted_control, _, _ = _engine(tmp_path, db_path=control.path)
    with patch.object(
        restarted.tools,
        "run_allowed",
        wraps=restarted.tools.run_allowed,
    ) as run:
        observed = restarted.step("T-restart-denial")

    run.assert_not_called()
    assert observed == failed
    assert restarted_control.task_store.get("T-restart-denial") == failed
    assert _audit_payloads(restarted_control) == audit_before


def test_tool_denial_rolls_back_state_when_audit_append_fails(tmp_path):
    engine, control, _, _ = _engine(tmp_path)
    _advance_to_testing(
        engine,
        control,
        Task.new(task_id="T-denial-rollback", goal="rollback denial", budget_limit=1.0),
    )
    audit_before = _audit_payloads(control)

    with (
        patch.object(
            AuditLog,
            "append_in_transaction",
            side_effect=RuntimeError("audit append failed"),
        ),
        patch.object(engine.tools, "_run_template", wraps=engine.tools._run_template) as execute,
        pytest.raises(RuntimeError, match="audit append failed"),
    ):
        engine.step("T-denial-rollback")

    execute.assert_not_called()
    assert control.task_store.get("T-denial-rollback").state is TaskState.TESTING
    assert _audit_payloads(control) == audit_before
