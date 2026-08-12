"""Restart-safe local service loop for Orchestrator v1."""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import StrEnum
from threading import Event

from .dispatcher import DispatchDiagnostic, Dispatcher
from .engine import WorkflowEngine
from .models import TaskState
from .notification import Notification, NotificationKind, NotificationSink, NullNotificationSink


class ServiceStatus(StrEnum):
    IDLE = "IDLE"
    ADVANCED = "ADVANCED"
    STOPPED = "STOPPED"


@dataclass(frozen=True, slots=True)
class ServiceTick:
    status: ServiceStatus
    task_id: str | None
    state: TaskState | None
    routing_diagnostics: tuple[DispatchDiagnostic, ...] = ()


class OrchestratorService:
    """Poll durable tasks and advance at most one workflow stage per tick."""

    def __init__(
        self,
        *,
        dispatcher: Dispatcher,
        engine: WorkflowEngine,
        notifications: NotificationSink | None = None,
        idle_sleep_seconds: float = 5.0,
    ) -> None:
        if idle_sleep_seconds < 0:
            raise ValueError("idle_sleep_seconds must be non-negative")
        self.dispatcher = dispatcher
        self.engine = engine
        self.notifications = notifications or NullNotificationSink()
        self.idle_sleep_seconds = float(idle_sleep_seconds)

    def run_once(self) -> ServiceTick:
        dispatch = self.dispatcher.next_dispatch()
        task = dispatch.task
        if task is None:
            return ServiceTick(
                ServiceStatus.IDLE,
                None,
                None,
                routing_diagnostics=dispatch.diagnostics,
            )

        updated = self.engine.step(task.task_id)
        if updated.state is TaskState.HUMAN_REQUIRED:
            self.notifications.send(
                Notification(
                    kind=NotificationKind.HUMAN_REQUIRED,
                    task_id=updated.task_id,
                    revision=updated.revision,
                    message=f"Task {updated.task_id} requires human approval",
                )
            )
            return ServiceTick(
                ServiceStatus.STOPPED,
                updated.task_id,
                updated.state,
                routing_diagnostics=dispatch.diagnostics,
            )

        if updated.state in {
            TaskState.FAILED,
            TaskState.REJECTED,
            TaskState.BLOCKED,
            TaskState.CANCELLED,
        }:
            self.notifications.send(
                Notification(
                    kind=NotificationKind.TASK_FAILED,
                    task_id=updated.task_id,
                    revision=updated.revision,
                    message=f"Task {updated.task_id} stopped in {updated.state.value}",
                    metadata={"state": updated.state.value},
                )
            )
            return ServiceTick(
                ServiceStatus.STOPPED,
                updated.task_id,
                updated.state,
                routing_diagnostics=dispatch.diagnostics,
            )

        status = (
            ServiceStatus.STOPPED
            if updated.state is TaskState.COMPLETED
            else ServiceStatus.ADVANCED
        )
        return ServiceTick(
            status,
            updated.task_id,
            updated.state,
            routing_diagnostics=dispatch.diagnostics,
        )

    def run_forever(self, stop_event: Event) -> None:
        """Run until externally stopped; persisted task state makes restart recovery deterministic."""
        while not stop_event.is_set():
            tick = self.run_once()
            if tick.status is ServiceStatus.IDLE:
                stop_event.wait(self.idle_sleep_seconds)
            else:
                time.sleep(0)
