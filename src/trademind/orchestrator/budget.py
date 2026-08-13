"""Persistent budget and request-deduplication controls for model calls."""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .agent_protocol import (
    AgentProtocolError,
    AgentResult,
    JsonPayloadError,
    canonical_json_dumps,
    canonical_json_loads,
)
from .models import BudgetCheck, Role


class BudgetManager:
    def __init__(
        self,
        path: str | Path,
        *,
        daily_cost_ceiling: float,
        monthly_cost_ceiling: float,
        per_task_call_limit: int,
        per_role_call_limit: int,
        failure_cooldown_seconds: int = 300,
        daily_token_ceiling: int | None = None,
        monthly_token_ceiling: int | None = None,
    ) -> None:
        numeric_limits = (
            daily_cost_ceiling,
            monthly_cost_ceiling,
            per_task_call_limit,
            per_role_call_limit,
            failure_cooldown_seconds,
        )
        if min(numeric_limits) < 0:
            raise ValueError("budget limits must be non-negative")
        if daily_token_ceiling is not None and daily_token_ceiling < 0:
            raise ValueError("daily_token_ceiling must be non-negative")
        if monthly_token_ceiling is not None and monthly_token_ceiling < 0:
            raise ValueError("monthly_token_ceiling must be non-negative")

        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.daily_cost_ceiling = float(daily_cost_ceiling)
        self.monthly_cost_ceiling = float(monthly_cost_ceiling)
        self.per_task_call_limit = int(per_task_call_limit)
        self.per_role_call_limit = int(per_role_call_limit)
        self.failure_cooldown_seconds = int(failure_cooldown_seconds)
        self.daily_token_ceiling = (
            int(daily_token_ceiling) if daily_token_ceiling is not None else None
        )
        self.monthly_token_ceiling = (
            int(monthly_token_ceiling) if monthly_token_ceiling is not None else None
        )
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def _init_schema(self) -> None:
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS model_usage (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    request_hash TEXT NOT NULL,
                    cost REAL NOT NULL,
                    tokens INTEGER NOT NULL,
                    success INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS request_cache (
                    request_hash TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    result_json TEXT
                );
                """
            )
            columns = {
                row["name"]
                for row in db.execute("PRAGMA table_info(request_cache)").fetchall()
            }
            if "result_json" not in columns:
                db.execute("ALTER TABLE request_cache ADD COLUMN result_json TEXT")

    @staticmethod
    def request_hash(payload: dict) -> str:
        canonical = canonical_json_dumps(payload)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def cached_result(self, request_hash: str) -> dict | None:
        """Return a previously persisted successful result for this exact request."""
        with self._connect() as db:
            row = db.execute(
                "SELECT result_json FROM request_cache WHERE request_hash=?",
                (request_hash,),
            ).fetchone()
        if row is None or row["result_json"] is None:
            return None
        try:
            payload = canonical_json_loads(row["result_json"])
            if not isinstance(payload, dict):
                return None
            result = AgentResult.from_payload(payload)
            if result.success is not True:
                return None
        except (AgentProtocolError, JsonPayloadError):
            return None
        return payload

    def has_cached_entry(self, request_hash: str) -> bool:
        """Report cache-key presence without interpreting or mutating its value."""
        with self._connect() as db:
            return (
                db.execute(
                    "SELECT 1 FROM request_cache WHERE request_hash=?",
                    (request_hash,),
                ).fetchone()
                is not None
            )

    def check(
        self,
        *,
        task_id: str,
        role: Role,
        request_hash: str,
        now: datetime | None = None,
        estimated_cost: float = 0.0,
        estimated_tokens: int = 0,
        task_cost_ceiling: float | None = None,
        include_cache: bool = True,
    ) -> BudgetCheck:
        if estimated_cost < 0 or estimated_tokens < 0:
            raise ValueError("estimated cost and tokens must be non-negative")
        if task_cost_ceiling is not None and task_cost_ceiling < 0:
            raise ValueError("task_cost_ceiling must be non-negative")

        current = now or datetime.now(timezone.utc)
        day_start = current.replace(hour=0, minute=0, second=0, microsecond=0)
        month_start = day_start.replace(day=1)

        if include_cache:
            cached = self.cached_result(request_hash)
            if cached is not None:
                return BudgetCheck(True, "materially identical request is cached", cached=True)

        with self._connect() as db:
            last_failure = db.execute(
                """
                SELECT timestamp FROM model_usage
                WHERE task_id=? AND role=? AND success=0
                ORDER BY id DESC LIMIT 1
                """,
                (task_id, role.value),
            ).fetchone()
            if last_failure and self.failure_cooldown_seconds:
                failed_at = datetime.fromisoformat(last_failure["timestamp"])
                if current < failed_at + timedelta(seconds=self.failure_cooldown_seconds):
                    return BudgetCheck(False, "failure cooldown is active")

            task_row = db.execute(
                """
                SELECT COUNT(*) AS calls, COALESCE(SUM(cost), 0) AS cost
                FROM model_usage WHERE task_id=?
                """,
                (task_id,),
            ).fetchone()
            if int(task_row["calls"]) >= self.per_task_call_limit:
                return BudgetCheck(False, "per-task model call limit exhausted")
            if (
                task_cost_ceiling is not None
                and float(task_row["cost"]) + estimated_cost > task_cost_ceiling
            ):
                return BudgetCheck(False, "task model cost ceiling would be exceeded")

            role_calls = db.execute(
                "SELECT COUNT(*) AS n FROM model_usage WHERE role=? AND timestamp>=?",
                (role.value, day_start.isoformat()),
            ).fetchone()["n"]
            if role_calls >= self.per_role_call_limit:
                return BudgetCheck(False, "daily per-role model call limit exhausted")

            day_row = db.execute(
                """
                SELECT COALESCE(SUM(cost), 0) AS cost, COALESCE(SUM(tokens), 0) AS tokens
                FROM model_usage WHERE timestamp>=?
                """,
                (day_start.isoformat(),),
            ).fetchone()
            if float(day_row["cost"]) + estimated_cost > self.daily_cost_ceiling:
                return BudgetCheck(False, "daily model cost ceiling would be exceeded")
            if (
                self.daily_token_ceiling is not None
                and int(day_row["tokens"]) + estimated_tokens > self.daily_token_ceiling
            ):
                return BudgetCheck(False, "daily model token ceiling would be exceeded")

            month_row = db.execute(
                """
                SELECT COALESCE(SUM(cost), 0) AS cost, COALESCE(SUM(tokens), 0) AS tokens
                FROM model_usage WHERE timestamp>=?
                """,
                (month_start.isoformat(),),
            ).fetchone()
            if float(month_row["cost"]) + estimated_cost > self.monthly_cost_ceiling:
                return BudgetCheck(False, "monthly model cost ceiling would be exceeded")
            if (
                self.monthly_token_ceiling is not None
                and int(month_row["tokens"]) + estimated_tokens > self.monthly_token_ceiling
            ):
                return BudgetCheck(False, "monthly model token ceiling would be exceeded")

        return BudgetCheck(True, "budget available")

    def record(
        self,
        *,
        task_id: str,
        role: Role,
        request_hash: str,
        cost: float,
        tokens: int,
        success: bool,
        cacheable: bool = False,
        cache_payload: dict | None = None,
        replace_invalid_cache: bool = False,
        now: datetime | None = None,
    ) -> None:
        if cost < 0 or tokens < 0:
            raise ValueError("cost and tokens must be non-negative")
        if type(success) is not bool:
            raise ValueError("success must be a bool")
        if cache_payload is not None and not cacheable:
            raise ValueError("cache_payload requires cacheable=True")
        if cacheable and success and cache_payload is None:
            raise ValueError("successful cacheable result requires cache_payload")
        if replace_invalid_cache and not (success and cacheable):
            raise ValueError("replace_invalid_cache requires a successful cacheable result")
        result_json = None
        if cache_payload is not None:
            cached_result = AgentResult.from_payload(cache_payload)
            if cached_result.success is not success:
                raise ValueError(
                    "record success metadata must match cache payload success"
                )
            if cached_result.success is not True:
                raise ValueError("request_cache accepts only successful AgentResult payloads")
        if success and cacheable:
            result_json = canonical_json_dumps(cache_payload)
        timestamp = (now or datetime.now(timezone.utc)).isoformat()
        with self._connect() as db:
            db.execute(
                """
                INSERT INTO model_usage(
                    timestamp, task_id, role, request_hash, cost, tokens, success
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    timestamp,
                    task_id,
                    role.value,
                    request_hash,
                    float(cost),
                    int(tokens),
                    1 if success else 0,
                ),
            )
            if success and cacheable:
                if replace_invalid_cache:
                    db.execute(
                        """
                        INSERT INTO request_cache(request_hash, created_at, result_json)
                        VALUES (?, ?, ?)
                        ON CONFLICT(request_hash) DO UPDATE SET
                            created_at=excluded.created_at,
                            result_json=excluded.result_json
                        """,
                        (request_hash, timestamp, result_json),
                    )
                else:
                    db.execute(
                        """
                        INSERT OR IGNORE INTO request_cache(request_hash, created_at, result_json)
                        VALUES (?, ?, ?)
                        """,
                        (request_hash, timestamp, result_json),
                    )

    def total_calls(self) -> int:
        with self._connect() as db:
            return int(db.execute("SELECT COUNT(*) AS n FROM model_usage").fetchone()["n"])
