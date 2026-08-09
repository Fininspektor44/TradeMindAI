"""Atomic orchestration state transitions with durable audit evidence."""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .audit_log import AuditLog
from .models import AuditEvent, PolicyDecision, Role, Task, TaskState
from .state_machine import transition
from .task_store import RevisionConflict, TaskStore


class UnauthorizedActor(RuntimeError):
    pass


class ApprovalRequired(RuntimeError):
    pass


_AI_ROLES = frozenset({Role.ARCHITECT, Role.DEVELOPER, Role.AUDITOR})
_HASH_REF = re.compile(r"^sha256:[0-9a-f]{64}$")
_SYSTEM_HALT_TARGETS = frozenset(
    {
        TaskState.BLOCKED,
        TaskState.REJECTED,
        TaskState.FAILED,
        TaskState.HUMAN_REQUIRED,
        TaskState.CANCELLED,
    }
)


class ControlPlane:
    """Own persistent task mutation and audit it in the same SQLite transaction."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.task_store = TaskStore(self.path)
        self.audit_log = AuditLog(self.path)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def _init_schema(self) -> None:
        with self._connect() as db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS human_approvals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    resume_state TEXT NOT NULL,
                    approved_at TEXT NOT NULL,
                    approved_by TEXT NOT NULL,
                    note TEXT NOT NULL,
                    consumed_at TEXT,
                    UNIQUE(task_id, revision, resume_state, approved_at)
                )
                """
            )

    @staticmethod
    def _task_row(
        db: sqlite3.Connection,
        task_id: str,
        revision: int | None,
    ) -> sqlite3.Row:
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
        return row

    @staticmethod
    def _validate_artifact_hashes(values: tuple[str, ...]) -> None:
        for value in values:
            if not _HASH_REF.fullmatch(value):
                raise ValueError(f"invalid artifact hash reference: {value!r}")

    @staticmethod
    def _persist_transition(
        db: sqlite3.Connection,
        current: Task,
        updated: Task,
        output_artifact_hashes: tuple[str, ...],
    ) -> Task:
        merged_artifacts = tuple(
            dict.fromkeys((*current.artifact_refs, *output_artifact_hashes))
        )
        updated = replace(updated, artifact_refs=merged_artifacts)
        cursor = db.execute(
            """
            UPDATE tasks
            SET state=?, assigned_role=?, resume_state=?, artifact_refs_json=?
            WHERE task_id=? AND revision=? AND state=?
            """,
            (
                updated.state.value,
                updated.assigned_role.value if updated.assigned_role else None,
                updated.resume_state.value if updated.resume_state else None,
                json.dumps(merged_artifacts),
                current.task_id,
                current.revision,
                current.state.value,
            ),
        )
        if cursor.rowcount != 1:
            raise RevisionConflict("task state changed concurrently")
        return updated

    def create_task(self, task: Task) -> Task:
        """Persist a pristine task and its creation audit in one transaction."""
        self._validate_artifact_hashes(task.artifact_refs)
        timestamp = datetime.now(timezone.utc).isoformat()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            TaskStore.insert_in_transaction(db, task)
            AuditLog.append_in_transaction(
                db,
                AuditEvent(
                    timestamp=timestamp,
                    task_id=task.task_id,
                    revision=task.revision,
                    actor_role=Role.OPERATOR,
                    action="CREATE_TASK",
                    from_state=None,
                    to_state=TaskState.NEW,
                    policy_result=PolicyDecision.AUTO_ALLOWED_WITH_AUDIT,
                    output_artifact_hashes=task.artifact_refs,
                ),
            )
        return task

    @staticmethod
    def _validate_actor(
        current: Task,
        *,
        actor_role: Role,
        approval_available: bool,
        model_provider: str | None,
        model_name: str | None,
    ) -> None:
        if current.state is TaskState.HUMAN_REQUIRED:
            if not approval_available:
                raise ApprovalRequired("HUMAN_REQUIRED needs a durable unused approval record")
            if actor_role is not Role.OPERATOR:
                raise UnauthorizedActor("recorded human approval may be resumed only by OPERATOR")
        elif current.assigned_role is not actor_role:
            expected = current.assigned_role.value if current.assigned_role else "NONE"
            raise UnauthorizedActor(
                f"{actor_role.value} cannot advance {current.state.value}; assigned role is {expected}"
            )

        if actor_role in _AI_ROLES:
            if not (model_provider and model_provider.strip() and model_name and model_name.strip()):
                raise UnauthorizedActor("AI role transitions require provider and model audit metadata")
        elif model_provider is not None or model_name is not None:
            raise UnauthorizedActor("OPERATOR transitions cannot claim model provider metadata")

    def record_human_approval(
        self,
        task_id: str,
        *,
        revision: int | None = None,
        approved_by: str,
        note: str = "",
    ) -> None:
        """Record one explicit human approval for a currently paused task."""
        if not approved_by.strip():
            raise ValueError("approved_by must not be empty")
        timestamp = datetime.now(timezone.utc).isoformat()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            task = TaskStore._row_to_task(self._task_row(db, task_id, revision))
            if task.state is not TaskState.HUMAN_REQUIRED or task.resume_state is None:
                raise ApprovalRequired("approval may be recorded only for HUMAN_REQUIRED task")

            existing = db.execute(
                """
                SELECT 1 FROM human_approvals
                WHERE task_id=? AND revision=? AND resume_state=? AND consumed_at IS NULL
                LIMIT 1
                """,
                (task.task_id, task.revision, task.resume_state.value),
            ).fetchone()
            if existing is not None:
                raise ApprovalRequired("an unused approval already exists for this human gate")

            db.execute(
                """
                INSERT INTO human_approvals(
                    task_id, revision, resume_state, approved_at, approved_by, note, consumed_at
                ) VALUES (?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    task.task_id,
                    task.revision,
                    task.resume_state.value,
                    timestamp,
                    approved_by.strip(),
                    note,
                ),
            )
            AuditLog.append_in_transaction(
                db,
                AuditEvent(
                    timestamp=timestamp,
                    task_id=task.task_id,
                    revision=task.revision,
                    actor_role=None,
                    action="HUMAN_APPROVAL_RECORDED",
                    from_state=task.state,
                    to_state=task.state,
                    policy_result=PolicyDecision.HUMAN_REQUIRED,
                    metadata={"approved_by": approved_by.strip(), "note": note},
                ),
            )

    def system_halt(
        self,
        task_id: str,
        target: TaskState,
        *,
        revision: int | None = None,
        action: str = "SYSTEM_HALT",
        policy_result: PolicyDecision | None = None,
        error: str | None = None,
        metadata: dict[str, Any] | None = None,
        model_provider: str | None = None,
        model_name: str | None = None,
        output_artifact_hashes: tuple[str, ...] = (),
    ) -> Task:
        """Let deterministic OPERATOR stop active work without impersonating an AI role."""
        if target not in _SYSTEM_HALT_TARGETS:
            raise UnauthorizedActor(f"OPERATOR system halt cannot advance work to {target.value}")
        self._validate_artifact_hashes(output_artifact_hashes)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = TaskStore._row_to_task(self._task_row(db, task_id, revision))
            updated = transition(current, target)
            updated = self._persist_transition(
                db,
                current,
                updated,
                output_artifact_hashes,
            )
            AuditLog.append_in_transaction(
                db,
                AuditEvent(
                    timestamp=datetime.now(timezone.utc).isoformat(),
                    task_id=current.task_id,
                    revision=current.revision,
                    actor_role=Role.OPERATOR,
                    action=action,
                    model_provider=model_provider,
                    model_name=model_name,
                    output_artifact_hashes=output_artifact_hashes,
                    from_state=current.state,
                    to_state=updated.state,
                    policy_result=policy_result,
                    error=error,
                    metadata=dict(metadata or {}),
                ),
            )
            return updated

    def reject_and_create_revision(
        self,
        task_id: str,
        *,
        revision: int | None = None,
        actor_role: Role = Role.AUDITOR,
        action: str = "AUDITOR_REJECT",
        policy_result: PolicyDecision | None = None,
        model_provider: str | None = None,
        model_name: str | None = None,
        output_artifact_hashes: tuple[str, ...] = (),
        reason: str = "",
    ) -> Task:
        """Reject the current revision and atomically create a pristine successor revision."""
        self._validate_artifact_hashes(output_artifact_hashes)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = TaskStore._row_to_task(self._task_row(db, task_id, revision))
            self._validate_actor(
                current,
                actor_role=actor_role,
                approval_available=False,
                model_provider=model_provider,
                model_name=model_name,
            )
            rejected = transition(current, TaskState.REJECTED)
            rejected = self._persist_transition(
                db,
                current,
                rejected,
                output_artifact_hashes,
            )
            timestamp = datetime.now(timezone.utc).isoformat()
            AuditLog.append_in_transaction(
                db,
                AuditEvent(
                    timestamp=timestamp,
                    task_id=current.task_id,
                    revision=current.revision,
                    actor_role=actor_role,
                    action=action,
                    model_provider=model_provider,
                    model_name=model_name,
                    output_artifact_hashes=output_artifact_hashes,
                    from_state=current.state,
                    to_state=TaskState.REJECTED,
                    policy_result=policy_result,
                    error=reason or None,
                ),
            )

            successor = replace(
                current,
                revision=current.revision + 1,
                parent_task_id=f"{current.task_id}@{current.revision}",
                created_at=timestamp,
                state=TaskState.NEW,
                assigned_role=Role.OPERATOR,
                artifact_refs=(),
                resume_state=None,
            )
            TaskStore.insert_in_transaction(db, successor)
            AuditLog.append_in_transaction(
                db,
                AuditEvent(
                    timestamp=timestamp,
                    task_id=successor.task_id,
                    revision=successor.revision,
                    actor_role=Role.OPERATOR,
                    action="CREATE_REVISION_AFTER_REJECTION",
                    input_artifact_hashes=output_artifact_hashes,
                    from_state=None,
                    to_state=TaskState.NEW,
                    policy_result=PolicyDecision.AUTO_ALLOWED_WITH_AUDIT,
                    metadata={
                        "parent_task_id": successor.parent_task_id,
                        "rejection_reason": reason,
                    },
                ),
            )
            return successor

    def advance(
        self,
        task_id: str,
        target: TaskState,
        *,
        revision: int | None = None,
        actor_role: Role = Role.OPERATOR,
        action: str = "STATE_TRANSITION",
        policy_result: PolicyDecision | None = None,
        model_provider: str | None = None,
        model_name: str | None = None,
        output_artifact_hashes: tuple[str, ...] = (),
    ) -> Task:
        """Advance one task and append its audit event atomically."""
        self._validate_artifact_hashes(output_artifact_hashes)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = TaskStore._row_to_task(self._task_row(db, task_id, revision))
            approval_row = None
            if current.state is TaskState.HUMAN_REQUIRED and current.resume_state is not None:
                approval_row = db.execute(
                    """
                    SELECT id FROM human_approvals
                    WHERE task_id=? AND revision=? AND resume_state=? AND consumed_at IS NULL
                    ORDER BY id ASC LIMIT 1
                    """,
                    (current.task_id, current.revision, current.resume_state.value),
                ).fetchone()

            approval_available = approval_row is not None
            self._validate_actor(
                current,
                actor_role=actor_role,
                approval_available=approval_available,
                model_provider=model_provider,
                model_name=model_name,
            )
            updated = transition(
                current,
                target,
                human_approval_recorded=approval_available,
            )
            updated = self._persist_transition(
                db,
                current,
                updated,
                output_artifact_hashes,
            )

            timestamp = datetime.now(timezone.utc).isoformat()
            if approval_row is not None:
                db.execute(
                    "UPDATE human_approvals SET consumed_at=? WHERE id=? AND consumed_at IS NULL",
                    (timestamp, int(approval_row["id"])),
                )

            AuditLog.append_in_transaction(
                db,
                AuditEvent(
                    timestamp=timestamp,
                    task_id=current.task_id,
                    revision=current.revision,
                    actor_role=actor_role,
                    action=action,
                    model_provider=model_provider,
                    model_name=model_name,
                    output_artifact_hashes=output_artifact_hashes,
                    from_state=current.state,
                    to_state=updated.state,
                    policy_result=policy_result,
                ),
            )
            return updated
