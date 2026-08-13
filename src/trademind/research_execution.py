"""Persistent provider-neutral control for authorized research executions.

This module is a trusted side-car control plane.  It never dispatches a model
request and never mutates the generic orchestrator ``Task`` state machine.  A
future provider adapter must durably mark an execution ``CALL_IN_FLIGHT`` before
performing its one automatic network-call attempt.
"""

from __future__ import annotations

import io
import math
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum

from trademind.orchestrator.agent_protocol import AgentEnvelope, V2_SCHEMA_VERSION
from trademind.orchestrator.artifact_store import ArtifactStore
from trademind.orchestrator.audit_log import AuditLog
from trademind.orchestrator.budget import (
    BudgetManager,
    BudgetReservation,
    BudgetReservationState,
)
from trademind.orchestrator.control_plane import ControlPlane
from trademind.orchestrator.models import AuditEvent, PolicyDecision, Role, Task, TaskState
from trademind.orchestrator.policy import FORBIDDEN_ACTIONS
from trademind.orchestrator.task_store import TaskStore
from trademind.research_proposal_response import (
    RESEARCH_PROPOSAL_RESPONSE_SCHEMA_VERSION,
    ResearchProposalResponseError,
    ResearchProposalResponseV1,
    parse_research_proposal_response_v1,
    validate_research_proposals_for_packet,
)
from trademind.signal_statistics_agent_packet import (
    PACKET_V2_SCHEMA_VERSION,
    SignalStatisticsPacketV2,
    load_packet_v2,
)
from trademind.signal_statistics_orchestrator_bridge import (
    RESEARCH_PROPOSAL_OUTPUT_KIND,
    VERIFIED_PACKET_BRIDGE_SCHEMA_VERSION,
    _same_verified_packet_task_contract,
    _verified_packet_task_contract,
)


RESEARCH_EXECUTION_SCHEMA_VERSION = "research-execution-control-v1"
RESEARCH_PROPOSAL_RESPONSE_MEDIA_TYPE = (
    "application/vnd.trademind.research-proposal-response-v1+json"
)

_EXPECTED_SCOPE = (
    "signal_statistics",
    "research_hypotheses_only",
    "verified_packet_v2",
    VERIFIED_PACKET_BRIDGE_SCHEMA_VERSION,
    f"agent_protocol:{V2_SCHEMA_VERSION}",
    f"input_schema:{PACKET_V2_SCHEMA_VERSION}",
    f"output_kind:{RESEARCH_PROPOSAL_OUTPUT_KIND}",
)
_REQUEST_KIND = "verified-packet-research-proposal-request-v1"
_REQUEST_HASH = re.compile(r"^[0-9a-f]{64}$")
_HASH_REF = re.compile(r"^sha256:[0-9a-f]{64}$")
_MAX_SQLITE_INTEGER = 2**63 - 1
_MAX_OPERATOR_ID_LENGTH = 256


class ResearchExecutionAuthorizationError(RuntimeError):
    """The trusted authorization is absent, malformed, stale, or insufficient."""


class ResearchExecutionConflictError(RuntimeError):
    """A persisted logical execution conflicts with the requested operation."""


class ResearchExecutionStateError(RuntimeError):
    """The requested side-car lifecycle transition is not legal."""


class ResearchExecutionDatabaseError(RuntimeError):
    """The execution-control schema or database topology is not trustworthy."""


class ResearchExecutionResultError(RuntimeError):
    """The proposed result or its verified artifact binding is invalid."""


class ResearchAuthorizationState(StrEnum):
    ACTIVE = "ACTIVE"
    CONSUMED = "CONSUMED"


class ResearchExecutionStatus(StrEnum):
    CLAIMED = "CLAIMED"
    CALL_IN_FLIGHT = "CALL_IN_FLIGHT"
    CANCELLED_BEFORE_DISPATCH = "CANCELLED_BEFORE_DISPATCH"
    UNKNOWN_REQUIRES_OPERATOR = "UNKNOWN_REQUIRES_OPERATOR"
    SUCCEEDED = "SUCCEEDED"


@dataclass(frozen=True, slots=True)
class ResearchExecutionAuthorizationV1:
    authorization_id: int
    task_id: str
    task_revision: int
    packet_artifact_hash_ref: str
    packet_semantic_hash: str
    request_hash: str
    reserved_cost: float
    reserved_tokens: int
    state: ResearchAuthorizationState
    authorized_at: str
    authorized_by: str
    consumed_at: str | None = None


@dataclass(frozen=True, slots=True)
class ResearchExecutionRecordV1:
    request_hash: str
    authorization_id: int
    task_id: str
    task_revision: int
    packet_artifact_hash_ref: str
    packet_semantic_hash: str
    reserved_cost: float
    reserved_tokens: int
    status: ResearchExecutionStatus
    created_at: str
    updated_at: str
    result_artifact_hash_ref: str | None = None
    result_media_type: str | None = None
    settled_cost: float | None = None
    settled_tokens: int | None = None


class ResearchExecutionControl:
    """Trusted operator API for one-dispatch research execution sidecars."""

    def __init__(
        self,
        *,
        control_plane: ControlPlane,
        budget_manager: BudgetManager,
        artifact_store: ArtifactStore,
    ) -> None:
        if not isinstance(control_plane, ControlPlane):
            raise TypeError("control_plane must be ControlPlane")
        if not isinstance(budget_manager, BudgetManager):
            raise TypeError("budget_manager must be BudgetManager")
        if not isinstance(artifact_store, ArtifactStore):
            raise TypeError("artifact_store must be ArtifactStore")
        control_path = control_plane.path.resolve()
        budget_path = budget_manager.path.resolve()
        task_store_path = control_plane.task_store.path.resolve()
        audit_log_path = control_plane.audit_log.path.resolve()
        if len({control_path, budget_path, task_store_path, audit_log_path}) != 1:
            raise ResearchExecutionDatabaseError(
                "ControlPlane, AuditLog, TaskStore, and BudgetManager must share one database"
            )
        self.path = control_path
        self.control_plane = control_plane
        self.budget_manager = budget_manager
        self.artifact_store = artifact_store
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30.0)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        return db

    @staticmethod
    def _table_names(db: sqlite3.Connection) -> set[str]:
        return {
            row["name"]
            for row in db.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type='table' AND name IN (
                    'research_execution_meta',
                    'research_execution_authorizations',
                    'research_executions'
                )
                """
            ).fetchall()
        }

    @staticmethod
    def _validate_schema(db: sqlite3.Connection) -> None:
        expected_tables = {
            "research_execution_meta",
            "research_execution_authorizations",
            "research_executions",
        }
        if ResearchExecutionControl._table_names(db) != expected_tables:
            raise ResearchExecutionDatabaseError(
                "incomplete research execution control schema cannot be adopted"
            )
        try:
            rows = db.execute("SELECT id, schema_version FROM research_execution_meta").fetchall()
        except sqlite3.Error as exc:
            raise ResearchExecutionDatabaseError(
                "invalid research execution control metadata schema"
            ) from exc
        if (
            len(rows) != 1
            or rows[0]["id"] != 1
            or rows[0]["schema_version"] != RESEARCH_EXECUTION_SCHEMA_VERSION
        ):
            raise ResearchExecutionDatabaseError(
                "unsupported research execution control schema version"
            )

        expected_columns = {
            "research_execution_meta": {"id", "schema_version"},
            "research_execution_authorizations": {
                "authorization_id",
                "schema_version",
                "task_id",
                "task_revision",
                "packet_artifact_hash_ref",
                "packet_semantic_hash",
                "request_hash",
                "reserved_cost",
                "reserved_tokens",
                "state",
                "authorized_at",
                "authorized_by",
                "consumed_at",
            },
            "research_executions": {
                "request_hash",
                "schema_version",
                "authorization_id",
                "task_id",
                "task_revision",
                "packet_artifact_hash_ref",
                "packet_semantic_hash",
                "reserved_cost",
                "reserved_tokens",
                "status",
                "created_at",
                "updated_at",
                "result_artifact_hash_ref",
                "result_media_type",
                "settled_cost",
                "settled_tokens",
            },
        }
        for table, expected in expected_columns.items():
            try:
                actual = {
                    row["name"] for row in db.execute(f"PRAGMA table_info({table})").fetchall()
                }
            except sqlite3.Error as exc:
                raise ResearchExecutionDatabaseError(
                    f"invalid research execution table: {table}"
                ) from exc
            if actual != expected:
                raise ResearchExecutionDatabaseError(
                    f"unsupported research execution table shape: {table}"
                )

    def _init_schema(self) -> None:
        with self._connect() as db:
            existing = self._table_names(db)
            if existing and len(existing) != 3:
                raise ResearchExecutionDatabaseError(
                    "incomplete research execution control schema cannot be adopted"
                )
            if existing:
                self._validate_schema(db)
                return
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS research_execution_meta (
                    id INTEGER PRIMARY KEY CHECK(id = 1),
                    schema_version TEXT NOT NULL
                        CHECK(schema_version = 'research-execution-control-v1')
                );

                CREATE TABLE IF NOT EXISTS research_execution_authorizations (
                    authorization_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    schema_version TEXT NOT NULL
                        CHECK(schema_version = 'research-execution-control-v1'),
                    task_id TEXT NOT NULL,
                    task_revision INTEGER NOT NULL CHECK(task_revision >= 1),
                    packet_artifact_hash_ref TEXT NOT NULL
                        CHECK(length(packet_artifact_hash_ref) = 71),
                    packet_semantic_hash TEXT NOT NULL
                        CHECK(length(packet_semantic_hash) = 71),
                    request_hash TEXT NOT NULL UNIQUE
                        CHECK(length(request_hash) = 64)
                        CHECK(request_hash NOT GLOB '*[^0-9a-f]*'),
                    reserved_cost REAL NOT NULL CHECK(reserved_cost >= 0),
                    reserved_tokens INTEGER NOT NULL CHECK(reserved_tokens >= 0),
                    state TEXT NOT NULL CHECK(state IN ('ACTIVE', 'CONSUMED')),
                    authorized_at TEXT NOT NULL,
                    authorized_by TEXT NOT NULL,
                    consumed_at TEXT,
                    UNIQUE(authorization_id, request_hash),
                    CHECK(
                        (state = 'ACTIVE' AND consumed_at IS NULL)
                        OR (state = 'CONSUMED' AND consumed_at IS NOT NULL)
                    )
                );

                CREATE TABLE IF NOT EXISTS research_executions (
                    request_hash TEXT PRIMARY KEY
                        CHECK(length(request_hash) = 64)
                        CHECK(request_hash NOT GLOB '*[^0-9a-f]*'),
                    schema_version TEXT NOT NULL
                        CHECK(schema_version = 'research-execution-control-v1'),
                    authorization_id INTEGER NOT NULL UNIQUE,
                    task_id TEXT NOT NULL,
                    task_revision INTEGER NOT NULL CHECK(task_revision >= 1),
                    packet_artifact_hash_ref TEXT NOT NULL
                        CHECK(length(packet_artifact_hash_ref) = 71),
                    packet_semantic_hash TEXT NOT NULL
                        CHECK(length(packet_semantic_hash) = 71),
                    reserved_cost REAL NOT NULL CHECK(reserved_cost >= 0),
                    reserved_tokens INTEGER NOT NULL CHECK(reserved_tokens >= 0),
                    status TEXT NOT NULL CHECK(status IN (
                        'CLAIMED', 'CALL_IN_FLIGHT', 'CANCELLED_BEFORE_DISPATCH',
                        'UNKNOWN_REQUIRES_OPERATOR', 'SUCCEEDED'
                    )),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    result_artifact_hash_ref TEXT,
                    result_media_type TEXT,
                    settled_cost REAL,
                    settled_tokens INTEGER,
                    FOREIGN KEY(authorization_id, request_hash)
                        REFERENCES research_execution_authorizations(
                            authorization_id, request_hash
                        ),
                    FOREIGN KEY(request_hash)
                        REFERENCES budget_reservations(request_hash),
                    CHECK(
                        (status = 'SUCCEEDED'
                            AND result_artifact_hash_ref IS NOT NULL
                            AND result_media_type IS NOT NULL
                            AND settled_cost IS NOT NULL
                            AND settled_tokens IS NOT NULL)
                        OR
                        (status != 'SUCCEEDED'
                            AND result_artifact_hash_ref IS NULL
                            AND result_media_type IS NULL
                            AND settled_cost IS NULL
                            AND settled_tokens IS NULL)
                    )
                );
                CREATE INDEX IF NOT EXISTS idx_research_execution_task
                    ON research_executions(task_id, task_revision);
                """
            )
            db.execute(
                """
                INSERT INTO research_execution_meta(id, schema_version)
                VALUES (1, ?)
                """,
                (RESEARCH_EXECUTION_SCHEMA_VERSION,),
            )
            self._validate_schema(db)

    @staticmethod
    def _timestamp() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _validate_operator_id(value: object) -> str:
        if (
            type(value) is not str
            or not value.strip()
            or value != value.strip()
            or len(value) > _MAX_OPERATOR_ID_LENGTH
        ):
            raise ResearchExecutionAuthorizationError(
                "authorized_by must be a non-empty bounded exact operator identity"
            )
        return value

    @staticmethod
    def _validate_id(value: object, *, field: str) -> int:
        if type(value) is not int or not 1 <= value <= _MAX_SQLITE_INTEGER:
            raise ValueError(f"{field} must be an exact positive SQLite-range integer")
        return value

    @staticmethod
    def _validate_request_hash(value: object) -> str:
        if type(value) is not str or _REQUEST_HASH.fullmatch(value) is None:
            raise ValueError("request_hash must be 64 lowercase hexadecimal characters")
        return value

    @staticmethod
    def _validate_hash_ref(value: object, *, field: str) -> str:
        if type(value) is not str or _HASH_REF.fullmatch(value) is None:
            raise ResearchExecutionDatabaseError(f"persisted {field} is not a SHA-256 ref")
        return value

    @staticmethod
    def _parse_timestamp(value: object, *, field: str) -> str:
        if type(value) is not str:
            raise ResearchExecutionDatabaseError(f"persisted {field} is invalid")
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise ResearchExecutionDatabaseError(f"persisted {field} is invalid") from exc
        if parsed.utcoffset() is None:
            raise ResearchExecutionDatabaseError(f"persisted {field} is not timezone-aware")
        return value

    @staticmethod
    def _parse_cost(value: object, *, field: str) -> float:
        if type(value) not in {int, float}:
            raise ResearchExecutionDatabaseError(f"persisted {field} is invalid")
        converted = float(value)
        if not math.isfinite(converted) or converted < 0:
            raise ResearchExecutionDatabaseError(f"persisted {field} is invalid")
        return converted

    @staticmethod
    def _parse_tokens(value: object, *, field: str) -> int:
        if type(value) is not int or not 0 <= value <= _MAX_SQLITE_INTEGER:
            raise ResearchExecutionDatabaseError(f"persisted {field} is invalid")
        return value

    @staticmethod
    def _task_row(
        db: sqlite3.Connection,
        *,
        task_id: str,
        task_revision: int,
    ) -> Task:
        row = db.execute(
            "SELECT * FROM tasks WHERE task_id=? AND revision=?",
            (task_id, task_revision),
        ).fetchone()
        if row is None:
            raise ResearchExecutionAuthorizationError("authorized research task does not exist")
        return TaskStore._row_to_task(row)

    def _authoritative_task_and_packet(
        self,
        db: sqlite3.Connection,
        *,
        task_id: str,
        task_revision: int,
    ) -> tuple[Task, SignalStatisticsPacketV2, str]:
        task = self._task_row(db, task_id=task_id, task_revision=task_revision)
        if task.scope != _EXPECTED_SCOPE:
            raise ResearchExecutionAuthorizationError(
                "task is not the exact verified Packet v2 research-only bridge task"
            )
        if len(task.artifact_refs) != 1:
            raise ResearchExecutionAuthorizationError(
                "verified Packet v2 research task must bind exactly one artifact"
            )
        packet_ref = task.artifact_refs[0]
        try:
            packet = load_packet_v2(packet_ref, artifact_store=self.artifact_store)
        except Exception as exc:
            raise ResearchExecutionAuthorizationError(
                "authoritative Packet v2 cannot be verified"
            ) from exc
        expected = _verified_packet_task_contract(
            packet_semantic_hash=packet.packet_semantic_hash,
            packet_artifact_hash_ref=packet_ref,
        )
        if not _same_verified_packet_task_contract(task, expected):
            raise ResearchExecutionAuthorizationError(
                "task conflicts with the verified Packet v2 bridge contract"
            )
        return task, packet, packet_ref

    @staticmethod
    def _request_envelope(task: Task, packet: SignalStatisticsPacketV2) -> AgentEnvelope:
        return AgentEnvelope(
            task_id=task.task_id,
            revision=task.revision,
            role=Role.OPERATOR,
            goal=task.goal,
            scope=task.scope,
            forbidden_actions=tuple(sorted(FORBIDDEN_ACTIONS)),
            acceptance_criteria=task.acceptance_criteria,
            artifact_refs=task.artifact_refs,
            required_output_schema=RESEARCH_PROPOSAL_RESPONSE_SCHEMA_VERSION,
            schema_version=V2_SCHEMA_VERSION,
            structured_input=packet.to_payload(),
            input_schema=PACKET_V2_SCHEMA_VERSION,
        )

    def _request_hash(self, task: Task, packet: SignalStatisticsPacketV2) -> str:
        envelope = self._request_envelope(task, packet)
        return self.budget_manager.request_hash(
            {
                "request_kind": _REQUEST_KIND,
                "envelope": envelope.to_payload(),
            }
        )

    @staticmethod
    def _authorization_from_row(row: sqlite3.Row) -> ResearchExecutionAuthorizationV1:
        if row["schema_version"] != RESEARCH_EXECUTION_SCHEMA_VERSION:
            raise ResearchExecutionDatabaseError("unsupported persisted authorization version")
        try:
            state = ResearchAuthorizationState(row["state"])
        except ValueError as exc:
            raise ResearchExecutionDatabaseError(
                "persisted authorization has unsupported state"
            ) from exc
        authorization_id = ResearchExecutionControl._validate_id(
            row["authorization_id"], field="persisted authorization_id"
        )
        task_revision = ResearchExecutionControl._validate_id(
            row["task_revision"], field="persisted task_revision"
        )
        task_id = row["task_id"]
        if type(task_id) is not str or not task_id:
            raise ResearchExecutionDatabaseError("persisted authorization task_id is invalid")
        authorized_by = row["authorized_by"]
        if type(authorized_by) is not str or not authorized_by:
            raise ResearchExecutionDatabaseError("persisted authorized_by is invalid")
        consumed_at = row["consumed_at"]
        if consumed_at is not None:
            consumed_at = ResearchExecutionControl._parse_timestamp(
                consumed_at, field="consumed_at"
            )
        if (state is ResearchAuthorizationState.ACTIVE) is not (consumed_at is None):
            raise ResearchExecutionDatabaseError("persisted authorization state is inconsistent")
        return ResearchExecutionAuthorizationV1(
            authorization_id=authorization_id,
            task_id=task_id,
            task_revision=task_revision,
            packet_artifact_hash_ref=ResearchExecutionControl._validate_hash_ref(
                row["packet_artifact_hash_ref"], field="packet_artifact_hash_ref"
            ),
            packet_semantic_hash=ResearchExecutionControl._validate_hash_ref(
                row["packet_semantic_hash"], field="packet_semantic_hash"
            ),
            request_hash=ResearchExecutionControl._validate_request_hash(row["request_hash"]),
            reserved_cost=ResearchExecutionControl._parse_cost(
                row["reserved_cost"], field="reserved_cost"
            ),
            reserved_tokens=ResearchExecutionControl._parse_tokens(
                row["reserved_tokens"], field="reserved_tokens"
            ),
            state=state,
            authorized_at=ResearchExecutionControl._parse_timestamp(
                row["authorized_at"], field="authorized_at"
            ),
            authorized_by=authorized_by,
            consumed_at=consumed_at,
        )

    @staticmethod
    def _execution_from_row(row: sqlite3.Row) -> ResearchExecutionRecordV1:
        if row["schema_version"] != RESEARCH_EXECUTION_SCHEMA_VERSION:
            raise ResearchExecutionDatabaseError("unsupported persisted execution version")
        try:
            status = ResearchExecutionStatus(row["status"])
        except ValueError as exc:
            raise ResearchExecutionDatabaseError(
                "persisted execution has unsupported status"
            ) from exc
        result_ref = row["result_artifact_hash_ref"]
        result_media_type = row["result_media_type"]
        settled_cost = row["settled_cost"]
        settled_tokens = row["settled_tokens"]
        if status is ResearchExecutionStatus.SUCCEEDED:
            result_ref = ResearchExecutionControl._validate_hash_ref(
                result_ref, field="result_artifact_hash_ref"
            )
            if result_media_type != RESEARCH_PROPOSAL_RESPONSE_MEDIA_TYPE:
                raise ResearchExecutionDatabaseError("persisted result media type is unsupported")
            settled_cost = ResearchExecutionControl._parse_cost(settled_cost, field="settled_cost")
            settled_tokens = ResearchExecutionControl._parse_tokens(
                settled_tokens, field="settled_tokens"
            )
        elif any(
            value is not None
            for value in (result_ref, result_media_type, settled_cost, settled_tokens)
        ):
            raise ResearchExecutionDatabaseError(
                "non-success execution contains result settlement fields"
            )
        task_id = row["task_id"]
        if type(task_id) is not str or not task_id:
            raise ResearchExecutionDatabaseError("persisted execution task_id is invalid")
        return ResearchExecutionRecordV1(
            request_hash=ResearchExecutionControl._validate_request_hash(row["request_hash"]),
            authorization_id=ResearchExecutionControl._validate_id(
                row["authorization_id"], field="persisted authorization_id"
            ),
            task_id=task_id,
            task_revision=ResearchExecutionControl._validate_id(
                row["task_revision"], field="persisted task_revision"
            ),
            packet_artifact_hash_ref=ResearchExecutionControl._validate_hash_ref(
                row["packet_artifact_hash_ref"], field="packet_artifact_hash_ref"
            ),
            packet_semantic_hash=ResearchExecutionControl._validate_hash_ref(
                row["packet_semantic_hash"], field="packet_semantic_hash"
            ),
            reserved_cost=ResearchExecutionControl._parse_cost(
                row["reserved_cost"], field="reserved_cost"
            ),
            reserved_tokens=ResearchExecutionControl._parse_tokens(
                row["reserved_tokens"], field="reserved_tokens"
            ),
            status=status,
            created_at=ResearchExecutionControl._parse_timestamp(
                row["created_at"], field="created_at"
            ),
            updated_at=ResearchExecutionControl._parse_timestamp(
                row["updated_at"], field="updated_at"
            ),
            result_artifact_hash_ref=result_ref,
            result_media_type=result_media_type,
            settled_cost=settled_cost,
            settled_tokens=settled_tokens,
        )

    @staticmethod
    def _authorization_row(db: sqlite3.Connection, authorization_id: int) -> sqlite3.Row | None:
        return db.execute(
            "SELECT * FROM research_execution_authorizations WHERE authorization_id=?",
            (authorization_id,),
        ).fetchone()

    @staticmethod
    def _execution_row(db: sqlite3.Connection, request_hash: str) -> sqlite3.Row | None:
        return db.execute(
            "SELECT * FROM research_executions WHERE request_hash=?", (request_hash,)
        ).fetchone()

    def create_authorization(
        self,
        *,
        task_id: str,
        task_revision: int,
        reserved_cost: float,
        reserved_tokens: int,
        authorized_by: str,
    ) -> ResearchExecutionAuthorizationV1:
        """Create one trusted, immutable authorization without reserving budget."""
        if type(task_id) is not str or not task_id:
            raise ValueError("task_id must be a non-empty exact string")
        task_revision = self._validate_id(task_revision, field="task_revision")
        authorized_by = self._validate_operator_id(authorized_by)
        reserved_cost = self.budget_manager._validate_cost(reserved_cost, field="reserved_cost")
        reserved_tokens = self.budget_manager._validate_tokens(
            reserved_tokens, field="reserved_tokens"
        )
        timestamp = self._timestamp()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._validate_schema(db)
            task, packet, packet_ref = self._authoritative_task_and_packet(
                db, task_id=task_id, task_revision=task_revision
            )
            request_hash = self._request_hash(task, packet)
            existing = db.execute(
                "SELECT * FROM research_execution_authorizations WHERE request_hash=?",
                (request_hash,),
            ).fetchone()
            if existing is not None:
                authorization = self._authorization_from_row(existing)
                if (
                    authorization.task_id == task.task_id
                    and authorization.task_revision == task.revision
                    and authorization.packet_artifact_hash_ref == packet_ref
                    and authorization.packet_semantic_hash == packet.packet_semantic_hash
                    and authorization.reserved_cost == reserved_cost
                    and authorization.reserved_tokens == reserved_tokens
                    and authorization.authorized_by == authorized_by
                ):
                    return authorization
                raise ResearchExecutionConflictError(
                    "existing research authorization conflicts with requested limits or bindings"
                )
            cursor = db.execute(
                """
                INSERT INTO research_execution_authorizations(
                    schema_version, task_id, task_revision,
                    packet_artifact_hash_ref, packet_semantic_hash, request_hash,
                    reserved_cost, reserved_tokens, state, authorized_at,
                    authorized_by, consumed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE', ?, ?, NULL)
                """,
                (
                    RESEARCH_EXECUTION_SCHEMA_VERSION,
                    task.task_id,
                    task.revision,
                    packet_ref,
                    packet.packet_semantic_hash,
                    request_hash,
                    reserved_cost,
                    reserved_tokens,
                    timestamp,
                    authorized_by,
                ),
            )
            authorization_id = int(cursor.lastrowid)
            AuditLog.append_in_transaction(
                db,
                AuditEvent(
                    timestamp=timestamp,
                    task_id=task.task_id,
                    revision=task.revision,
                    actor_role=Role.OPERATOR,
                    action="RESEARCH_EXECUTION_AUTHORIZATION_CREATED",
                    input_artifact_hashes=(packet_ref,),
                    from_state=TaskState.NEW,
                    to_state=TaskState.NEW,
                    policy_result=PolicyDecision.AUTO_ALLOWED_WITH_AUDIT,
                    metadata={
                        "authorization_id": authorization_id,
                        "request_hash": request_hash,
                        "packet_semantic_hash": packet.packet_semantic_hash,
                        "reserved_cost": reserved_cost,
                        "reserved_tokens": reserved_tokens,
                        "authorized_by": authorized_by,
                    },
                ),
            )
            row = self._authorization_row(db, authorization_id)
            if row is None:
                raise ResearchExecutionDatabaseError("authorization insert was not visible")
            return self._authorization_from_row(row)

    def get_authorization(self, authorization_id: int) -> ResearchExecutionAuthorizationV1 | None:
        authorization_id = self._validate_id(authorization_id, field="authorization_id")
        with self._connect() as db:
            self._validate_schema(db)
            row = self._authorization_row(db, authorization_id)
            return self._authorization_from_row(row) if row is not None else None

    def _validate_authorization_bindings(
        self,
        db: sqlite3.Connection,
        authorization: ResearchExecutionAuthorizationV1,
    ) -> tuple[Task, SignalStatisticsPacketV2]:
        task, packet, packet_ref = self._authoritative_task_and_packet(
            db,
            task_id=authorization.task_id,
            task_revision=authorization.task_revision,
        )
        request_hash = self._request_hash(task, packet)
        if (
            packet_ref != authorization.packet_artifact_hash_ref
            or packet.packet_semantic_hash != authorization.packet_semantic_hash
            or request_hash != authorization.request_hash
        ):
            raise ResearchExecutionAuthorizationError(
                "authorization no longer matches authoritative Task and Packet"
            )
        return task, packet

    def _reservation_in_transaction(
        self, db: sqlite3.Connection, request_hash: str
    ) -> BudgetReservation:
        self.budget_manager._validate_transaction_connection(db)
        row = self.budget_manager._reservation_row(db, request_hash)
        if row is None:
            raise ResearchExecutionDatabaseError("bound budget reservation is missing")
        return self.budget_manager._row_to_reservation(row)

    @staticmethod
    def _assert_reservation_binding(
        execution: ResearchExecutionRecordV1,
        reservation: BudgetReservation,
        *,
        expected_state: BudgetReservationState,
    ) -> None:
        if (
            reservation.request_hash != execution.request_hash
            or reservation.task_id != execution.task_id
            or reservation.task_revision != execution.task_revision
            or reservation.role is not Role.OPERATOR
            or reservation.reserved_cost != execution.reserved_cost
            or reservation.reserved_tokens != execution.reserved_tokens
            or reservation.task_cost_ceiling != execution.reserved_cost
            or reservation.state is not expected_state
        ):
            raise ResearchExecutionDatabaseError(
                "execution does not match its Budget Reservation v1 binding"
            )
        if expected_state is BudgetReservationState.SETTLED:
            if (
                reservation.settled_success is not True
                or reservation.settled_cache_persisted is not False
                or reservation.model_usage_id is None
            ):
                raise ResearchExecutionDatabaseError(
                    "successful execution requires one non-cache successful settlement"
                )
            if execution.status is ResearchExecutionStatus.SUCCEEDED and (
                reservation.settled_cost != execution.settled_cost
                or reservation.settled_tokens != execution.settled_tokens
            ):
                raise ResearchExecutionDatabaseError(
                    "execution actual usage does not match its settled reservation"
                )

    def claim_execution(self, authorization_id: int) -> ResearchExecutionRecordV1:
        """Atomically reserve budget, create one claim, consume auth, and audit it."""
        authorization_id = self._validate_id(authorization_id, field="authorization_id")
        timestamp = self._timestamp()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._validate_schema(db)
            row = self._authorization_row(db, authorization_id)
            if row is None:
                raise ResearchExecutionAuthorizationError("authorization does not exist")
            authorization = self._authorization_from_row(row)
            if authorization.state is not ResearchAuthorizationState.ACTIVE:
                raise ResearchExecutionConflictError("authorization is already consumed")
            task, _packet = self._validate_authorization_bindings(db, authorization)
            reservation_result = self.budget_manager.reserve_in_transaction(
                db,
                task_id=task.task_id,
                task_revision=task.revision,
                role=Role.OPERATOR,
                request_hash=authorization.request_hash,
                estimated_cost=authorization.reserved_cost,
                estimated_tokens=authorization.reserved_tokens,
                task_cost_ceiling=authorization.reserved_cost,
                include_cache=False,
            )
            if not reservation_result.allowed or reservation_result.reservation is None:
                raise ResearchExecutionAuthorizationError(
                    f"budget reservation denied: {reservation_result.reason}"
                )
            try:
                db.execute(
                    """
                    INSERT INTO research_executions(
                        request_hash, schema_version, authorization_id, task_id,
                        task_revision, packet_artifact_hash_ref, packet_semantic_hash,
                        reserved_cost, reserved_tokens, status, created_at, updated_at,
                        result_artifact_hash_ref, result_media_type,
                        settled_cost, settled_tokens
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'CLAIMED', ?, ?, NULL, NULL, NULL, NULL)
                    """,
                    (
                        authorization.request_hash,
                        RESEARCH_EXECUTION_SCHEMA_VERSION,
                        authorization.authorization_id,
                        authorization.task_id,
                        authorization.task_revision,
                        authorization.packet_artifact_hash_ref,
                        authorization.packet_semantic_hash,
                        authorization.reserved_cost,
                        authorization.reserved_tokens,
                        timestamp,
                        timestamp,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ResearchExecutionConflictError(
                    "logical research execution is already claimed"
                ) from exc
            cursor = db.execute(
                """
                UPDATE research_execution_authorizations
                SET state='CONSUMED', consumed_at=?
                WHERE authorization_id=? AND state='ACTIVE'
                """,
                (timestamp, authorization.authorization_id),
            )
            if cursor.rowcount != 1:
                raise ResearchExecutionConflictError("authorization was consumed concurrently")
            AuditLog.append_in_transaction(
                db,
                AuditEvent(
                    timestamp=timestamp,
                    task_id=task.task_id,
                    revision=task.revision,
                    actor_role=Role.OPERATOR,
                    action="RESEARCH_EXECUTION_CLAIMED",
                    input_artifact_hashes=(authorization.packet_artifact_hash_ref,),
                    from_state=TaskState.NEW,
                    to_state=TaskState.NEW,
                    policy_result=PolicyDecision.AUTO_ALLOWED_WITH_AUDIT,
                    metadata={
                        "authorization_id": authorization.authorization_id,
                        "request_hash": authorization.request_hash,
                        "packet_semantic_hash": authorization.packet_semantic_hash,
                        "reserved_cost": authorization.reserved_cost,
                        "reserved_tokens": authorization.reserved_tokens,
                        "execution_status": ResearchExecutionStatus.CLAIMED.value,
                    },
                ),
            )
            execution_row = self._execution_row(db, authorization.request_hash)
            if execution_row is None:
                raise ResearchExecutionDatabaseError("execution insert was not visible")
            execution = self._execution_from_row(execution_row)
            self._assert_reservation_binding(
                execution,
                reservation_result.reservation,
                expected_state=BudgetReservationState.RESERVED,
            )
            return execution

    def _load_execution_for_update(
        self, db: sqlite3.Connection, request_hash: str
    ) -> ResearchExecutionRecordV1:
        row = self._execution_row(db, request_hash)
        if row is None:
            raise ResearchExecutionStateError("research execution does not exist")
        execution = self._execution_from_row(row)
        authorization_row = self._authorization_row(db, execution.authorization_id)
        if authorization_row is None:
            raise ResearchExecutionDatabaseError("execution authorization is missing")
        authorization = self._authorization_from_row(authorization_row)
        if (
            authorization.state is not ResearchAuthorizationState.CONSUMED
            or authorization.request_hash != execution.request_hash
            or authorization.task_id != execution.task_id
            or authorization.task_revision != execution.task_revision
            or authorization.packet_artifact_hash_ref != execution.packet_artifact_hash_ref
            or authorization.packet_semantic_hash != execution.packet_semantic_hash
            or authorization.reserved_cost != execution.reserved_cost
            or authorization.reserved_tokens != execution.reserved_tokens
        ):
            raise ResearchExecutionDatabaseError(
                "execution and consumed authorization bindings conflict"
            )
        self._validate_authorization_bindings(db, authorization)
        return execution

    def _transition(
        self,
        request_hash: str,
        *,
        source: ResearchExecutionStatus,
        target: ResearchExecutionStatus,
        action: str,
        release_budget: bool = False,
    ) -> ResearchExecutionRecordV1:
        request_hash = self._validate_request_hash(request_hash)
        timestamp = self._timestamp()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._validate_schema(db)
            execution = self._load_execution_for_update(db, request_hash)
            if execution.status is not source:
                raise ResearchExecutionStateError(
                    f"{target.value} requires {source.value}; found {execution.status.value}"
                )
            reservation = self._reservation_in_transaction(db, request_hash)
            self._assert_reservation_binding(
                execution,
                reservation,
                expected_state=BudgetReservationState.RESERVED,
            )
            if release_budget:
                released = self.budget_manager.release_in_transaction(db, request_hash=request_hash)
                self._assert_reservation_binding(
                    execution,
                    released,
                    expected_state=BudgetReservationState.RELEASED,
                )
            cursor = db.execute(
                """
                UPDATE research_executions
                SET status=?, updated_at=?
                WHERE request_hash=? AND status=?
                """,
                (target.value, timestamp, request_hash, source.value),
            )
            if cursor.rowcount != 1:
                raise ResearchExecutionConflictError("execution state changed concurrently")
            AuditLog.append_in_transaction(
                db,
                AuditEvent(
                    timestamp=timestamp,
                    task_id=execution.task_id,
                    revision=execution.task_revision,
                    actor_role=Role.OPERATOR,
                    action=action,
                    input_artifact_hashes=(execution.packet_artifact_hash_ref,),
                    from_state=TaskState.NEW,
                    to_state=TaskState.NEW,
                    policy_result=PolicyDecision.AUTO_ALLOWED_WITH_AUDIT,
                    metadata={
                        "authorization_id": execution.authorization_id,
                        "request_hash": execution.request_hash,
                        "sidecar_from": source.value,
                        "sidecar_to": target.value,
                        "budget_reservation_state": (
                            BudgetReservationState.RELEASED.value
                            if release_budget
                            else BudgetReservationState.RESERVED.value
                        ),
                    },
                ),
            )
            updated = self._execution_row(db, request_hash)
            if updated is None:
                raise ResearchExecutionDatabaseError("execution disappeared during transition")
            return self._execution_from_row(updated)

    def mark_call_in_flight(self, request_hash: str) -> ResearchExecutionRecordV1:
        """Commit CLAIMED -> CALL_IN_FLIGHT; no provider call occurs here."""
        return self._transition(
            request_hash,
            source=ResearchExecutionStatus.CLAIMED,
            target=ResearchExecutionStatus.CALL_IN_FLIGHT,
            action="RESEARCH_EXECUTION_CALL_IN_FLIGHT",
        )

    def cancel_before_dispatch(self, request_hash: str) -> ResearchExecutionRecordV1:
        """Release budget only while the provider call is proven not dispatched."""
        return self._transition(
            request_hash,
            source=ResearchExecutionStatus.CLAIMED,
            target=ResearchExecutionStatus.CANCELLED_BEFORE_DISPATCH,
            action="RESEARCH_EXECUTION_CANCELLED_BEFORE_DISPATCH",
            release_budget=True,
        )

    def mark_unknown_requires_operator(self, request_hash: str) -> ResearchExecutionRecordV1:
        """Persist ambiguity without releasing budget or authorizing a retry."""
        return self._transition(
            request_hash,
            source=ResearchExecutionStatus.CALL_IN_FLIGHT,
            target=ResearchExecutionStatus.UNKNOWN_REQUIRES_OPERATOR,
            action="RESEARCH_EXECUTION_UNKNOWN_REQUIRES_OPERATOR",
        )

    @staticmethod
    def _canonical_response(
        response: ResearchProposalResponseV1 | bytes,
    ) -> tuple[ResearchProposalResponseV1, bytes]:
        if type(response) is ResearchProposalResponseV1:
            return response, response.canonical_bytes()
        if type(response) is bytes:
            try:
                parsed = parse_research_proposal_response_v1(response)
            except ResearchProposalResponseError as exc:
                raise ResearchExecutionResultError(
                    "research proposal response failed strict parsing"
                ) from exc
            return parsed, parsed.canonical_bytes()
        raise ResearchExecutionResultError(
            "response must be ResearchProposalResponseV1 or exact canonical bytes"
        )

    def finalize_success(
        self,
        request_hash: str,
        *,
        response: ResearchProposalResponseV1 | bytes,
        actual_cost: float,
        actual_tokens: int,
    ) -> ResearchExecutionRecordV1:
        """Validate, persist, settle, and bind one unreviewed proposal response."""
        request_hash = self._validate_request_hash(request_hash)
        parsed, canonical = self._canonical_response(response)
        actual_cost = self.budget_manager._validate_cost(actual_cost, field="actual_cost")
        actual_tokens = self.budget_manager._validate_tokens(actual_tokens, field="actual_tokens")

        with self._connect() as db:
            self._validate_schema(db)
            execution = self._load_execution_for_update(db, request_hash)
            if execution.status not in {
                ResearchExecutionStatus.CALL_IN_FLIGHT,
                ResearchExecutionStatus.SUCCEEDED,
            }:
                raise ResearchExecutionStateError("successful finalization requires CALL_IN_FLIGHT")
            _task, packet, _packet_ref = self._authoritative_task_and_packet(
                db,
                task_id=execution.task_id,
                task_revision=execution.task_revision,
            )
        try:
            validate_research_proposals_for_packet(parsed, packet)
        except ResearchProposalResponseError as exc:
            raise ResearchExecutionResultError(
                "research proposal response is not bound to the authoritative Packet v2"
            ) from exc

        try:
            artifact = self.artifact_store.import_snapshot(
                io.BytesIO(canonical),
                media_type=RESEARCH_PROPOSAL_RESPONSE_MEDIA_TYPE,
            )
            if (
                self.artifact_store.read_verified(
                    artifact.hash_ref,
                    expected_media_type=RESEARCH_PROPOSAL_RESPONSE_MEDIA_TYPE,
                )
                != canonical
            ):
                raise ResearchExecutionResultError(
                    "Verified CAS did not return exact response bytes"
                )
        except ResearchExecutionResultError:
            raise
        except Exception as exc:
            raise ResearchExecutionResultError(
                "research proposal response could not be persisted in Verified CAS"
            ) from exc

        timestamp = self._timestamp()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._validate_schema(db)
            execution = self._load_execution_for_update(db, request_hash)
            _task, authoritative_packet, _packet_ref = self._authoritative_task_and_packet(
                db,
                task_id=execution.task_id,
                task_revision=execution.task_revision,
            )
            try:
                validate_research_proposals_for_packet(parsed, authoritative_packet)
            except ResearchProposalResponseError as exc:
                raise ResearchExecutionResultError(
                    "response candidate binding changed before finalization"
                ) from exc

            if execution.status is ResearchExecutionStatus.SUCCEEDED:
                if (
                    execution.result_artifact_hash_ref == artifact.hash_ref
                    and execution.result_media_type == RESEARCH_PROPOSAL_RESPONSE_MEDIA_TYPE
                    and execution.settled_cost == actual_cost
                    and execution.settled_tokens == actual_tokens
                ):
                    reservation = self._reservation_in_transaction(db, request_hash)
                    self._assert_reservation_binding(
                        execution,
                        reservation,
                        expected_state=BudgetReservationState.SETTLED,
                    )
                    self._load_and_verify_result(execution, authoritative_packet)
                    return execution
                raise ResearchExecutionConflictError(
                    "successful execution conflicts with repeated result or usage"
                )
            if execution.status is not ResearchExecutionStatus.CALL_IN_FLIGHT:
                raise ResearchExecutionStateError("successful finalization requires CALL_IN_FLIGHT")
            reservation = self._reservation_in_transaction(db, request_hash)
            self._assert_reservation_binding(
                execution,
                reservation,
                expected_state=BudgetReservationState.RESERVED,
            )
            settled = self.budget_manager.record_and_settle_in_transaction(
                db,
                request_hash=request_hash,
                cost=actual_cost,
                tokens=actual_tokens,
                success=True,
                cacheable=False,
            )
            self._assert_reservation_binding(
                execution,
                settled,
                expected_state=BudgetReservationState.SETTLED,
            )
            if settled.settled_cost != actual_cost or settled.settled_tokens != actual_tokens:
                raise ResearchExecutionDatabaseError(
                    "budget settlement did not preserve exact actual usage"
                )
            cursor = db.execute(
                """
                UPDATE research_executions
                SET status='SUCCEEDED', updated_at=?, result_artifact_hash_ref=?,
                    result_media_type=?, settled_cost=?, settled_tokens=?
                WHERE request_hash=? AND status='CALL_IN_FLIGHT'
                """,
                (
                    timestamp,
                    artifact.hash_ref,
                    RESEARCH_PROPOSAL_RESPONSE_MEDIA_TYPE,
                    actual_cost,
                    actual_tokens,
                    request_hash,
                ),
            )
            if cursor.rowcount != 1:
                raise ResearchExecutionConflictError("execution finalization raced")
            AuditLog.append_in_transaction(
                db,
                AuditEvent(
                    timestamp=timestamp,
                    task_id=execution.task_id,
                    revision=execution.task_revision,
                    actor_role=Role.OPERATOR,
                    action="RESEARCH_EXECUTION_SUCCEEDED",
                    input_artifact_hashes=(execution.packet_artifact_hash_ref,),
                    output_artifact_hashes=(artifact.hash_ref,),
                    from_state=TaskState.NEW,
                    to_state=TaskState.NEW,
                    policy_result=PolicyDecision.AUTO_ALLOWED_WITH_AUDIT,
                    metadata={
                        "authorization_id": execution.authorization_id,
                        "request_hash": execution.request_hash,
                        "result_artifact_hash_ref": artifact.hash_ref,
                        "actual_cost": actual_cost,
                        "actual_tokens": actual_tokens,
                        "result_status": "UNREVIEWED_EXTERNAL_MODEL_PROPOSAL_RESPONSE",
                    },
                ),
            )
            row = self._execution_row(db, request_hash)
            if row is None:
                raise ResearchExecutionDatabaseError("finalized execution disappeared")
            finalized = self._execution_from_row(row)
            self._load_and_verify_result(finalized, authoritative_packet)
            return finalized

    def _load_and_verify_result(
        self,
        execution: ResearchExecutionRecordV1,
        packet: SignalStatisticsPacketV2,
    ) -> ResearchProposalResponseV1:
        if (
            execution.status is not ResearchExecutionStatus.SUCCEEDED
            or execution.result_artifact_hash_ref is None
            or execution.result_media_type != RESEARCH_PROPOSAL_RESPONSE_MEDIA_TYPE
        ):
            raise ResearchExecutionResultError("execution has no successful result binding")
        try:
            exact_bytes = self.artifact_store.read_verified(
                execution.result_artifact_hash_ref,
                expected_media_type=RESEARCH_PROPOSAL_RESPONSE_MEDIA_TYPE,
            )
            response = parse_research_proposal_response_v1(exact_bytes)
            return validate_research_proposals_for_packet(response, packet)
        except Exception as exc:
            raise ResearchExecutionResultError(
                "successful execution result artifact failed verification"
            ) from exc

    def get_execution(self, request_hash: str) -> ResearchExecutionRecordV1 | None:
        """Load one sidecar; successful rows fail closed on result corruption."""
        request_hash = self._validate_request_hash(request_hash)
        with self._connect() as db:
            db.execute("BEGIN")
            self._validate_schema(db)
            row = self._execution_row(db, request_hash)
            if row is None:
                return None
            execution = self._execution_from_row(row)
            authorization_row = self._authorization_row(db, execution.authorization_id)
            if authorization_row is None:
                raise ResearchExecutionDatabaseError("execution authorization is missing")
            authorization = self._authorization_from_row(authorization_row)
            _task, packet = self._validate_authorization_bindings(db, authorization)
            reservation = self._reservation_in_transaction(db, request_hash)
            expected_reservation_state = {
                ResearchExecutionStatus.CANCELLED_BEFORE_DISPATCH: (
                    BudgetReservationState.RELEASED
                ),
                ResearchExecutionStatus.SUCCEEDED: BudgetReservationState.SETTLED,
            }.get(execution.status, BudgetReservationState.RESERVED)
            self._assert_reservation_binding(
                execution,
                reservation,
                expected_state=expected_reservation_state,
            )
            if execution.status is ResearchExecutionStatus.SUCCEEDED:
                self._load_and_verify_result(execution, packet)
            return execution

    def load_result(self, request_hash: str) -> ResearchProposalResponseV1:
        """Return a strictly verified and Packet-bound successful proposal response."""
        execution = self.get_execution(request_hash)
        if execution is None:
            raise ResearchExecutionResultError("research execution does not exist")
        with self._connect() as db:
            _task, packet, _packet_ref = self._authoritative_task_and_packet(
                db,
                task_id=execution.task_id,
                task_revision=execution.task_revision,
            )
        return self._load_and_verify_result(execution, packet)

    def request_envelope(self, authorization_id: int) -> AgentEnvelope:
        """Rebuild the provider-neutral request without exposing filesystem paths."""
        authorization_id = self._validate_id(authorization_id, field="authorization_id")
        with self._connect() as db:
            self._validate_schema(db)
            row = self._authorization_row(db, authorization_id)
            if row is None:
                raise ResearchExecutionAuthorizationError("authorization does not exist")
            authorization = self._authorization_from_row(row)
            task, packet = self._validate_authorization_bindings(db, authorization)
            envelope = self._request_envelope(task, packet)
            if self._request_hash(task, packet) != authorization.request_hash:
                raise ResearchExecutionAuthorizationError("request identity mismatch")
            return envelope


__all__ = [
    "RESEARCH_EXECUTION_SCHEMA_VERSION",
    "RESEARCH_PROPOSAL_RESPONSE_MEDIA_TYPE",
    "ResearchAuthorizationState",
    "ResearchExecutionAuthorizationError",
    "ResearchExecutionAuthorizationV1",
    "ResearchExecutionConflictError",
    "ResearchExecutionControl",
    "ResearchExecutionDatabaseError",
    "ResearchExecutionRecordV1",
    "ResearchExecutionResultError",
    "ResearchExecutionStateError",
    "ResearchExecutionStatus",
]
