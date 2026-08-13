"""Persistent budget and request-deduplication controls for model calls."""

from __future__ import annotations

import hashlib
import math
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from functools import wraps
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar, cast

from .agent_protocol import (
    AgentProtocolError,
    AgentResult,
    JsonPayloadError,
    canonical_json_dumps,
    canonical_json_loads,
)
from .models import BudgetCheck, Role

if TYPE_CHECKING:
    from collections.abc import Callable


_RESERVATION_SCHEMA_VERSION = "orchestrator-budget-reservation-v1"
_REQUEST_HASH = re.compile(r"^[0-9a-f]{64}$")
_MAX_SQLITE_INTEGER = 2**63 - 1
_MAX_TIMEDELTA_SECONDS = 86_399_999_999_999
_ReservationMethod = TypeVar("_ReservationMethod", bound="Callable[..., Any]")


def _atomic_reservation_method(method: _ReservationMethod) -> _ReservationMethod:
    """Rollback this operation's writes even if its caller catches the exception."""

    @wraps(method)
    def atomic(
        self: BudgetManager,
        db: sqlite3.Connection,
        *args: object,
        **kwargs: object,
    ) -> Any:
        self._validate_transaction_connection(db)
        db.execute("SAVEPOINT orchestrator_budget_reservation_v1")
        try:
            result = method(self, db, *args, **kwargs)
        except BaseException:
            db.execute("ROLLBACK TO SAVEPOINT orchestrator_budget_reservation_v1")
            db.execute("RELEASE SAVEPOINT orchestrator_budget_reservation_v1")
            raise
        db.execute("RELEASE SAVEPOINT orchestrator_budget_reservation_v1")
        return result

    return cast("_ReservationMethod", atomic)


class BudgetReservationError(RuntimeError):
    """Base error for persistent budget reservation failures."""


class BudgetReservationConflict(BudgetReservationError):
    """Raised when one request identity conflicts with its persisted reservation."""


class BudgetReservationStateConflict(BudgetReservationError):
    """Raised when a reservation transition is invalid or conflicting."""


class BudgetReservationCapabilityError(BudgetReservationError):
    """Raised when caller-owned transaction requirements are not satisfied."""


class BudgetReservationState(StrEnum):
    RESERVED = "RESERVED"
    SETTLED = "SETTLED"
    RELEASED = "RELEASED"


@dataclass(frozen=True, slots=True)
class BudgetReservation:
    """Immutable view of one persistent logical request reservation."""

    request_hash: str
    task_id: str
    task_revision: int
    role: Role
    reserved_cost: float
    reserved_tokens: int
    task_cost_ceiling: float | None
    state: BudgetReservationState
    created_at: str
    updated_at: str
    settled_cost: float | None = None
    settled_tokens: int | None = None
    settled_success: bool | None = None
    settled_cache_persisted: bool | None = None
    model_usage_id: int | None = None


@dataclass(frozen=True, slots=True)
class BudgetReservationResult:
    """Atomic reservation decision; cached hits never create budget reservations."""

    allowed: bool
    reason: str
    reservation: BudgetReservation | None = None
    cached: bool = False


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
        daily_cost_ceiling = self._validate_cost(daily_cost_ceiling, field="daily_cost_ceiling")
        monthly_cost_ceiling = self._validate_cost(
            monthly_cost_ceiling, field="monthly_cost_ceiling"
        )
        per_task_call_limit = self._validate_tokens(
            per_task_call_limit, field="per_task_call_limit"
        )
        per_role_call_limit = self._validate_tokens(
            per_role_call_limit, field="per_role_call_limit"
        )
        failure_cooldown_seconds = self._validate_tokens(
            failure_cooldown_seconds, field="failure_cooldown_seconds"
        )
        if failure_cooldown_seconds > _MAX_TIMEDELTA_SECONDS:
            raise ValueError(f"failure_cooldown_seconds must not exceed {_MAX_TIMEDELTA_SECONDS}")
        if daily_token_ceiling is not None:
            daily_token_ceiling = self._validate_tokens(
                daily_token_ceiling, field="daily_token_ceiling"
            )
        if monthly_token_ceiling is not None:
            monthly_token_ceiling = self._validate_tokens(
                monthly_token_ceiling, field="monthly_token_ceiling"
            )

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
            reservation_tables = {
                row["name"]
                for row in db.execute(
                    """
                    SELECT name FROM sqlite_master
                    WHERE type='table'
                      AND name IN ('budget_reservation_meta', 'budget_reservations')
                    """
                ).fetchall()
            }
            has_meta = "budget_reservation_meta" in reservation_tables
            has_reservations = "budget_reservations" in reservation_tables
            if has_meta is not has_reservations:
                raise BudgetReservationCapabilityError(
                    "incomplete budget reservation schema cannot be adopted"
                )
            if has_meta:
                try:
                    meta = db.execute(
                        "SELECT schema_version FROM budget_reservation_meta WHERE id=1"
                    ).fetchone()
                except sqlite3.Error as exc:
                    raise BudgetReservationCapabilityError(
                        "invalid budget reservation metadata schema"
                    ) from exc
                if meta is None or meta["schema_version"] != _RESERVATION_SCHEMA_VERSION:
                    raise BudgetReservationCapabilityError(
                        "unsupported budget reservation schema version"
                    )

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
                CREATE TABLE IF NOT EXISTS budget_reservation_meta (
                    id INTEGER PRIMARY KEY CHECK(id = 1),
                    schema_version TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS budget_reservations (
                    request_hash TEXT PRIMARY KEY
                        CHECK(length(request_hash) = 64)
                        CHECK(request_hash NOT GLOB '*[^0-9a-f]*'),
                    schema_version TEXT NOT NULL
                        CHECK(schema_version = 'orchestrator-budget-reservation-v1'),
                    task_id TEXT NOT NULL,
                    task_revision INTEGER NOT NULL CHECK(task_revision >= 1),
                    role TEXT NOT NULL
                        CHECK(role IN ('ARCHITECT', 'DEVELOPER', 'AUDITOR', 'OPERATOR')),
                    reserved_cost REAL NOT NULL CHECK(reserved_cost >= 0),
                    reserved_tokens INTEGER NOT NULL CHECK(reserved_tokens >= 0),
                    task_cost_ceiling REAL CHECK(task_cost_ceiling >= 0),
                    state TEXT NOT NULL CHECK(state IN ('RESERVED', 'SETTLED', 'RELEASED')),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    settled_cost REAL,
                    settled_tokens INTEGER,
                    settled_success INTEGER,
                    settled_cache_persisted INTEGER,
                    settled_cache_json TEXT,
                    model_usage_id INTEGER UNIQUE,
                    CHECK(
                        (state = 'SETTLED'
                            AND settled_cost IS NOT NULL
                            AND settled_tokens IS NOT NULL
                            AND settled_success IN (0, 1)
                            AND settled_cache_persisted IN (0, 1)
                            AND (
                                (settled_cache_persisted = 1 AND settled_cache_json IS NOT NULL)
                                OR
                                (settled_cache_persisted = 0 AND settled_cache_json IS NULL)
                            )
                            AND model_usage_id IS NOT NULL)
                        OR
                        (state IN ('RESERVED', 'RELEASED')
                            AND settled_cost IS NULL
                            AND settled_tokens IS NULL
                            AND settled_success IS NULL
                            AND settled_cache_persisted IS NULL
                            AND settled_cache_json IS NULL
                            AND model_usage_id IS NULL)
                    )
                );
                CREATE INDEX IF NOT EXISTS idx_budget_reservations_task_state
                    ON budget_reservations(task_id, state);
                CREATE INDEX IF NOT EXISTS idx_budget_reservations_role_time_state
                    ON budget_reservations(role, state);
                CREATE INDEX IF NOT EXISTS idx_budget_reservations_time_state
                    ON budget_reservations(state);
                """
            )
            db.execute(
                """
                INSERT OR IGNORE INTO budget_reservation_meta(id, schema_version)
                VALUES (1, ?)
                """,
                (_RESERVATION_SCHEMA_VERSION,),
            )
            initialized_meta = db.execute(
                "SELECT schema_version FROM budget_reservation_meta WHERE id=1"
            ).fetchone()
            if (
                initialized_meta is None
                or initialized_meta["schema_version"] != _RESERVATION_SCHEMA_VERSION
            ):
                raise BudgetReservationCapabilityError(
                    "unsupported budget reservation schema version"
                )
            columns = {
                row["name"] for row in db.execute("PRAGMA table_info(request_cache)").fetchall()
            }
            if "result_json" not in columns:
                db.execute("ALTER TABLE request_cache ADD COLUMN result_json TEXT")

    @staticmethod
    def request_hash(payload: dict) -> str:
        canonical = canonical_json_dumps(payload)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def _validate_request_hash(request_hash: object) -> str:
        if type(request_hash) is not str or _REQUEST_HASH.fullmatch(request_hash) is None:
            raise ValueError("request_hash must be exactly 64 lowercase hexadecimal characters")
        return request_hash

    @staticmethod
    def _validate_cost(value: object, *, field: str) -> float:
        if type(value) not in {int, float}:
            raise ValueError(f"{field} must be a non-negative finite number")
        try:
            converted = float(value)
        except OverflowError as exc:
            raise ValueError(f"{field} must be a non-negative finite number") from exc
        if not math.isfinite(converted) or converted < 0:
            raise ValueError(f"{field} must be a non-negative finite number")
        return converted

    @staticmethod
    def _validate_tokens(value: object, *, field: str) -> int:
        if type(value) is not int or value < 0 or value > _MAX_SQLITE_INTEGER:
            raise ValueError(f"{field} must be an exact non-negative SQLite-range integer")
        return value

    @staticmethod
    def _validate_task_cost_ceiling(value: object) -> float | None:
        if value is None:
            return None
        return BudgetManager._validate_cost(value, field="task_cost_ceiling")

    @staticmethod
    def _timestamp(value: datetime | None) -> str:
        current = value or datetime.now(timezone.utc)
        if type(current) is not datetime or current.utcoffset() is None:
            raise ValueError("reservation timestamps must be timezone-aware datetimes")
        return current.isoformat()

    def _validate_transaction_connection(self, db: sqlite3.Connection) -> None:
        if not isinstance(db, sqlite3.Connection) or not db.in_transaction:
            raise BudgetReservationCapabilityError(
                "caller-owned connection must have an active transaction"
            )
        if db.row_factory is not sqlite3.Row:
            raise BudgetReservationCapabilityError(
                "budget reservation transaction requires sqlite3.Row row_factory"
            )
        rows = db.execute("PRAGMA database_list").fetchall()
        main_path = next((row[2] for row in rows if row[1] == "main"), "")
        if not main_path or Path(main_path).resolve() != self.path.resolve():
            raise BudgetReservationCapabilityError(
                "budget reservation transaction must use the BudgetManager database"
            )

    @staticmethod
    def _acquire_reservation_write_lock(db: sqlite3.Connection) -> None:
        # Acquire/confirm the database write lock before any reservation or ceiling
        # read. This upgrades an accidentally deferred transaction and closes races.
        cursor = db.execute(
            """
            UPDATE budget_reservation_meta
            SET schema_version=schema_version
            WHERE id=1 AND schema_version=?
            """,
            (_RESERVATION_SCHEMA_VERSION,),
        )
        if cursor.rowcount != 1:
            raise BudgetReservationCapabilityError(
                "budget reservation schema version is missing or unsupported"
            )

    @staticmethod
    def _validate_identity(
        *,
        task_id: object,
        task_revision: object,
        role: object,
    ) -> tuple[str, int, Role]:
        if type(task_id) is not str or not task_id.strip():
            raise ValueError("task_id must be a non-empty exact string")
        if (
            type(task_revision) is not int
            or task_revision < 1
            or task_revision > _MAX_SQLITE_INTEGER
        ):
            raise ValueError("task_revision must be an exact positive SQLite-range integer")
        if not isinstance(role, Role):
            raise ValueError("role must be a Role")
        return task_id, task_revision, role

    @staticmethod
    def _cached_result_connection(
        db: sqlite3.Connection,
        request_hash: str,
    ) -> dict | None:
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

    def cached_result(self, request_hash: str) -> dict | None:
        """Return a previously persisted successful result for this exact request."""
        with self._connect() as db:
            return self._cached_result_connection(db, request_hash)

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

    @staticmethod
    def _row_to_reservation(row: sqlite3.Row) -> BudgetReservation:
        try:
            state = BudgetReservationState(row["state"])
            role = Role(row["role"])
        except ValueError as exc:
            raise BudgetReservationCapabilityError(
                "persisted budget reservation contains unsupported enum values"
            ) from exc
        request_hash = BudgetManager._validate_request_hash(row["request_hash"])
        task_id, task_revision, role = BudgetManager._validate_identity(
            task_id=row["task_id"],
            task_revision=row["task_revision"],
            role=role,
        )
        reserved_cost = BudgetManager._validate_cost(
            row["reserved_cost"], field="persisted reserved_cost"
        )
        reserved_tokens = BudgetManager._validate_tokens(
            row["reserved_tokens"], field="persisted reserved_tokens"
        )
        task_cost_ceiling = BudgetManager._validate_task_cost_ceiling(row["task_cost_ceiling"])
        for field in ("created_at", "updated_at"):
            try:
                parsed = datetime.fromisoformat(row[field])
            except (TypeError, ValueError) as exc:
                raise BudgetReservationCapabilityError(
                    f"persisted budget reservation has invalid {field}"
                ) from exc
            if parsed.utcoffset() is None:
                raise BudgetReservationCapabilityError(
                    f"persisted budget reservation has naive {field}"
                )
        settled_cost = (
            BudgetManager._validate_cost(row["settled_cost"], field="persisted settled_cost")
            if row["settled_cost"] is not None
            else None
        )
        settled_tokens = (
            BudgetManager._validate_tokens(row["settled_tokens"], field="persisted settled_tokens")
            if row["settled_tokens"] is not None
            else None
        )
        settled_success = row["settled_success"]
        if settled_success not in {None, 0, 1}:
            raise BudgetReservationCapabilityError(
                "persisted reservation has invalid settled_success"
            )
        settled_cache_persisted = row["settled_cache_persisted"]
        if settled_cache_persisted not in {None, 0, 1}:
            raise BudgetReservationCapabilityError(
                "persisted reservation has invalid settled_cache_persisted"
            )
        settled_cache_json = row["settled_cache_json"]
        if settled_cache_json is not None and type(settled_cache_json) is not str:
            raise BudgetReservationCapabilityError(
                "persisted reservation has invalid settled_cache_json"
            )
        if state is BudgetReservationState.SETTLED:
            if None in {
                settled_cost,
                settled_tokens,
                settled_success,
                settled_cache_persisted,
            }:
                raise BudgetReservationCapabilityError(
                    "persisted SETTLED reservation is missing reconciliation fields"
                )
            if (settled_cache_json is not None) is not bool(settled_cache_persisted):
                raise BudgetReservationCapabilityError(
                    "persisted SETTLED reservation has inconsistent cache binding"
                )
        elif any(
            value is not None
            for value in (
                settled_cost,
                settled_tokens,
                settled_success,
                settled_cache_persisted,
                settled_cache_json,
            )
        ):
            raise BudgetReservationCapabilityError(
                "persisted non-SETTLED reservation contains reconciliation fields"
            )
        model_usage_id = row["model_usage_id"]
        if model_usage_id is not None and (type(model_usage_id) is not int or model_usage_id < 1):
            raise BudgetReservationCapabilityError(
                "persisted reservation has invalid model_usage_id"
            )
        if (model_usage_id is not None) is not (state is BudgetReservationState.SETTLED):
            raise BudgetReservationCapabilityError(
                "persisted reservation has inconsistent model_usage binding"
            )
        return BudgetReservation(
            request_hash=request_hash,
            task_id=task_id,
            task_revision=task_revision,
            role=role,
            reserved_cost=reserved_cost,
            reserved_tokens=reserved_tokens,
            task_cost_ceiling=task_cost_ceiling,
            state=state,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            settled_cost=settled_cost,
            settled_tokens=settled_tokens,
            settled_success=(bool(settled_success) if settled_success is not None else None),
            settled_cache_persisted=(
                bool(settled_cache_persisted) if settled_cache_persisted is not None else None
            ),
            model_usage_id=model_usage_id,
        )

    @classmethod
    def _reservation_row(
        cls,
        db: sqlite3.Connection,
        request_hash: str,
    ) -> sqlite3.Row | None:
        row = db.execute(
            "SELECT * FROM budget_reservations WHERE request_hash=?",
            (request_hash,),
        ).fetchone()
        if row is not None and row["schema_version"] != _RESERVATION_SCHEMA_VERSION:
            raise BudgetReservationCapabilityError(
                "persisted budget reservation has an unsupported schema version"
            )
        if row is not None and row["state"] == BudgetReservationState.SETTLED.value:
            usage = db.execute(
                "SELECT * FROM model_usage WHERE id=?",
                (row["model_usage_id"],),
            ).fetchone()
            if usage is None or (
                usage["task_id"] != row["task_id"]
                or usage["role"] != row["role"]
                or usage["request_hash"] != row["request_hash"]
                or type(usage["cost"]) is not float
                or not math.isfinite(usage["cost"])
                or usage["cost"] != row["settled_cost"]
                or type(usage["tokens"]) is not int
                or usage["tokens"] != row["settled_tokens"]
                or type(usage["success"]) is not int
                or usage["success"] not in {0, 1}
                or usage["success"] != row["settled_success"]
            ):
                raise BudgetReservationCapabilityError(
                    "settled reservation does not match its committed model_usage row"
                )
            if row["settled_cache_persisted"] == 1:
                cache = db.execute(
                    "SELECT result_json FROM request_cache WHERE request_hash=?",
                    (row["request_hash"],),
                ).fetchone()
                cached_payload = cls._cached_result_connection(db, row["request_hash"])
                if (
                    cache is None
                    or cache["result_json"] != row["settled_cache_json"]
                    or cached_payload is None
                    or canonical_json_dumps(cached_payload) != row["settled_cache_json"]
                ):
                    raise BudgetReservationCapabilityError(
                        "settled reservation does not match its bound request_cache row"
                    )
        return row

    def get_reservation(self, request_hash: str) -> BudgetReservation | None:
        request_hash = self._validate_request_hash(request_hash)
        with self._connect() as db:
            row = self._reservation_row(db, request_hash)
        return self._row_to_reservation(row) if row is not None else None

    @classmethod
    def _validate_active_reservations(cls, db: sqlite3.Connection) -> None:
        for row in db.execute(
            "SELECT * FROM budget_reservations WHERE state='RESERVED'"
        ).fetchall():
            if row["schema_version"] != _RESERVATION_SCHEMA_VERSION:
                raise BudgetReservationCapabilityError(
                    "active reservation has an unsupported schema version"
                )
            cls._row_to_reservation(row)

    def _check_connection(
        self,
        db: sqlite3.Connection,
        *,
        task_id: str,
        role: Role,
        current: datetime,
        estimated_cost: float,
        estimated_tokens: int,
        task_cost_ceiling: float | None,
        include_reservations: bool,
    ) -> BudgetCheck:
        day_start = current.replace(hour=0, minute=0, second=0, microsecond=0)
        month_start = day_start.replace(day=1)
        if include_reservations:
            self._validate_active_reservations(db)

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
        task_reserved = (
            db.execute(
                """
            SELECT COUNT(*) AS calls, COALESCE(SUM(reserved_cost), 0) AS cost
            FROM budget_reservations WHERE task_id=? AND state='RESERVED'
            """,
                (task_id,),
            ).fetchone()
            if include_reservations
            else {"calls": 0, "cost": 0}
        )
        task_calls = int(task_row["calls"]) + int(task_reserved["calls"])
        task_cost = float(task_row["cost"]) + float(task_reserved["cost"])
        if task_calls >= self.per_task_call_limit:
            return BudgetCheck(False, "per-task model call limit exhausted")
        if task_cost_ceiling is not None and task_cost + estimated_cost > task_cost_ceiling:
            return BudgetCheck(False, "task model cost ceiling would be exceeded")

        role_calls = int(
            db.execute(
                "SELECT COUNT(*) AS n FROM model_usage WHERE role=? AND timestamp>=?",
                (role.value, day_start.isoformat()),
            ).fetchone()["n"]
        )
        if include_reservations:
            role_calls += int(
                db.execute(
                    """
                    SELECT COUNT(*) AS n FROM budget_reservations
                    WHERE role=? AND state='RESERVED'
                    """,
                    (role.value,),
                ).fetchone()["n"]
            )
        if role_calls >= self.per_role_call_limit:
            return BudgetCheck(False, "daily per-role model call limit exhausted")

        day_row = db.execute(
            """
            SELECT COALESCE(SUM(cost), 0) AS cost, COALESCE(SUM(tokens), 0) AS tokens
            FROM model_usage WHERE timestamp>=?
            """,
            (day_start.isoformat(),),
        ).fetchone()
        day_cost = float(day_row["cost"])
        day_tokens = int(day_row["tokens"])
        reserved_global = {"cost": 0, "tokens": 0}
        if include_reservations:
            reserved_global = db.execute(
                """
                SELECT COALESCE(SUM(reserved_cost), 0) AS cost,
                       COALESCE(SUM(reserved_tokens), 0) AS tokens
                FROM budget_reservations WHERE state='RESERVED'
                """,
            ).fetchone()
            day_cost += float(reserved_global["cost"])
            day_tokens += int(reserved_global["tokens"])
        if day_cost + estimated_cost > self.daily_cost_ceiling:
            return BudgetCheck(False, "daily model cost ceiling would be exceeded")
        if (
            self.daily_token_ceiling is not None
            and day_tokens + estimated_tokens > self.daily_token_ceiling
        ):
            return BudgetCheck(False, "daily model token ceiling would be exceeded")

        month_row = db.execute(
            """
            SELECT COALESCE(SUM(cost), 0) AS cost, COALESCE(SUM(tokens), 0) AS tokens
            FROM model_usage WHERE timestamp>=?
            """,
            (month_start.isoformat(),),
        ).fetchone()
        month_cost = float(month_row["cost"])
        month_tokens = int(month_row["tokens"])
        if include_reservations:
            month_cost += float(reserved_global["cost"])
            month_tokens += int(reserved_global["tokens"])
        if month_cost + estimated_cost > self.monthly_cost_ceiling:
            return BudgetCheck(False, "monthly model cost ceiling would be exceeded")
        if (
            self.monthly_token_ceiling is not None
            and month_tokens + estimated_tokens > self.monthly_token_ceiling
        ):
            return BudgetCheck(False, "monthly model token ceiling would be exceeded")
        return BudgetCheck(True, "budget available")

    @_atomic_reservation_method
    def reserve_in_transaction(
        self,
        db: sqlite3.Connection,
        *,
        task_id: str,
        task_revision: int,
        role: Role,
        request_hash: str,
        estimated_cost: float,
        estimated_tokens: int,
        task_cost_ceiling: float | None = None,
        now: datetime | None = None,
        include_cache: bool = True,
    ) -> BudgetReservationResult:
        """Atomically check ceilings and reserve capacity in a caller transaction.

        The caller must open this manager's database, set ``sqlite3.Row`` as the
        row factory, and hold an active transaction. The method acquires the SQLite
        write lock before reading ceilings; callers should use ``BEGIN IMMEDIATE`` to
        avoid upgrade contention. Existing rows are idempotent only when every
        immutable reservation input matches; released requests cannot be implicitly
        re-authorized under the same request identity.
        """
        if type(include_cache) is not bool:
            raise ValueError("include_cache must be a bool")
        task_id, task_revision, role = self._validate_identity(
            task_id=task_id,
            task_revision=task_revision,
            role=role,
        )
        request_hash = self._validate_request_hash(request_hash)
        estimated_cost = self._validate_cost(estimated_cost, field="estimated_cost")
        estimated_tokens = self._validate_tokens(estimated_tokens, field="estimated_tokens")
        task_cost_ceiling = self._validate_task_cost_ceiling(task_cost_ceiling)
        timestamp = self._timestamp(now)
        current = datetime.fromisoformat(timestamp)
        self._acquire_reservation_write_lock(db)

        existing_row = self._reservation_row(db, request_hash)
        if existing_row is not None:
            existing = self._row_to_reservation(existing_row)
            identity = (
                existing.task_id,
                existing.task_revision,
                existing.role,
                existing.reserved_cost,
                existing.reserved_tokens,
                existing.task_cost_ceiling,
            )
            requested = (
                task_id,
                task_revision,
                role,
                estimated_cost,
                estimated_tokens,
                task_cost_ceiling,
            )
            if identity != requested:
                raise BudgetReservationConflict(
                    "request_hash already has a conflicting budget reservation"
                )
            if existing.state is BudgetReservationState.RELEASED:
                raise BudgetReservationStateConflict(
                    "released reservation cannot be recreated for the same request identity"
                )
            cached = self._cached_result_connection(db, request_hash) if include_cache else None
            if existing.state is BudgetReservationState.SETTLED and cached is not None:
                return BudgetReservationResult(
                    True,
                    "materially identical settled request is cached",
                    reservation=existing,
                    cached=True,
                )
            return BudgetReservationResult(
                False,
                "materially identical budget reservation already exists",
                reservation=existing,
            )

        if include_cache and self._cached_result_connection(db, request_hash) is not None:
            return BudgetReservationResult(
                True,
                "materially identical request is cached",
                cached=True,
            )

        check = self._check_connection(
            db,
            task_id=task_id,
            role=role,
            current=current,
            estimated_cost=estimated_cost,
            estimated_tokens=estimated_tokens,
            task_cost_ceiling=task_cost_ceiling,
            include_reservations=True,
        )
        if not check.allowed:
            return BudgetReservationResult(False, check.reason)

        db.execute(
            """
            INSERT INTO budget_reservations(
                request_hash, schema_version, task_id, task_revision, role,
                reserved_cost, reserved_tokens, task_cost_ceiling, state,
                created_at, updated_at, settled_cost, settled_tokens,
                settled_success, settled_cache_persisted, settled_cache_json,
                model_usage_id
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, 'RESERVED', ?, ?, NULL, NULL, NULL, NULL, NULL, NULL
            )
            """,
            (
                request_hash,
                _RESERVATION_SCHEMA_VERSION,
                task_id,
                task_revision,
                role.value,
                estimated_cost,
                estimated_tokens,
                task_cost_ceiling,
                timestamp,
                timestamp,
            ),
        )
        row = self._reservation_row(db, request_hash)
        if row is None:  # Defensive: INSERT in this transaction must be visible.
            raise BudgetReservationCapabilityError("budget reservation insert was not visible")
        return BudgetReservationResult(
            True,
            "budget capacity reserved",
            reservation=self._row_to_reservation(row),
        )

    @staticmethod
    def _prepare_record_payload(
        *,
        cost: object,
        tokens: object,
        success: object,
        cacheable: bool,
        cache_payload: dict | None,
        replace_invalid_cache: bool,
    ) -> tuple[float, int, bool, str | None]:
        cost = BudgetManager._validate_cost(cost, field="cost")
        tokens = BudgetManager._validate_tokens(tokens, field="tokens")
        if type(success) is not bool:
            raise ValueError("success must be a bool")
        if type(cacheable) is not bool or type(replace_invalid_cache) is not bool:
            raise ValueError("cache flags must be bool values")
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
                raise ValueError("record success metadata must match cache payload success")
            if cached_result.success is not True:
                raise ValueError("request_cache accepts only successful AgentResult payloads")
        if success and cacheable:
            result_json = canonical_json_dumps(cache_payload)
        return cost, tokens, success, result_json

    @staticmethod
    def _insert_usage_and_cache(
        db: sqlite3.Connection,
        *,
        timestamp: str,
        task_id: str,
        role: Role,
        request_hash: str,
        cost: float,
        tokens: int,
        success: bool,
        cacheable: bool,
        result_json: str | None,
        replace_invalid_cache: bool,
    ) -> int:
        cursor = db.execute(
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
                cost,
                tokens,
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
        return int(cursor.lastrowid)

    @_atomic_reservation_method
    def record_and_settle_in_transaction(
        self,
        db: sqlite3.Connection,
        *,
        request_hash: str,
        cost: float,
        tokens: int,
        success: bool,
        cacheable: bool = False,
        cache_payload: dict | None = None,
        replace_invalid_cache: bool = False,
        now: datetime | None = None,
    ) -> BudgetReservation:
        """Commit actual usage and settle its reservation atomically.

        V1 reservations are hard maxima: actual cost/tokens above the reserved
        amounts fail closed instead of bypassing a pre-dispatch ceiling decision.
        """
        request_hash = self._validate_request_hash(request_hash)
        cost, tokens, success, result_json = self._prepare_record_payload(
            cost=cost,
            tokens=tokens,
            success=success,
            cacheable=cacheable,
            cache_payload=cache_payload,
            replace_invalid_cache=replace_invalid_cache,
        )
        timestamp = self._timestamp(now)
        self._acquire_reservation_write_lock(db)
        row = self._reservation_row(db, request_hash)
        if row is None:
            raise BudgetReservationStateConflict("budget reservation does not exist")
        reservation = self._row_to_reservation(row)
        if reservation.state is BudgetReservationState.SETTLED:
            if not (
                reservation.settled_cost == cost
                and reservation.settled_tokens == tokens
                and reservation.settled_success is success
                and reservation.settled_cache_persisted is (result_json is not None)
                and row["settled_cache_json"] == result_json
            ):
                raise BudgetReservationConflict(
                    "settled reservation conflicts with requested actual usage"
                )
            if result_json is not None:
                cache_row = db.execute(
                    "SELECT result_json FROM request_cache WHERE request_hash=?",
                    (request_hash,),
                ).fetchone()
                persisted_result_json = cache_row["result_json"] if cache_row is not None else None
                if persisted_result_json != result_json:
                    raise BudgetReservationConflict(
                        "settled reservation conflicts with requested cache persistence"
                    )
            return reservation
        if reservation.state is not BudgetReservationState.RESERVED:
            raise BudgetReservationStateConflict("only RESERVED capacity may be settled")
        if cost > reservation.reserved_cost or tokens > reservation.reserved_tokens:
            raise BudgetReservationConflict("actual usage exceeds the reserved cost/token maximum")

        usage_id = self._insert_usage_and_cache(
            db,
            timestamp=timestamp,
            task_id=reservation.task_id,
            role=reservation.role,
            request_hash=request_hash,
            cost=cost,
            tokens=tokens,
            success=success,
            cacheable=cacheable,
            result_json=result_json,
            replace_invalid_cache=replace_invalid_cache,
        )
        if result_json is not None:
            cache_row = db.execute(
                "SELECT result_json FROM request_cache WHERE request_hash=?",
                (request_hash,),
            ).fetchone()
            if cache_row is None or cache_row["result_json"] != result_json:
                raise BudgetReservationConflict(
                    "successful settlement cache was not persisted exactly; "
                    "replace_invalid_cache=True is required for an existing invalid entry"
                )
        cursor = db.execute(
            """
            UPDATE budget_reservations
            SET state='SETTLED', updated_at=?, settled_cost=?, settled_tokens=?,
                settled_success=?, settled_cache_persisted=?, settled_cache_json=?,
                model_usage_id=?
            WHERE request_hash=? AND state='RESERVED'
            """,
            (
                timestamp,
                cost,
                tokens,
                1 if success else 0,
                1 if result_json is not None else 0,
                result_json,
                usage_id,
                request_hash,
            ),
        )
        if cursor.rowcount != 1:
            raise BudgetReservationStateConflict("reservation state changed concurrently")
        settled = self._reservation_row(db, request_hash)
        if settled is None:
            raise BudgetReservationCapabilityError("settled reservation disappeared")
        return self._row_to_reservation(settled)

    @_atomic_reservation_method
    def release_in_transaction(
        self,
        db: sqlite3.Connection,
        *,
        request_hash: str,
        now: datetime | None = None,
    ) -> BudgetReservation:
        """Release capacity only when the caller proves no billable dispatch occurred."""
        request_hash = self._validate_request_hash(request_hash)
        timestamp = self._timestamp(now)
        self._acquire_reservation_write_lock(db)
        row = self._reservation_row(db, request_hash)
        if row is None:
            raise BudgetReservationStateConflict("budget reservation does not exist")
        reservation = self._row_to_reservation(row)
        if reservation.state is BudgetReservationState.RELEASED:
            return reservation
        if reservation.state is not BudgetReservationState.RESERVED:
            raise BudgetReservationStateConflict("only RESERVED capacity may be released")
        cursor = db.execute(
            """
            UPDATE budget_reservations
            SET state='RELEASED', updated_at=?
            WHERE request_hash=? AND state='RESERVED'
            """,
            (timestamp, request_hash),
        )
        if cursor.rowcount != 1:
            raise BudgetReservationStateConflict("reservation state changed concurrently")
        released = self._reservation_row(db, request_hash)
        if released is None:
            raise BudgetReservationCapabilityError("released reservation disappeared")
        return self._row_to_reservation(released)

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
        estimated_cost = self._validate_cost(estimated_cost, field="estimated_cost")
        estimated_tokens = self._validate_tokens(estimated_tokens, field="estimated_tokens")
        task_cost_ceiling = self._validate_task_cost_ceiling(task_cost_ceiling)

        current = now or datetime.now(timezone.utc)
        if include_cache:
            cached = self.cached_result(request_hash)
            if cached is not None:
                return BudgetCheck(True, "materially identical request is cached", cached=True)

        with self._connect() as db:
            return self._check_connection(
                db,
                task_id=task_id,
                role=role,
                current=current,
                estimated_cost=estimated_cost,
                estimated_tokens=estimated_tokens,
                task_cost_ceiling=task_cost_ceiling,
                include_reservations=False,
            )

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
        cost, tokens, success, result_json = self._prepare_record_payload(
            cost=cost,
            tokens=tokens,
            success=success,
            cacheable=cacheable,
            cache_payload=cache_payload,
            replace_invalid_cache=replace_invalid_cache,
        )
        timestamp = (now or datetime.now(timezone.utc)).isoformat()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if self._reservation_row(db, request_hash) is not None:
                raise BudgetReservationStateConflict(
                    "reserved requests require record_and_settle_in_transaction"
                )
            self._insert_usage_and_cache(
                db,
                timestamp=timestamp,
                task_id=task_id,
                role=role,
                request_hash=request_hash,
                cost=cost,
                tokens=tokens,
                success=success,
                cacheable=cacheable,
                result_json=result_json,
                replace_invalid_cache=replace_invalid_cache,
            )

    def total_calls(self) -> int:
        with self._connect() as db:
            return int(db.execute("SELECT COUNT(*) AS n FROM model_usage").fetchone()["n"])
