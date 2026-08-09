from trademind.orchestrator.models import Task, TaskState
from trademind.orchestrator.state_machine import transition
from trademind.orchestrator.task_store import TaskStore


def test_task_persists_across_store_restart(tmp_path):
    path = tmp_path / "orchestrator.db"
    store = TaskStore(path)
    task = transition(Task.new(task_id="T1", goal="persist"), TaskState.TRIAGED)
    store.save(task)

    reopened = TaskStore(path)
    loaded = reopened.get("T1")
    assert loaded == task


def test_revision_does_not_overwrite_previous(tmp_path):
    store = TaskStore(tmp_path / "orchestrator.db")
    first = Task.new(task_id="T1", goal="first")
    store.save(first)
    second = store.create_revision("T1", goal="second")
    assert second.revision == 2
    assert second.parent_task_id == "T1@1"
    assert store.get("T1", 1).goal == "first"
    assert store.get("T1", 2).goal == "second"
