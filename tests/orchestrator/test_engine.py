import json
import sqlite3
import sys

from trademind.orchestrator.agent_protocol import (
    AgentDecision,
    AgentEnvelope,
    AgentResult,
    canonical_json_dumps,
)
from trademind.orchestrator.artifact_store import ArtifactStore
from trademind.orchestrator.budget import BudgetManager
from trademind.orchestrator.control_plane import ControlPlane
from trademind.orchestrator.engine import WorkflowEngine
from trademind.orchestrator.models import Role, Task, TaskState
from trademind.orchestrator.role_router import RoleRouter
from trademind.orchestrator.tool_runner import CommandTemplate, ToolRunner


class MockProvider:
    def __init__(self, role: Role) -> None:
        self.role = role
        self.seen: list[AgentEnvelope] = []

    @property
    def provider_name(self) -> str:
        return "mock-provider"

    @property
    def model_name(self) -> str:
        return f"mock-{self.role.value.lower()}"

    def execute(self, envelope: AgentEnvelope) -> AgentResult:
        assert envelope.role is self.role
        self.seen.append(envelope)
        decision = AgentDecision.APPROVE if self.role is Role.AUDITOR else AgentDecision.CONTINUE
        return AgentResult(
            success=True,
            summary=f"{self.role.value} completed {envelope.required_output_schema}",
            output_schema=envelope.required_output_schema,
            tokens=10,
            cost=0.01,
            decision=decision,
        )


class AlternateArchitectProvider(MockProvider):
    @property
    def provider_name(self) -> str:
        return "alternate-provider"

    @property
    def model_name(self) -> str:
        return "alternate-architect-model"


class StructuredArchitectProvider(MockProvider):
    def execute(self, envelope: AgentEnvelope) -> AgentResult:
        self.seen.append(envelope)
        return AgentResult(
            success=True,
            summary="structured provider result",
            artifact_refs=("sha256:declared",),
            output_schema=envelope.required_output_schema,
            tokens=10,
            cost=0.01,
            structured_output={
                "hypothesis": "cached structured machine result",
                "score": 0.75,
            },
        )


class RejectingAuditor(MockProvider):
    def execute(self, envelope: AgentEnvelope) -> AgentResult:
        self.seen.append(envelope)
        return AgentResult(
            success=True,
            summary="specification needs a new revision",
            output_schema=envelope.required_output_schema,
            tokens=10,
            cost=0.01,
            decision=AgentDecision.REJECT,
        )


def _engine(tmp_path, *, per_task_call_limit: int = 10):
    db_path = tmp_path / "orchestrator.db"
    control = ControlPlane(db_path)
    providers = {
        Role.ARCHITECT: MockProvider(Role.ARCHITECT),
        Role.DEVELOPER: MockProvider(Role.DEVELOPER),
        Role.AUDITOR: MockProvider(Role.AUDITOR),
    }
    router = RoleRouter(providers)
    budget = BudgetManager(
        db_path,
        daily_cost_ceiling=100.0,
        monthly_cost_ceiling=1000.0,
        per_task_call_limit=per_task_call_limit,
        per_role_call_limit=100,
        failure_cooldown_seconds=0,
        daily_token_ceiling=100_000,
        monthly_token_ceiling=1_000_000,
    )
    artifacts = ArtifactStore(tmp_path / "artifacts")
    tools = ToolRunner(
        allowed_roots=(tmp_path,),
        templates={
            "orchestrator-tests": CommandTemplate(
                executable=sys.executable,
                args=("-c", "print('mock operator tests pass')"),
                timeout_seconds=10,
            )
        },
    )
    engine = WorkflowEngine(
        control=control,
        router=router,
        budget=budget,
        artifacts=artifacts,
        tools=tools,
        working_directory=str(tmp_path),
        estimated_model_cost=0.01,
        estimated_model_tokens=10,
    )
    return engine, control, budget, providers


def test_complete_mock_cycle_separates_roles_and_preserves_evidence(tmp_path):
    engine, control, budget, providers = _engine(tmp_path)
    control.create_task(
        Task.new(
            task_id="T-cycle",
            goal="prove the bounded mock orchestration cycle",
            scope=("src/trademind/orchestrator",),
            allowed_tools=("orchestrator-tests",),
            budget_limit=1.0,
            acceptance_criteria=("mock cycle reaches COMPLETED",),
        )
    )

    completed = engine.run_until_stopped("T-cycle")

    assert completed.state is TaskState.COMPLETED
    assert completed.assigned_role is None
    assert len(completed.artifact_refs) == 6
    assert budget.total_calls() == 5
    assert len(providers[Role.ARCHITECT].seen) == 1
    assert len(providers[Role.DEVELOPER].seen) == 2
    assert len(providers[Role.AUDITOR].seen) == 2
    assert control.audit_log.verify()

    with sqlite3.connect(control.path) as db:
        payloads = [
            row[0]
            for row in db.execute("SELECT payload FROM audit_events").fetchall()
        ]
    events = sum(
        1 for payload in payloads if json.loads(payload)["task_id"] == "T-cycle"
    )
    assert events == 9


def test_auditor_rejection_creates_new_revision_without_overwrite(tmp_path):
    engine, control, _, providers = _engine(tmp_path)
    rejecting = RejectingAuditor(Role.AUDITOR)
    providers[Role.AUDITOR] = rejecting
    engine.router = RoleRouter(providers)
    control.create_task(
        Task.new(task_id="T-reject", goal="force revision", budget_limit=1.0)
    )

    engine.step("T-reject")
    engine.step("T-reject")
    revised = engine.step("T-reject")

    assert revised.revision == 2
    assert revised.state is TaskState.NEW
    assert revised.parent_task_id == "T-reject@1"
    assert control.task_store.get("T-reject", 1).state is TaskState.REJECTED
    assert control.task_store.get("T-reject", 2).state is TaskState.NEW
    assert control.audit_log.verify()


def test_budget_gate_halts_durably_before_model_call(tmp_path):
    engine, control, budget, providers = _engine(tmp_path, per_task_call_limit=0)
    control.create_task(
        Task.new(task_id="T-budget", goal="respect budget", budget_limit=1.0)
    )

    triaged = engine.step("T-budget")
    assert triaged.state is TaskState.TRIAGED

    halted = engine.step("T-budget")
    assert halted.state is TaskState.HUMAN_REQUIRED
    assert halted.resume_state is TaskState.TRIAGED
    assert len(halted.artifact_refs) == 1
    assert providers[Role.ARCHITECT].seen == []
    assert budget.total_calls() == 0
    assert control.audit_log.verify()


def test_zero_task_cost_budget_blocks_spend_even_with_global_budget(tmp_path):
    engine, control, budget, providers = _engine(tmp_path)
    control.create_task(Task.new(task_id="T-zero", goal="zero spend by default"))

    engine.step("T-zero")
    halted = engine.step("T-zero")

    assert halted.state is TaskState.HUMAN_REQUIRED
    assert providers[Role.ARCHITECT].seen == []
    assert budget.total_calls() == 0


def test_cached_result_advances_without_second_provider_call(tmp_path):
    engine, control, budget, providers = _engine(tmp_path)
    control.create_task(
        Task.new(task_id="T-cache", goal="reuse exact request", budget_limit=1.0)
    )
    triaged = engine.step("T-cache")
    provider = StructuredArchitectProvider(Role.ARCHITECT)
    providers[Role.ARCHITECT] = provider
    engine.router = RoleRouter(providers)
    envelope = engine.router.envelope_for(
        triaged,
        role=Role.ARCHITECT,
        required_output_schema="architect-spec-v1",
    )
    request_hash = engine._model_request_hash(
        budget,
        provider_name=provider.provider_name,
        model_name=provider.model_name,
        envelope_payload=envelope.to_payload(),
    )
    result = provider.execute(envelope)
    budget.record(
        task_id="T-cache",
        role=Role.ARCHITECT,
        request_hash=request_hash,
        cost=0.01,
        tokens=10,
        success=True,
        cacheable=True,
        cache_payload=result.to_payload(),
    )

    engine, control, budget, providers = _engine(tmp_path)

    advanced = engine.step("T-cache")

    assert advanced.state is TaskState.SPECIFIED
    assert providers[Role.ARCHITECT].seen == []
    assert budget.total_calls() == 1
    assert control.audit_log.verify()
    artifacts = list((tmp_path / "artifacts").rglob("*triaged-architect*.json"))
    assert len(artifacts) == 1
    stored = json.loads(artifacts[0].read_text(encoding="utf-8"))
    assert stored["structured_output"] == {
        "hypothesis": "cached structured machine result",
        "score": 0.75,
    }
    assert stored["summary"] == "structured provider result"
    assert stored["success"] is True
    assert stored["output_schema"] == "architect-spec-v1"
    assert stored["tokens"] == 10
    assert stored["cost"] == 0.01
    assert stored["decision"] == "CONTINUE"
    assert stored["declared_artifact_refs"] == ["sha256:declared"]


def test_corrupted_cache_cannot_advance_task_and_valid_provider_result_replaces_it(tmp_path):
    engine, control, budget, providers = _engine(tmp_path)
    control.create_task(
        Task.new(task_id="T-corrupt-cache", goal="reject corrupt cache", budget_limit=1.0)
    )
    triaged = engine.step("T-corrupt-cache")
    provider = providers[Role.ARCHITECT]
    envelope = engine.router.envelope_for(
        triaged,
        role=Role.ARCHITECT,
        required_output_schema="architect-spec-v1",
    )
    request_hash = engine._model_request_hash(
        budget,
        provider_name=provider.provider_name,
        model_name=provider.model_name,
        envelope_payload=envelope.to_payload(),
    )
    corrupt_json = '{"success":"false"}'
    with sqlite3.connect(control.path) as db:
        db.execute(
            "INSERT INTO request_cache(request_hash, created_at, result_json) VALUES (?, ?, ?)",
            (request_hash, "2026-08-13T00:00:00+00:00", corrupt_json),
        )

    advanced = engine.step("T-corrupt-cache")

    assert advanced.state is TaskState.SPECIFIED
    assert len(provider.seen) == 1
    assert budget.total_calls() == 1
    cached = budget.cached_result(request_hash)
    assert cached is not None
    assert cached["success"] is True
    assert control.audit_log.verify()


def test_failure_cache_entry_is_inert_then_provider_runs_normally(tmp_path):
    engine, control, budget, providers = _engine(tmp_path)
    control.create_task(
        Task.new(
            task_id="T-failure-cache",
            goal="ignore cached failed result",
            budget_limit=1.0,
        )
    )
    triaged = engine.step("T-failure-cache")
    provider = providers[Role.ARCHITECT]
    envelope = engine.router.envelope_for(
        triaged,
        role=Role.ARCHITECT,
        required_output_schema="architect-spec-v1",
    )
    request_hash = engine._model_request_hash(
        budget,
        provider_name=provider.provider_name,
        model_name=provider.model_name,
        envelope_payload=envelope.to_payload(),
    )
    failed_json = canonical_json_dumps(
        AgentResult(
            success=False,
            summary="historical failed result",
            output_schema="architect-spec-v1",
            error="provider failed",
        ).to_payload()
    )
    with sqlite3.connect(control.path) as db:
        db.execute(
            "INSERT INTO request_cache(request_hash, created_at, result_json) VALUES (?, ?, ?)",
            (request_hash, "2026-08-13T00:00:00+00:00", failed_json),
        )
        audit_count_before = db.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0]

    task_before = control.task_store.get("T-failure-cache")
    assert budget.cached_result(request_hash) is None
    assert control.task_store.get("T-failure-cache") == task_before
    assert budget.total_calls() == 0
    with sqlite3.connect(control.path) as db:
        assert db.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0] == audit_count_before
        assert db.execute(
            "SELECT result_json FROM request_cache WHERE request_hash=?", (request_hash,)
        ).fetchone()[0] == failed_json

    advanced = engine.step("T-failure-cache")

    assert advanced.state is TaskState.SPECIFIED
    assert len(provider.seen) == 1
    assert budget.total_calls() == 1
    cached = budget.cached_result(request_hash)
    assert cached is not None
    assert cached["success"] is True
    assert control.audit_log.verify()


def test_corrupted_cache_cannot_advance_when_provider_call_is_budget_blocked(tmp_path):
    engine, control, budget, providers = _engine(tmp_path, per_task_call_limit=0)
    control.create_task(
        Task.new(task_id="T-corrupt-blocked", goal="do not trust cache", budget_limit=1.0)
    )
    triaged = engine.step("T-corrupt-blocked")
    provider = providers[Role.ARCHITECT]
    envelope = engine.router.envelope_for(
        triaged,
        role=Role.ARCHITECT,
        required_output_schema="architect-spec-v1",
    )
    request_hash = engine._model_request_hash(
        budget,
        provider_name=provider.provider_name,
        model_name=provider.model_name,
        envelope_payload=envelope.to_payload(),
    )
    with sqlite3.connect(control.path) as db:
        failed_json = canonical_json_dumps(
            AgentResult(
                success=False,
                summary="historical failed result",
                output_schema="architect-spec-v1",
                error="provider failed",
            ).to_payload()
        )
        db.execute(
            "INSERT INTO request_cache(request_hash, created_at, result_json) VALUES (?, ?, ?)",
            (request_hash, "2026-08-13T00:00:00+00:00", failed_json),
        )

    halted = engine.step("T-corrupt-blocked")

    assert halted.state is TaskState.HUMAN_REQUIRED
    assert providers[Role.ARCHITECT].seen == []
    assert budget.total_calls() == 0
    assert budget.has_cached_entry(request_hash)
    with sqlite3.connect(control.path) as db:
        assert db.execute(
            "SELECT result_json FROM request_cache WHERE request_hash=?", (request_hash,)
        ).fetchone()[0] == failed_json
    assert control.audit_log.verify()


def test_cache_is_not_reused_after_provider_or_model_change(tmp_path):
    engine, control, budget, providers = _engine(tmp_path)
    control.create_task(
        Task.new(task_id="T-provenance", goal="keep cache provenance", budget_limit=1.0)
    )
    triaged = engine.step("T-provenance")
    original = providers[Role.ARCHITECT]
    envelope = engine.router.envelope_for(
        triaged,
        role=Role.ARCHITECT,
        required_output_schema="architect-spec-v1",
    )
    original_hash = engine._model_request_hash(
        budget,
        provider_name=original.provider_name,
        model_name=original.model_name,
        envelope_payload=envelope.to_payload(),
    )
    assert original_hash != engine._model_request_hash(
        budget,
        provider_name="other-provider",
        model_name=original.model_name,
        envelope_payload=envelope.to_payload(),
    )
    assert original_hash != engine._model_request_hash(
        budget,
        provider_name=original.provider_name,
        model_name="other-model",
        envelope_payload=envelope.to_payload(),
    )
    budget.record(
        task_id="T-provenance",
        role=Role.ARCHITECT,
        request_hash=original_hash,
        cost=0.01,
        tokens=10,
        success=True,
        cacheable=True,
        cache_payload=AgentResult(
            success=True,
            summary="old provider result",
            output_schema="architect-spec-v1",
            tokens=10,
            cost=0.01,
        ).to_payload(),
    )

    alternate = AlternateArchitectProvider(Role.ARCHITECT)
    providers[Role.ARCHITECT] = alternate
    engine.router = RoleRouter(providers)
    advanced = engine.step("T-provenance")

    assert advanced.state is TaskState.SPECIFIED
    assert len(alternate.seen) == 1
    assert budget.total_calls() == 2
    assert control.audit_log.verify()


def test_failed_operator_tests_fail_closed_and_keep_test_artifact(tmp_path):
    engine, control, _, _ = _engine(tmp_path)
    engine.tools = ToolRunner(
        allowed_roots=(tmp_path,),
        templates={
            "orchestrator-tests": CommandTemplate(
                executable=sys.executable,
                args=("-c", "raise SystemExit(3)"),
                timeout_seconds=10,
            )
        },
    )
    control.create_task(
        Task.new(
            task_id="T-fail",
            goal="fail closed",
            allowed_tools=("orchestrator-tests",),
            budget_limit=1.0,
        )
    )

    task = control.task_store.get("T-fail")
    for _ in range(5):
        task = engine.step("T-fail")
    assert task.state is TaskState.TESTING

    failed = engine.step("T-fail")
    assert failed.state is TaskState.FAILED
    assert len(failed.artifact_refs) == 5
    assert control.audit_log.verify()
