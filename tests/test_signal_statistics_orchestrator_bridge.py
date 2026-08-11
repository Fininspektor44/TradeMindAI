from __future__ import annotations

from pathlib import Path

import pytest

from trademind.orchestrator.artifact_store import ArtifactStore
from trademind.orchestrator.control_plane import ControlPlane
from trademind.orchestrator.models import Role, TaskState
from trademind.signal_statistics_agent_packet import AGENT_PACKET_SCHEMA_VERSION
from trademind.signal_statistics_orchestrator_bridge import register_agent_packet


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
