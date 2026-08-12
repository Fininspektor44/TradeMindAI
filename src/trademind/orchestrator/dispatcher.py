"""Deterministic selection of the next locally runnable task."""

from __future__ import annotations

from dataclasses import dataclass

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

_DISCOVERY_SCOPE_MARKERS = frozenset(
    {
        "signal_statistics",
        "research_hypotheses_only",
    }
)
_DISCOVERY_WORKFLOW_REASON = (
    "task scope signal_statistics/research_hypotheses_only belongs to the separate "
    "discovery workflow"
)


@dataclass(frozen=True, slots=True)
class GenericWorkflowRoute:
    accepted: bool
    reason: str


@dataclass(frozen=True, slots=True)
class DispatchDiagnostic:
    task_id: str
    reason: str


@dataclass(frozen=True, slots=True)
class DispatchResult:
    task: Task | None
    diagnostics: tuple[DispatchDiagnostic, ...] = ()


def route_to_generic_workflow(task: Task) -> GenericWorkflowRoute:
    """Return a deterministic workflow-family decision without mutating the task."""
    if _DISCOVERY_SCOPE_MARKERS.issubset(task.scope):
        return GenericWorkflowRoute(False, _DISCOVERY_WORKFLOW_REASON)
    return GenericWorkflowRoute(True, "task belongs to the generic workflow")


class Dispatcher:
    def __init__(self, store: TaskStore) -> None:
        self.store = store

    def next_dispatch(self) -> DispatchResult:
        diagnostics: list[DispatchDiagnostic] = []
        for task in self.store.list_latest():
            if task.state not in _RUNNABLE:
                continue
            route = route_to_generic_workflow(task)
            if route.accepted:
                return DispatchResult(task, tuple(diagnostics))
            diagnostics.append(DispatchDiagnostic(task.task_id, route.reason))
        return DispatchResult(None, tuple(diagnostics))

    def next_runnable(self) -> Task | None:
        """Backward-compatible task-only view of the typed dispatch result."""
        return self.next_dispatch().task
