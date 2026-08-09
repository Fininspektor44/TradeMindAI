from trademind.orchestrator.models import PolicyDecision, RiskClass
from trademind.orchestrator.policy import classify_action


def test_forbidden_trade_action():
    result = classify_action("BROKER_ORDER_PLACE")
    assert result.decision is PolicyDecision.FORBIDDEN


def test_architecture_breaking_requires_human():
    result = classify_action("UPDATE_DOCUMENTATION", risk_class=RiskClass.ARCHITECTURE_BREAKING)
    assert result.decision is PolicyDecision.HUMAN_REQUIRED


def test_routine_tests_are_auto_allowed_with_audit():
    result = classify_action("RUN_TESTS")
    assert result.decision is PolicyDecision.AUTO_ALLOWED_WITH_AUDIT


def test_unknown_action_is_not_silently_allowed():
    result = classify_action("INVENTED_MODEL_ACTION")
    assert result.decision is PolicyDecision.HUMAN_REQUIRED


def test_explicit_safe_action_is_auto_allowed():
    result = classify_action("LOCAL_HEALTH_CHECK")
    assert result.decision is PolicyDecision.AUTO_ALLOWED
