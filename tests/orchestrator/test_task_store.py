from dataclasses import replace

from trademind.orchestrator.models import Task, TaskState
from trademind.orchestrator.task_store import RevisionConflict, TaskStore


def test_task_persists_across_store_restart(tmp_path):
    path = tmp_path / "orchestrator.db"
    store = TaskStore(path)
    task = Task.new(task_id="T1", goal="persist")
    store.save(task)
    task = store.advance("T1", TaskState.TRIAGED)

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


def test_direct_state_insertion_and_revision_overwrite_are_rejected(tmp_path):
    store = TaskStore(tmp_path / "orchestrator.db")
    task = Task.new(task_id="T1", goal="guard")
    store.save(task)

    try:
        store.save(replace(task, state=TaskState.COMPLETED))
    except RevisionConflict:
        pass
    else:
        raise AssertionError("direct state insertion must be rejected")

    try:
        store.save(task)
    except RevisionConflict:
        pass
    else:
        raise AssertionError("existing revision must not be overwritten")
