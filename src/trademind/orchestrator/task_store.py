"""SQLite-backed durable task/revision store."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from pathlib import Path

from .models import RiskClass, Role, Task, TaskState


class TaskStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def _init_schema(self) -> None:
        with self._connect() as db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    parent_task_id TEXT,
                    created_at TEXT NOT NULL,
                    goal TEXT NOT NULL,
                    scope_json TEXT NOT NULL,
                    risk_class TEXT NOT NULL,
                    state TEXT NOT NULL,
                    assigned_role TEXT,
                    allowed_tools_json TEXT NOT NULL,
                    budget_limit REAL NOT NULL,
                    acceptance_criteria_json TEXT NOT NULL,
                    artifact_refs_json TEXT NOT NULL,
                    priority INTEGER NOT NULL,
                    resume_state TEXT,
                    PRIMARY KEY (task_id, revision)
                )
                """
            )

    def save(self, task: Task) -> None:
        with self._connect() as db:
            db.execute(
                """
                INSERT OR REPLACE INTO tasks (
                    task_id, revision, parent_task_id, created_at, goal, scope_json,
                    risk_class, state, assigned_role, allowed_tools_json, budget_limit,
                    acceptance_criteria_json, artifact_refs_json, priority, resume_state
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task.task_id,
                    task.revision,
                    task.parent_task_id,
                    task.created_at,
                    task.goal,
                    json.dumps(task.scope),
                    task.risk_class.value,
                    task.state.value,
                    task.assigned_role.value if task.assigned_role else None,
                    json.dumps(task.allowed_tools),
                    task.budget_limit,
                    json.dumps(task.acceptance_criteria),
                    json.dumps(task.artifact_refs),
                    task.priority,
                    task.resume_state.value if task.resume_state else None,
                ),
            )

    def get(self, task_id: str, revision: int | None = None) -> Task | None:
        with self._connect() as db:
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
        return self._row_to_task(row) if row else None

    def list_latest(self) -> list[Task]:
        with self._connect() as db:
            rows = db.execute(
                """
                SELECT t.*
                FROM tasks t
                JOIN (
                    SELECT task_id, MAX(revision) AS revision
                    FROM tasks
                    GROUP BY task_id
                ) latest
                ON latest.task_id=t.task_id AND latest.revision=t.revision
                ORDER BY t.priority DESC, t.created_at ASC, t.task_id ASC
                """
            ).fetchall()
        return [self._row_to_task(row) for row in rows]

    def create_revision(self, task_id: str, *, goal: str | None = None) -> Task:
        previous = self.get(task_id)
        if previous is None:
            raise KeyError(task_id)
        revised = replace(
            previous,
            revision=previous.revision + 1,
            parent_task_id=f"{previous.task_id}@{previous.revision}",
            goal=goal if goal is not None else previous.goal,
            state=TaskState.NEW,
            assigned_role=None,
            artifact_refs=(),
            resume_state=None,
        )
        self.save(revised)
        return revised

    @staticmethod
    def _row_to_task(row: sqlite3.Row) -> Task:
        return Task(
            task_id=row["task_id"],
            revision=int(row["revision"]),
            parent_task_id=row["parent_task_id"],
            created_at=row["created_at"],
            goal=row["goal"],
            scope=tuple(json.loads(row["scope_json"])),
            risk_class=RiskClass(row["risk_class"]),
            state=TaskState(row["state"]),
            assigned_role=Role(row["assigned_role"]) if row["assigned_role"] else None,
            allowed_tools=tuple(json.loads(row["allowed_tools_json"])),
            budget_limit=float(row["budget_limit"]),
            acceptance_criteria=tuple(json.loads(row["acceptance_criteria_json"])),
            artifact_refs=tuple(json.loads(row["artifact_refs_json"])),
            priority=int(row["priority"]),
            resume_state=TaskState(row["resume_state"]) if row["resume_state"] else None,
        )
