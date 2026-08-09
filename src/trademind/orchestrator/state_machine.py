"""Strict task-state transitions for the orchestrator."""

from __future__ import annotations

from dataclasses import replace

from .models import Role, Task, TaskState


class InvalidTransition(RuntimeError):
    pass


_FORWARD = {
    TaskState.NEW: TaskState.TRIAGED,
    TaskState.TRIAGED: TaskState.SPECIFIED,
    TaskState.SPECIFIED: TaskState.ARCH_REVIEWED,
    TaskState.ARCH_REVIEWED: TaskState.IMPLEMENTING,
    TaskState.IMPLEMENTING: TaskState.TESTING,
    TaskState.TESTING: TaskState.AUDITING,
    TaskState.AUDITING: TaskState.READY,
    TaskState.READY: TaskState.COMPLETED,
}

_TERMINAL = {
    TaskState.COMPLETED,
    TaskState.REJECTED,
    TaskState.FAILED,
    TaskState.CANCELLED,
}

_FAILURE_FROM_ACTIVE = {
    TaskState.BLOCKED,
    TaskState.REJECTED,
    TaskState.FAILED,
    TaskState.HUMAN_REQUIRED,
    TaskState.CANCELLED,
}

_ACTIVE = set(_FORWARD)


def next_role_for_state(state: TaskState) -> Role | None:
    if state in {TaskState.SPECIFIED, TaskState.ARCH_REVIEWED}:
        return Role.ARCHITECT
    if state in {TaskState.IMPLEMENTING, TaskState.TESTING}:
        return Role.DEVELOPER
    if state in {TaskState.AUDITING, TaskState.READY}:
        return Role.AUDITOR
    return Role.OPERATOR if state in {TaskState.NEW, TaskState.TRIAGED} else None


def transition(
    task: Task,
    target: TaskState,
    *,
    human_approval_recorded: bool = False,
) -> Task:
    """Return a new immutable task after validating the requested transition."""
    current = task.state
    if current in _TERMINAL:
        raise InvalidTransition(f"{current} is terminal")

    if current is TaskState.HUMAN_REQUIRED:
        if not human_approval_recorded:
            raise InvalidTransition("HUMAN_REQUIRED needs recorded user approval")
        if task.resume_state is None:
            raise InvalidTransition("HUMAN_REQUIRED task has no resume_state")
        if target is not task.resume_state:
            raise InvalidTransition(
                f"HUMAN_REQUIRED may resume only to {task.resume_state}, not {target}"
            )
        return replace(
            task,
            state=target,
            resume_state=None,
            assigned_role=next_role_for_state(target),
        )

    if current is TaskState.BLOCKED:
        raise InvalidTransition("BLOCKED work must continue as a new revision")

    expected = _FORWARD.get(current)
    if target is expected:
        return replace(task, state=target, assigned_role=next_role_for_state(target))

    if current in _ACTIVE and target in _FAILURE_FROM_ACTIVE:
        resume_state = expected if target is TaskState.HUMAN_REQUIRED else None
        return replace(task, state=target, resume_state=resume_state, assigned_role=None)

    raise InvalidTransition(f"illegal transition {current} -> {target}")
