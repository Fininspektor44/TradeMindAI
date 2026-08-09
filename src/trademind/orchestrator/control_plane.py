"""Atomic orchestration state transitions with durable audit evidence."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .audit_log import AuditLog
from .models import AuditEvent, PolicyDecision, Role, Task, TaskState
from .state_machine import transition
from .task_store import RevisionConflict, TaskStore


class ControlPlane:
    """Own persistent task mutation and audit it in the same SQLite transaction."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.task_store = TaskStore(self.path)
        self.audit_log = AuditLog(self.path)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def advance(
        self,
        task_id: str,
        target: TaskState,
        *,
        revision: int | None = None,
        actor_role: Role = Role.OPERATOR,
        action: str = "STATE_TRANSITION",
        policy_result: PolicyDecision | None = None,
        human_approval_recorded: bool = False,
        model_provider: str | None = None,
        model_name: str | None = None,
    ) -> Task:
        """Advance one task and append its audit event atomically."""
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if revision is None:
                row = db.execute(
                    "SELECT * FROM tasks WHERE task_id=? ORDER BY revision DESC LIMIT 1",
                    (task_id,),
                ).fetchone()
            else:
                row = db.execute(
                    "SELECT * FROM tasks WHERE task_id=? AND revision=?",
                    (task_id, revision),
                ).fetchone()
            if row is None:
                raise KeyError(task_id)

            current = TaskStore._row_to_task(row)
            updated = transition(
                current,
                target,
                human_approval_recorded=human_approval_recorded,
            )
            cursor = db.execute(
                """
                UPDATE tasks
                SET state=?, assigned_role=?, resume_state=?
                WHERE task_id=? AND revision=? AND state=?
                """,
                (
                    updated.state.value,
                    updated.assigned_role.value if updated.assigned_role else None,
                    updated.resume_state.value if updated.resume_state else None,
                    current.task_id,
                    current.revision,
                    current.state.value,
                ),
            )
            if cursor.rowcount != 1:
                raise RevisionConflict("task state changed concurrently")

            AuditLog.append_in_transaction(
                db,
                AuditEvent(
                    timestamp=datetime.now(timezone.utc).isoformat(),
                    task_id=current.task_id,
                    revision=current.revision,
                    actor_role=actor_role,
                    action=action,
                    model_provider=model_provider,
                    model_name=model_name,
                    from_state=current.state,
                    to_state=updated.state,
                    policy_result=policy_result,
                ),
            )
            return updated
