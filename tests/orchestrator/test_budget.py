import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from trademind.orchestrator.agent_protocol import (
    AgentProtocolError,
    AgentResult,
    JsonPayloadError,
    V1_SCHEMA_VERSION,
    V2_SCHEMA_VERSION,
    canonical_json_dumps,
)
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
    payload = AgentResult(
        success=True,
        summary="cached answer",
        output_schema="result-v1",
        tokens=10,
        cost=0.1,
        structured_output={"hypothesis": {"direction": "POSITIVE"}},
    ).to_payload()
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


@pytest.mark.parametrize(
    "corrupt_json",
    (
        "{malformed",
        '{"success":NaN}',
        '{"success":true,"success":false}',
        '{"success":"false"}',
    ),
)
def test_corrupted_cache_is_inert_miss_and_is_not_deleted(tmp_path, corrupt_json):
    manager = make_manager(tmp_path)
    request_hash = manager.request_hash({"goal": "corrupt"})
    with sqlite3.connect(manager.path) as db:
        db.execute(
            "INSERT INTO request_cache(request_hash, created_at, result_json) VALUES (?, ?, ?)",
            (request_hash, datetime.now(timezone.utc).isoformat(), corrupt_json),
        )

    assert manager.cached_result(request_hash) is None
    assert manager.has_cached_entry(request_hash)
    check = manager.check(
        task_id="T-corrupt", role=Role.ARCHITECT, request_hash=request_hash
    )
    assert check.allowed and not check.cached
    with sqlite3.connect(manager.path) as db:
        assert db.execute(
            "SELECT result_json FROM request_cache WHERE request_hash=?", (request_hash,)
        ).fetchone()[0] == corrupt_json


def test_record_rejects_malformed_cache_payload_before_persistence(tmp_path):
    manager = make_manager(tmp_path)
    with pytest.raises((AgentProtocolError, JsonPayloadError, ValueError)):
        manager.record(
            task_id="T",
            role=Role.ARCHITECT,
            request_hash=manager.request_hash({"goal": "bad"}),
            cost=0.0,
            tokens=0,
            success=True,
            cacheable=True,
            cache_payload={"success": "false"},
        )
    assert manager.total_calls() == 0


def test_record_rejects_failure_result_before_usage_or_cache_persistence(tmp_path):
    manager = make_manager(tmp_path)
    request_hash = manager.request_hash({"goal": "failure-is-not-cacheable"})
    failed_payload = AgentResult(
        success=False,
        summary="provider failed",
        error="deterministic failure",
    ).to_payload()

    with pytest.raises(ValueError, match="metadata must match"):
        manager.record(
            task_id="T-failed-cache-write",
            role=Role.ARCHITECT,
            request_hash=request_hash,
            cost=0.1,
            tokens=10,
            success=True,
            cacheable=True,
            cache_payload=failed_payload,
        )

    assert manager.total_calls() == 0
    assert manager.cached_result(request_hash) is None
    assert not manager.has_cached_entry(request_hash)


@pytest.mark.parametrize(
    ("outer_success", "payload_success", "error_match"),
    (
        (False, True, "metadata must match"),
        (False, False, "only successful AgentResult"),
    ),
)
def test_record_requires_consistent_success_metadata_for_cache_payload(
    tmp_path, outer_success, payload_success, error_match
):
    manager = make_manager(tmp_path)
    request_hash = manager.request_hash(
        {"outer_success": outer_success, "payload_success": payload_success}
    )
    payload = AgentResult(
        success=payload_success,
        summary="cache candidate",
        output_schema="cache-result-v1" if payload_success else "",
        error=None if payload_success else "failed",
    ).to_payload()

    with pytest.raises(ValueError, match=error_match):
        manager.record(
            task_id="T-cache-metadata",
            role=Role.ARCHITECT,
            request_hash=request_hash,
            cost=0.0,
            tokens=0,
            success=outer_success,
            cacheable=True,
            cache_payload=payload,
        )

    assert manager.total_calls() == 0
    assert manager.cached_result(request_hash) is None
    assert not manager.has_cached_entry(request_hash)


def test_well_formed_failure_cache_entry_is_inert_and_not_deleted(tmp_path):
    manager = make_manager(tmp_path)
    request_hash = manager.request_hash({"goal": "stored-failure"})
    failed_json = canonical_json_dumps(
        AgentResult(
            success=False,
            summary="historical failed result",
            error="provider failed",
        ).to_payload()
    )
    with sqlite3.connect(manager.path) as db:
        db.execute(
            "INSERT INTO request_cache(request_hash, created_at, result_json) VALUES (?, ?, ?)",
            (request_hash, datetime.now(timezone.utc).isoformat(), failed_json),
        )

    assert manager.cached_result(request_hash) is None
    assert manager.has_cached_entry(request_hash)
    check = manager.check(
        task_id="T-stored-failure", role=Role.ARCHITECT, request_hash=request_hash
    )
    assert check.allowed and not check.cached
    assert manager.total_calls() == 0
    with sqlite3.connect(manager.path) as db:
        assert db.execute(
            "SELECT result_json FROM request_cache WHERE request_hash=?", (request_hash,)
        ).fetchone()[0] == failed_json


def test_structured_request_hash_changes_with_content_but_not_mapping_order(tmp_path):
    manager = make_manager(tmp_path)
    first = {
        "envelope": {
            "input_schema": "candidate-v0",
            "structured_input": {"symbol": "EURUSD", "horizon": 12},
        }
    }
    reordered = {
        "envelope": {
            "structured_input": {"horizon": 12, "symbol": "EURUSD"},
            "input_schema": "candidate-v0",
        }
    }
    different = {
        "envelope": {
            "input_schema": "candidate-v0",
            "structured_input": {"symbol": "EURUSD", "horizon": 24},
        }
    }

    assert manager.request_hash(first) == manager.request_hash(reordered)
    assert manager.request_hash(first) != manager.request_hash(different)


def test_protocol_versions_have_distinct_request_hashes(tmp_path):
    manager = make_manager(tmp_path)

    assert manager.request_hash({"schema_version": V1_SCHEMA_VERSION}) != manager.request_hash(
        {"schema_version": V2_SCHEMA_VERSION}
    )


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
