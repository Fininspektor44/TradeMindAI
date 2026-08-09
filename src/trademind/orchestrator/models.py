"""Immutable schemas used by the TradeMindAI orchestrator."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any


class Role(StrEnum):
    ARCHITECT = "ARCHITECT"
    DEVELOPER = "DEVELOPER"
    AUDITOR = "AUDITOR"
    OPERATOR = "OPERATOR"


class TaskState(StrEnum):
    NEW = "NEW"
    TRIAGED = "TRIAGED"
    SPECIFIED = "SPECIFIED"
    ARCH_REVIEWED = "ARCH_REVIEWED"
    IMPLEMENTING = "IMPLEMENTING"
    TESTING = "TESTING"
    AUDITING = "AUDITING"
    READY = "READY"
    COMPLETED = "COMPLETED"
    BLOCKED = "BLOCKED"
    REJECTED = "REJECTED"
    FAILED = "FAILED"
    HUMAN_REQUIRED = "HUMAN_REQUIRED"
    CANCELLED = "CANCELLED"


class RiskClass(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    ARCHITECTURE_BREAKING = "ARCHITECTURE_BREAKING"


class PolicyDecision(StrEnum):
    AUTO_ALLOWED = "AUTO_ALLOWED"
    AUTO_ALLOWED_WITH_AUDIT = "AUTO_ALLOWED_WITH_AUDIT"
    HUMAN_REQUIRED = "HUMAN_REQUIRED"
    FORBIDDEN = "FORBIDDEN"


@dataclass(frozen=True, slots=True)
class Task:
    task_id: str
    revision: int
    parent_task_id: str | None
    created_at: str
    goal: str
    scope: tuple[str, ...]
    risk_class: RiskClass
    state: TaskState
    assigned_role: Role | None
    allowed_tools: tuple[str, ...]
    budget_limit: float
    acceptance_criteria: tuple[str, ...]
    artifact_refs: tuple[str, ...] = ()
    priority: int = 0
    resume_state: TaskState | None = None

    @classmethod
    def new(
        cls,
        *,
        task_id: str,
        goal: str,
        scope: tuple[str, ...] = (),
        risk_class: RiskClass = RiskClass.LOW,
        allowed_tools: tuple[str, ...] = (),
        budget_limit: float = 0.0,
        acceptance_criteria: tuple[str, ...] = (),
        priority: int = 0,
    ) -> "Task":
        if not task_id.strip():
            raise ValueError("task_id must not be empty")
        if not goal.strip():
            raise ValueError("goal must not be empty")
        if budget_limit < 0:
            raise ValueError("budget_limit must not be negative")
        return cls(
            task_id=task_id,
            revision=1,
            parent_task_id=None,
            created_at=datetime.now(timezone.utc).isoformat(),
            goal=goal,
            scope=tuple(scope),
            risk_class=risk_class,
            state=TaskState.NEW,
            assigned_role=Role.OPERATOR,
            allowed_tools=tuple(allowed_tools),
            budget_limit=float(budget_limit),
            acceptance_criteria=tuple(acceptance_criteria),
            priority=int(priority),
        )


@dataclass(frozen=True, slots=True)
class AuditEvent:
    timestamp: str
    task_id: str
    revision: int
    actor_role: Role | None
    action: str
    model_provider: str | None = None
    model_name: str | None = None
    input_artifact_hashes: tuple[str, ...] = ()
    output_artifact_hashes: tuple[str, ...] = ()
    from_state: TaskState | None = None
    to_state: TaskState | None = None
    policy_result: PolicyDecision | None = None
    exit_code: int | None = None
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class BudgetCheck:
    allowed: bool
    reason: str
    cached: bool = False
