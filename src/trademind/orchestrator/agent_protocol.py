"""Vendor-neutral structured contract for orchestrator AI workers."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from .models import Role


SCHEMA_VERSION = "orchestrator-agent-v1"


class AgentProtocolError(RuntimeError):
    pass


class AgentDecision(StrEnum):
    CONTINUE = "CONTINUE"
    APPROVE = "APPROVE"
    REJECT = "REJECT"


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

    def __post_init__(self) -> None:
        if not self.task_id.strip():
            raise ValueError("task_id must not be empty")
        if self.revision < 1:
            raise ValueError("revision must be positive")
        if not self.goal.strip():
            raise ValueError("goal must not be empty")
        if not self.required_output_schema.strip():
            raise ValueError("required_output_schema must not be empty")

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
    decision: AgentDecision = AgentDecision.CONTINUE

    def __post_init__(self) -> None:
        if self.tokens < 0 or self.cost < 0:
            raise ValueError("agent usage must be non-negative")
        if self.success and self.error:
            raise ValueError("successful agent result cannot contain an error")
        if self.success and not self.output_schema.strip():
            raise ValueError("successful agent result must declare output_schema")
        if not self.success and not self.error:
            raise ValueError("failed agent result must contain an error")
        if not self.success and self.decision is not AgentDecision.CONTINUE:
            raise ValueError("failed transport/result cannot carry approve/reject decision")


class AgentProvider(Protocol):
    """Replaceable model-provider adapter. No vendor is hard-coded into workflow logic."""

    @property
    def provider_name(self) -> str: ...

    @property
    def model_name(self) -> str: ...

    def execute(self, envelope: AgentEnvelope) -> AgentResult: ...


def validate_result(envelope: AgentEnvelope, result: AgentResult) -> AgentResult:
    """Reject a successful response whose schema does not match the frozen request."""
    if result.success and result.output_schema != envelope.required_output_schema:
        raise AgentProtocolError(
            "agent output schema mismatch: "
            f"expected {envelope.required_output_schema}, got {result.output_schema}"
        )
    return result
