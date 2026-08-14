from __future__ import annotations

import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import pytest

from trademind.discovery.hypothesis_registry import HypothesisRegistry, HypothesisState
from trademind.orchestrator.artifact_store import ArtifactStore
from trademind.orchestrator.audit_log import AuditLog
from trademind.orchestrator.budget import BudgetManager, BudgetReservationState
from trademind.orchestrator.control_plane import ControlPlane
from trademind.orchestrator.dispatcher import Dispatcher, route_to_generic_workflow
from trademind.orchestrator.engine import WorkflowEngine, WorkflowRoutingError
from trademind.orchestrator.models import Task, TaskState
from trademind.research_execution import (
    ResearchExecutionControl,
    ResearchExecutionRecordV1,
)
from trademind.research_proposal_intake import (
    MAX_REJECTION_REASONS,
    ResearchProposalIntakeControl,
    ResearchProposalIntakeDatabaseError,
    ResearchProposalIntakeState,
    ResearchProposalRejectionReason,
    ResearchProposalReviewConflict,
    ResearchProposalSourceError,
)
from trademind.research_proposal_response import (
    RESEARCH_PROPOSAL_RESPONSE_KIND,
    RESEARCH_PROPOSAL_RESPONSE_SCHEMA_VERSION,
    ResearchProposalResponseV1,
)
from trademind.signal_statistics_agent_packet import (
    SignalStatisticsPacketV2,
    build_packet_v2_from_artifact,
    persist_packet_v2,
)
from trademind.signal_statistics_orchestrator_bridge import register_verified_packet_v2_task
from trademind.signal_statistics_provenance import (
    CandidateContentV2,
    CandidateDefinitionV2,
    CodeProvenance,
)
from trademind.signal_statistics_report import build_report_v2, persist_report_v2


_POLICY_HASH = f"sha256:{'2' * 64}"
_SOURCE_HASH = f"sha256:{'3' * 64}"


@dataclass(frozen=True, slots=True)
class _Context:
    db_path: Path
    artifact_root: Path
    control: ControlPlane
    budget: BudgetManager
    execution_control: ResearchExecutionControl
    registry: HypothesisRegistry
    intake_control: ResearchProposalIntakeControl
    task: Task
    packet: SignalStatisticsPacketV2
    execution: ResearchExecutionRecordV1


def _candidate() -> CandidateContentV2:
    return CandidateContentV2(
        candidate_definition=CandidateDefinitionV2(
            source_kind="signal_journal",
            source_namespace="trademind_signal_journal",
            symbol="XAUUSD",
            timeframe="M5",
            feature="spread_pressure",
            horizon=3,
            action_scope="BUY_SELL_DIRECTIONAL",
            evaluation_method_version="signal-statistics-v2",
        ),
        evaluation_policy_hash=_POLICY_HASH,
        metrics={"trades": 24, "win_rate": 0.5},
        status="RESEARCH_CANDIDATE",
        reason_codes=("BELOW_RESEARCH_MINIMUM",),
    )


def _manager(path: Path) -> BudgetManager:
    return BudgetManager(
        path,
        daily_cost_ceiling=100.0,
        monthly_cost_ceiling=100.0,
        daily_token_ceiling=100_000,
        monthly_token_ceiling=100_000,
        per_task_call_limit=8,
        per_role_call_limit=32,
    )


def _response(packet: SignalStatisticsPacketV2, proposal_count: int) -> ResearchProposalResponseV1:
    candidate_id = packet.candidate_bindings[0]["candidate_id"]
    proposals = [
        {
            "candidate_id": candidate_id,
            "title": f"Regime-conditioned continuation {index}",
            "rationale": f"The candidate may depend on volatility regime {index}.",
            "falsifiable_claim": (f"The effect remains positive in predefined regime {index}."),
            "proposed_test": f"Compare predefined public-data subsets for regime {index}.",
            "rejection_condition": f"Reject if regime {index} effect is non-positive.",
            "confidence": "HIGH" if index == 0 else "LOW",
        }
        for index in range(proposal_count)
    ]
    return ResearchProposalResponseV1.from_payload(
        {
            "schema_version": RESEARCH_PROPOSAL_RESPONSE_SCHEMA_VERSION,
            "response_kind": RESEARCH_PROPOSAL_RESPONSE_KIND,
            "proposals": proposals,
        }
    )


def _setup(
    tmp_path: Path,
    *,
    proposal_count: int = 1,
    succeed: bool = True,
) -> _Context:
    db_path = tmp_path / "orchestrator.db"
    artifact_root = tmp_path / "artifacts"
    store = ArtifactStore(artifact_root)
    control = ControlPlane(db_path)
    budget = _manager(db_path)
    report = build_report_v2(
        (_candidate(),),
        source_snapshot_hash_ref=_SOURCE_HASH,
        source_schema_version="1.1",
        code_provenance=CodeProvenance(
            producer_name="trademind",
            producer_version="proposal-intake-test",
            git_commit="1" * 40,
            revision_source="git_worktree",
        ),
        journal_rows=24,
        generated_at="2026-08-14T12:00:00+00:00",
    )
    report_ref = persist_report_v2(report, artifact_store=store)
    packet = build_packet_v2_from_artifact(report_ref.hash_ref, artifact_store=store)
    packet_ref = persist_packet_v2(packet, artifact_store=store)
    task = register_verified_packet_v2_task(
        packet_ref.hash_ref,
        control_plane=control,
        artifact_store=store,
    )
    execution_control = ResearchExecutionControl(
        control_plane=control,
        budget_manager=budget,
        artifact_store=store,
    )
    authorization = execution_control.create_authorization(
        task_id=task.task_id,
        task_revision=task.revision,
        reserved_cost=1.0,
        reserved_tokens=100,
        authorized_by="operator:execution",
    )
    execution = execution_control.claim_execution(authorization.authorization_id)
    execution = execution_control.mark_call_in_flight(execution.request_hash)
    if succeed:
        execution = execution_control.finalize_success(
            execution.request_hash,
            response=_response(packet, proposal_count),
            actual_cost=0.5,
            actual_tokens=50,
        )
    registry = HypothesisRegistry(db_path)
    intake_control = ResearchProposalIntakeControl(
        execution_control=execution_control,
        hypothesis_registry=registry,
    )
    return _Context(
        db_path=db_path,
        artifact_root=artifact_root,
        control=control,
        budget=budget,
        execution_control=execution_control,
        registry=registry,
        intake_control=intake_control,
        task=task,
        packet=packet,
        execution=execution,
    )


def _counts(path: Path) -> dict[str, int]:
    with sqlite3.connect(path) as db:
        return {
            table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "research_proposal_ingestions",
                "research_proposal_intakes",
                "hypotheses",
                "hypothesis_families",
                "model_usage",
                "audit_events",
            )
        }


def _audit_actions(path: Path) -> list[str]:
    with sqlite3.connect(path) as db:
        return [
            json.loads(row[0])["action"]
            for row in db.execute("SELECT payload FROM audit_events ORDER BY id")
        ]


def _restart(context: _Context) -> ResearchProposalIntakeControl:
    execution_control = ResearchExecutionControl(
        control_plane=ControlPlane(context.db_path),
        budget_manager=_manager(context.db_path),
        artifact_store=ArtifactStore(context.artifact_root),
    )
    return ResearchProposalIntakeControl(
        execution_control=execution_control,
        hypothesis_registry=HypothesisRegistry(context.db_path),
    )


def test_ingestion_creates_independent_pending_items_and_preserves_closed_layers(
    tmp_path: Path,
) -> None:
    context = _setup(tmp_path, proposal_count=2)
    task_before = context.control.task_store.get(context.task.task_id)
    execution_before = context.execution_control.get_execution(context.execution.request_hash)
    usage_before = _counts(context.db_path)["model_usage"]

    intakes = context.intake_control.ingest_succeeded_research_execution_v1(
        context.execution.request_hash
    )

    assert len(intakes) == 2
    assert intakes[0].candidate_id == intakes[1].candidate_id
    assert intakes[0].intake_id != intakes[1].intake_id
    assert {item.proposal_index for item in intakes} == {0, 1}
    assert all(item.state is ResearchProposalIntakeState.PENDING_REVIEW for item in intakes)
    assert all(item.hypothesis_id is None for item in intakes)
    assert _counts(context.db_path)["hypotheses"] == 0
    ingestion = context.intake_control.get_ingestion(context.execution.request_hash)
    assert ingestion is not None
    assert ingestion.result_artifact_hash_ref == context.execution.result_artifact_hash_ref
    assert ingestion.packet_semantic_hash == context.packet.packet_semantic_hash
    assert context.control.task_store.get(context.task.task_id) == task_before == context.task
    assert (
        context.execution_control.get_execution(context.execution.request_hash) == execution_before
    )
    assert _counts(context.db_path)["model_usage"] == usage_before
    assert _audit_actions(context.db_path)[-3:] == [
        "RESEARCH_PROPOSAL_RESPONSE_INGESTED",
        "RESEARCH_PROPOSAL_INTAKE_CREATED",
        "RESEARCH_PROPOSAL_INTAKE_CREATED",
    ]


def test_empty_response_is_deterministic_abstention_without_hypothesis(tmp_path: Path) -> None:
    context = _setup(tmp_path, proposal_count=0)
    first = context.intake_control.ingest_succeeded_research_execution_v1(
        context.execution.request_hash
    )
    second = context.intake_control.ingest_succeeded_research_execution_v1(
        context.execution.request_hash
    )
    assert first == second == ()
    assert context.intake_control.get_ingestion(context.execution.request_hash).proposal_count == 0
    assert _counts(context.db_path)["research_proposal_ingestions"] == 1
    assert _counts(context.db_path)["research_proposal_intakes"] == 0
    assert _counts(context.db_path)["hypotheses"] == 0
    assert _audit_actions(context.db_path).count("RESEARCH_PROPOSAL_RESPONSE_INGESTED") == 1


def test_non_succeeded_execution_cannot_be_ingested(tmp_path: Path) -> None:
    context = _setup(tmp_path, succeed=False)
    before = _counts(context.db_path)
    with pytest.raises(ResearchProposalSourceError):
        context.intake_control.ingest_succeeded_research_execution_v1(
            context.execution.request_hash
        )
    assert _counts(context.db_path) == before


def test_sixteen_concurrent_ingestions_create_one_logical_set(tmp_path: Path) -> None:
    context = _setup(tmp_path, proposal_count=2)
    barrier = threading.Barrier(16)

    def ingest(_index: int):
        barrier.wait()
        return context.intake_control.ingest_succeeded_research_execution_v1(
            context.execution.request_hash
        )

    with ThreadPoolExecutor(max_workers=16) as executor:
        results = list(executor.map(ingest, range(16)))
    assert all(result == results[0] for result in results)
    assert _counts(context.db_path)["research_proposal_ingestions"] == 1
    assert _counts(context.db_path)["research_proposal_intakes"] == 2
    assert _audit_actions(context.db_path).count("RESEARCH_PROPOSAL_RESPONSE_INGESTED") == 1
    assert _audit_actions(context.db_path).count("RESEARCH_PROPOSAL_INTAKE_CREATED") == 2


def test_rejection_is_explicit_idempotent_and_never_creates_hypothesis(tmp_path: Path) -> None:
    context = _setup(tmp_path)
    pending = context.intake_control.ingest_succeeded_research_execution_v1(
        context.execution.request_hash
    )[0]
    task_before = context.control.task_store.get(context.task.task_id)
    execution_before = context.execution_control.get_execution(context.execution.request_hash)
    usage_before = _counts(context.db_path)["model_usage"]
    reasons = (
        ResearchProposalRejectionReason.NOT_FALSIFIABLE,
        ResearchProposalRejectionReason.INSUFFICIENT_SOURCE_GROUNDING,
    )

    rejected = context.intake_control.reject(
        pending.intake_id,
        reviewer_id="operator:reviewer",
        reason_codes=reasons,
    )
    repeated = context.intake_control.reject(
        pending.intake_id,
        reviewer_id="operator:reviewer",
        reason_codes=reasons,
    )
    assert repeated == rejected
    assert rejected.state is ResearchProposalIntakeState.REJECTED
    assert rejected.reason_codes == tuple(sorted(reasons, key=lambda item: item.value))
    assert _counts(context.db_path)["hypotheses"] == 0
    with pytest.raises(ResearchProposalReviewConflict):
        context.intake_control.accept_for_hypothesis(
            pending.intake_id,
            reviewer_id="operator:reviewer",
        )
    assert context.control.task_store.get(context.task.task_id) == task_before
    assert (
        context.execution_control.get_execution(context.execution.request_hash) == execution_before
    )
    assert _counts(context.db_path)["model_usage"] == usage_before
    assert _audit_actions(context.db_path).count("RESEARCH_PROPOSAL_REJECTED") == 1


def test_acceptance_creates_one_proposed_hypothesis_with_exact_provenance(
    tmp_path: Path,
) -> None:
    context = _setup(tmp_path)
    pending = context.intake_control.ingest_succeeded_research_execution_v1(
        context.execution.request_hash
    )[0]
    task_before = context.control.task_store.get(context.task.task_id)
    execution_before = context.execution_control.get_execution(context.execution.request_hash)
    usage_before = _counts(context.db_path)["model_usage"]

    accepted, hypothesis = context.intake_control.accept_for_hypothesis(
        pending.intake_id,
        reviewer_id="operator:reviewer",
    )
    repeated = context.intake_control.accept_for_hypothesis(
        pending.intake_id,
        reviewer_id="operator:reviewer",
    )

    assert repeated == (accepted, hypothesis)
    assert accepted.state is ResearchProposalIntakeState.ACCEPTED_FOR_HYPOTHESIS
    assert accepted.hypothesis_id == accepted.intake_id == hypothesis.hypothesis_id
    assert hypothesis.state is HypothesisState.PROPOSED
    assert hypothesis.manifest_hash is None
    with sqlite3.connect(context.db_path) as db:
        family_json, content_json = db.execute(
            """
            SELECT f.definition_json, h.content_definition_json
            FROM hypotheses h JOIN hypothesis_families f ON f.family_id=h.family_id
            WHERE h.hypothesis_id=?
            """,
            (hypothesis.hypothesis_id,),
        ).fetchone()
    family = json.loads(family_json)
    content = json.loads(content_json)
    assert family["falsifiable_claim"] == pending.proposal.falsifiable_claim
    assert family["proposed_test"] == pending.proposal.proposed_test
    assert family["rejection_condition"] == pending.proposal.rejection_condition
    assert content["proposal"]["title"] == pending.proposal.title
    assert content["proposal"]["rationale"] == pending.proposal.rationale
    assert content["model_metadata"] == {
        "interpretation": "external_model_self_assessment_only",
        "qualitative_confidence": "HIGH",
    }
    assert content["provenance"] == {
        "intake_schema_version": "research-proposal-intake-v1",
        "intake_id": pending.intake_id,
        "execution_request_hash": pending.request_hash,
        "authorization_id": pending.authorization_id,
        "task_id": pending.task_id,
        "task_revision": pending.task_revision,
        "packet_artifact_hash_ref": pending.packet_artifact_hash_ref,
        "packet_semantic_hash": pending.packet_semantic_hash,
        "result_artifact_hash_ref": pending.result_artifact_hash_ref,
        "proposal_index": pending.proposal_index,
        "candidate_id": pending.candidate_id,
    }
    assert context.control.task_store.get(context.task.task_id) == task_before
    assert (
        context.execution_control.get_execution(context.execution.request_hash) == execution_before
    )
    assert _counts(context.db_path)["model_usage"] == usage_before
    assert _audit_actions(context.db_path).count("RESEARCH_PROPOSAL_ACCEPTED_FOR_HYPOTHESIS") == 1
    assert _audit_actions(context.db_path).count("HYPOTHESIS_REGISTRY_BINDING_CREATED") == 1


def test_sixteen_concurrent_identical_accepts_create_one_binding(tmp_path: Path) -> None:
    context = _setup(tmp_path)
    pending = context.intake_control.ingest_succeeded_research_execution_v1(
        context.execution.request_hash
    )[0]
    barrier = threading.Barrier(16)

    def accept(_index: int):
        barrier.wait()
        return context.intake_control.accept_for_hypothesis(
            pending.intake_id,
            reviewer_id="operator:reviewer",
        )

    with ThreadPoolExecutor(max_workers=16) as executor:
        results = list(executor.map(accept, range(16)))
    assert all(result == results[0] for result in results)
    assert _counts(context.db_path)["hypotheses"] == 1
    assert _counts(context.db_path)["hypothesis_families"] == 1
    assert _audit_actions(context.db_path).count("RESEARCH_PROPOSAL_ACCEPTED_FOR_HYPOTHESIS") == 1


def test_identical_acceptance_remains_idempotent_after_downstream_freeze(
    tmp_path: Path,
) -> None:
    context = _setup(tmp_path)
    pending = context.intake_control.ingest_succeeded_research_execution_v1(
        context.execution.request_hash
    )[0]
    accepted, _hypothesis = context.intake_control.accept_for_hypothesis(
        pending.intake_id,
        reviewer_id="operator:reviewer",
    )
    frozen = context.registry.freeze(accepted.hypothesis_id, manifest_hash="a" * 64)

    repeated_intake, repeated_hypothesis = context.intake_control.accept_for_hypothesis(
        pending.intake_id,
        reviewer_id="operator:reviewer",
    )

    assert repeated_intake == accepted
    assert repeated_hypothesis == frozen
    assert repeated_hypothesis.state is HypothesisState.FROZEN
    assert _counts(context.db_path)["hypotheses"] == 1
    assert _audit_actions(context.db_path).count("RESEARCH_PROPOSAL_ACCEPTED_FOR_HYPOTHESIS") == 1


def test_review_fails_closed_on_persisted_ingestion_binding_tamper(tmp_path: Path) -> None:
    context = _setup(tmp_path)
    pending = context.intake_control.ingest_succeeded_research_execution_v1(
        context.execution.request_hash
    )[0]
    with sqlite3.connect(context.db_path) as db:
        db.execute(
            "UPDATE research_proposal_ingestions SET task_id='tampered' WHERE request_hash=?",
            (context.execution.request_hash,),
        )

    before = _counts(context.db_path)
    with pytest.raises(ResearchProposalIntakeDatabaseError):
        context.intake_control.accept_for_hypothesis(
            pending.intake_id,
            reviewer_id="operator:reviewer",
        )
    assert _counts(context.db_path) == before
    assert context.intake_control.get(pending.intake_id) == pending


def test_idempotent_acceptance_fails_closed_on_registry_definition_tamper(
    tmp_path: Path,
) -> None:
    context = _setup(tmp_path)
    pending = context.intake_control.ingest_succeeded_research_execution_v1(
        context.execution.request_hash
    )[0]
    context.intake_control.accept_for_hypothesis(
        pending.intake_id,
        reviewer_id="operator:reviewer",
    )
    with sqlite3.connect(context.db_path) as db:
        db.execute(
            "UPDATE hypotheses SET content_definition_json=? WHERE hypothesis_id=?",
            ('{"tampered":true}', pending.intake_id),
        )

    before = _counts(context.db_path)
    with pytest.raises(ResearchProposalIntakeDatabaseError):
        context.intake_control.accept_for_hypothesis(
            pending.intake_id,
            reviewer_id="operator:reviewer",
        )
    assert _counts(context.db_path) == before


def test_concurrent_accept_vs_reject_has_exactly_one_legal_winner(tmp_path: Path) -> None:
    context = _setup(tmp_path)
    pending = context.intake_control.ingest_succeeded_research_execution_v1(
        context.execution.request_hash
    )[0]
    barrier = threading.Barrier(2)

    def accept():
        barrier.wait()
        return context.intake_control.accept_for_hypothesis(
            pending.intake_id,
            reviewer_id="operator:accept",
        )

    def reject():
        barrier.wait()
        return context.intake_control.reject(
            pending.intake_id,
            reviewer_id="operator:reject",
            reason_codes=(ResearchProposalRejectionReason.OUT_OF_SCOPE,),
        )

    successes = []
    conflicts = []
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = (executor.submit(accept), executor.submit(reject))
        for future in futures:
            try:
                successes.append(future.result())
            except ResearchProposalReviewConflict as exc:
                conflicts.append(exc)
    assert len(successes) == 1
    assert len(conflicts) == 1
    final = context.intake_control.get(pending.intake_id)
    assert final.state in {
        ResearchProposalIntakeState.ACCEPTED_FOR_HYPOTHESIS,
        ResearchProposalIntakeState.REJECTED,
    }
    assert _counts(context.db_path)["hypotheses"] == (
        1 if final.state is ResearchProposalIntakeState.ACCEPTED_FOR_HYPOTHESIS else 0
    )


@pytest.mark.parametrize("review", ["pending", "reject", "accept"])
def test_restart_preserves_review_state_binding_and_source_reverification(
    tmp_path: Path,
    review: str,
) -> None:
    context = _setup(tmp_path)
    pending = context.intake_control.ingest_succeeded_research_execution_v1(
        context.execution.request_hash
    )[0]
    if review == "reject":
        expected = context.intake_control.reject(
            pending.intake_id,
            reviewer_id="operator:reviewer",
            reason_codes=(ResearchProposalRejectionReason.OUT_OF_SCOPE,),
        )
    elif review == "accept":
        expected, _hypothesis = context.intake_control.accept_for_hypothesis(
            pending.intake_id,
            reviewer_id="operator:reviewer",
        )
    else:
        expected = pending

    restarted = _restart(context)
    assert restarted.get(pending.intake_id) == expected
    assert restarted.reverify(pending.intake_id) == expected
    if review == "accept":
        assert HypothesisRegistry(context.db_path).get(pending.intake_id).state is (
            HypothesisState.PROPOSED
        )


@pytest.mark.parametrize("failure_point", ["insert", "audit"])
def test_ingestion_failure_is_atomic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
) -> None:
    context = _setup(tmp_path)
    if failure_point == "insert":
        with sqlite3.connect(context.db_path) as db:
            db.execute(
                """
                CREATE TRIGGER fail_intake_insert
                BEFORE INSERT ON research_proposal_intakes
                BEGIN SELECT RAISE(ABORT, 'injected intake insert failure'); END
                """
            )
    else:
        original = AuditLog.append_in_transaction

        def fail_audit(db, event):
            if event.action == "RESEARCH_PROPOSAL_RESPONSE_INGESTED":
                raise RuntimeError("injected ingestion audit failure")
            return original(db, event)

        monkeypatch.setattr(AuditLog, "append_in_transaction", fail_audit)
    before = _counts(context.db_path)
    with pytest.raises(Exception):
        context.intake_control.ingest_succeeded_research_execution_v1(
            context.execution.request_hash
        )
    assert _counts(context.db_path) == before


@pytest.mark.parametrize("failure_point", ["update", "audit"])
def test_rejection_failure_is_atomic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
) -> None:
    context = _setup(tmp_path)
    pending = context.intake_control.ingest_succeeded_research_execution_v1(
        context.execution.request_hash
    )[0]
    if failure_point == "update":
        with sqlite3.connect(context.db_path) as db:
            db.execute(
                """
                CREATE TRIGGER fail_reject_update
                BEFORE UPDATE OF state ON research_proposal_intakes
                WHEN NEW.state='REJECTED'
                BEGIN SELECT RAISE(ABORT, 'injected reject update failure'); END
                """
            )
    else:
        original = AuditLog.append_in_transaction

        def fail_audit(db, event):
            if event.action == "RESEARCH_PROPOSAL_REJECTED":
                raise RuntimeError("injected rejection audit failure")
            return original(db, event)

        monkeypatch.setattr(AuditLog, "append_in_transaction", fail_audit)
    before = _counts(context.db_path)
    with pytest.raises(Exception):
        context.intake_control.reject(
            pending.intake_id,
            reviewer_id="operator:reviewer",
            reason_codes=(ResearchProposalRejectionReason.OUT_OF_SCOPE,),
        )
    assert context.intake_control.get(pending.intake_id) == pending
    assert _counts(context.db_path) == before


@pytest.mark.parametrize(
    "failure_point",
    ["registry", "binding_update", "accept_audit", "binding_audit"],
)
def test_acceptance_failure_never_leaves_orphan_or_half_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
) -> None:
    context = _setup(tmp_path)
    pending = context.intake_control.ingest_succeeded_research_execution_v1(
        context.execution.request_hash
    )[0]
    if failure_point == "registry":

        def fail_registry(*_args, **_kwargs):
            raise RuntimeError("injected registry failure")

        monkeypatch.setattr(context.registry, "register_in_transaction", fail_registry)
    elif failure_point == "binding_update":
        with sqlite3.connect(context.db_path) as db:
            db.execute(
                """
                CREATE TRIGGER fail_accept_update
                BEFORE UPDATE OF state ON research_proposal_intakes
                WHEN NEW.state='ACCEPTED_FOR_HYPOTHESIS'
                BEGIN SELECT RAISE(ABORT, 'injected accept update failure'); END
                """
            )
    else:
        original = AuditLog.append_in_transaction
        target = (
            "RESEARCH_PROPOSAL_ACCEPTED_FOR_HYPOTHESIS"
            if failure_point == "accept_audit"
            else "HYPOTHESIS_REGISTRY_BINDING_CREATED"
        )

        def fail_audit(db, event):
            if event.action == target:
                raise RuntimeError("injected acceptance audit failure")
            return original(db, event)

        monkeypatch.setattr(AuditLog, "append_in_transaction", fail_audit)
    before = _counts(context.db_path)
    with pytest.raises(Exception):
        context.intake_control.accept_for_hypothesis(
            pending.intake_id,
            reviewer_id="operator:reviewer",
        )
    assert context.intake_control.get(pending.intake_id) == pending
    assert _counts(context.db_path) == before


def test_reviewer_and_rejection_resource_bounds_are_exact(tmp_path: Path) -> None:
    context = _setup(tmp_path)
    pending = context.intake_control.ingest_succeeded_research_execution_v1(
        context.execution.request_hash
    )[0]
    invalid = [
        ("", (ResearchProposalRejectionReason.OUT_OF_SCOPE,)),
        (" reviewer", (ResearchProposalRejectionReason.OUT_OF_SCOPE,)),
        ("x" * 257, (ResearchProposalRejectionReason.OUT_OF_SCOPE,)),
        ("reviewer", ()),
        ("reviewer", (ResearchProposalRejectionReason.OUT_OF_SCOPE,) * 2),
        (
            "reviewer",
            tuple(ResearchProposalRejectionReason)[: MAX_REJECTION_REASONS + 1],
        ),
    ]
    for reviewer, reasons in invalid:
        with pytest.raises(ValueError):
            context.intake_control.reject(
                pending.intake_id,
                reviewer_id=reviewer,
                reason_codes=reasons,
            )
    assert context.intake_control.get(pending.intake_id) == pending


def test_incomplete_unknown_or_changed_intake_schema_fails_closed(tmp_path: Path) -> None:
    incomplete = _setup(tmp_path / "incomplete")
    with sqlite3.connect(incomplete.db_path) as db:
        db.execute("DROP TABLE research_proposal_intakes")
    with pytest.raises(ResearchProposalIntakeDatabaseError):
        ResearchProposalIntakeControl(
            execution_control=incomplete.execution_control,
            hypothesis_registry=incomplete.registry,
        )

    unknown = _setup(tmp_path / "unknown")
    with sqlite3.connect(unknown.db_path) as db:
        db.execute("PRAGMA ignore_check_constraints=ON")
        db.execute(
            "UPDATE research_proposal_intake_meta SET schema_version='unknown-v9' WHERE id=1"
        )
    with pytest.raises(ResearchProposalIntakeDatabaseError):
        ResearchProposalIntakeControl(
            execution_control=unknown.execution_control,
            hypothesis_registry=unknown.registry,
        )

    changed = _setup(tmp_path / "changed")
    with sqlite3.connect(changed.db_path) as db:
        db.execute("ALTER TABLE research_proposal_intakes ADD COLUMN untrusted TEXT")
    with pytest.raises(ResearchProposalIntakeDatabaseError):
        ResearchProposalIntakeControl(
            execution_control=changed.execution_control,
            hypothesis_registry=changed.registry,
        )


def test_generic_orchestrator_still_ignores_and_cannot_advance_research_task(
    tmp_path: Path,
) -> None:
    context = _setup(tmp_path)
    pending = context.intake_control.ingest_succeeded_research_execution_v1(
        context.execution.request_hash
    )[0]
    context.intake_control.accept_for_hypothesis(
        pending.intake_id,
        reviewer_id="operator:reviewer",
    )
    task_before = context.control.task_store.get(context.task.task_id)
    audit_before = _counts(context.db_path)["audit_events"]
    assert route_to_generic_workflow(task_before).accepted is False
    dispatch = Dispatcher(context.control.task_store).next_dispatch()
    assert dispatch.task is None
    assert dispatch.diagnostics[0].task_id == context.task.task_id
    engine = WorkflowEngine.__new__(WorkflowEngine)
    engine.control = context.control
    with pytest.raises(WorkflowRoutingError):
        engine.step(context.task.task_id)
    assert context.control.task_store.get(context.task.task_id) == task_before == context.task
    assert context.control.task_store.get(context.task.task_id).state is TaskState.NEW
    assert _counts(context.db_path)["audit_events"] == audit_before
    assert context.budget.get_reservation(context.execution.request_hash).state is (
        BudgetReservationState.SETTLED
    )


def test_registry_transaction_adapter_rejects_wrong_database(tmp_path: Path) -> None:
    first = HypothesisRegistry(tmp_path / "first.db")
    second = HypothesisRegistry(tmp_path / "second.db")
    with second._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        with pytest.raises(Exception, match="different database"):
            first.register_in_transaction(
                db,
                hypothesis_id="H-WRONG-DB",
                family_definition={"event": "x"},
                content_definition={"event": "x", "parameter": 1},
            )
