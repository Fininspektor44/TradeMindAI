"""Vendor-neutral structured contract for orchestrator AI workers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .models import Role


SCHEMA_VERSION = "orchestrator-agent-v1"


@dataclass(frozen=True, slots=True)
class AgentEnvelope:
    task_id: str
    revision: int
    role: Role
    goal: str
    scope: tuple[str, ...]
    forbidden_actions: tuple[str, ...]
    acceptance_criteria: tuple[str, ...]
    artifact_refs: tuple[str, ...]
    required_output_schema: str
    schema_version: str = SCHEMA_VERSION

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "task_id": self.task_id,
            "revision": self.revision,
            "role": self.role.value,
            "goal": self.goal,
            "scope": list(self.scope),
            "forbidden_actions": list(self.forbidden_actions),
            "acceptance_criteria": list(self.acceptance_criteria),
            "artifacts": list(self.artifact_refs),
            "required_output_schema": self.required_output_schema,
        }


@dataclass(frozen=True, slots=True)
class AgentResult:
    success: bool
    summary: str
    artifact_refs: tuple[str, ...] = ()
    output_schema: str = ""
    tokens: int = 0
    cost: float = 0.0
    error: str | None = None

    def __post_init__(self) -> None:
        if self.tokens < 0 or self.cost < 0:
            raise ValueError("agent usage must be non-negative")
        if self.success and self.error:
            raise ValueError("successful agent result cannot contain an error")


class AgentProvider(Protocol):
    """Replaceable model-provider adapter. No vendor is hard-coded into workflow logic."""

    @property
    def provider_name(self) -> str: ...

    @property
    def model_name(self) -> str: ...

    def execute(self, envelope: AgentEnvelope) -> AgentResult: ...
