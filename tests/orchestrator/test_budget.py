from datetime import datetime, timezone

from trademind.orchestrator.budget import BudgetManager
from trademind.orchestrator.models import Role


def make_manager(tmp_path):
    return BudgetManager(
        tmp_path / "budget.db",
        daily_cost_ceiling=1.0,
        monthly_cost_ceiling=10.0,
        per_task_call_limit=2,
        per_role_call_limit=10,
        failure_cooldown_seconds=0,
    )


def test_idle_manager_makes_zero_calls(tmp_path):
    manager = make_manager(tmp_path)
    assert manager.total_calls() == 0


def test_per_task_limit_and_persistence(tmp_path):
    manager = make_manager(tmp_path)
    now = datetime.now(timezone.utc)
    for index in range(2):
        request_hash = manager.request_hash({"n": index})
        assert manager.check(
            task_id="T", role=Role.DEVELOPER, request_hash=request_hash, now=now
        ).allowed
        manager.record(
            task_id="T",
            role=Role.DEVELOPER,
            request_hash=request_hash,
            cost=0.1,
            tokens=10,
            success=True,
            now=now,
        )
    reopened = make_manager(tmp_path)
    request_hash = reopened.request_hash({"n": 3})
    result = reopened.check(
        task_id="T", role=Role.DEVELOPER, request_hash=request_hash, now=now
    )
    assert not result.allowed


def test_cache_deduplicates_identical_request(tmp_path):
    manager = make_manager(tmp_path)
    request_hash = manager.request_hash({"goal": "same"})
    manager.record(
        task_id="T",
        role=Role.ARCHITECT,
        request_hash=request_hash,
        cost=0.1,
        tokens=10,
        success=True,
        cacheable=True,
    )
    result = manager.check(
        task_id="OTHER", role=Role.ARCHITECT, request_hash=request_hash
    )
    assert result.allowed and result.cached
