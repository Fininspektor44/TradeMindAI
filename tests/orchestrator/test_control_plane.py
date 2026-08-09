import sqlite3

from trademind.orchestrator.control_plane import ControlPlane
from trademind.orchestrator.models import PolicyDecision, Role, Task, TaskState


def test_transition_and_audit_commit_together(tmp_path):
    path = tmp_path / "orchestrator.db"
    control = ControlPlane(path)
    control.task_store.save(Task.new(task_id="T1", goal="atomic"))

    updated = control.advance(
        "T1",
        TaskState.TRIAGED,
        actor_role=Role.OPERATOR,
        action="DISPATCH_TASK",
        policy_result=PolicyDecision.AUTO_ALLOWED,
    )

    assert updated.state is TaskState.TRIAGED
    assert control.task_store.get("T1").state is TaskState.TRIAGED
    assert control.audit_log.verify()
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0] == 1


def test_corrupt_audit_rolls_back_state_transition(tmp_path):
    path = tmp_path / "orchestrator.db"
    control = ControlPlane(path)
    control.task_store.save(Task.new(task_id="T1", goal="rollback"))

    with sqlite3.connect(path) as db:
        db.execute("UPDATE audit_meta SET head_hash='tampered' WHERE id=1")

    try:
        control.advance("T1", TaskState.TRIAGED, action="DISPATCH_TASK")
    except RuntimeError as exc:
        assert "audit log integrity" in str(exc)
    else:
        raise AssertionError("corrupt audit chain must block state transition")

    assert control.task_store.get("T1").state is TaskState.NEW
