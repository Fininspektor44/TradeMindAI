import sqlite3

from trademind.orchestrator.control_plane import ControlPlane, UnauthorizedActor
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


def test_wrong_role_cannot_advance_someone_elses_stage(tmp_path):
    control = ControlPlane(tmp_path / "orchestrator.db")
    control.task_store.save(Task.new(task_id="T1", goal="separation"))
    control.advance("T1", TaskState.TRIAGED, actor_role=Role.OPERATOR)

    try:
        control.advance("T1", TaskState.SPECIFIED, actor_role=Role.DEVELOPER)
    except UnauthorizedActor:
        pass
    else:
        raise AssertionError("developer must not perform architect stage")

    assert control.task_store.get("T1").state is TaskState.TRIAGED


def test_ai_role_transition_requires_provider_and_model_metadata(tmp_path):
    control = ControlPlane(tmp_path / "orchestrator.db")
    control.task_store.save(Task.new(task_id="T1", goal="trace model"))
    control.advance("T1", TaskState.TRIAGED, actor_role=Role.OPERATOR)

    try:
        control.advance("T1", TaskState.SPECIFIED, actor_role=Role.ARCHITECT)
    except UnauthorizedActor:
        pass
    else:
        raise AssertionError("AI transition must identify provider and model")

    updated = control.advance(
        "T1",
        TaskState.SPECIFIED,
        actor_role=Role.ARCHITECT,
        model_provider="mock-provider",
        model_name="mock-model",
    )
    assert updated.state is TaskState.SPECIFIED


def test_operator_transition_rejects_model_metadata(tmp_path):
    control = ControlPlane(tmp_path / "orchestrator.db")
    control.task_store.save(Task.new(task_id="T1", goal="local operator"))

    try:
        control.advance(
            "T1",
            TaskState.TRIAGED,
            actor_role=Role.OPERATOR,
            model_provider="should-not-exist",
            model_name="should-not-exist",
        )
    except UnauthorizedActor:
        pass
    else:
        raise AssertionError("OPERATOR must remain deterministic local software")
