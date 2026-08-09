import json
import sqlite3

from trademind.orchestrator.control_plane import ControlPlane, UnauthorizedActor
from trademind.orchestrator.models import PolicyDecision, Role, Task, TaskState


def test_operator_can_halt_ai_owned_stage_without_impersonating_ai(tmp_path):
    path = tmp_path / "orchestrator.db"
    control = ControlPlane(path)
    control.create_task(Task.new(task_id="T1", goal="budget halt"))
    control.advance("T1", TaskState.TRIAGED, actor_role=Role.OPERATOR)

    halted = control.system_halt(
        "T1",
        TaskState.HUMAN_REQUIRED,
        action="MODEL_BUDGET_GATE",
        policy_result=PolicyDecision.HUMAN_REQUIRED,
        error="task budget exhausted",
        metadata={"requested_role": Role.ARCHITECT.value},
    )

    assert halted.state is TaskState.HUMAN_REQUIRED
    assert halted.resume_state is TaskState.TRIAGED
    assert halted.assigned_role is None
    assert control.audit_log.verify()

    with sqlite3.connect(path) as db:
        payload = json.loads(
            db.execute("SELECT payload FROM audit_events ORDER BY id DESC LIMIT 1").fetchone()[0]
        )
    assert payload["actor_role"] == Role.OPERATOR.value
    assert payload["action"] == "MODEL_BUDGET_GATE"
    assert payload["error"] == "task budget exhausted"


def test_system_halt_cannot_be_used_to_advance_work(tmp_path):
    control = ControlPlane(tmp_path / "orchestrator.db")
    control.create_task(Task.new(task_id="T1", goal="no bypass"))

    try:
        control.system_halt("T1", TaskState.TRIAGED)
    except UnauthorizedActor:
        pass
    else:
        raise AssertionError("system halt must never become a forward-progress bypass")

    assert control.task_store.get("T1").state is TaskState.NEW
