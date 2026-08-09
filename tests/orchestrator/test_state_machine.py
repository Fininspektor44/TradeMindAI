from trademind.orchestrator.models import Role, Task, TaskState
from trademind.orchestrator.state_machine import InvalidTransition, transition


def test_happy_path_and_completed_is_immutable():
    task = Task.new(task_id="T1", goal="build")
    for state in (
        TaskState.TRIAGED,
        TaskState.SPECIFIED,
        TaskState.ARCH_REVIEWED,
        TaskState.IMPLEMENTING,
        TaskState.TESTING,
        TaskState.AUDITING,
        TaskState.READY,
        TaskState.COMPLETED,
    ):
        task = transition(task, state)
    try:
        transition(task, TaskState.NEW)
    except InvalidTransition:
        pass
    else:
        raise AssertionError("COMPLETED must be immutable")


def test_roles_are_separated_across_author_review_implementation_and_audit():
    task = Task.new(task_id="T-role", goal="separate duties")
    assert task.assigned_role is Role.OPERATOR

    task = transition(task, TaskState.TRIAGED)
    assert task.assigned_role is Role.ARCHITECT

    task = transition(task, TaskState.SPECIFIED)
    assert task.assigned_role is Role.AUDITOR

    task = transition(task, TaskState.ARCH_REVIEWED)
    assert task.assigned_role is Role.DEVELOPER

    task = transition(task, TaskState.IMPLEMENTING)
    assert task.assigned_role is Role.DEVELOPER

    task = transition(task, TaskState.TESTING)
    assert task.assigned_role is Role.OPERATOR

    task = transition(task, TaskState.AUDITING)
    assert task.assigned_role is Role.AUDITOR

    task = transition(task, TaskState.READY)
    assert task.assigned_role is Role.OPERATOR

    task = transition(task, TaskState.COMPLETED)
    assert task.assigned_role is None


def test_human_required_pauses_and_resumes_same_state_only_after_approval():
    task = Task.new(task_id="T2", goal="change")
    task = transition(task, TaskState.TRIAGED)
    task = transition(task, TaskState.HUMAN_REQUIRED)

    try:
        transition(task, TaskState.TRIAGED)
    except InvalidTransition:
        pass
    else:
        raise AssertionError("approval must be required")

    try:
        transition(task, TaskState.SPECIFIED, human_approval_recorded=True)
    except InvalidTransition:
        pass
    else:
        raise AssertionError("approval must not skip the paused state")

    resumed = transition(task, TaskState.TRIAGED, human_approval_recorded=True)
    assert resumed.state is TaskState.TRIAGED
    assert resumed.resume_state is None
    assert resumed.assigned_role is Role.ARCHITECT
