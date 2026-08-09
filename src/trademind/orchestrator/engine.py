"""Bounded deterministic workflow engine for one Orchestrator v1 task revision."""

from __future__ import annotations

from dataclasses import asdict, dataclass

from .agent_protocol import AgentResult
from .artifact_store import ArtifactStore
from .budget import BudgetManager
from .control_plane import ControlPlane
from .models import PolicyDecision, Role, Task, TaskState
from .policy import classify_action
from .role_router import RoleRouter
from .tool_runner import ToolRunner


class WorkflowError(RuntimeError):
    pass


class WorkflowPolicyError(WorkflowError):
    pass


@dataclass(frozen=True, slots=True)
class StageSpec:
    target: TaskState
    action: str
    output_schema: str | None = None
    tool_template: str | None = None


_STAGES = {
    TaskState.NEW: StageSpec(TaskState.TRIAGED, "DISPATCH_TASK"),
    TaskState.TRIAGED: StageSpec(
        TaskState.SPECIFIED,
        "UPDATE_DOCUMENTATION",
        output_schema="architect-spec-v1",
    ),
    TaskState.SPECIFIED: StageSpec(
        TaskState.ARCH_REVIEWED,
        "WRITE_AUDIT_EVENT",
        output_schema="spec-audit-v1",
    ),
    TaskState.ARCH_REVIEWED: StageSpec(
        TaskState.IMPLEMENTING,
        "CREATE_CODE_PATCH",
        output_schema="implementation-plan-v1",
    ),
    TaskState.IMPLEMENTING: StageSpec(
        TaskState.TESTING,
        "CREATE_CODE_PATCH",
        output_schema="implementation-result-v1",
    ),
    TaskState.TESTING: StageSpec(
        TaskState.AUDITING,
        "RUN_TESTS",
        tool_template="orchestrator-tests",
    ),
    TaskState.AUDITING: StageSpec(
        TaskState.READY,
        "WRITE_AUDIT_EVENT",
        output_schema="final-audit-v1",
    ),
    TaskState.READY: StageSpec(TaskState.COMPLETED, "LOCAL_HEALTH_CHECK"),
}

_STOPPED = {
    TaskState.COMPLETED,
    TaskState.BLOCKED,
    TaskState.REJECTED,
    TaskState.FAILED,
    TaskState.HUMAN_REQUIRED,
    TaskState.CANCELLED,
}

_AI_ROLES = frozenset({Role.ARCHITECT, Role.DEVELOPER, Role.AUDITOR})


class WorkflowEngine:
    """Advance one task through role-separated stages with bounded side effects."""

    def __init__(
        self,
        *,
        control: ControlPlane,
        router: RoleRouter,
        budget: BudgetManager,
        artifacts: ArtifactStore,
        tools: ToolRunner,
        working_directory: str,
        estimated_model_cost: float = 0.0,
        estimated_model_tokens: int = 0,
    ) -> None:
        if estimated_model_cost < 0 or estimated_model_tokens < 0:
            raise ValueError("model estimates must be non-negative")
        self.control = control
        self.router = router
        self.budget = budget
        self.artifacts = artifacts
        self.tools = tools
        self.working_directory = working_directory
        self.estimated_model_cost = float(estimated_model_cost)
        self.estimated_model_tokens = int(estimated_model_tokens)

    @staticmethod
    def _policy(task: Task, action: str) -> PolicyDecision:
        result = classify_action(action, risk_class=task.risk_class)
        if result.decision not in {
            PolicyDecision.AUTO_ALLOWED,
            PolicyDecision.AUTO_ALLOWED_WITH_AUDIT,
        }:
            raise WorkflowPolicyError(result.reason)
        return result.decision

    def _store_agent_result(
        self,
        task: Task,
        *,
        role: Role,
        provider_name: str,
        model_name: str,
        result: AgentResult,
    ) -> str:
        artifact = self.artifacts.store_json(
            task_id=task.task_id,
            revision=task.revision,
            kind=f"{task.state.value.lower()}-{role.value.lower()}",
            payload={
                "role": role.value,
                "provider": provider_name,
                "model": model_name,
                "success": result.success,
                "summary": result.summary,
                "output_schema": result.output_schema,
                "tokens": result.tokens,
                "cost": result.cost,
                "error": result.error,
                "declared_artifact_refs": list(result.artifact_refs),
            },
        )
        return artifact.hash_ref

    @staticmethod
    def _cached_agent_result(payload: dict) -> AgentResult:
        return AgentResult(
            success=bool(payload["success"]),
            summary=str(payload["summary"]),
            artifact_refs=tuple(payload.get("artifact_refs", ())),
            output_schema=str(payload.get("output_schema", "")),
            tokens=int(payload.get("tokens", 0)),
            cost=float(payload.get("cost", 0.0)),
            error=payload.get("error"),
        )

    def _run_ai_stage(self, task: Task, spec: StageSpec) -> Task:
        role = task.assigned_role
        if role not in _AI_ROLES or spec.output_schema is None:
            raise WorkflowError(f"invalid AI stage configuration for {task.state.value}")

        policy = self._policy(task, spec.action)
        provider = self.router.provider_for(role)
        envelope = self.router.envelope_for(
            task,
            role=role,
            required_output_schema=spec.output_schema,
        )
        request_hash = self.budget.request_hash(envelope.to_payload())

        cached_payload = self.budget.cached_result(request_hash)
        if cached_payload is not None:
            result = self._cached_agent_result(cached_payload)
            if result.output_schema != spec.output_schema:
                raise WorkflowError("cached model result schema does not match requested stage")
            cached_provider = f"cache:{provider.provider_name}"
            artifact_hash = self._store_agent_result(
                task,
                role=role,
                provider_name=cached_provider,
                model_name=provider.model_name,
                result=result,
            )
            return self.control.advance(
                task.task_id,
                spec.target,
                revision=task.revision,
                actor_role=role,
                action=spec.action,
                policy_result=policy,
                model_provider=cached_provider,
                model_name=provider.model_name,
                output_artifact_hashes=(artifact_hash,),
            )

        check = self.budget.check(
            task_id=task.task_id,
            role=role,
            request_hash=request_hash,
            estimated_cost=self.estimated_model_cost,
            estimated_tokens=self.estimated_model_tokens,
            task_cost_ceiling=task.budget_limit,
        )
        if not check.allowed:
            artifact = self.artifacts.store_json(
                task_id=task.task_id,
                revision=task.revision,
                kind=f"{task.state.value.lower()}-budget-gate",
                payload={
                    "role": role.value,
                    "reason": check.reason,
                    "request_hash": request_hash,
                    "estimated_cost": self.estimated_model_cost,
                    "estimated_tokens": self.estimated_model_tokens,
                    "task_cost_ceiling": task.budget_limit,
                },
            )
            return self.control.system_halt(
                task.task_id,
                TaskState.HUMAN_REQUIRED,
                revision=task.revision,
                action="MODEL_BUDGET_GATE",
                policy_result=PolicyDecision.HUMAN_REQUIRED,
                error=check.reason,
                metadata={
                    "requested_role": role.value,
                    "configured_provider": provider.provider_name,
                    "configured_model": provider.model_name,
                },
                output_artifact_hashes=(artifact.hash_ref,),
            )

        try:
            result = self.router.execute(
                task,
                role=role,
                required_output_schema=spec.output_schema,
            )
        except Exception as exc:
            self.budget.record(
                task_id=task.task_id,
                role=role,
                request_hash=request_hash,
                cost=0.0,
                tokens=0,
                success=False,
            )
            artifact = self.artifacts.store_json(
                task_id=task.task_id,
                revision=task.revision,
                kind=f"{task.state.value.lower()}-provider-error",
                payload={
                    "role": role.value,
                    "provider": provider.provider_name,
                    "model": provider.model_name,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "request_hash": request_hash,
                },
            )
            return self.control.system_halt(
                task.task_id,
                TaskState.FAILED,
                revision=task.revision,
                action="MODEL_PROVIDER_FAILURE",
                policy_result=policy,
                error=str(exc),
                model_provider=provider.provider_name,
                model_name=provider.model_name,
                output_artifact_hashes=(artifact.hash_ref,),
            )

        self.budget.record(
            task_id=task.task_id,
            role=role,
            request_hash=request_hash,
            cost=result.cost,
            tokens=result.tokens,
            success=result.success,
            cacheable=result.success,
            cache_payload=asdict(result) if result.success else None,
        )
        artifact_hash = self._store_agent_result(
            task,
            role=role,
            provider_name=provider.provider_name,
            model_name=provider.model_name,
            result=result,
        )

        target = spec.target if result.success else TaskState.FAILED
        return self.control.advance(
            task.task_id,
            target,
            revision=task.revision,
            actor_role=role,
            action=spec.action,
            policy_result=policy,
            model_provider=provider.provider_name,
            model_name=provider.model_name,
            output_artifact_hashes=(artifact_hash,),
        )

    def _run_operator_stage(self, task: Task, spec: StageSpec) -> Task:
        if task.assigned_role is not Role.OPERATOR:
            raise WorkflowError(f"{task.state.value} is not assigned to OPERATOR")
        policy = self._policy(task, spec.action)
        artifact_hashes: tuple[str, ...] = ()
        target = spec.target

        if spec.tool_template is not None:
            result = self.tools.run(spec.tool_template, cwd=self.working_directory)
            artifact = self.artifacts.store_json(
                task_id=task.task_id,
                revision=task.revision,
                kind=f"{task.state.value.lower()}-operator",
                payload=asdict(result),
            )
            artifact_hashes = (artifact.hash_ref,)
            if not result.success:
                target = TaskState.FAILED

        return self.control.advance(
            task.task_id,
            target,
            revision=task.revision,
            actor_role=Role.OPERATOR,
            action=spec.action,
            policy_result=policy,
            output_artifact_hashes=artifact_hashes,
        )

    def step(self, task_id: str) -> Task:
        task = self.control.task_store.get(task_id)
        if task is None:
            raise KeyError(task_id)
        if task.state in _STOPPED:
            return task
        spec = _STAGES.get(task.state)
        if spec is None:
            raise WorkflowError(f"no workflow stage for {task.state.value}")
        if task.assigned_role in _AI_ROLES:
            return self._run_ai_stage(task, spec)
        return self._run_operator_stage(task, spec)

    def run_until_stopped(self, task_id: str, *, max_steps: int = 16) -> Task:
        if max_steps < 1:
            raise ValueError("max_steps must be positive")
        task = self.control.task_store.get(task_id)
        if task is None:
            raise KeyError(task_id)
        for _ in range(max_steps):
            if task.state in _STOPPED:
                return task
            task = self.step(task_id)
        raise WorkflowError(f"workflow exceeded max_steps={max_steps}")
