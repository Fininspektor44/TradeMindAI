"""Route AI-capable orchestrator roles to replaceable provider adapters."""

from __future__ import annotations

from collections.abc import Mapping

from .agent_protocol import AgentEnvelope, AgentProvider, AgentResult
from .models import Role, Task
from .policy import FORBIDDEN_ACTIONS


class RoleRoutingError(RuntimeError):
    pass


_AI_ROLES = frozenset({Role.ARCHITECT, Role.DEVELOPER, Role.AUDITOR})


class RoleRouter:
    """Build structured envelopes and route only explicitly configured AI roles."""

    def __init__(self, providers: Mapping[Role, AgentProvider]) -> None:
        invalid = set(providers) - _AI_ROLES
        if invalid:
            names = ", ".join(sorted(role.value for role in invalid))
            raise ValueError(f"non-AI roles cannot have model providers: {names}")
        self._providers = dict(providers)

    @staticmethod
    def envelope_for(
        task: Task,
        *,
        role: Role,
        required_output_schema: str,
    ) -> AgentEnvelope:
        if role not in _AI_ROLES:
            raise RoleRoutingError(f"{role.value} is deterministic local software in v1")
        if not required_output_schema.strip():
            raise ValueError("required_output_schema must not be empty")
        return AgentEnvelope(
            task_id=task.task_id,
            revision=task.revision,
            role=role,
            goal=task.goal,
            scope=task.scope,
            forbidden_actions=tuple(sorted(FORBIDDEN_ACTIONS)),
            acceptance_criteria=task.acceptance_criteria,
            artifact_refs=task.artifact_refs,
            required_output_schema=required_output_schema,
        )

    def provider_for(self, role: Role) -> AgentProvider:
        if role not in _AI_ROLES:
            raise RoleRoutingError(f"{role.value} is deterministic local software in v1")
        provider = self._providers.get(role)
        if provider is None:
            raise RoleRoutingError(f"no provider configured for {role.value}")
        return provider

    def execute(
        self,
        task: Task,
        *,
        role: Role,
        required_output_schema: str,
    ) -> AgentResult:
        provider = self.provider_for(role)
        envelope = self.envelope_for(
            task,
            role=role,
            required_output_schema=required_output_schema,
        )
        return provider.execute(envelope)
