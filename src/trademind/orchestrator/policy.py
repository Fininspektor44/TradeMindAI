"""Deterministic action policy for Orchestrator v1."""

from __future__ import annotations

from dataclasses import dataclass

from .models import PolicyDecision, RiskClass


FORBIDDEN_ACTIONS = frozenset(
    {
        "BROKER_ORDER_PLACE",
        "BROKER_ORDER_MODIFY",
        "BROKER_ORDER_CANCEL",
        "ENABLE_WRITE_BROKER_API",
        "EXPOSE_SECRET_TO_MODEL",
        "BYPASS_FAILED_TESTS",
        "BYPASS_FAILED_AUDIT",
        "READ_PROTECTED_FINAL_HOLDOUT",
        "DELETE_AUDIT_HISTORY",
    }
)

HUMAN_REQUIRED_ACTIONS = frozenset(
    {
        "ENABLE_REAL_MONEY_TRADING",
        "ADD_WRITE_BROKER_CREDENTIAL",
        "INCREASE_RISK_LIMIT",
        "CHANGE_MASTER_RESEARCH_METHODOLOGY",
        "CHANGE_HOLDOUT_RULES",
        "REWRITE_PROTECTED_HISTORY",
        "MERGE_ARCHITECTURE_BREAKING",
        "RAISE_AI_BUDGET",
        "ALTER_APPROVAL_POLICY",
    }
)

AUDITED_ACTIONS = frozenset(
    {
        "RUN_TESTS",
        "RUN_LINTER",
        "CREATE_CODE_PATCH",
        "CREATE_RESEARCH_INFRASTRUCTURE",
        "UPDATE_DOCUMENTATION",
    }
)


@dataclass(frozen=True, slots=True)
class PolicyResult:
    decision: PolicyDecision
    reason: str


def classify_action(action: str, *, risk_class: RiskClass = RiskClass.LOW) -> PolicyResult:
    normalized = action.strip().upper()
    if normalized in FORBIDDEN_ACTIONS:
        return PolicyResult(PolicyDecision.FORBIDDEN, f"{normalized} is forbidden in v1")
    if normalized in HUMAN_REQUIRED_ACTIONS or risk_class is RiskClass.ARCHITECTURE_BREAKING:
        return PolicyResult(
            PolicyDecision.HUMAN_REQUIRED,
            f"{normalized} requires explicit user approval",
        )
    if normalized in AUDITED_ACTIONS or risk_class is RiskClass.HIGH:
        return PolicyResult(
            PolicyDecision.AUTO_ALLOWED_WITH_AUDIT,
            f"{normalized} is allowed with durable audit evidence",
        )
    return PolicyResult(PolicyDecision.AUTO_ALLOWED, f"{normalized} is auto-allowed")
