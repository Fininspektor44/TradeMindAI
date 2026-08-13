from __future__ import annotations

import copy
import io
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import pytest

from trademind.orchestrator.artifact_store import (
    ArtifactIntegrityError,
    ArtifactMediaTypeError,
    ArtifactNotFoundError,
    ArtifactStore,
)
from trademind.orchestrator.control_plane import ControlPlane
from trademind.orchestrator.dispatcher import route_to_generic_workflow
from trademind.orchestrator.models import Role, TaskState
from trademind.signal_statistics_agent_packet import (
    AGENT_PACKET_SCHEMA_VERSION,
    PACKET_V2_MEDIA_TYPE,
    build_packet_v2_from_artifact,
    persist_packet_v2,
)
from trademind.signal_statistics_orchestrator_bridge import (
    RESEARCH_PROPOSAL_OUTPUT_KIND,
    VERIFIED_PACKET_BRIDGE_SCHEMA_VERSION,
    VerifiedPacketTaskBridgeError,
    register_agent_packet,
    register_verified_packet_v2_task,
)
from trademind.signal_statistics_provenance import (
    CandidateContentV2,
    CandidateDefinitionV2,
    CodeProvenance,
    PacketContentHashProjection,
    ProvenanceError,
    canonical_json_bytes,
    packet_content_hash,
    task_id_from_packet_hash,
)
from trademind.signal_statistics_report import build_report_v2, persist_report_v2


_POLICY_HASH = f"sha256:{'2' * 64}"
_SOURCE_HASH = f"sha256:{'3' * 64}"


def _packet() -> dict[str, object]:
    return {
        "schema_version": AGENT_PACKET_SCHEMA_VERSION,
        "source_report_schema_version": "signal-statistics-report-v1",
        "source_generated_at": "2026-08-11T03:00:00+00:00",
        "read_only": True,
        "orders_enabled": False,
        "decision_scope": "research_hypotheses_only",
        "candidate_count": 2,
        "selection_policy": {},
        "prohibited_actions": ["enable_orders"],
        "analysis_questions": ["What falsifiable hypothesis should be tested next?"],
        "candidates": [
            {
                "symbol": ".US30CASH",
                "pattern": "BULLISH_FVG",
                "horizon": 12,
                "trades": 99,
            },
            {
                "symbol": ".US30CASH",
                "pattern": "BULLISH_FVG",
                "horizon": 3,
                "trades": 154,
            },
        ],
    }


def _candidate(*, feature: str = "BULLISH_BOS", trades: int = 12) -> CandidateContentV2:
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
        metrics={"trades": trades, "win_rate": 0.5},
        status="RESEARCH_CANDIDATE",
        reason_codes=("BELOW_RESEARCH_MINIMUM",),
    )


def _persist_packet_v2(
    store: ArtifactStore,
    *,
    candidates: tuple[CandidateContentV2, ...] | None = None,
):
    report = build_report_v2(
        candidates
        or (
            _candidate(feature="BULLISH_BOS"),
            _candidate(feature="BEARISH_BOS", trades=24),
        ),
        source_snapshot_hash_ref=_SOURCE_HASH,
        source_schema_version="1.1",
        code_provenance=CodeProvenance(
            producer_name="trademind",
            producer_version="verified-packet-bridge-test",
            git_commit="1" * 40,
            revision_source="git_worktree",
        ),
        journal_rows=24,
        generated_at="2026-08-13T12:00:00+00:00",
    )
    report_artifact = persist_report_v2(report, artifact_store=store)
    packet = build_packet_v2_from_artifact(report_artifact.hash_ref, artifact_store=store)
    packet_artifact = persist_packet_v2(packet, artifact_store=store)
    return report, report_artifact, packet, packet_artifact


def _audit_count(db_path: Path) -> int:
    with sqlite3.connect(db_path) as db:
        return int(db.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0])


def test_register_agent_packet_creates_content_addressed_research_task(tmp_path: Path) -> None:
    db_path = tmp_path / "orchestrator.db"
    artifact_store = ArtifactStore(tmp_path / "artifacts")
    control_plane = ControlPlane(db_path)

    task, artifact = register_agent_packet(
        _packet(),
        task_id="signal-stats-research-001",
        control_plane=control_plane,
        artifact_store=artifact_store,
    )

    assert task.state is TaskState.NEW
    assert task.assigned_role is Role.OPERATOR
    assert task.artifact_refs == (artifact.hash_ref,)
    assert artifact_store.verify(artifact)

    persisted = control_plane.task_store.get(task.task_id)
    assert persisted is not None
    assert persisted.artifact_refs == (artifact.hash_ref,)
    assert persisted.scope == ("signal_statistics", "research_hypotheses_only")
    assert persisted.allowed_tools == ()
    assert persisted.budget_limit == 0.0


def test_register_agent_packet_rejects_any_order_enabled_packet(tmp_path: Path) -> None:
    packet = _packet()
    packet["orders_enabled"] = True

    with pytest.raises(ValueError, match="orders disabled"):
        register_agent_packet(
            packet,
            task_id="signal-stats-research-002",
            control_plane=ControlPlane(tmp_path / "orchestrator.db"),
            artifact_store=ArtifactStore(tmp_path / "artifacts"),
        )


def test_register_agent_packet_rejects_empty_candidate_set(tmp_path: Path) -> None:
    packet = _packet()
    packet["candidate_count"] = 0
    packet["candidates"] = []

    with pytest.raises(ValueError, match="at least one candidate"):
        register_agent_packet(
            packet,
            task_id="signal-stats-research-003",
            control_plane=ControlPlane(tmp_path / "orchestrator.db"),
            artifact_store=ArtifactStore(tmp_path / "artifacts"),
        )


def test_verified_packet_v2_creates_deterministic_research_only_new_task(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "orchestrator.db"
    store = ArtifactStore(tmp_path / "artifacts")
    _, _, packet, packet_artifact = _persist_packet_v2(store)
    control = ControlPlane(db_path)

    task = register_verified_packet_v2_task(
        packet_artifact.hash_ref,
        control_plane=control,
        artifact_store=store,
    )

    assert task.task_id == task_id_from_packet_hash(packet.packet_semantic_hash)
    assert task.task_id == f"signal-stats-v2-{packet.packet_semantic_hash.removeprefix('sha256:')}"
    assert task.revision == 1
    assert task.parent_task_id is None
    assert task.state is TaskState.NEW
    assert task.assigned_role is Role.OPERATOR
    assert task.allowed_tools == ()
    assert task.budget_limit == 0.0
    assert task.artifact_refs == (packet_artifact.hash_ref,)
    assert task.scope[:2] == ("signal_statistics", "research_hypotheses_only")
    assert VERIFIED_PACKET_BRIDGE_SCHEMA_VERSION in task.scope
    assert "agent_protocol:orchestrator-agent-v2" in task.scope
    assert "input_schema:signal-statistics-packet-v2" in task.scope
    assert f"output_kind:{RESEARCH_PROPOSAL_OUTPUT_KIND}" in task.scope
    assert route_to_generic_workflow(task).accepted is False
    assert "orders" in " ".join(task.acceptance_criteria).lower()
    assert "broker" in " ".join(task.acceptance_criteria).lower()
    assert control.task_store.get(task.task_id) == task
    assert _audit_count(db_path) == 1


def test_verified_packet_v2_repeated_call_and_restart_are_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "orchestrator.db"
    artifact_root = tmp_path / "artifacts"
    store = ArtifactStore(artifact_root)
    _, _, _, packet_artifact = _persist_packet_v2(store)
    first_control = ControlPlane(db_path)

    first = register_verified_packet_v2_task(
        packet_artifact.hash_ref,
        control_plane=first_control,
        artifact_store=store,
    )
    second = register_verified_packet_v2_task(
        packet_artifact.hash_ref,
        control_plane=first_control,
        artifact_store=store,
    )
    restarted_control = ControlPlane(db_path)
    restarted_store = ArtifactStore(artifact_root)
    third = register_verified_packet_v2_task(
        packet_artifact.hash_ref,
        control_plane=restarted_control,
        artifact_store=restarted_store,
    )

    assert first == second == third
    assert restarted_control.task_store.get(first.task_id) == first
    assert _audit_count(db_path) == 1


def test_verified_packet_v2_same_report_has_one_packet_and_task_identity(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    _, report_artifact, packet, first_artifact = _persist_packet_v2(store)
    rebuilt_packet = build_packet_v2_from_artifact(
        report_artifact.hash_ref,
        artifact_store=store,
    )
    second_artifact = persist_packet_v2(rebuilt_packet, artifact_store=store)
    control = ControlPlane(tmp_path / "orchestrator.db")

    first = register_verified_packet_v2_task(
        first_artifact.hash_ref,
        control_plane=control,
        artifact_store=store,
    )
    second = register_verified_packet_v2_task(
        second_artifact.hash_ref,
        control_plane=control,
        artifact_store=store,
    )

    assert rebuilt_packet.packet_semantic_hash == packet.packet_semantic_hash
    assert second_artifact.hash_ref == first_artifact.hash_ref
    assert second == first
    assert _audit_count(control.path) == 1


@pytest.mark.parametrize(
    "override",
    (
        {"allowed_tools": ("broker",)},
        {"budget_limit": 1.0},
        {"decision_scope": "execute_trades"},
    ),
)
def test_verified_packet_v2_bridge_rejects_caller_policy_overrides(
    tmp_path: Path,
    override: dict[str, object],
) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    _, _, _, packet_artifact = _persist_packet_v2(store)
    control = ControlPlane(tmp_path / "orchestrator.db")

    with pytest.raises(TypeError, match="unexpected keyword argument"):
        register_verified_packet_v2_task(
            packet_artifact.hash_ref,
            control_plane=control,
            artifact_store=store,
            **override,  # type: ignore[arg-type]
        )

    assert control.task_store.list_latest() == []
    assert _audit_count(control.path) == 0


def test_verified_packet_v2_bridge_never_calls_provider_or_generic_engine(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    _, _, _, packet_artifact = _persist_packet_v2(store)

    with (
        patch("trademind.orchestrator.role_router.RoleRouter.execute") as provider_execute,
        patch("trademind.orchestrator.engine.WorkflowEngine.step") as engine_step,
    ):
        task = register_verified_packet_v2_task(
            packet_artifact.hash_ref,
            control_plane=ControlPlane(tmp_path / "orchestrator.db"),
            artifact_store=store,
        )

    assert task.state is TaskState.NEW
    provider_execute.assert_not_called()
    engine_step.assert_not_called()


@pytest.mark.parametrize(
    "value",
    (
        "sha256:" + "A" * 64,
        " sha256:" + "a" * 64,
        "sha256:" + "a" * 63,
        "sha256:" + "а" * 64,
    ),
)
def test_verified_packet_v2_bridge_rejects_malformed_hash_without_task(
    tmp_path: Path,
    value: str,
) -> None:
    db_path = tmp_path / "orchestrator.db"
    control = ControlPlane(db_path)

    with pytest.raises(ProvenanceError, match="sha256"):
        register_verified_packet_v2_task(
            value,
            control_plane=control,
            artifact_store=ArtifactStore(tmp_path / "artifacts"),
        )

    assert control.task_store.list_latest() == []
    assert _audit_count(db_path) == 0


@pytest.mark.parametrize("unverified_input", [b"packet bytes", Path("packet.json"), {"packet": 1}])
def test_verified_packet_v2_bridge_rejects_non_hash_authoritative_inputs(
    tmp_path: Path,
    unverified_input: object,
) -> None:
    db_path = tmp_path / "orchestrator.db"
    control = ControlPlane(db_path)

    with pytest.raises(ProvenanceError, match="sha256"):
        register_verified_packet_v2_task(
            unverified_input,  # type: ignore[arg-type]
            control_plane=control,
            artifact_store=ArtifactStore(tmp_path / "artifacts"),
        )

    assert control.task_store.list_latest() == []
    assert _audit_count(db_path) == 0


def test_verified_packet_v2_bridge_rejects_missing_tampered_and_wrong_media(
    tmp_path: Path,
) -> None:
    missing_control = ControlPlane(tmp_path / "missing.db")
    missing_store = ArtifactStore(tmp_path / "missing-artifacts")
    with pytest.raises(ArtifactNotFoundError):
        register_verified_packet_v2_task(
            f"sha256:{'0' * 64}",
            control_plane=missing_control,
            artifact_store=missing_store,
        )
    assert missing_control.task_store.list_latest() == []

    tampered_store = ArtifactStore(tmp_path / "tampered-artifacts")
    _, _, packet, packet_artifact = _persist_packet_v2(tampered_store)
    Path(packet_artifact.path).write_bytes(packet.canonical_bytes()[:-1] + b" ")
    tampered_control = ControlPlane(tmp_path / "tampered.db")
    with pytest.raises(ArtifactIntegrityError):
        register_verified_packet_v2_task(
            packet_artifact.hash_ref,
            control_plane=tampered_control,
            artifact_store=tampered_store,
        )
    assert tampered_control.task_store.list_latest() == []

    wrong_source_store = ArtifactStore(tmp_path / "wrong-source-artifacts")
    _, _, wrong_packet, _ = _persist_packet_v2(wrong_source_store)
    wrong_store = ArtifactStore(tmp_path / "wrong-artifacts")
    wrong_artifact = wrong_store.import_snapshot(
        io.BytesIO(wrong_packet.canonical_bytes()),
        media_type="application/json",
    )
    wrong_control = ControlPlane(tmp_path / "wrong.db")
    with pytest.raises(ArtifactMediaTypeError):
        register_verified_packet_v2_task(
            wrong_artifact.hash_ref,
            control_plane=wrong_control,
            artifact_store=wrong_store,
        )
    assert wrong_control.task_store.list_latest() == []


def test_verified_packet_v2_bridge_rejects_packet_semantic_tamper_without_task(
    tmp_path: Path,
) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    _, _, packet, _ = _persist_packet_v2(store)
    payload = copy.deepcopy(packet.to_payload())
    payload["analysis_questions"][0] = "Trade immediately."
    tampered = store.import_snapshot(
        io.BytesIO(canonical_json_bytes(payload)),
        media_type=PACKET_V2_MEDIA_TYPE,
    )
    control = ControlPlane(tmp_path / "orchestrator.db")

    with pytest.raises(ProvenanceError):
        register_verified_packet_v2_task(
            tampered.hash_ref,
            control_plane=control,
            artifact_store=store,
        )

    assert control.task_store.list_latest() == []
    assert _audit_count(control.path) == 0


def test_verified_packet_v2_bridge_rejects_upstream_report_tamper_without_task(
    tmp_path: Path,
) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    _, report_artifact, _, packet_artifact = _persist_packet_v2(store)
    Path(report_artifact.path).write_bytes(b"tampered report")
    control = ControlPlane(tmp_path / "orchestrator.db")

    with pytest.raises(ArtifactIntegrityError):
        register_verified_packet_v2_task(
            packet_artifact.hash_ref,
            control_plane=control,
            artifact_store=store,
        )

    assert control.task_store.list_latest() == []
    assert _audit_count(control.path) == 0


def test_verified_packet_v2_bridge_rejects_wrong_candidate_binding_without_task(
    tmp_path: Path,
) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    _, _, packet, _ = _persist_packet_v2(store)
    payload = copy.deepcopy(packet.to_payload())
    payload["candidate_bindings"][0]["candidate_content_hash"] = f"sha256:{'f' * 64}"
    projection = dict(payload)
    projection.pop("packet_semantic_hash")
    payload["packet_semantic_hash"] = packet_content_hash(PacketContentHashProjection(projection))
    forged = store.import_snapshot(
        io.BytesIO(canonical_json_bytes(payload)),
        media_type=PACKET_V2_MEDIA_TYPE,
    )
    control = ControlPlane(tmp_path / "orchestrator.db")

    with pytest.raises(ProvenanceError, match="candidate binding mismatch"):
        register_verified_packet_v2_task(
            forged.hash_ref,
            control_plane=control,
            artifact_store=store,
        )

    assert control.task_store.list_latest() == []
    assert _audit_count(control.path) == 0


def test_verified_packet_v2_different_packets_create_distinct_tasks(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    _, _, first_packet, first_artifact = _persist_packet_v2(store)
    _, _, second_packet, second_artifact = _persist_packet_v2(
        store,
        candidates=(_candidate(feature="DIFFERENT_FEATURE"),),
    )
    control = ControlPlane(tmp_path / "orchestrator.db")

    first = register_verified_packet_v2_task(
        first_artifact.hash_ref,
        control_plane=control,
        artifact_store=store,
    )
    second = register_verified_packet_v2_task(
        second_artifact.hash_ref,
        control_plane=control,
        artifact_store=store,
    )

    assert first_packet.packet_semantic_hash != second_packet.packet_semantic_hash
    assert first_artifact.hash_ref != second_artifact.hash_ref
    assert first.task_id != second.task_id
    assert len(control.task_store.list_latest()) == 2


def test_verified_packet_v2_existing_conflicting_task_fails_closed(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    _, _, packet, packet_artifact = _persist_packet_v2(store)
    control = ControlPlane(tmp_path / "orchestrator.db")
    conflicting = register_agent_packet(
        _packet(),
        task_id=task_id_from_packet_hash(packet.packet_semantic_hash),
        control_plane=control,
        artifact_store=store,
    )[0]

    with pytest.raises(VerifiedPacketTaskBridgeError, match="conflicts"):
        register_verified_packet_v2_task(
            packet_artifact.hash_ref,
            control_plane=control,
            artifact_store=store,
        )

    assert control.task_store.get(conflicting.task_id) == conflicting
    assert _audit_count(control.path) == 1


def test_verified_packet_v2_task_persistence_failure_is_atomic_and_retryable(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "orchestrator.db"
    store = ArtifactStore(tmp_path / "artifacts")
    _, _, _, packet_artifact = _persist_packet_v2(store)
    control = ControlPlane(db_path)

    with (
        patch(
            "trademind.orchestrator.control_plane.AuditLog.append_in_transaction",
            side_effect=RuntimeError("audit unavailable"),
        ),
        pytest.raises(RuntimeError, match="audit unavailable"),
    ):
        register_verified_packet_v2_task(
            packet_artifact.hash_ref,
            control_plane=control,
            artifact_store=store,
        )

    assert control.task_store.list_latest() == []
    assert _audit_count(db_path) == 0

    recovered = register_verified_packet_v2_task(
        packet_artifact.hash_ref,
        control_plane=control,
        artifact_store=store,
    )
    assert recovered.state is TaskState.NEW
    assert control.task_store.list_latest() == [recovered]
    assert _audit_count(db_path) == 1


def test_verified_packet_v2_concurrent_creation_has_one_logical_task(tmp_path: Path) -> None:
    db_path = tmp_path / "orchestrator.db"
    store = ArtifactStore(tmp_path / "artifacts")
    _, _, _, packet_artifact = _persist_packet_v2(store)

    def create() -> object:
        return register_verified_packet_v2_task(
            packet_artifact.hash_ref,
            control_plane=ControlPlane(db_path),
            artifact_store=ArtifactStore(tmp_path / "artifacts"),
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        tasks = list(pool.map(lambda _: create(), range(16)))

    assert all(task == tasks[0] for task in tasks)
    restarted = ControlPlane(db_path)
    assert restarted.task_store.list_latest() == [tasks[0]]
    assert _audit_count(db_path) == 1


def test_verified_packet_v2_task_id_golden_vector_preserves_full_semantic_digest() -> None:
    packet_semantic_hash = "sha256:175f309ecccb0a4e5e8654f393d50365708726f606e264795d1ee2bd78d187ec"
    expected_task_id = (
        "signal-stats-v2-175f309ecccb0a4e5e8654f393d50365708726f606e264795d1ee2bd78d187ec"
    )

    assert task_id_from_packet_hash(packet_semantic_hash) == expected_task_id
