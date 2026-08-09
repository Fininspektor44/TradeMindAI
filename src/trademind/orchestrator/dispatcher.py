"""Deterministic selection of the next locally runnable task."""

from __future__ import annotations

from .models import Task, TaskState
from .task_store import TaskStore

_RUNNABLE = {
    TaskState.NEW,
    TaskState.TRIAGED,
    TaskState.SPECIFIED,
    TaskState.ARCH_REVIEWED,
    TaskState.IMPLEMENTING,
    TaskState.TESTING,
    TaskState.AUDITING,
    TaskState.READY,
}


class Dispatcher:
    def __init__(self, store: TaskStore) -> None:
        self.store = store

    def next_runnable(self) -> Task | None:
        for task in self.store.list_latest():
            if task.state in _RUNNABLE:
                return task
        return None
