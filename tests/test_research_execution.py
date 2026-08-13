from __future__ import annotations

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from trademind.orchestrator.artifact_store import ArtifactStore
from trademind.orchestrator.audit_log import AuditLog
from trademind.orchestrator.budget import BudgetManager, BudgetReservationState
from trademind.orchestrator.control_plane import ControlPlane
from trademind.orchestrator.dispatcher import Dispatcher, route_to_generic_workflow
from trademind.orchestrator.engine import WorkflowEngine, WorkflowRoutingError
from trademind.orchestrator.models import TaskState
from trademind.research_execution import (
    RESEARCH_PROPOSAL_RESPONSE_MEDIA_TYPE,
    ResearchAuthorizationState,
    ResearchExecutionAuthorizationError,
    ResearchExecutionConflictError,
    ResearchExecutionControl,
    ResearchExecutionDatabaseError,
    ResearchExecutionResultError,
    ResearchExecutionStateError,
    ResearchExecutionStatus,
)
from trademind.research_proposal_response import (
    RESEARCH_PROPOSAL_RESPONSE_KIND,
    RESEARCH_PROPOSAL_RESPONSE_SCHEMA_VERSION,
    ResearchProposalResponseV1,
)
from trademind.signal_statistics_agent_packet import (
    PACKET_V2_SCHEMA_VERSION,
    build_packet_v2_from_artifact,
    persist_packet_v2,
)
from trademind.signal_statistics_orchestrator_bridge import (
    register_verified_packet_v2_task,
)
from trademind.signal_statistics_provenance import (
    CandidateContentV2,
    CandidateDefinitionV2,
    CodeProvenance,
)
from trademind.signal_statistics_report import build_report_v2, persist_report_v2


_POLICY_HASH = f"sha256:{'2' * 64}"
_SOURCE_HASH = f"sha256:{'3' * 64}"


def _candidate(*, feature: str) -> CandidateContentV2:
    return CandidateContentV2(
        candidate_definition=CandidateDefinitionV2(
            source_kind="signal_journal",
            source_namespace="trademind_signal_journal",
            symbol="XAUUSD",
            timeframe="M5",
            feature=feature,
            horizon=3,
            action_scope="BUY_SELL_DIRECTIONAL",
            evaluation_method_version="signal-statistics-v2",
        ),
        evaluation_policy_hash=_POLICY_HASH,
        metrics={"trades": 24, "win_rate": 0.5},
        status="RESEARCH_CANDIDATE",
        reason_codes=("BELOW_RESEARCH_MINIMUM",),
    )


def _create_task(
    control: ControlPlane,
    store: ArtifactStore,
    *,
    feature: str,
):
    report = build_report_v2(
        (_candidate(feature=feature),),
        source_snapshot_hash_ref=_SOURCE_HASH,
        source_schema_version="1.1",
        code_provenance=CodeProvenance(
            producer_name="trademind",
            producer_version="research-execution-test",
            git_commit="1" * 40,
            revision_source="git_worktree",
        ),
        journal_rows=24,
        generated_at="2026-08-13T12:00:00+00:00",
    )
    report_ref = persist_report_v2(report, artifact_store=store)
    packet = build_packet_v2_from_artifact(report_ref.hash_ref, artifact_store=store)
    packet_ref = persist_packet_v2(packet, artifact_store=store)
    task = register_verified_packet_v2_task(
        packet_ref.hash_ref,
        control_plane=control,
        artifact_store=store,
    )
    return task, packet


def _manager(
    db_path: Path,
    *,
    daily_cost: float = 100.0,
    monthly_cost: float = 100.0,
) -> BudgetManager:
    return BudgetManager(
        db_path,
        daily_cost_ceiling=daily_cost,
        monthly_cost_ceiling=monthly_cost,
        daily_token_ceiling=100_000,
        monthly_token_ceiling=100_000,
        per_task_call_limit=8,
        per_role_call_limit=32,
    )


def _setup(
    tmp_path: Path,
    *,
    feature: str = "spread_pressure",
    daily_cost: float = 100.0,
):
    db_path = tmp_path / "orchestrator.db"
    store = ArtifactStore(tmp_path / "artifacts")
    control = ControlPlane(db_path)
    budget = _manager(db_path, daily_cost=daily_cost)
    task, packet = _create_task(control, store, feature=feature)
    execution = ResearchExecutionControl(
        control_plane=control,
        budget_manager=budget,
        artifact_store=store,
    )
    return db_path, store, control, budget, execution, task, packet


def _response(packet, *, title: str = "Regime-conditioned continuation"):
    candidate_id = packet.candidate_bindings[0]["candidate_id"]
    return ResearchProposalResponseV1.from_payload(
        {
            "schema_version": RESEARCH_PROPOSAL_RESPONSE_SCHEMA_VERSION,
            "response_kind": RESEARCH_PROPOSAL_RESPONSE_KIND,
            "proposals": [
                {
                    "candidate_id": candidate_id,
                    "title": title,
                    "rationale": "The candidate may depend on volatility regime.",
                    "falsifiable_claim": (
                        "The effect remains positive in high-volatility periods."
                    ),
                    "proposed_test": (
                        "Compare predefined high- and low-volatility public-data subsets."
                    ),
                    "rejection_condition": (
                        "Reject if the high-volatility effect is non-positive."
                    ),
                    "confidence": "MEDIUM",
                }
            ],
        }
    )


def _audit_actions(db_path: Path) -> list[str]:
    with sqlite3.connect(db_path) as db:
        return [
            __import__("json").loads(row[0])["action"]
            for row in db.execute("SELECT payload FROM audit_events ORDER BY id")
        ]


def _counts(db_path: Path) -> dict[str, int]:
    with sqlite3.connect(db_path) as db:
        return {
            table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "research_execution_authorizations",
                "research_executions",
                "budget_reservations",
                "model_usage",
                "audit_events",
            )
        }


def test_authorization_is_trusted_sidecar_and_task_remains_pristine(tmp_path: Path) -> None:
    db, _store, control, budget, runtime, task, packet = _setup(tmp_path)
    before = control.task_store.get(task.task_id)

    authorization = runtime.create_authorization(
        task_id=task.task_id,
        task_revision=task.revision,
        reserved_cost=1.25,
        reserved_tokens=400,
        authorized_by="operator:alice",
    )
    envelope = runtime.request_envelope(authorization.authorization_id)

    assert authorization.state is ResearchAuthorizationState.ACTIVE
    assert authorization.packet_artifact_hash_ref == task.artifact_refs[0]
    assert authorization.packet_semantic_hash == packet.packet_semantic_hash
    assert authorization.request_hash == budget.request_hash(
        {
            "request_kind": "verified-packet-research-proposal-request-v1",
            "envelope": envelope.to_payload(),
        }
    )
    assert envelope.input_schema == PACKET_V2_SCHEMA_VERSION
    assert envelope.to_payload()["structured_input"] == packet.to_payload()
    assert all("/" not in artifact for artifact in envelope.artifact_refs)
    assert budget.get_reservation(authorization.request_hash) is None
    assert control.task_store.get(task.task_id) == before == task
    assert _audit_actions(db)[-1] == "RESEARCH_EXECUTION_AUTHORIZATION_CREATED"


@pytest.mark.parametrize(
    ("cost", "tokens", "authorized_by"),
    [
        (True, 1, "operator"),
        (float("nan"), 1, "operator"),
        (float("inf"), 1, "operator"),
        (-1.0, 1, "operator"),
        (1.0, True, "operator"),
        (1.0, -1, "operator"),
        (1.0, 1, ""),
    ],
)
def test_authorization_exact_numeric_and_operator_bounds(
    tmp_path: Path, cost: object, tokens: object, authorized_by: str
) -> None:
    _db, _store, _control, _budget, runtime, task, _packet = _setup(tmp_path)
    with pytest.raises((ValueError, ResearchExecutionAuthorizationError)):
        runtime.create_authorization(
            task_id=task.task_id,
            task_revision=task.revision,
            reserved_cost=cost,  # type: ignore[arg-type]
            reserved_tokens=tokens,  # type: ignore[arg-type]
            authorized_by=authorized_by,
        )


def test_authorization_is_idempotent_only_for_identical_limits(tmp_path: Path) -> None:
    db, _store, _control, _budget, runtime, task, _packet = _setup(tmp_path)
    first = runtime.create_authorization(
        task_id=task.task_id,
        task_revision=1,
        reserved_cost=1.0,
        reserved_tokens=10,
        authorized_by="operator",
    )
    repeated = runtime.create_authorization(
        task_id=task.task_id,
        task_revision=1,
        reserved_cost=1.0,
        reserved_tokens=10,
        authorized_by="operator",
    )
    assert repeated == first
    assert _counts(db)["research_execution_authorizations"] == 1
    with pytest.raises(ResearchExecutionConflictError):
        runtime.create_authorization(
            task_id=task.task_id,
            task_revision=1,
            reserved_cost=2.0,
            reserved_tokens=10,
            authorized_by="operator",
        )


def test_active_authorization_and_claim_survive_separate_restarts(tmp_path: Path) -> None:
    db, store, _control, _budget, runtime, task, _packet = _setup(tmp_path)
    authorization = runtime.create_authorization(
        task_id=task.task_id,
        task_revision=1,
        reserved_cost=1.0,
        reserved_tokens=100,
        authorized_by="operator",
    )
    restarted = ResearchExecutionControl(
        control_plane=ControlPlane(db),
        budget_manager=_manager(db),
        artifact_store=ArtifactStore(store.root),
    )
    assert restarted.get_authorization(authorization.authorization_id) == authorization
    claimed = restarted.claim_execution(authorization.authorization_id)

    restarted_again = ResearchExecutionControl(
        control_plane=ControlPlane(db),
        budget_manager=_manager(db),
        artifact_store=ArtifactStore(store.root),
    )
    assert restarted_again.get_execution(claimed.request_hash) == claimed
    with pytest.raises(ResearchExecutionConflictError):
        restarted_again.claim_execution(authorization.authorization_id)


def test_atomic_claim_reserves_budget_consumes_auth_and_audits(tmp_path: Path) -> None:
    db, _store, control, budget, runtime, task, _packet = _setup(tmp_path)
    authorization = runtime.create_authorization(
        task_id=task.task_id,
        task_revision=1,
        reserved_cost=1.0,
        reserved_tokens=100,
        authorized_by="operator",
    )

    claimed = runtime.claim_execution(authorization.authorization_id)

    assert claimed.status is ResearchExecutionStatus.CLAIMED
    assert runtime.get_authorization(authorization.authorization_id).state is (
        ResearchAuthorizationState.CONSUMED
    )
    reservation = budget.get_reservation(claimed.request_hash)
    assert reservation is not None
    assert reservation.state is BudgetReservationState.RESERVED
    assert reservation.role.value == "OPERATOR"
    assert reservation.task_cost_ceiling == authorization.reserved_cost
    assert control.task_store.get(task.task_id) == task
    assert _audit_actions(db)[-1] == "RESEARCH_EXECUTION_CLAIMED"
    with pytest.raises(ResearchExecutionConflictError):
        runtime.claim_execution(authorization.authorization_id)


def test_call_in_flight_and_unknown_persist_without_release_or_retry(tmp_path: Path) -> None:
    db, store, _control, budget, runtime, task, _packet = _setup(tmp_path)
    authorization = runtime.create_authorization(
        task_id=task.task_id,
        task_revision=1,
        reserved_cost=1.0,
        reserved_tokens=100,
        authorized_by="operator",
    )
    claimed = runtime.claim_execution(authorization.authorization_id)
    in_flight = runtime.mark_call_in_flight(claimed.request_hash)
    assert in_flight.status is ResearchExecutionStatus.CALL_IN_FLIGHT

    restarted = ResearchExecutionControl(
        control_plane=ControlPlane(db),
        budget_manager=_manager(db),
        artifact_store=ArtifactStore(store.root),
    )
    assert restarted.get_execution(claimed.request_hash).status is (
        ResearchExecutionStatus.CALL_IN_FLIGHT
    )
    with pytest.raises(ResearchExecutionConflictError):
        restarted.claim_execution(authorization.authorization_id)
    unknown = restarted.mark_unknown_requires_operator(claimed.request_hash)
    assert unknown.status is ResearchExecutionStatus.UNKNOWN_REQUIRES_OPERATOR
    assert budget.get_reservation(claimed.request_hash).state is (BudgetReservationState.RESERVED)
    restarted_unknown = ResearchExecutionControl(
        control_plane=ControlPlane(db),
        budget_manager=_manager(db),
        artifact_store=ArtifactStore(store.root),
    )
    assert restarted_unknown.get_execution(claimed.request_hash) == unknown
    with pytest.raises(ResearchExecutionStateError):
        restarted_unknown.mark_call_in_flight(claimed.request_hash)
    assert _audit_actions(db)[-1] == "RESEARCH_EXECUTION_UNKNOWN_REQUIRES_OPERATOR"


def test_pre_dispatch_cancel_releases_capacity_and_is_durable(tmp_path: Path) -> None:
    db, store, _control, budget, runtime, task, _packet = _setup(tmp_path)
    auth = runtime.create_authorization(
        task_id=task.task_id,
        task_revision=1,
        reserved_cost=1.0,
        reserved_tokens=100,
        authorized_by="operator",
    )
    claimed = runtime.claim_execution(auth.authorization_id)
    cancelled = runtime.cancel_before_dispatch(claimed.request_hash)
    assert cancelled.status is ResearchExecutionStatus.CANCELLED_BEFORE_DISPATCH
    assert budget.get_reservation(claimed.request_hash).state is (BudgetReservationState.RELEASED)
    restarted = ResearchExecutionControl(
        control_plane=ControlPlane(db),
        budget_manager=_manager(db),
        artifact_store=ArtifactStore(store.root),
    )
    assert restarted.get_execution(claimed.request_hash) == cancelled
    for operation in (
        restarted.mark_call_in_flight,
        restarted.cancel_before_dispatch,
        restarted.mark_unknown_requires_operator,
    ):
        with pytest.raises(ResearchExecutionStateError):
            operation(claimed.request_hash)


def test_success_validates_packet_persists_cas_and_settles_atomically(
    tmp_path: Path,
) -> None:
    db, store, control, budget, runtime, task, packet = _setup(tmp_path)
    auth = runtime.create_authorization(
        task_id=task.task_id,
        task_revision=1,
        reserved_cost=2.0,
        reserved_tokens=500,
        authorized_by="operator",
    )
    claimed = runtime.claim_execution(auth.authorization_id)
    runtime.mark_call_in_flight(claimed.request_hash)
    response = _response(packet)

    succeeded = runtime.finalize_success(
        claimed.request_hash,
        response=response,
        actual_cost=1.5,
        actual_tokens=300,
    )

    assert succeeded.status is ResearchExecutionStatus.SUCCEEDED
    assert succeeded.result_media_type == RESEARCH_PROPOSAL_RESPONSE_MEDIA_TYPE
    assert (
        store.read_verified(
            succeeded.result_artifact_hash_ref,
            expected_media_type=RESEARCH_PROPOSAL_RESPONSE_MEDIA_TYPE,
        )
        == response.canonical_bytes()
    )
    assert runtime.load_result(claimed.request_hash) == response
    reservation = budget.get_reservation(claimed.request_hash)
    assert reservation.state is BudgetReservationState.SETTLED
    assert reservation.settled_cost == 1.5
    assert reservation.settled_tokens == 300
    assert control.task_store.get(task.task_id) == task
    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT COUNT(*) FROM model_usage").fetchone()[0] == 1

    restarted = ResearchExecutionControl(
        control_plane=ControlPlane(db),
        budget_manager=_manager(db),
        artifact_store=ArtifactStore(store.root),
    )
    assert restarted.get_execution(claimed.request_hash) == succeeded
    repeated = restarted.finalize_success(
        claimed.request_hash,
        response=response.canonical_bytes(),
        actual_cost=1.5,
        actual_tokens=300,
    )
    assert repeated == succeeded
    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT COUNT(*) FROM model_usage").fetchone()[0] == 1

    with pytest.raises(ResearchExecutionConflictError):
        restarted.finalize_success(
            claimed.request_hash,
            response=_response(packet, title="Conflicting proposal"),
            actual_cost=1.5,
            actual_tokens=300,
        )


def test_result_candidate_binding_and_usage_maxima_fail_closed(tmp_path: Path) -> None:
    _db, _store, _control, budget, runtime, task, packet = _setup(tmp_path)
    auth = runtime.create_authorization(
        task_id=task.task_id,
        task_revision=1,
        reserved_cost=1.0,
        reserved_tokens=100,
        authorized_by="operator",
    )
    claimed = runtime.claim_execution(auth.authorization_id)
    runtime.mark_call_in_flight(claimed.request_hash)
    wrong_payload = _response(packet).to_payload()
    wrong_payload["proposals"][0]["candidate_id"] = f"ssc-v2-{'f' * 64}"
    wrong = ResearchProposalResponseV1.from_payload(wrong_payload)
    with pytest.raises(ResearchExecutionResultError):
        runtime.finalize_success(
            claimed.request_hash,
            response=wrong,
            actual_cost=0.5,
            actual_tokens=50,
        )
    with pytest.raises(Exception, match="exceeds the reserved"):
        runtime.finalize_success(
            claimed.request_hash,
            response=_response(packet),
            actual_cost=1.1,
            actual_tokens=50,
        )
    assert runtime.get_execution(claimed.request_hash).status is (
        ResearchExecutionStatus.CALL_IN_FLIGHT
    )
    assert budget.get_reservation(claimed.request_hash).state is (BudgetReservationState.RESERVED)


def test_finalization_from_claimed_fails_before_cas_side_effect(
    tmp_path: Path, monkeypatch
) -> None:
    _db, store, _control, _budget, runtime, task, packet = _setup(tmp_path)
    auth = runtime.create_authorization(
        task_id=task.task_id,
        task_revision=1,
        reserved_cost=1.0,
        reserved_tokens=100,
        authorized_by="operator",
    )
    claim = runtime.claim_execution(auth.authorization_id)
    called = False
    original = store.import_snapshot

    def observe_import(*args, **kwargs):
        nonlocal called
        called = True
        return original(*args, **kwargs)

    monkeypatch.setattr(store, "import_snapshot", observe_import)
    with pytest.raises(ResearchExecutionStateError):
        runtime.finalize_success(
            claim.request_hash,
            response=_response(packet),
            actual_cost=0.5,
            actual_tokens=50,
        )
    assert called is False


def test_success_load_fails_closed_for_corrupt_or_missing_result(tmp_path: Path) -> None:
    _db, store, _control, _budget, runtime, task, packet = _setup(tmp_path)
    auth = runtime.create_authorization(
        task_id=task.task_id,
        task_revision=1,
        reserved_cost=1.0,
        reserved_tokens=100,
        authorized_by="operator",
    )
    claim = runtime.claim_execution(auth.authorization_id)
    runtime.mark_call_in_flight(claim.request_hash)
    succeeded = runtime.finalize_success(
        claim.request_hash,
        response=_response(packet),
        actual_cost=0.5,
        actual_tokens=50,
    )
    object_path = Path(store.resolve_verified(succeeded.result_artifact_hash_ref).path)
    object_path.write_bytes(b"tampered")
    with pytest.raises(ResearchExecutionResultError):
        runtime.get_execution(claim.request_hash)


def test_success_load_rejects_execution_usage_not_matching_settlement(tmp_path: Path) -> None:
    db, _store, _control, _budget, runtime, task, packet = _setup(tmp_path)
    auth = runtime.create_authorization(
        task_id=task.task_id,
        task_revision=1,
        reserved_cost=1.0,
        reserved_tokens=100,
        authorized_by="operator",
    )
    claim = runtime.claim_execution(auth.authorization_id)
    runtime.mark_call_in_flight(claim.request_hash)
    runtime.finalize_success(
        claim.request_hash,
        response=_response(packet),
        actual_cost=0.5,
        actual_tokens=50,
    )
    with sqlite3.connect(db) as connection:
        connection.execute(
            "UPDATE research_executions SET settled_tokens=49 WHERE request_hash=?",
            (claim.request_hash,),
        )
    with pytest.raises(ResearchExecutionDatabaseError, match="actual usage"):
        runtime.get_execution(claim.request_hash)
    with pytest.raises(ResearchExecutionDatabaseError, match="actual usage"):
        runtime.finalize_success(
            claim.request_hash,
            response=_response(packet),
            actual_cost=0.5,
            actual_tokens=49,
        )


def test_same_database_and_schema_version_fail_closed(tmp_path: Path) -> None:
    db, store, control, _budget, _runtime, _task, _packet = _setup(tmp_path)
    with pytest.raises(ResearchExecutionDatabaseError, match="share one database"):
        ResearchExecutionControl(
            control_plane=control,
            budget_manager=_manager(tmp_path / "other.db"),
            artifact_store=store,
        )
    with sqlite3.connect(db) as connection:
        connection.execute("PRAGMA ignore_check_constraints=ON")
        connection.execute("UPDATE research_execution_meta SET schema_version='unknown' WHERE id=1")
    with pytest.raises(ResearchExecutionDatabaseError, match="unsupported"):
        ResearchExecutionControl(
            control_plane=ControlPlane(db),
            budget_manager=_manager(db),
            artifact_store=store,
        )


def test_sixteen_concurrent_claims_create_one_logical_execution(tmp_path: Path) -> None:
    db, _store, _control, _budget, runtime, task, _packet = _setup(tmp_path)
    auth = runtime.create_authorization(
        task_id=task.task_id,
        task_revision=1,
        reserved_cost=1.0,
        reserved_tokens=100,
        authorized_by="operator",
    )
    barrier = threading.Barrier(16)

    def claim():
        barrier.wait()
        try:
            return runtime.claim_execution(auth.authorization_id)
        except ResearchExecutionConflictError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(lambda _index: claim(), range(16)))
    assert sum(not isinstance(result, Exception) for result in results) == 1
    assert sum(isinstance(result, ResearchExecutionConflictError) for result in results) == 15
    counts = _counts(db)
    assert counts["research_executions"] == 1
    assert counts["budget_reservations"] == 1


def test_concurrent_different_claims_compete_for_real_budget(tmp_path: Path) -> None:
    db, store, control, _budget, runtime, first_task, _packet = _setup(tmp_path, daily_cost=1.0)
    second_task, _second_packet = _create_task(control, store, feature="volatility_regime")
    first = runtime.create_authorization(
        task_id=first_task.task_id,
        task_revision=1,
        reserved_cost=0.75,
        reserved_tokens=100,
        authorized_by="operator",
    )
    second = runtime.create_authorization(
        task_id=second_task.task_id,
        task_revision=1,
        reserved_cost=0.75,
        reserved_tokens=100,
        authorized_by="operator",
    )
    barrier = threading.Barrier(2)

    def claim(authorization_id: int):
        barrier.wait()
        try:
            return runtime.claim_execution(authorization_id)
        except ResearchExecutionAuthorizationError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, (first.authorization_id, second.authorization_id)))
    assert sum(not isinstance(result, Exception) for result in results) == 1
    assert sum(isinstance(result, ResearchExecutionAuthorizationError) for result in results) == 1
    counts = _counts(db)
    assert counts["research_executions"] == 1
    assert counts["budget_reservations"] == 1


def test_concurrent_cancel_and_dispatch_declaration_have_one_winner(tmp_path: Path) -> None:
    _db, _store, _control, budget, runtime, task, _packet = _setup(tmp_path)
    auth = runtime.create_authorization(
        task_id=task.task_id,
        task_revision=1,
        reserved_cost=1.0,
        reserved_tokens=100,
        authorized_by="operator",
    )
    claim = runtime.claim_execution(auth.authorization_id)
    barrier = threading.Barrier(2)

    def transition(operation):
        barrier.wait()
        try:
            return operation(claim.request_hash)
        except ResearchExecutionStateError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(transition, runtime.cancel_before_dispatch),
            pool.submit(transition, runtime.mark_call_in_flight),
        ]
        results = [future.result() for future in futures]
    assert sum(not isinstance(result, Exception) for result in results) == 1
    final = runtime.get_execution(claim.request_hash)
    if final.status is ResearchExecutionStatus.CALL_IN_FLIGHT:
        assert budget.get_reservation(claim.request_hash).state is (BudgetReservationState.RESERVED)
    else:
        assert final.status is ResearchExecutionStatus.CANCELLED_BEFORE_DISPATCH
        assert budget.get_reservation(claim.request_hash).state is (BudgetReservationState.RELEASED)


def test_concurrent_duplicate_success_is_one_settlement(tmp_path: Path) -> None:
    db, _store, _control, _budget, runtime, task, packet = _setup(tmp_path)
    auth = runtime.create_authorization(
        task_id=task.task_id,
        task_revision=1,
        reserved_cost=1.0,
        reserved_tokens=100,
        authorized_by="operator",
    )
    claim = runtime.claim_execution(auth.authorization_id)
    runtime.mark_call_in_flight(claim.request_hash)
    response = _response(packet)
    barrier = threading.Barrier(2)

    def finalize():
        barrier.wait()
        return runtime.finalize_success(
            claim.request_hash,
            response=response,
            actual_cost=0.5,
            actual_tokens=50,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _index: finalize(), range(2)))
    assert results[0] == results[1]
    assert results[0].status is ResearchExecutionStatus.SUCCEEDED
    assert _counts(db)["model_usage"] == 1


@pytest.mark.parametrize(
    "failure_point",
    ["execution_insert", "authorization_consumption", "claim_audit"],
)
def test_claim_failure_is_fully_atomic(tmp_path: Path, monkeypatch, failure_point: str) -> None:
    db, _store, _control, budget, runtime, task, _packet = _setup(tmp_path)
    auth = runtime.create_authorization(
        task_id=task.task_id,
        task_revision=1,
        reserved_cost=1.0,
        reserved_tokens=100,
        authorized_by="operator",
    )
    before = _counts(db)
    if failure_point == "execution_insert":
        with sqlite3.connect(db) as connection:
            connection.execute(
                """
                CREATE TRIGGER fail_execution_insert
                BEFORE INSERT ON research_executions
                BEGIN SELECT RAISE(ABORT, 'injected execution insert failure'); END
                """
            )
    elif failure_point == "authorization_consumption":
        with sqlite3.connect(db) as connection:
            connection.execute(
                """
                CREATE TRIGGER fail_authorization_consumption
                BEFORE UPDATE OF state ON research_execution_authorizations
                BEGIN SELECT RAISE(ABORT, 'injected authorization consumption failure'); END
                """
            )
    else:
        original = AuditLog.append_in_transaction

        def fail_claim_audit(connection, event):
            if event.action == "RESEARCH_EXECUTION_CLAIMED":
                raise RuntimeError("injected audit failure")
            return original(connection, event)

        monkeypatch.setattr(AuditLog, "append_in_transaction", fail_claim_audit)

    with pytest.raises(Exception):
        runtime.claim_execution(auth.authorization_id)
    after = _counts(db)
    assert after == before
    assert runtime.get_authorization(auth.authorization_id).state is (
        ResearchAuthorizationState.ACTIVE
    )
    assert budget.get_reservation(auth.request_hash) is None


def test_budget_reserve_failure_leaves_authorization_and_control_unchanged(
    tmp_path: Path, monkeypatch
) -> None:
    db, _store, _control, budget, runtime, task, _packet = _setup(tmp_path)
    auth = runtime.create_authorization(
        task_id=task.task_id,
        task_revision=1,
        reserved_cost=1.0,
        reserved_tokens=100,
        authorized_by="operator",
    )
    before = _counts(db)

    def fail_reserve(*_args, **_kwargs):
        raise RuntimeError("injected reserve failure")

    monkeypatch.setattr(budget, "reserve_in_transaction", fail_reserve)
    with pytest.raises(RuntimeError, match="injected reserve"):
        runtime.claim_execution(auth.authorization_id)
    assert _counts(db) == before
    assert runtime.get_authorization(auth.authorization_id).state is (
        ResearchAuthorizationState.ACTIVE
    )


@pytest.mark.parametrize("failure_point", ["insert", "audit"])
def test_authorization_creation_failure_is_atomic(
    tmp_path: Path, monkeypatch, failure_point: str
) -> None:
    db, _store, _control, _budget, runtime, task, _packet = _setup(tmp_path)
    before = _counts(db)
    if failure_point == "insert":
        with sqlite3.connect(db) as connection:
            connection.execute(
                """
                CREATE TRIGGER fail_authorization_insert
                BEFORE INSERT ON research_execution_authorizations
                BEGIN SELECT RAISE(ABORT, 'injected authorization insert failure'); END
                """
            )
    else:
        original = AuditLog.append_in_transaction

        def fail_authorization_audit(connection, event):
            if event.action == "RESEARCH_EXECUTION_AUTHORIZATION_CREATED":
                raise RuntimeError("injected authorization audit failure")
            return original(connection, event)

        monkeypatch.setattr(AuditLog, "append_in_transaction", fail_authorization_audit)

    with pytest.raises(Exception):
        runtime.create_authorization(
            task_id=task.task_id,
            task_revision=1,
            reserved_cost=1.0,
            reserved_tokens=100,
            authorized_by="operator",
        )
    assert _counts(db) == before


def test_mark_call_audit_failure_rolls_back_state(tmp_path: Path, monkeypatch) -> None:
    _db, _store, _control, _budget, runtime, task, _packet = _setup(tmp_path)
    auth = runtime.create_authorization(
        task_id=task.task_id,
        task_revision=1,
        reserved_cost=1.0,
        reserved_tokens=100,
        authorized_by="operator",
    )
    claim = runtime.claim_execution(auth.authorization_id)
    original = AuditLog.append_in_transaction

    def fail_mark_audit(connection, event):
        if event.action == "RESEARCH_EXECUTION_CALL_IN_FLIGHT":
            raise RuntimeError("injected mark audit failure")
        return original(connection, event)

    monkeypatch.setattr(AuditLog, "append_in_transaction", fail_mark_audit)
    with pytest.raises(RuntimeError, match="injected mark"):
        runtime.mark_call_in_flight(claim.request_hash)
    assert runtime.get_execution(claim.request_hash).status is ResearchExecutionStatus.CLAIMED


def test_cancel_audit_failure_rolls_back_release_and_state(tmp_path: Path, monkeypatch) -> None:
    _db, _store, _control, budget, runtime, task, _packet = _setup(tmp_path)
    auth = runtime.create_authorization(
        task_id=task.task_id,
        task_revision=1,
        reserved_cost=1.0,
        reserved_tokens=100,
        authorized_by="operator",
    )
    claim = runtime.claim_execution(auth.authorization_id)
    original = AuditLog.append_in_transaction

    def fail_cancel_audit(connection, event):
        if event.action == "RESEARCH_EXECUTION_CANCELLED_BEFORE_DISPATCH":
            raise RuntimeError("injected cancellation audit failure")
        return original(connection, event)

    monkeypatch.setattr(AuditLog, "append_in_transaction", fail_cancel_audit)
    with pytest.raises(RuntimeError, match="injected cancellation"):
        runtime.cancel_before_dispatch(claim.request_hash)
    assert runtime.get_execution(claim.request_hash).status is ResearchExecutionStatus.CLAIMED
    assert budget.get_reservation(claim.request_hash).state is (BudgetReservationState.RESERVED)


@pytest.mark.parametrize("failure_point", ["settlement", "success_update", "final_audit"])
def test_finalization_database_failures_roll_back_settlement(
    tmp_path: Path, monkeypatch, failure_point: str
) -> None:
    db, _store, _control, budget, runtime, task, packet = _setup(tmp_path)
    auth = runtime.create_authorization(
        task_id=task.task_id,
        task_revision=1,
        reserved_cost=1.0,
        reserved_tokens=100,
        authorized_by="operator",
    )
    claim = runtime.claim_execution(auth.authorization_id)
    runtime.mark_call_in_flight(claim.request_hash)
    if failure_point == "settlement":

        def fail_settlement(*_args, **_kwargs):
            raise RuntimeError("injected settlement failure")

        monkeypatch.setattr(budget, "record_and_settle_in_transaction", fail_settlement)
    elif failure_point == "success_update":
        with sqlite3.connect(db) as connection:
            connection.execute(
                """
                CREATE TRIGGER fail_success_update
                BEFORE UPDATE OF status ON research_executions
                WHEN NEW.status='SUCCEEDED'
                BEGIN SELECT RAISE(ABORT, 'injected success update failure'); END
                """
            )
    else:
        original = AuditLog.append_in_transaction

        def fail_final_audit(connection, event):
            if event.action == "RESEARCH_EXECUTION_SUCCEEDED":
                raise RuntimeError("injected final audit failure")
            return original(connection, event)

        monkeypatch.setattr(AuditLog, "append_in_transaction", fail_final_audit)

    before_usage = _counts(db)["model_usage"]
    with pytest.raises(Exception):
        runtime.finalize_success(
            claim.request_hash,
            response=_response(packet),
            actual_cost=0.5,
            actual_tokens=50,
        )
    assert runtime.get_execution(claim.request_hash).status is (
        ResearchExecutionStatus.CALL_IN_FLIGHT
    )
    assert budget.get_reservation(claim.request_hash).state is (BudgetReservationState.RESERVED)
    assert _counts(db)["model_usage"] == before_usage


def test_cas_persistence_failure_never_changes_execution_or_usage(
    tmp_path: Path, monkeypatch
) -> None:
    db, store, _control, budget, runtime, task, packet = _setup(tmp_path)
    auth = runtime.create_authorization(
        task_id=task.task_id,
        task_revision=1,
        reserved_cost=1.0,
        reserved_tokens=100,
        authorized_by="operator",
    )
    claim = runtime.claim_execution(auth.authorization_id)
    runtime.mark_call_in_flight(claim.request_hash)

    def fail_import(*_args, **_kwargs):
        raise RuntimeError("injected CAS failure")

    monkeypatch.setattr(store, "import_snapshot", fail_import)
    before = _counts(db)
    with pytest.raises(ResearchExecutionResultError):
        runtime.finalize_success(
            claim.request_hash,
            response=_response(packet),
            actual_cost=0.5,
            actual_tokens=50,
        )
    assert _counts(db) == before
    assert runtime.get_execution(claim.request_hash).status is (
        ResearchExecutionStatus.CALL_IN_FLIGHT
    )
    assert budget.get_reservation(claim.request_hash).state is (BudgetReservationState.RESERVED)


def test_generic_orchestrator_isolation_and_task_state_are_unchanged(
    tmp_path: Path,
) -> None:
    db, store, control, budget, runtime, task, packet = _setup(tmp_path)
    before = task
    assert route_to_generic_workflow(task).accepted is False
    dispatch = Dispatcher(control.task_store).next_dispatch()
    assert dispatch.task is None
    assert dispatch.diagnostics[0].task_id == task.task_id

    engine = WorkflowEngine.__new__(WorkflowEngine)
    engine.control = control
    with pytest.raises(WorkflowRoutingError):
        engine.step(task.task_id)

    auth = runtime.create_authorization(
        task_id=task.task_id,
        task_revision=1,
        reserved_cost=1.0,
        reserved_tokens=100,
        authorized_by="operator",
    )
    claim = runtime.claim_execution(auth.authorization_id)
    runtime.mark_call_in_flight(claim.request_hash)
    runtime.finalize_success(
        claim.request_hash,
        response=_response(packet),
        actual_cost=0.5,
        actual_tokens=50,
    )
    assert control.task_store.get(task.task_id) == before
    assert control.task_store.get(task.task_id).state is TaskState.NEW
    assert budget.get_reservation(claim.request_hash).state is BudgetReservationState.SETTLED
    assert store
    assert db
