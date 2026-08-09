from datetime import datetime, timedelta, timezone

from trademind.orchestrator.budget import BudgetManager
from trademind.orchestrator.models import Role


def make_manager(tmp_path, **overrides):
    options = {
        "daily_cost_ceiling": 1.0,
        "monthly_cost_ceiling": 10.0,
        "per_task_call_limit": 2,
        "per_role_call_limit": 10,
        "failure_cooldown_seconds": 0,
        "daily_token_ceiling": 100,
        "monthly_token_ceiling": 1000,
    }
    options.update(overrides)
    return BudgetManager(tmp_path / "budget.db", **options)


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


def test_cache_deduplicates_and_restores_identical_result(tmp_path):
    manager = make_manager(tmp_path)
    request_hash = manager.request_hash({"goal": "same"})
    payload = {
        "success": True,
        "summary": "cached answer",
        "artifact_refs": [],
        "output_schema": "result-v1",
        "tokens": 10,
        "cost": 0.1,
        "error": None,
    }
    manager.record(
        task_id="T",
        role=Role.ARCHITECT,
        request_hash=request_hash,
        cost=0.1,
        tokens=10,
        success=True,
        cacheable=True,
        cache_payload=payload,
    )

    reopened = make_manager(tmp_path)
    result = reopened.check(
        task_id="OTHER", role=Role.ARCHITECT, request_hash=request_hash
    )
    assert result.allowed and result.cached
    assert reopened.cached_result(request_hash) == payload


def test_projected_cost_and_token_usage_cannot_overshoot_ceiling(tmp_path):
    manager = make_manager(tmp_path)
    now = datetime(2026, 8, 9, 12, tzinfo=timezone.utc)
    manager.record(
        task_id="T1",
        role=Role.ARCHITECT,
        request_hash=manager.request_hash({"n": 1}),
        cost=0.8,
        tokens=80,
        success=True,
        now=now,
    )

    cost_check = manager.check(
        task_id="T2",
        role=Role.DEVELOPER,
        request_hash=manager.request_hash({"n": 2}),
        now=now,
        estimated_cost=0.21,
    )
    assert not cost_check.allowed
    assert "daily model cost" in cost_check.reason

    token_check = manager.check(
        task_id="T2",
        role=Role.DEVELOPER,
        request_hash=manager.request_hash({"n": 3}),
        now=now,
        estimated_tokens=21,
    )
    assert not token_check.allowed
    assert "daily model token" in token_check.reason


def test_daily_role_limit_resets_on_next_day(tmp_path):
    manager = make_manager(tmp_path, per_role_call_limit=1)
    first_day = datetime(2026, 8, 9, 12, tzinfo=timezone.utc)
    manager.record(
        task_id="T1",
        role=Role.AUDITOR,
        request_hash=manager.request_hash({"day": 1}),
        cost=0.1,
        tokens=10,
        success=True,
        now=first_day,
    )

    same_day = manager.check(
        task_id="T2",
        role=Role.AUDITOR,
        request_hash=manager.request_hash({"day": "same"}),
        now=first_day,
    )
    assert not same_day.allowed

    next_day = manager.check(
        task_id="T3",
        role=Role.AUDITOR,
        request_hash=manager.request_hash({"day": 2}),
        now=first_day + timedelta(days=1),
    )
    assert next_day.allowed
