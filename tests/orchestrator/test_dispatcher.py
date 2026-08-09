from trademind.orchestrator.dispatcher import Dispatcher
from trademind.orchestrator.models import Task, TaskState
from trademind.orchestrator.task_store import TaskStore


def test_dispatcher_skips_human_required(tmp_path):
    store = TaskStore(tmp_path / "tasks.db")
    blocked = Task.new(task_id="A", goal="human", priority=10)
    runnable = Task.new(task_id="B", goal="run", priority=1)
    store.save(blocked)
    store.advance("A", TaskState.HUMAN_REQUIRED)
    store.save(runnable)
    assert Dispatcher(store).next_runnable().task_id == "B"
