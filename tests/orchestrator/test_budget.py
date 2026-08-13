import sqlite3
import threading
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from trademind.orchestrator.agent_protocol import (
    AgentProtocolError,
    AgentResult,
    JsonPayloadError,
    V1_SCHEMA_VERSION,
    V2_SCHEMA_VERSION,
    canonical_json_dumps,
)
from trademind.orchestrator.budget import (
    BudgetManager,
    BudgetReservationCapabilityError,
    BudgetReservationConflict,
    BudgetReservationState,
    BudgetReservationStateConflict,
)
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


@contextmanager
def reservation_transaction(manager: BudgetManager):
    db = sqlite3.connect(manager.path, timeout=30)
    db.row_factory = sqlite3.Row
    try:
        db.execute("BEGIN IMMEDIATE")
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def reserve(
    manager: BudgetManager,
    *,
    request_hash: str | None = None,
    task_id: str = "T-reserve",
    task_revision: int = 1,
    role: Role = Role.ARCHITECT,
    cost: float = 0.1,
    tokens: int = 10,
    task_cost_ceiling: float | None = None,
    now: datetime | None = None,
):
    request_hash = request_hash or manager.request_hash(
        {"task_id": task_id, "cost": cost, "tokens": tokens}
    )
    with reservation_transaction(manager) as db:
        return manager.reserve_in_transaction(
            db,
            task_id=task_id,
            task_revision=task_revision,
            role=role,
            request_hash=request_hash,
            estimated_cost=cost,
            estimated_tokens=tokens,
            task_cost_ceiling=task_cost_ceiling,
            now=now,
        )


def test_idle_manager_makes_zero_calls(tmp_path):
    manager = make_manager(tmp_path)
    assert manager.total_calls() == 0


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("daily_cost_ceiling", True),
        ("daily_cost_ceiling", float("nan")),
        ("daily_cost_ceiling", float("inf")),
        ("daily_cost_ceiling", 10**1000),
        ("per_task_call_limit", True),
        ("per_task_call_limit", 2**63),
        ("daily_token_ceiling", True),
        ("daily_token_ceiling", 2**63),
        ("failure_cooldown_seconds", 86_400_000_000_000),
    ),
)
def test_budget_configuration_rejects_unsafe_numeric_values(tmp_path, field, value):
    with pytest.raises(ValueError):
        make_manager(tmp_path, **{field: value})


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("cost", True),
        ("cost", float("nan")),
        ("cost", float("inf")),
        ("cost", 10**1000),
        ("tokens", True),
        ("tokens", 2**63),
    ),
)
def test_legacy_record_rejects_unsafe_numeric_values_without_usage(tmp_path, field, value):
    manager = make_manager(tmp_path)
    options = {
        "task_id": "T",
        "role": Role.ARCHITECT,
        "request_hash": manager.request_hash({"legacy-record": field}),
        "cost": 0.1,
        "tokens": 10,
        "success": True,
    }
    options[field] = value

    with pytest.raises(ValueError):
        manager.record(**options)

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
    result = reopened.check(task_id="T", role=Role.DEVELOPER, request_hash=request_hash, now=now)
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
    result = reopened.check(task_id="OTHER", role=Role.ARCHITECT, request_hash=request_hash)
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
    check = manager.check(task_id="T-corrupt", role=Role.ARCHITECT, request_hash=request_hash)
    assert check.allowed and not check.cached
    with sqlite3.connect(manager.path) as db:
        assert (
            db.execute(
                "SELECT result_json FROM request_cache WHERE request_hash=?", (request_hash,)
            ).fetchone()[0]
            == corrupt_json
        )


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
        assert (
            db.execute(
                "SELECT result_json FROM request_cache WHERE request_hash=?", (request_hash,)
            ).fetchone()[0]
            == failed_json
        )


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


def test_reserve_at_exact_limit_persists_and_blocks_following_capacity(tmp_path):
    manager = make_manager(tmp_path)
    request_hash = manager.request_hash({"reservation": "exact"})

    result = reserve(manager, request_hash=request_hash, cost=1.0, tokens=100)

    assert result.allowed and not result.cached
    assert result.reservation is not None
    assert result.reservation.state is BudgetReservationState.RESERVED
    assert result.reservation.reserved_cost == 1.0
    assert result.reservation.reserved_tokens == 100
    blocked = reserve(
        manager,
        request_hash=manager.request_hash({"reservation": "overflow"}),
        task_id="T-other",
        role=Role.DEVELOPER,
        cost=0.000001,
        tokens=0,
    )
    assert not blocked.allowed
    assert "daily model cost" in blocked.reason


def test_reservation_preserves_existing_aware_timestamp_convention(tmp_path):
    manager = make_manager(tmp_path)
    local_time = datetime(2026, 8, 13, 12, tzinfo=timezone(timedelta(hours=3)))
    request_hash = manager.request_hash({"timestamp": "existing-convention"})

    result = reserve(manager, request_hash=request_hash, now=local_time)

    assert result.reservation is not None
    assert result.reservation.created_at == local_time.isoformat()
    assert result.reservation.updated_at == local_time.isoformat()


@pytest.mark.parametrize(
    ("overrides", "first", "second", "reason"),
    (
        (
            {"per_task_call_limit": 1},
            {"task_id": "T", "role": Role.ARCHITECT, "cost": 0.0, "tokens": 0},
            {"task_id": "T", "role": Role.DEVELOPER, "cost": 0.0, "tokens": 0},
            "per-task model call",
        ),
        (
            {"per_role_call_limit": 1},
            {"task_id": "T1", "role": Role.AUDITOR, "cost": 0.0, "tokens": 0},
            {"task_id": "T2", "role": Role.AUDITOR, "cost": 0.0, "tokens": 0},
            "per-role model call",
        ),
        (
            {"daily_cost_ceiling": 0.1},
            {"task_id": "T1", "role": Role.ARCHITECT, "cost": 0.1, "tokens": 0},
            {"task_id": "T2", "role": Role.DEVELOPER, "cost": 0.000001, "tokens": 0},
            "daily model cost",
        ),
        (
            {"monthly_cost_ceiling": 0.1, "daily_cost_ceiling": 10.0},
            {"task_id": "T1", "role": Role.ARCHITECT, "cost": 0.1, "tokens": 0},
            {"task_id": "T2", "role": Role.DEVELOPER, "cost": 0.000001, "tokens": 0},
            "monthly model cost",
        ),
        (
            {"daily_token_ceiling": 10},
            {"task_id": "T1", "role": Role.ARCHITECT, "cost": 0.0, "tokens": 10},
            {"task_id": "T2", "role": Role.DEVELOPER, "cost": 0.0, "tokens": 1},
            "daily model token",
        ),
        (
            {"monthly_token_ceiling": 10, "daily_token_ceiling": 100},
            {"task_id": "T1", "role": Role.ARCHITECT, "cost": 0.0, "tokens": 10},
            {"task_id": "T2", "role": Role.DEVELOPER, "cost": 0.0, "tokens": 1},
            "monthly model token",
        ),
    ),
)
def test_reservations_apply_every_existing_ceiling(
    tmp_path,
    overrides,
    first,
    second,
    reason,
):
    manager = make_manager(tmp_path, **overrides)
    first_identity = {
        "request": "first",
        "task_id": first["task_id"],
        "role": first["role"].value,
        "cost": first["cost"],
        "tokens": first["tokens"],
    }
    second_identity = {
        "request": "second",
        "task_id": second["task_id"],
        "role": second["role"].value,
        "cost": second["cost"],
        "tokens": second["tokens"],
    }

    assert reserve(
        manager,
        request_hash=manager.request_hash(first_identity),
        **first,
    ).allowed
    blocked = reserve(
        manager,
        request_hash=manager.request_hash(second_identity),
        **second,
    )

    assert not blocked.allowed
    assert reason in blocked.reason


def test_reservation_applies_task_cost_ceiling(tmp_path):
    manager = make_manager(tmp_path, per_task_call_limit=10)
    assert reserve(
        manager,
        request_hash=manager.request_hash({"task-cost": 1}),
        task_id="T",
        cost=0.6,
        tokens=0,
        task_cost_ceiling=1.0,
    ).allowed

    blocked = reserve(
        manager,
        request_hash=manager.request_hash({"task-cost": 2}),
        task_id="T",
        cost=0.41,
        tokens=0,
        task_cost_ceiling=1.0,
    )

    assert not blocked.allowed
    assert "task model cost" in blocked.reason


def test_committed_usage_and_reserved_capacity_are_both_counted(tmp_path):
    manager = make_manager(tmp_path)
    now = datetime(2026, 8, 13, 12, tzinfo=timezone.utc)
    manager.record(
        task_id="T-committed",
        role=Role.ARCHITECT,
        request_hash=manager.request_hash({"committed": 1}),
        cost=0.4,
        tokens=40,
        success=True,
        now=now,
    )
    assert reserve(
        manager,
        request_hash=manager.request_hash({"reserved": 1}),
        task_id="T-reserved",
        role=Role.DEVELOPER,
        cost=0.5,
        tokens=50,
        now=now,
    ).allowed

    blocked = reserve(
        manager,
        request_hash=manager.request_hash({"combined": "overflow"}),
        task_id="T-overflow",
        role=Role.AUDITOR,
        cost=0.11,
        tokens=11,
        now=now,
    )

    assert not blocked.allowed
    assert "daily model cost" in blocked.reason


def test_reservation_restart_persists_and_never_auto_releases(tmp_path):
    manager = make_manager(tmp_path)
    request_hash = manager.request_hash({"restart": "reservation"})
    assert reserve(manager, request_hash=request_hash, cost=1.0, tokens=100).allowed

    reopened = make_manager(tmp_path)
    restored = reopened.get_reservation(request_hash)
    blocked = reserve(
        reopened,
        request_hash=reopened.request_hash({"restart": "second"}),
        task_id="T-second",
        role=Role.DEVELOPER,
        cost=0.01,
        tokens=1,
    )

    assert restored is not None
    assert restored.state is BudgetReservationState.RESERVED
    assert not blocked.allowed


def test_reservation_honors_existing_failure_cooldown(tmp_path):
    manager = make_manager(tmp_path, failure_cooldown_seconds=60)
    now = datetime(2026, 8, 13, 12, tzinfo=timezone.utc)
    manager.record(
        task_id="T-cooldown",
        role=Role.ARCHITECT,
        request_hash=manager.request_hash({"cooldown": "failed"}),
        cost=0.0,
        tokens=0,
        success=False,
        now=now,
    )

    blocked = reserve(
        manager,
        request_hash=manager.request_hash({"cooldown": "retry"}),
        task_id="T-cooldown",
        role=Role.ARCHITECT,
        cost=0.0,
        tokens=0,
        now=now + timedelta(seconds=30),
    )

    assert not blocked.allowed
    assert blocked.reason == "failure cooldown is active"


def test_identical_reservation_is_not_a_second_dispatch_grant(tmp_path):
    manager = make_manager(tmp_path)
    request_hash = manager.request_hash({"same": "request"})
    first = reserve(manager, request_hash=request_hash)
    repeated = reserve(manager, request_hash=request_hash)

    assert first.allowed
    assert not repeated.allowed
    assert repeated.reservation == first.reservation
    assert manager.total_calls() == 0


def test_conflicting_and_released_request_identity_fail_closed(tmp_path):
    manager = make_manager(tmp_path)
    request_hash = manager.request_hash({"identity": "fixed"})
    assert reserve(manager, request_hash=request_hash).allowed
    with pytest.raises(BudgetReservationConflict):
        reserve(manager, request_hash=request_hash, cost=0.2)

    with reservation_transaction(manager) as db:
        released = manager.release_in_transaction(db, request_hash=request_hash)
    assert released.state is BudgetReservationState.RELEASED
    with pytest.raises(BudgetReservationStateConflict):
        reserve(manager, request_hash=request_hash)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("request_hash", "A" * 64),
        ("request_hash", "a" * 63),
        ("task_revision", True),
        ("task_revision", 0),
        ("task_revision", 2**63),
        ("estimated_cost", True),
        ("estimated_cost", -1.0),
        ("estimated_cost", float("nan")),
        ("estimated_cost", float("inf")),
        ("estimated_tokens", True),
        ("estimated_tokens", -1),
        ("estimated_tokens", 2**63),
        ("include_cache", 1),
    ),
)
def test_reservation_wire_inputs_fail_closed(tmp_path, field, value):
    manager = make_manager(tmp_path)
    options = {
        "task_id": "T",
        "task_revision": 1,
        "role": Role.ARCHITECT,
        "request_hash": manager.request_hash({"valid": True}),
        "estimated_cost": 0.1,
        "estimated_tokens": 10,
    }
    options[field] = value

    with reservation_transaction(manager) as db:
        with pytest.raises(ValueError):
            manager.reserve_in_transaction(db, **options)


def test_same_database_and_active_transaction_are_required(tmp_path):
    manager = make_manager(tmp_path)
    request_hash = manager.request_hash({"database": "identity"})
    same = sqlite3.connect(manager.path)
    same.row_factory = sqlite3.Row
    with pytest.raises(BudgetReservationCapabilityError, match="active"):
        manager.reserve_in_transaction(
            same,
            task_id="T",
            task_revision=1,
            role=Role.ARCHITECT,
            request_hash=request_hash,
            estimated_cost=0.1,
            estimated_tokens=1,
        )
    same.close()

    wrong_factory = sqlite3.connect(manager.path)
    wrong_factory.row_factory = lambda _cursor, _row: {"hostile": "shape"}
    wrong_factory.execute("BEGIN IMMEDIATE")
    with pytest.raises(BudgetReservationCapabilityError, match="row_factory"):
        manager.reserve_in_transaction(
            wrong_factory,
            task_id="T",
            task_revision=1,
            role=Role.ARCHITECT,
            request_hash=request_hash,
            estimated_cost=0.1,
            estimated_tokens=1,
        )
    wrong_factory.rollback()
    wrong_factory.close()

    other_path = tmp_path / "other.db"
    other_manager = BudgetManager(
        other_path,
        daily_cost_ceiling=1,
        monthly_cost_ceiling=10,
        per_task_call_limit=2,
        per_role_call_limit=10,
    )
    other = sqlite3.connect(other_path)
    other.row_factory = sqlite3.Row
    other.execute("BEGIN IMMEDIATE")
    with pytest.raises(BudgetReservationCapabilityError, match="BudgetManager database"):
        manager.reserve_in_transaction(
            other,
            task_id="T",
            task_revision=1,
            role=Role.ARCHITECT,
            request_hash=request_hash,
            estimated_cost=0.1,
            estimated_tokens=1,
        )
    other.rollback()
    other.close()
    assert other_manager.total_calls() == 0


def test_deferred_caller_transaction_is_upgraded_before_budget_reads(tmp_path):
    manager = make_manager(tmp_path)
    request_hash = manager.request_hash({"transaction": "deferred"})
    db = sqlite3.connect(manager.path)
    db.row_factory = sqlite3.Row
    try:
        db.execute("BEGIN")
        result = manager.reserve_in_transaction(
            db,
            task_id="T",
            task_revision=1,
            role=Role.ARCHITECT,
            request_hash=request_hash,
            estimated_cost=0.1,
            estimated_tokens=10,
        )
        db.commit()
    finally:
        db.close()

    assert result.allowed
    assert manager.get_reservation(request_hash).state is BudgetReservationState.RESERVED


def test_reservation_settlement_is_atomic_and_not_double_counted(tmp_path):
    manager = make_manager(tmp_path, per_task_call_limit=3)
    request_hash = manager.request_hash({"settle": "success"})
    assert reserve(
        manager,
        request_hash=request_hash,
        task_id="T",
        cost=0.8,
        tokens=80,
        task_cost_ceiling=1.0,
    ).allowed

    with reservation_transaction(manager) as db:
        settled = manager.record_and_settle_in_transaction(
            db,
            request_hash=request_hash,
            cost=0.6,
            tokens=60,
            success=True,
        )

    assert settled.state is BudgetReservationState.SETTLED
    assert manager.total_calls() == 1
    next_result = reserve(
        manager,
        request_hash=manager.request_hash({"settle": "next"}),
        task_id="T",
        cost=0.4,
        tokens=40,
        task_cost_ceiling=1.0,
    )
    assert next_result.allowed


def test_successful_settlement_preserves_success_only_cache_and_restart_hit(tmp_path):
    manager = make_manager(tmp_path)
    request_hash = manager.request_hash({"settle": "cache"})
    payload = AgentResult(
        success=True,
        summary="reserved result",
        output_schema="result-v2",
        tokens=10,
        cost=0.1,
        structured_output={"proposal": "bounded"},
    ).to_payload()
    assert reserve(manager, request_hash=request_hash).allowed
    with reservation_transaction(manager) as db:
        manager.record_and_settle_in_transaction(
            db,
            request_hash=request_hash,
            cost=0.1,
            tokens=10,
            success=True,
            cacheable=True,
            cache_payload=payload,
        )

    reopened = make_manager(tmp_path)
    assert reopened.cached_result(request_hash) == payload
    repeated = reserve(reopened, request_hash=request_hash)
    assert repeated.allowed and repeated.cached
    assert repeated.reservation is not None
    assert repeated.reservation.state is BudgetReservationState.SETTLED
    assert reopened.total_calls() == 1


def test_cached_settlement_replay_rejects_conflicting_payload(tmp_path):
    manager = make_manager(tmp_path)
    request_hash = manager.request_hash({"settle": "cache-replay"})
    first_payload = AgentResult(
        success=True,
        summary="first",
        output_schema="result-v2",
    ).to_payload()
    conflicting_payload = AgentResult(
        success=True,
        summary="conflicting",
        output_schema="result-v2",
    ).to_payload()
    assert reserve(manager, request_hash=request_hash).allowed
    with reservation_transaction(manager) as db:
        first = manager.record_and_settle_in_transaction(
            db,
            request_hash=request_hash,
            cost=0.1,
            tokens=10,
            success=True,
            cacheable=True,
            cache_payload=first_payload,
        )
    with reservation_transaction(manager) as db:
        repeated = manager.record_and_settle_in_transaction(
            db,
            request_hash=request_hash,
            cost=0.1,
            tokens=10,
            success=True,
            cacheable=True,
            cache_payload=first_payload,
        )
    assert repeated == first

    with pytest.raises(BudgetReservationConflict, match="actual usage"):
        with reservation_transaction(manager) as db:
            manager.record_and_settle_in_transaction(
                db,
                request_hash=request_hash,
                cost=0.1,
                tokens=10,
                success=True,
                cacheable=True,
                cache_payload=conflicting_payload,
            )

    assert manager.cached_result(request_hash) == first_payload
    assert manager.total_calls() == 1


def test_cached_settlement_binding_rejects_tampered_cache_replay(tmp_path):
    manager = make_manager(tmp_path)
    request_hash = manager.request_hash({"settle": "cache-binding"})
    original_payload = AgentResult(
        success=True,
        summary="original",
        output_schema="result-v2",
    ).to_payload()
    tampered_payload = AgentResult(
        success=True,
        summary="tampered",
        output_schema="result-v2",
    ).to_payload()
    assert reserve(manager, request_hash=request_hash).allowed
    with reservation_transaction(manager) as db:
        manager.record_and_settle_in_transaction(
            db,
            request_hash=request_hash,
            cost=0.1,
            tokens=10,
            success=True,
            cacheable=True,
            cache_payload=original_payload,
        )
    with sqlite3.connect(manager.path) as db:
        db.execute(
            "UPDATE request_cache SET result_json=? WHERE request_hash=?",
            (canonical_json_dumps(tampered_payload), request_hash),
        )

    with pytest.raises(BudgetReservationCapabilityError, match="request_cache"):
        with reservation_transaction(manager) as db:
            manager.record_and_settle_in_transaction(
                db,
                request_hash=request_hash,
                cost=0.1,
                tokens=10,
                success=True,
                cacheable=True,
                cache_payload=tampered_payload,
            )

    assert manager.total_calls() == 1
    with pytest.raises(BudgetReservationCapabilityError, match="request_cache"):
        manager.get_reservation(request_hash)


def test_preexisting_valid_legacy_cache_skips_reservation(tmp_path):
    manager = make_manager(tmp_path)
    request_hash = manager.request_hash({"legacy-cache": "hit"})
    payload = AgentResult(
        success=True,
        summary="legacy cached result",
        output_schema="result-v2",
    ).to_payload()
    manager.record(
        task_id="T-legacy",
        role=Role.ARCHITECT,
        request_hash=request_hash,
        cost=0.1,
        tokens=10,
        success=True,
        cacheable=True,
        cache_payload=payload,
    )

    result = reserve(manager, request_hash=request_hash)

    assert result.allowed and result.cached
    assert result.reservation is None
    assert manager.get_reservation(request_hash) is None
    assert manager.total_calls() == 1


def test_successful_settlement_fails_atomically_if_invalid_cache_is_not_replaced(tmp_path):
    manager = make_manager(tmp_path)
    request_hash = manager.request_hash({"settle": "invalid-cache"})
    payload = AgentResult(
        success=True,
        summary="replacement",
        output_schema="result-v2",
    ).to_payload()
    invalid_json = "{malformed"
    with sqlite3.connect(manager.path) as db:
        db.execute(
            "INSERT INTO request_cache(request_hash, created_at, result_json) VALUES (?, ?, ?)",
            (request_hash, datetime.now(timezone.utc).isoformat(), invalid_json),
        )
    assert reserve(manager, request_hash=request_hash).allowed

    with pytest.raises(BudgetReservationConflict, match="not persisted exactly"):
        with reservation_transaction(manager) as db:
            manager.record_and_settle_in_transaction(
                db,
                request_hash=request_hash,
                cost=0.1,
                tokens=10,
                success=True,
                cacheable=True,
                cache_payload=payload,
            )

    assert manager.total_calls() == 0
    assert manager.get_reservation(request_hash).state is BudgetReservationState.RESERVED
    with sqlite3.connect(manager.path) as db:
        assert (
            db.execute(
                "SELECT result_json FROM request_cache WHERE request_hash=?", (request_hash,)
            ).fetchone()[0]
            == invalid_json
        )

    with reservation_transaction(manager) as db:
        manager.record_and_settle_in_transaction(
            db,
            request_hash=request_hash,
            cost=0.1,
            tokens=10,
            success=True,
            cacheable=True,
            cache_payload=payload,
            replace_invalid_cache=True,
        )
    assert manager.cached_result(request_hash) == payload


def test_no_cache_settlement_ignores_preexisting_invalid_cache_and_is_idempotent(tmp_path):
    manager = make_manager(tmp_path)
    request_hash = manager.request_hash({"settle": "no-cache-with-invalid-row"})
    invalid_json = "{malformed"
    with sqlite3.connect(manager.path) as db:
        db.execute(
            "INSERT INTO request_cache(request_hash, created_at, result_json) VALUES (?, ?, ?)",
            (request_hash, datetime.now(timezone.utc).isoformat(), invalid_json),
        )
    assert reserve(manager, request_hash=request_hash).allowed

    with reservation_transaction(manager) as db:
        first = manager.record_and_settle_in_transaction(
            db,
            request_hash=request_hash,
            cost=0.1,
            tokens=10,
            success=True,
        )
    with reservation_transaction(manager) as db:
        second = manager.record_and_settle_in_transaction(
            db,
            request_hash=request_hash,
            cost=0.1,
            tokens=10,
            success=True,
        )

    assert first == second
    assert first.settled_cache_persisted is False
    assert manager.total_calls() == 1
    with sqlite3.connect(manager.path) as db:
        assert (
            db.execute(
                "SELECT result_json FROM request_cache WHERE request_hash=?", (request_hash,)
            ).fetchone()[0]
            == invalid_json
        )


def test_failure_settlement_never_creates_cache(tmp_path):
    manager = make_manager(tmp_path)
    request_hash = manager.request_hash({"settle": "failure"})
    assert reserve(manager, request_hash=request_hash).allowed
    with reservation_transaction(manager) as db:
        settled = manager.record_and_settle_in_transaction(
            db,
            request_hash=request_hash,
            cost=0.0,
            tokens=0,
            success=False,
        )
    assert settled.state is BudgetReservationState.SETTLED
    assert settled.settled_success is False
    assert manager.cached_result(request_hash) is None
    assert not manager.has_cached_entry(request_hash)


def test_actual_usage_above_reservation_rolls_back_without_usage(tmp_path):
    manager = make_manager(tmp_path)
    request_hash = manager.request_hash({"settle": "overflow"})
    assert reserve(manager, request_hash=request_hash, cost=0.1, tokens=10).allowed

    with pytest.raises(BudgetReservationConflict, match="exceeds"):
        with reservation_transaction(manager) as db:
            manager.record_and_settle_in_transaction(
                db,
                request_hash=request_hash,
                cost=0.100001,
                tokens=10,
                success=True,
            )

    assert manager.total_calls() == 0
    assert manager.get_reservation(request_hash).state is BudgetReservationState.RESERVED


def test_identical_settlement_is_idempotent_but_conflict_fails_closed(tmp_path):
    manager = make_manager(tmp_path)
    request_hash = manager.request_hash({"settle": "idempotent"})
    assert reserve(manager, request_hash=request_hash).allowed
    with reservation_transaction(manager) as db:
        first = manager.record_and_settle_in_transaction(
            db,
            request_hash=request_hash,
            cost=0.1,
            tokens=10,
            success=True,
        )
    with reservation_transaction(manager) as db:
        second = manager.record_and_settle_in_transaction(
            db,
            request_hash=request_hash,
            cost=0.1,
            tokens=10,
            success=True,
        )
    assert first == second
    assert manager.total_calls() == 1

    with pytest.raises(BudgetReservationConflict):
        with reservation_transaction(manager) as db:
            manager.record_and_settle_in_transaction(
                db,
                request_hash=request_hash,
                cost=0.09,
                tokens=10,
                success=True,
            )


def test_release_is_idempotent_frees_capacity_and_settled_cannot_release(tmp_path):
    manager = make_manager(tmp_path)
    released_hash = manager.request_hash({"release": True})
    assert reserve(manager, request_hash=released_hash, cost=1.0, tokens=100).allowed
    with reservation_transaction(manager) as db:
        first_release = manager.release_in_transaction(db, request_hash=released_hash)
    with reservation_transaction(manager) as db:
        second_release = manager.release_in_transaction(db, request_hash=released_hash)
    assert first_release == second_release
    assert first_release.state is BudgetReservationState.RELEASED
    assert reserve(
        manager,
        request_hash=manager.request_hash({"release": "capacity-reused"}),
        task_id="T-new",
        cost=1.0,
        tokens=100,
    ).allowed

    settled_manager = make_manager(tmp_path / "settled")
    settled_hash = settled_manager.request_hash({"release": "settled"})
    assert reserve(settled_manager, request_hash=settled_hash).allowed
    with reservation_transaction(settled_manager) as db:
        settled_manager.record_and_settle_in_transaction(
            db,
            request_hash=settled_hash,
            cost=0.1,
            tokens=10,
            success=True,
        )
    with pytest.raises(BudgetReservationStateConflict):
        with reservation_transaction(settled_manager) as db:
            settled_manager.release_in_transaction(db, request_hash=settled_hash)


def test_legacy_record_cannot_bypass_outstanding_reservation(tmp_path):
    manager = make_manager(tmp_path)
    request_hash = manager.request_hash({"mixed-path": True})
    assert reserve(manager, request_hash=request_hash).allowed

    with pytest.raises(BudgetReservationStateConflict):
        manager.record(
            task_id="T-reserve",
            role=Role.ARCHITECT,
            request_hash=request_hash,
            cost=0.1,
            tokens=10,
            success=True,
        )
    assert manager.total_calls() == 0


def test_same_request_sixteen_way_concurrency_creates_one_reservation(tmp_path):
    manager = make_manager(tmp_path, per_task_call_limit=20)
    request_hash = manager.request_hash({"concurrency": "same"})
    barrier = threading.Barrier(16)

    def attempt(_index: int) -> bool:
        barrier.wait()
        return reserve(manager, request_hash=request_hash).allowed

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(attempt, range(16)))

    assert results.count(True) == 1
    with sqlite3.connect(manager.path) as db:
        assert db.execute("SELECT COUNT(*) FROM budget_reservations").fetchone()[0] == 1


def test_different_requests_cannot_concurrently_oversubscribe_ceiling(tmp_path):
    manager = make_manager(tmp_path, daily_cost_ceiling=1.0)
    barrier = threading.Barrier(2)

    def attempt(index: int) -> bool:
        barrier.wait()
        return reserve(
            manager,
            request_hash=manager.request_hash({"concurrency": index}),
            task_id=f"T-{index}",
            role=Role.ARCHITECT if index == 0 else Role.DEVELOPER,
            cost=0.7,
            tokens=0,
        ).allowed

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, range(2)))

    assert sorted(results) == [False, True]
    with sqlite3.connect(manager.path) as db:
        row = db.execute(
            "SELECT COUNT(*), SUM(reserved_cost) FROM budget_reservations WHERE state='RESERVED'"
        ).fetchone()
    assert row == (1, 0.7)


def test_reservation_insert_failure_rolls_back_and_leaves_no_capacity(tmp_path):
    manager = make_manager(tmp_path)
    request_hash = manager.request_hash({"failure": "insert"})
    with reservation_transaction(manager) as db:
        db.execute(
            """
            CREATE TEMP TRIGGER fail_reservation_insert
            BEFORE INSERT ON budget_reservations BEGIN
                SELECT RAISE(ABORT, 'injected reservation insert failure');
            END
            """
        )
        with pytest.raises(sqlite3.IntegrityError):
            manager.reserve_in_transaction(
                db,
                task_id="T",
                task_revision=1,
                role=Role.ARCHITECT,
                request_hash=request_hash,
                estimated_cost=0.1,
                estimated_tokens=10,
            )
    assert manager.get_reservation(request_hash) is None


def test_caller_rollback_after_reserve_leaves_no_half_reservation(tmp_path):
    manager = make_manager(tmp_path)
    request_hash = manager.request_hash({"failure": "caller-audit"})

    with pytest.raises(RuntimeError, match="injected caller failure"):
        with reservation_transaction(manager) as db:
            result = manager.reserve_in_transaction(
                db,
                task_id="T",
                task_revision=1,
                role=Role.ARCHITECT,
                request_hash=request_hash,
                estimated_cost=0.1,
                estimated_tokens=10,
            )
            assert result.allowed
            raise RuntimeError("injected caller failure")

    assert manager.get_reservation(request_hash) is None


def test_budget_calculation_database_failure_leaves_no_reservation(tmp_path):
    manager = make_manager(tmp_path)
    request_hash = manager.request_hash({"failure": "budget-read"})

    with pytest.raises(sqlite3.OperationalError, match="model_usage"):
        with reservation_transaction(manager) as db:
            db.execute("ALTER TABLE model_usage RENAME TO unavailable_model_usage")
            manager.reserve_in_transaction(
                db,
                task_id="T",
                task_revision=1,
                role=Role.ARCHITECT,
                request_hash=request_hash,
                estimated_cost=0.1,
                estimated_tokens=10,
            )

    assert manager.get_reservation(request_hash) is None
    assert manager.total_calls() == 0


def test_settlement_usage_insert_failure_rolls_back_state(tmp_path):
    manager = make_manager(tmp_path)
    request_hash = manager.request_hash({"failure": "usage"})
    assert reserve(manager, request_hash=request_hash).allowed
    with reservation_transaction(manager) as db:
        db.execute(
            """
            CREATE TEMP TRIGGER fail_usage_insert
            BEFORE INSERT ON model_usage BEGIN
                SELECT RAISE(ABORT, 'injected usage insert failure');
            END
            """
        )
        with pytest.raises(sqlite3.IntegrityError):
            manager.record_and_settle_in_transaction(
                db,
                request_hash=request_hash,
                cost=0.1,
                tokens=10,
                success=True,
            )
    assert manager.total_calls() == 0
    assert manager.get_reservation(request_hash).state is BudgetReservationState.RESERVED


def test_settlement_state_update_failure_rolls_back_usage_and_cache(tmp_path):
    manager = make_manager(tmp_path)
    request_hash = manager.request_hash({"failure": "settlement-update"})
    payload = AgentResult(
        success=True,
        summary="rollback",
        output_schema="result-v1",
    ).to_payload()
    assert reserve(manager, request_hash=request_hash).allowed
    with reservation_transaction(manager) as db:
        db.execute(
            """
            CREATE TEMP TRIGGER fail_settlement_update
            BEFORE UPDATE ON budget_reservations BEGIN
                SELECT RAISE(ABORT, 'injected settlement update failure');
            END
            """
        )
        with pytest.raises(sqlite3.IntegrityError):
            manager.record_and_settle_in_transaction(
                db,
                request_hash=request_hash,
                cost=0.1,
                tokens=10,
                success=True,
                cacheable=True,
                cache_payload=payload,
            )
    assert manager.total_calls() == 0
    assert manager.cached_result(request_hash) is None
    assert manager.get_reservation(request_hash).state is BudgetReservationState.RESERVED


def test_settled_reservation_fails_closed_if_usage_link_is_corrupted(tmp_path):
    manager = make_manager(tmp_path)
    request_hash = manager.request_hash({"tamper": "usage-link"})
    assert reserve(manager, request_hash=request_hash).allowed
    with reservation_transaction(manager) as db:
        manager.record_and_settle_in_transaction(
            db,
            request_hash=request_hash,
            cost=0.1,
            tokens=10,
            success=True,
        )
    with sqlite3.connect(manager.path) as db:
        db.execute("UPDATE model_usage SET cost=0.2 WHERE request_hash=?", (request_hash,))

    with pytest.raises(BudgetReservationCapabilityError, match="does not match"):
        manager.get_reservation(request_hash)


def test_non_settled_reservation_fails_closed_if_usage_binding_is_corrupted(tmp_path):
    manager = make_manager(tmp_path)
    request_hash = manager.request_hash({"tamper": "unexpected-usage-link"})
    assert reserve(manager, request_hash=request_hash).allowed
    with sqlite3.connect(manager.path) as db:
        db.execute("PRAGMA ignore_check_constraints=ON")
        usage_id = db.execute(
            """
            INSERT INTO model_usage(timestamp, task_id, role, request_hash, cost, tokens, success)
            VALUES (?, 'T-reserve', 'ARCHITECT', ?, 0.1, 10, 1)
            """,
            (datetime.now(timezone.utc).isoformat(), request_hash),
        ).lastrowid
        db.execute(
            "UPDATE budget_reservations SET model_usage_id=? WHERE request_hash=?",
            (usage_id, request_hash),
        )

    with pytest.raises(BudgetReservationCapabilityError, match="model_usage binding"):
        manager.get_reservation(request_hash)


def test_additive_schema_opens_legacy_database_without_rewriting_usage_or_cache(tmp_path):
    path = Path(tmp_path) / "legacy.db"
    with sqlite3.connect(path) as db:
        db.executescript(
            """
            CREATE TABLE model_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                task_id TEXT NOT NULL,
                role TEXT NOT NULL,
                request_hash TEXT NOT NULL,
                cost REAL NOT NULL,
                tokens INTEGER NOT NULL,
                success INTEGER NOT NULL
            );
            CREATE TABLE request_cache (
                request_hash TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                result_json TEXT
            );
            """
        )
        db.execute(
            """
            INSERT INTO model_usage(timestamp, task_id, role, request_hash, cost, tokens, success)
            VALUES (?, 'legacy', 'ARCHITECT', ?, 0.1, 10, 1)
            """,
            (datetime.now(timezone.utc).isoformat(), "a" * 64),
        )

    manager = BudgetManager(
        path,
        daily_cost_ceiling=1.0,
        monthly_cost_ceiling=10.0,
        per_task_call_limit=2,
        per_role_call_limit=10,
    )

    assert manager.total_calls() == 1
    assert manager.get_reservation("b" * 64) is None
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM model_usage").fetchone()[0] == 1
        assert (
            db.execute("SELECT schema_version FROM budget_reservation_meta WHERE id=1").fetchone()[
                0
            ]
            == "orchestrator-budget-reservation-v1"
        )


def test_unknown_reservation_schema_version_fails_closed_on_reopen(tmp_path):
    manager = make_manager(tmp_path)
    with sqlite3.connect(manager.path) as db:
        db.execute(
            "UPDATE budget_reservation_meta SET schema_version='orchestrator-budget-v999' "
            "WHERE id=1"
        )

    with pytest.raises(BudgetReservationCapabilityError, match="unsupported"):
        make_manager(tmp_path)


@pytest.mark.parametrize("existing_table", ("budget_reservation_meta", "budget_reservations"))
def test_incomplete_reservation_schema_is_not_adopted(tmp_path, existing_table):
    path = Path(tmp_path) / "budget.db"
    with sqlite3.connect(path) as db:
        if existing_table == "budget_reservation_meta":
            db.execute(
                "CREATE TABLE budget_reservation_meta "
                "(id INTEGER PRIMARY KEY, schema_version TEXT NOT NULL)"
            )
        else:
            db.execute("CREATE TABLE budget_reservations (request_hash TEXT PRIMARY KEY)")

    with pytest.raises(BudgetReservationCapabilityError, match="incomplete"):
        make_manager(tmp_path)


def test_schema_version_tamper_after_open_fails_each_transaction_closed(tmp_path):
    manager = make_manager(tmp_path)
    request_hash = manager.request_hash({"schema": "runtime-tamper"})
    with sqlite3.connect(manager.path) as db:
        db.execute(
            "UPDATE budget_reservation_meta SET schema_version='orchestrator-budget-v999' "
            "WHERE id=1"
        )

    with pytest.raises(BudgetReservationCapabilityError, match="unsupported"):
        reserve(manager, request_hash=request_hash)

    with sqlite3.connect(manager.path) as db:
        assert db.execute("SELECT COUNT(*) FROM budget_reservations").fetchone()[0] == 0
