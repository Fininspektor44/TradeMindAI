"""Register a read-only signal-statistics agent packet as orchestrator evidence."""

from __future__ import annotations

import argparse
import json
from dataclasses import replace
from pathlib import Path
from typing import Mapping, Sequence

from trademind.orchestrator.artifact_store import ArtifactRef, ArtifactStore
from trademind.orchestrator.control_plane import ControlPlane
from trademind.orchestrator.models import RiskClass, Task
from trademind.signal_statistics_agent_packet import AGENT_PACKET_SCHEMA_VERSION


def register_agent_packet(
    packet: Mapping[str, object],
    *,
    task_id: str,
    control_plane: ControlPlane,
    artifact_store: ArtifactStore,
) -> tuple[Task, ArtifactRef]:
    """Persist packet immutably and create a research-only orchestrator task."""
    if packet.get("schema_version") != AGENT_PACKET_SCHEMA_VERSION:
        raise ValueError("unsupported signal statistics agent packet schema")
    if packet.get("read_only") is not True:
        raise ValueError("agent packet must be read-only")
    if packet.get("orders_enabled") is not False:
        raise ValueError("agent packet must keep orders disabled")
    if packet.get("decision_scope") != "research_hypotheses_only":
        raise ValueError("agent packet must be research-hypotheses-only")

    candidate_count = int(packet.get("candidate_count", 0))
    if candidate_count < 1:
        raise ValueError("agent packet must contain at least one candidate")

    artifact = artifact_store.store_json(
        task_id=task_id,
        revision=1,
        kind="signal_statistics_agent_packet",
        payload=dict(packet),
    )

    task = Task.new(
        task_id=task_id,
        goal=(
            "Analyze selected signal-statistics candidates and propose falsifiable "
            "research hypotheses only."
        ),
        scope=("signal_statistics", "research_hypotheses_only"),
        risk_class=RiskClass.LOW,
        allowed_tools=(),
        budget_limit=0.0,
        acceptance_criteria=(
            "Do not change signal weights or signal-generation logic.",
            "Do not enable orders.",
            "Do not publish or sell signals.",
            "Produce research hypotheses with explicit falsification criteria.",
        ),
    )
    task = replace(task, artifact_refs=(artifact.hash_ref,))
    control_plane.create_task(task)
    return task, artifact


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Register a read-only signal-statistics AI packet in the orchestrator"
    )
    parser.add_argument("--packet", type=Path, required=True)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--task-id", required=True)
    args = parser.parse_args(argv)

    if not args.packet.is_file():
        parser.error(f"packet not found: {args.packet}")

    packet = json.loads(args.packet.read_text(encoding="utf-8"))
    task, artifact = register_agent_packet(
        packet,
        task_id=args.task_id,
        control_plane=ControlPlane(args.db),
        artifact_store=ArtifactStore(args.artifact_root),
    )
    print(
        json.dumps(
            {
                "task_id": task.task_id,
                "revision": task.revision,
                "state": task.state.value,
                "risk_class": task.risk_class.value,
                "artifact_ref": artifact.hash_ref,
                "artifact_path": artifact.path,
                "read_only": True,
                "orders_enabled": False,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
