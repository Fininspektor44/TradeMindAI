from dataclasses import replace

from trademind.orchestrator.dispatcher import Dispatcher
from trademind.orchestrator.models import Task, TaskState
from trademind.orchestrator.task_store import TaskStore


def test_dispatcher_skips_human_required(tmp_path):
    store = TaskStore(tmp_path / "tasks.db")
    blocked = replace(
        Task.new(task_id="A", goal="human", priority=10),
        state=TaskState.HUMAN_REQUIRED,
        resume_state=TaskState.TRIAGED,
    )
    runnable = Task.new(task_id="B", goal="run", priority=1)
    store.save(blocked)
    store.save(runnable)
    assert Dispatcher(store).next_runnable().task_id == "B"
