from trademind.orchestrator.models import Task, TaskState
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


def test_human_required_needs_recorded_approval():
    task = Task.new(task_id="T2", goal="change")
    task = transition(task, TaskState.TRIAGED)
    task = transition(task, TaskState.HUMAN_REQUIRED)
    try:
        transition(task, TaskState.SPECIFIED)
    except InvalidTransition:
        pass
    else:
        raise AssertionError("approval must be required")
    resumed = transition(task, TaskState.SPECIFIED, human_approval_recorded=True)
    assert resumed.state is TaskState.SPECIFIED
