"""Trusted review intake for successful external-model research proposals.

This additive sidecar turns each proposal in one verified, successful research
execution into a durable review item.  It performs no provider, broker, trading,
backtest, holdout, or manifest action.  Acceptance means only that a trusted
operator permits the proposal to become a ``PROPOSED`` hypothesis in the existing
``HypothesisRegistry``; it is not scientific validation.

``reviewer_id`` is bounded audit identity, not authentication.  Deployments must
expose review operations only through a trusted operator/application boundary.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from trademind.discovery.hypothesis_registry import (
    HypothesisRecord,
    HypothesisRegistry,
    HypothesisState,
    derive_content_hash,
    derive_hypothesis_family_id,
)
from trademind.orchestrator.audit_log import AuditLog
from trademind.orchestrator.budget import BudgetReservationState
from trademind.orchestrator.models import AuditEvent, PolicyDecision, Role, TaskState
from trademind.research_execution import (
    RESEARCH_PROPOSAL_RESPONSE_MEDIA_TYPE,
    ResearchExecutionControl,
    ResearchExecutionRecordV1,
    ResearchExecutionStatus,
)
from trademind.research_proposal_response import (
    MAX_RESEARCH_PROPOSALS,
    ResearchProposalResponseV1,
    ResearchProposalV1,
)
from trademind.signal_statistics_agent_packet import SignalStatisticsPacketV2
from trademind.signal_statistics_provenance import (
    ProvenanceError,
    canonical_json_bytes,
    parse_json,
)


RESEARCH_PROPOSAL_INTAKE_SCHEMA_VERSION = "research-proposal-intake-v1"
RESEARCH_PROPOSAL_HYPOTHESIS_SCHEMA_VERSION = "research-proposal-hypothesis-v1"

MAX_REVIEWER_ID_LENGTH = 256
MAX_REJECTION_REASONS = 4

_HASH_REF = re.compile(r"^sha256:[0-9a-f]{64}$")
_REQUEST_HASH = re.compile(r"^[0-9a-f]{64}$")
_CANDIDATE_ID = re.compile(r"^ssc-v2-[0-9a-f]{64}$")
_INTAKE_ID = re.compile(r"^rpi-v1:sha256:[0-9a-f]{64}:[0-7]$")


class ResearchProposalIntakeError(RuntimeError):
    """Base error for proposal intake or review failures."""


class ResearchProposalIntakeDatabaseError(ResearchProposalIntakeError):
    """The intake schema, topology, or persisted record is not trustworthy."""


class ResearchProposalSourceError(ResearchProposalIntakeError):
    """The authoritative execution/result/Packet source cannot be reverified."""


class ResearchProposalReviewConflict(ResearchProposalIntakeError):
    """A requested review conflicts with an immutable prior decision."""


class ResearchProposalIntakeState(StrEnum):
    PENDING_REVIEW = "PENDING_REVIEW"
    ACCEPTED_FOR_HYPOTHESIS = "ACCEPTED_FOR_HYPOTHESIS"
    REJECTED = "REJECTED"


class ResearchProposalRejectionReason(StrEnum):
    """Bounded v1 machine-readable rejection vocabulary."""

    DUPLICATE_RESEARCH_DIRECTION = "DUPLICATE_RESEARCH_DIRECTION"
    INSUFFICIENT_SOURCE_GROUNDING = "INSUFFICIENT_SOURCE_GROUNDING"
    NOT_FALSIFIABLE = "NOT_FALSIFIABLE"
    OUT_OF_SCOPE = "OUT_OF_SCOPE"
    TEST_NOT_ACTIONABLE = "TEST_NOT_ACTIONABLE"
    UNSAFE_RESEARCH_SCOPE = "UNSAFE_RESEARCH_SCOPE"


@dataclass(frozen=True, slots=True)
class ResearchProposalIngestionV1:
    request_hash: str
    authorization_id: int
    task_id: str
    task_revision: int
    packet_artifact_hash_ref: str
    packet_semantic_hash: str
    result_artifact_hash_ref: str
    result_media_type: str
    proposal_count: int
    ingested_at: str


@dataclass(frozen=True, slots=True)
class ResearchProposalIntakeV1:
    intake_id: str
    request_hash: str
    authorization_id: int
    task_id: str
    task_revision: int
    packet_artifact_hash_ref: str
    packet_semantic_hash: str
    result_artifact_hash_ref: str
    proposal_index: int
    candidate_id: str
    proposal: ResearchProposalV1
    state: ResearchProposalIntakeState
    reviewer_id: str | None
    reason_codes: tuple[ResearchProposalRejectionReason, ...]
    hypothesis_id: str | None
    created_at: str
    updated_at: str
    reviewed_at: str | None


@dataclass(frozen=True, slots=True)
class _VerifiedSource:
    execution: ResearchExecutionRecordV1
    response: ResearchProposalResponseV1
    packet: SignalStatisticsPacketV2


class ResearchProposalIntakeControl:
    """Trusted, provider-neutral proposal ingestion and explicit review boundary."""

    def __init__(
        self,
        *,
        execution_control: ResearchExecutionControl,
        hypothesis_registry: HypothesisRegistry,
    ) -> None:
        if not isinstance(execution_control, ResearchExecutionControl):
            raise TypeError("execution_control must be ResearchExecutionControl")
        if not isinstance(hypothesis_registry, HypothesisRegistry):
            raise TypeError("hypothesis_registry must be HypothesisRegistry")
        execution_path = execution_control.path.resolve()
        registry_path = hypothesis_registry.path.resolve()
        if execution_path != registry_path:
            raise ResearchProposalIntakeDatabaseError(
                "ResearchExecutionControl and HypothesisRegistry must share one database"
            )
        self.path = execution_path
        self.execution_control = execution_control
        self.hypothesis_registry = hypothesis_registry
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
                    'research_proposal_intake_meta',
                    'research_proposal_ingestions',
                    'research_proposal_intakes'
                )
                """
            ).fetchall()
        }

    @staticmethod
    def _validate_schema(db: sqlite3.Connection) -> None:
        expected_tables = {
            "research_proposal_intake_meta",
            "research_proposal_ingestions",
            "research_proposal_intakes",
        }
        if ResearchProposalIntakeControl._table_names(db) != expected_tables:
            raise ResearchProposalIntakeDatabaseError(
                "incomplete research proposal intake schema cannot be adopted"
            )
        try:
            meta = db.execute(
                "SELECT id, schema_version FROM research_proposal_intake_meta"
            ).fetchall()
        except sqlite3.Error as exc:
            raise ResearchProposalIntakeDatabaseError(
                "invalid research proposal intake metadata schema"
            ) from exc
        if (
            len(meta) != 1
            or meta[0]["id"] != 1
            or meta[0]["schema_version"] != RESEARCH_PROPOSAL_INTAKE_SCHEMA_VERSION
        ):
            raise ResearchProposalIntakeDatabaseError(
                "unsupported research proposal intake schema version"
            )

        expected_columns = {
            "research_proposal_intake_meta": {"id", "schema_version"},
            "research_proposal_ingestions": {
                "request_hash",
                "schema_version",
                "authorization_id",
                "task_id",
                "task_revision",
                "packet_artifact_hash_ref",
                "packet_semantic_hash",
                "result_artifact_hash_ref",
                "result_media_type",
                "proposal_count",
                "ingested_at",
            },
            "research_proposal_intakes": {
                "intake_id",
                "schema_version",
                "request_hash",
                "authorization_id",
                "task_id",
                "task_revision",
                "packet_artifact_hash_ref",
                "packet_semantic_hash",
                "result_artifact_hash_ref",
                "proposal_index",
                "candidate_id",
                "proposal_json",
                "state",
                "reviewer_id",
                "reason_codes_json",
                "hypothesis_id",
                "created_at",
                "updated_at",
                "reviewed_at",
            },
        }
        for table, expected in expected_columns.items():
            try:
                actual = {
                    row["name"] for row in db.execute(f"PRAGMA table_info({table})").fetchall()
                }
            except sqlite3.Error as exc:
                raise ResearchProposalIntakeDatabaseError(
                    f"invalid research proposal intake table: {table}"
                ) from exc
            if actual != expected:
                raise ResearchProposalIntakeDatabaseError(
                    f"unsupported research proposal intake table shape: {table}"
                )

    def _init_schema(self) -> None:
        with self._connect() as db:
            existing = self._table_names(db)
            if existing and len(existing) != 3:
                raise ResearchProposalIntakeDatabaseError(
                    "incomplete research proposal intake schema cannot be adopted"
                )
            if existing:
                self._validate_schema(db)
                return
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS research_proposal_intake_meta (
                    id INTEGER PRIMARY KEY CHECK(id = 1),
                    schema_version TEXT NOT NULL
                        CHECK(schema_version = 'research-proposal-intake-v1')
                );

                CREATE TABLE IF NOT EXISTS research_proposal_ingestions (
                    request_hash TEXT PRIMARY KEY
                        CHECK(length(request_hash) = 64)
                        CHECK(request_hash NOT GLOB '*[^0-9a-f]*'),
                    schema_version TEXT NOT NULL
                        CHECK(schema_version = 'research-proposal-intake-v1'),
                    authorization_id INTEGER NOT NULL UNIQUE,
                    task_id TEXT NOT NULL,
                    task_revision INTEGER NOT NULL CHECK(task_revision >= 1),
                    packet_artifact_hash_ref TEXT NOT NULL CHECK(length(packet_artifact_hash_ref)=71),
                    packet_semantic_hash TEXT NOT NULL CHECK(length(packet_semantic_hash)=71),
                    result_artifact_hash_ref TEXT NOT NULL UNIQUE
                        CHECK(length(result_artifact_hash_ref)=71),
                    result_media_type TEXT NOT NULL
                        CHECK(result_media_type =
                            'application/vnd.trademind.research-proposal-response-v1+json'),
                    proposal_count INTEGER NOT NULL CHECK(proposal_count BETWEEN 0 AND 8),
                    ingested_at TEXT NOT NULL,
                    FOREIGN KEY(request_hash) REFERENCES research_executions(request_hash),
                    FOREIGN KEY(authorization_id) REFERENCES
                        research_execution_authorizations(authorization_id)
                );

                CREATE TABLE IF NOT EXISTS research_proposal_intakes (
                    intake_id TEXT PRIMARY KEY,
                    schema_version TEXT NOT NULL
                        CHECK(schema_version = 'research-proposal-intake-v1'),
                    request_hash TEXT NOT NULL,
                    authorization_id INTEGER NOT NULL,
                    task_id TEXT NOT NULL,
                    task_revision INTEGER NOT NULL CHECK(task_revision >= 1),
                    packet_artifact_hash_ref TEXT NOT NULL CHECK(length(packet_artifact_hash_ref)=71),
                    packet_semantic_hash TEXT NOT NULL CHECK(length(packet_semantic_hash)=71),
                    result_artifact_hash_ref TEXT NOT NULL CHECK(length(result_artifact_hash_ref)=71),
                    proposal_index INTEGER NOT NULL CHECK(proposal_index BETWEEN 0 AND 7),
                    candidate_id TEXT NOT NULL CHECK(length(candidate_id)=71),
                    proposal_json TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN (
                        'PENDING_REVIEW', 'ACCEPTED_FOR_HYPOTHESIS', 'REJECTED'
                    )),
                    reviewer_id TEXT,
                    reason_codes_json TEXT NOT NULL DEFAULT '[]',
                    hypothesis_id TEXT UNIQUE,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    reviewed_at TEXT,
                    FOREIGN KEY(request_hash) REFERENCES
                        research_proposal_ingestions(request_hash),
                    FOREIGN KEY(authorization_id) REFERENCES
                        research_execution_authorizations(authorization_id),
                    FOREIGN KEY(hypothesis_id) REFERENCES hypotheses(hypothesis_id),
                    UNIQUE(request_hash, proposal_index),
                    UNIQUE(result_artifact_hash_ref, proposal_index),
                    CHECK(
                        (state = 'PENDING_REVIEW'
                            AND reviewer_id IS NULL
                            AND reviewed_at IS NULL
                            AND hypothesis_id IS NULL
                            AND reason_codes_json = '[]')
                        OR
                        (state = 'ACCEPTED_FOR_HYPOTHESIS'
                            AND reviewer_id IS NOT NULL
                            AND reviewed_at IS NOT NULL
                            AND hypothesis_id IS NOT NULL
                            AND reason_codes_json = '[]')
                        OR
                        (state = 'REJECTED'
                            AND reviewer_id IS NOT NULL
                            AND reviewed_at IS NOT NULL
                            AND hypothesis_id IS NULL
                            AND reason_codes_json != '[]')
                    )
                );
                CREATE INDEX IF NOT EXISTS idx_research_proposal_intake_request
                    ON research_proposal_intakes(request_hash, proposal_index);
                """
            )
            db.execute(
                """
                INSERT INTO research_proposal_intake_meta(id, schema_version)
                VALUES (1, ?)
                """,
                (RESEARCH_PROPOSAL_INTAKE_SCHEMA_VERSION,),
            )
            self._validate_schema(db)

    @staticmethod
    def _timestamp(value: object, *, field: str) -> str:
        if type(value) is not str:
            raise ResearchProposalIntakeDatabaseError(f"persisted {field} is invalid")
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise ResearchProposalIntakeDatabaseError(f"persisted {field} is invalid") from exc
        if parsed.utcoffset() is None:
            raise ResearchProposalIntakeDatabaseError(f"persisted {field} is not timezone-aware")
        return value

    @staticmethod
    def _positive_id(value: object, *, field: str) -> int:
        if type(value) is not int or not 1 <= value <= 2**63 - 1:
            raise ResearchProposalIntakeDatabaseError(f"persisted {field} is invalid")
        return value

    @staticmethod
    def _request_hash(value: object) -> str:
        if type(value) is not str or _REQUEST_HASH.fullmatch(value) is None:
            raise ValueError("request_hash must be 64 lowercase hexadecimal characters")
        return value

    @staticmethod
    def _hash_ref(value: object, *, field: str) -> str:
        if type(value) is not str or _HASH_REF.fullmatch(value) is None:
            raise ResearchProposalIntakeDatabaseError(f"persisted {field} is invalid")
        return value

    @staticmethod
    def _intake_identity(result_ref: str, proposal_index: int) -> str:
        if _HASH_REF.fullmatch(result_ref) is None:
            raise ResearchProposalSourceError("successful execution result ref is malformed")
        if type(proposal_index) is not int or not 0 <= proposal_index < MAX_RESEARCH_PROPOSALS:
            raise ValueError("proposal_index is outside the response v1 bound")
        return f"rpi-v1:{result_ref}:{proposal_index}"

    @staticmethod
    def _validate_intake_id(value: object) -> str:
        if type(value) is not str or _INTAKE_ID.fullmatch(value) is None:
            raise ValueError("intake_id is not a Research Proposal Intake v1 identity")
        return value

    @staticmethod
    def _reviewer_id(value: object) -> str:
        if (
            type(value) is not str
            or not value
            or value != value.strip()
            or len(value) > MAX_REVIEWER_ID_LENGTH
            or any(ord(character) <= 0x1F or 0x7F <= ord(character) <= 0x9F for character in value)
        ):
            raise ValueError("reviewer_id must be a bounded exact non-whitespace identity")
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ValueError("reviewer_id must be valid UTF-8") from exc
        return value

    @staticmethod
    def _reason_codes(
        values: object,
        *,
        require_nonempty: bool,
    ) -> tuple[ResearchProposalRejectionReason, ...]:
        if type(values) is not tuple:
            raise ValueError("reason_codes must be an immutable tuple")
        if require_nonempty and not values:
            raise ValueError("rejection requires at least one reason code")
        if len(values) > MAX_REJECTION_REASONS:
            raise ValueError("reason_codes exceeds the v1 bound")
        parsed: list[ResearchProposalRejectionReason] = []
        for value in values:
            if type(value) is not ResearchProposalRejectionReason:
                raise ValueError("reason_codes must contain exact v1 enum values")
            if value in parsed:
                raise ValueError(f"duplicate reason code: {value.value}")
            parsed.append(value)
        return tuple(sorted(parsed, key=lambda item: item.value))

    @staticmethod
    def _proposal_json(proposal: ResearchProposalV1) -> str:
        if type(proposal) is not ResearchProposalV1:
            raise ResearchProposalSourceError("authoritative response contains invalid proposal")
        try:
            return canonical_json_bytes(proposal.to_payload()).decode("utf-8")
        except (ProvenanceError, UnicodeDecodeError) as exc:
            raise ResearchProposalSourceError("proposal canonicalization failed") from exc

    def _verified_source(self, request_hash: str) -> _VerifiedSource:
        request_hash = self._request_hash(request_hash)
        try:
            execution = self.execution_control.get_execution(request_hash)
            if execution is None or execution.status is not ResearchExecutionStatus.SUCCEEDED:
                raise ResearchProposalSourceError("research execution is not SUCCEEDED")
            if (
                execution.result_artifact_hash_ref is None
                or execution.result_media_type != RESEARCH_PROPOSAL_RESPONSE_MEDIA_TYPE
            ):
                raise ResearchProposalSourceError("successful execution result binding is invalid")
            response = self.execution_control.load_result(request_hash)
            with self.execution_control._connect() as db:
                _task, packet, packet_ref = self.execution_control._authoritative_task_and_packet(
                    db,
                    task_id=execution.task_id,
                    task_revision=execution.task_revision,
                )
            if (
                packet_ref != execution.packet_artifact_hash_ref
                or packet.packet_semantic_hash != execution.packet_semantic_hash
            ):
                raise ResearchProposalSourceError("execution and Packet bindings conflict")
        except ResearchProposalSourceError:
            raise
        except Exception as exc:
            raise ResearchProposalSourceError(
                "authoritative successful research execution failed verification"
            ) from exc
        return _VerifiedSource(execution=execution, response=response, packet=packet)

    def _verify_source_in_transaction(
        self,
        db: sqlite3.Connection,
        source: _VerifiedSource,
    ) -> tuple[ResearchProposalResponseV1, SignalStatisticsPacketV2]:
        try:
            execution = self.execution_control._load_execution_for_update(
                db, source.execution.request_hash
            )
            if (
                execution != source.execution
                or execution.status is not ResearchExecutionStatus.SUCCEEDED
            ):
                raise ResearchProposalSourceError(
                    "successful execution changed before intake commit"
                )
            reservation = self.execution_control._reservation_in_transaction(
                db, execution.request_hash
            )
            self.execution_control._assert_reservation_binding(
                execution,
                reservation,
                expected_state=BudgetReservationState.SETTLED,
            )
            _task, packet, packet_ref = self.execution_control._authoritative_task_and_packet(
                db,
                task_id=execution.task_id,
                task_revision=execution.task_revision,
            )
            if (
                packet_ref != execution.packet_artifact_hash_ref
                or packet.packet_semantic_hash != execution.packet_semantic_hash
            ):
                raise ResearchProposalSourceError("Packet binding changed before intake commit")
            response = self.execution_control._load_and_verify_result(execution, packet)
            if (
                response.canonical_bytes() != source.response.canonical_bytes()
                or packet.canonical_bytes() != source.packet.canonical_bytes()
            ):
                raise ResearchProposalSourceError("verified source changed before intake commit")
            return response, packet
        except ResearchProposalSourceError:
            raise
        except Exception as exc:
            raise ResearchProposalSourceError(
                "authoritative source failed in-transaction verification"
            ) from exc

    @staticmethod
    def _ingestion_from_row(row: sqlite3.Row) -> ResearchProposalIngestionV1:
        if row["schema_version"] != RESEARCH_PROPOSAL_INTAKE_SCHEMA_VERSION:
            raise ResearchProposalIntakeDatabaseError("unsupported persisted ingestion version")
        request_hash = row["request_hash"]
        if type(request_hash) is not str or _REQUEST_HASH.fullmatch(request_hash) is None:
            raise ResearchProposalIntakeDatabaseError("persisted ingestion request_hash is invalid")
        task_id = row["task_id"]
        if type(task_id) is not str or not task_id:
            raise ResearchProposalIntakeDatabaseError("persisted ingestion task_id is invalid")
        media_type = row["result_media_type"]
        if media_type != RESEARCH_PROPOSAL_RESPONSE_MEDIA_TYPE:
            raise ResearchProposalIntakeDatabaseError("persisted result media type is unsupported")
        proposal_count = row["proposal_count"]
        if type(proposal_count) is not int or not 0 <= proposal_count <= MAX_RESEARCH_PROPOSALS:
            raise ResearchProposalIntakeDatabaseError("persisted proposal_count is invalid")
        return ResearchProposalIngestionV1(
            request_hash=request_hash,
            authorization_id=ResearchProposalIntakeControl._positive_id(
                row["authorization_id"], field="authorization_id"
            ),
            task_id=task_id,
            task_revision=ResearchProposalIntakeControl._positive_id(
                row["task_revision"], field="task_revision"
            ),
            packet_artifact_hash_ref=ResearchProposalIntakeControl._hash_ref(
                row["packet_artifact_hash_ref"], field="packet_artifact_hash_ref"
            ),
            packet_semantic_hash=ResearchProposalIntakeControl._hash_ref(
                row["packet_semantic_hash"], field="packet_semantic_hash"
            ),
            result_artifact_hash_ref=ResearchProposalIntakeControl._hash_ref(
                row["result_artifact_hash_ref"], field="result_artifact_hash_ref"
            ),
            result_media_type=media_type,
            proposal_count=proposal_count,
            ingested_at=ResearchProposalIntakeControl._timestamp(
                row["ingested_at"], field="ingested_at"
            ),
        )

    @classmethod
    def _intake_from_row(cls, row: sqlite3.Row) -> ResearchProposalIntakeV1:
        if row["schema_version"] != RESEARCH_PROPOSAL_INTAKE_SCHEMA_VERSION:
            raise ResearchProposalIntakeDatabaseError("unsupported persisted intake version")
        intake_id = row["intake_id"]
        if type(intake_id) is not str or _INTAKE_ID.fullmatch(intake_id) is None:
            raise ResearchProposalIntakeDatabaseError("persisted intake_id is invalid")
        request_hash = row["request_hash"]
        if type(request_hash) is not str or _REQUEST_HASH.fullmatch(request_hash) is None:
            raise ResearchProposalIntakeDatabaseError("persisted request_hash is invalid")
        proposal_index = row["proposal_index"]
        if type(proposal_index) is not int or not 0 <= proposal_index < MAX_RESEARCH_PROPOSALS:
            raise ResearchProposalIntakeDatabaseError("persisted proposal_index is invalid")
        result_ref = cls._hash_ref(
            row["result_artifact_hash_ref"], field="result_artifact_hash_ref"
        )
        if intake_id != cls._intake_identity(result_ref, proposal_index):
            raise ResearchProposalIntakeDatabaseError("persisted intake identity is not derived")
        candidate_id = row["candidate_id"]
        if type(candidate_id) is not str or _CANDIDATE_ID.fullmatch(candidate_id) is None:
            raise ResearchProposalIntakeDatabaseError("persisted candidate_id is invalid")
        proposal_json = row["proposal_json"]
        if type(proposal_json) is not str:
            raise ResearchProposalIntakeDatabaseError("persisted proposal_json is invalid")
        try:
            payload = parse_json(proposal_json)
            if not isinstance(payload, Mapping):
                raise ResearchProposalIntakeDatabaseError("persisted proposal root is invalid")
            proposal = ResearchProposalV1.from_payload(payload)
            if cls._proposal_json(proposal) != proposal_json:
                raise ResearchProposalIntakeDatabaseError(
                    "persisted proposal_json is not canonical"
                )
        except ResearchProposalIntakeDatabaseError:
            raise
        except Exception as exc:
            raise ResearchProposalIntakeDatabaseError(
                "persisted proposal_json failed strict parsing"
            ) from exc
        if proposal.candidate_id != candidate_id:
            raise ResearchProposalIntakeDatabaseError(
                "persisted proposal candidate binding conflicts"
            )
        try:
            state = ResearchProposalIntakeState(row["state"])
        except ValueError as exc:
            raise ResearchProposalIntakeDatabaseError(
                "persisted intake state is unsupported"
            ) from exc

        reason_json = row["reason_codes_json"]
        if type(reason_json) is not str:
            raise ResearchProposalIntakeDatabaseError("persisted reason_codes_json is invalid")
        try:
            raw_reasons = parse_json(reason_json)
            if type(raw_reasons) is not tuple:
                raise TypeError
            reasons = tuple(ResearchProposalRejectionReason(value) for value in raw_reasons)
            reasons = cls._reason_codes(
                reasons,
                require_nonempty=state is ResearchProposalIntakeState.REJECTED,
            )
            if canonical_json_bytes([reason.value for reason in reasons]).decode() != reason_json:
                raise ValueError
        except (ProvenanceError, TypeError, ValueError) as exc:
            raise ResearchProposalIntakeDatabaseError(
                "persisted rejection reason codes are invalid"
            ) from exc

        reviewer_id = row["reviewer_id"]
        reviewed_at = row["reviewed_at"]
        hypothesis_id = row["hypothesis_id"]
        if state is ResearchProposalIntakeState.PENDING_REVIEW:
            if (
                reviewer_id is not None
                or reviewed_at is not None
                or hypothesis_id is not None
                or reasons
            ):
                raise ResearchProposalIntakeDatabaseError(
                    "persisted pending intake is inconsistent"
                )
        else:
            try:
                reviewer_id = cls._reviewer_id(reviewer_id)
            except ValueError as exc:
                raise ResearchProposalIntakeDatabaseError(
                    "persisted reviewer_id is invalid"
                ) from exc
            reviewed_at = cls._timestamp(reviewed_at, field="reviewed_at")
            if state is ResearchProposalIntakeState.ACCEPTED_FOR_HYPOTHESIS:
                if hypothesis_id != intake_id or reasons:
                    raise ResearchProposalIntakeDatabaseError(
                        "persisted accepted intake binding is inconsistent"
                    )
            elif hypothesis_id is not None:
                raise ResearchProposalIntakeDatabaseError(
                    "persisted rejected intake has a hypothesis binding"
                )

        task_id = row["task_id"]
        if type(task_id) is not str or not task_id:
            raise ResearchProposalIntakeDatabaseError("persisted intake task_id is invalid")
        return ResearchProposalIntakeV1(
            intake_id=intake_id,
            request_hash=request_hash,
            authorization_id=cls._positive_id(row["authorization_id"], field="authorization_id"),
            task_id=task_id,
            task_revision=cls._positive_id(row["task_revision"], field="task_revision"),
            packet_artifact_hash_ref=cls._hash_ref(
                row["packet_artifact_hash_ref"], field="packet_artifact_hash_ref"
            ),
            packet_semantic_hash=cls._hash_ref(
                row["packet_semantic_hash"], field="packet_semantic_hash"
            ),
            result_artifact_hash_ref=result_ref,
            proposal_index=proposal_index,
            candidate_id=candidate_id,
            proposal=proposal,
            state=state,
            reviewer_id=reviewer_id,
            reason_codes=reasons,
            hypothesis_id=hypothesis_id,
            created_at=cls._timestamp(row["created_at"], field="created_at"),
            updated_at=cls._timestamp(row["updated_at"], field="updated_at"),
            reviewed_at=reviewed_at,
        )

    @staticmethod
    def _ingestion_matches_source(
        ingestion: ResearchProposalIngestionV1,
        source: _VerifiedSource,
    ) -> bool:
        execution = source.execution
        return (
            ingestion.request_hash == execution.request_hash
            and ingestion.authorization_id == execution.authorization_id
            and ingestion.task_id == execution.task_id
            and ingestion.task_revision == execution.task_revision
            and ingestion.packet_artifact_hash_ref == execution.packet_artifact_hash_ref
            and ingestion.packet_semantic_hash == execution.packet_semantic_hash
            and ingestion.result_artifact_hash_ref == execution.result_artifact_hash_ref
            and ingestion.result_media_type == execution.result_media_type
            and ingestion.proposal_count == len(source.response.proposals)
        )

    def _require_ingestion_in_transaction(
        self,
        db: sqlite3.Connection,
        source: _VerifiedSource,
    ) -> ResearchProposalIngestionV1:
        row = db.execute(
            "SELECT * FROM research_proposal_ingestions WHERE request_hash=?",
            (source.execution.request_hash,),
        ).fetchone()
        if row is None:
            raise ResearchProposalIntakeDatabaseError("proposal ingestion record is missing")
        ingestion = self._ingestion_from_row(row)
        if not self._ingestion_matches_source(ingestion, source):
            raise ResearchProposalIntakeDatabaseError(
                "proposal ingestion conflicts with authoritative source"
            )
        return ingestion

    @classmethod
    def _intake_matches_source(
        cls,
        intake: ResearchProposalIntakeV1,
        source: _VerifiedSource,
    ) -> bool:
        execution = source.execution
        if not 0 <= intake.proposal_index < len(source.response.proposals):
            return False
        proposal = source.response.proposals[intake.proposal_index]
        return (
            intake.intake_id
            == cls._intake_identity(intake.result_artifact_hash_ref, intake.proposal_index)
            and intake.request_hash == execution.request_hash
            and intake.authorization_id == execution.authorization_id
            and intake.task_id == execution.task_id
            and intake.task_revision == execution.task_revision
            and intake.packet_artifact_hash_ref == execution.packet_artifact_hash_ref
            and intake.packet_semantic_hash == execution.packet_semantic_hash
            and intake.result_artifact_hash_ref == execution.result_artifact_hash_ref
            and intake.candidate_id == proposal.candidate_id
            and cls._proposal_json(intake.proposal) == cls._proposal_json(proposal)
        )

    def ingest_succeeded_research_execution_v1(
        self,
        request_hash: str,
    ) -> tuple[ResearchProposalIntakeV1, ...]:
        """Idempotently create one pending intake per authoritative proposal."""
        source = self._verified_source(request_hash)
        execution = source.execution
        timestamp = ResearchExecutionControl._timestamp()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._validate_schema(db)
            response, _packet = self._verify_source_in_transaction(db, source)
            existing_row = db.execute(
                "SELECT * FROM research_proposal_ingestions WHERE request_hash=?",
                (execution.request_hash,),
            ).fetchone()
            if existing_row is not None:
                ingestion = self._ingestion_from_row(existing_row)
                if not self._ingestion_matches_source(ingestion, source):
                    raise ResearchProposalReviewConflict(
                        "existing proposal ingestion conflicts with authoritative execution"
                    )
                rows = db.execute(
                    """
                    SELECT * FROM research_proposal_intakes
                    WHERE request_hash=? ORDER BY proposal_index
                    """,
                    (execution.request_hash,),
                ).fetchall()
                intakes = tuple(self._intake_from_row(row) for row in rows)
                if len(intakes) != len(response.proposals) or any(
                    not self._intake_matches_source(intake, source) for intake in intakes
                ):
                    raise ResearchProposalIntakeDatabaseError(
                        "persisted ingestion does not match its proposal rows"
                    )
                return intakes

            db.execute(
                """
                INSERT INTO research_proposal_ingestions(
                    request_hash, schema_version, authorization_id, task_id,
                    task_revision, packet_artifact_hash_ref, packet_semantic_hash,
                    result_artifact_hash_ref, result_media_type, proposal_count, ingested_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    execution.request_hash,
                    RESEARCH_PROPOSAL_INTAKE_SCHEMA_VERSION,
                    execution.authorization_id,
                    execution.task_id,
                    execution.task_revision,
                    execution.packet_artifact_hash_ref,
                    execution.packet_semantic_hash,
                    execution.result_artifact_hash_ref,
                    execution.result_media_type,
                    len(response.proposals),
                    timestamp,
                ),
            )
            AuditLog.append_in_transaction(
                db,
                AuditEvent(
                    timestamp=timestamp,
                    task_id=execution.task_id,
                    revision=execution.task_revision,
                    actor_role=Role.OPERATOR,
                    action="RESEARCH_PROPOSAL_RESPONSE_INGESTED",
                    input_artifact_hashes=(
                        execution.packet_artifact_hash_ref,
                        execution.result_artifact_hash_ref,
                    ),
                    from_state=TaskState.NEW,
                    to_state=TaskState.NEW,
                    policy_result=PolicyDecision.AUTO_ALLOWED_WITH_AUDIT,
                    metadata={
                        "request_hash": execution.request_hash,
                        "authorization_id": execution.authorization_id,
                        "packet_semantic_hash": execution.packet_semantic_hash,
                        "proposal_count": len(response.proposals),
                    },
                ),
            )
            created: list[ResearchProposalIntakeV1] = []
            for index, proposal in enumerate(response.proposals):
                intake_id = self._intake_identity(execution.result_artifact_hash_ref, index)
                db.execute(
                    """
                    INSERT INTO research_proposal_intakes(
                        intake_id, schema_version, request_hash, authorization_id,
                        task_id, task_revision, packet_artifact_hash_ref,
                        packet_semantic_hash, result_artifact_hash_ref, proposal_index,
                        candidate_id, proposal_json, state, reviewer_id,
                        reason_codes_json, hypothesis_id, created_at, updated_at, reviewed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING_REVIEW',
                        NULL, '[]', NULL, ?, ?, NULL)
                    """,
                    (
                        intake_id,
                        RESEARCH_PROPOSAL_INTAKE_SCHEMA_VERSION,
                        execution.request_hash,
                        execution.authorization_id,
                        execution.task_id,
                        execution.task_revision,
                        execution.packet_artifact_hash_ref,
                        execution.packet_semantic_hash,
                        execution.result_artifact_hash_ref,
                        index,
                        proposal.candidate_id,
                        self._proposal_json(proposal),
                        timestamp,
                        timestamp,
                    ),
                )
                AuditLog.append_in_transaction(
                    db,
                    AuditEvent(
                        timestamp=timestamp,
                        task_id=execution.task_id,
                        revision=execution.task_revision,
                        actor_role=Role.OPERATOR,
                        action="RESEARCH_PROPOSAL_INTAKE_CREATED",
                        input_artifact_hashes=(execution.result_artifact_hash_ref,),
                        from_state=TaskState.NEW,
                        to_state=TaskState.NEW,
                        policy_result=PolicyDecision.AUTO_ALLOWED_WITH_AUDIT,
                        metadata={
                            "intake_id": intake_id,
                            "request_hash": execution.request_hash,
                            "proposal_index": index,
                            "candidate_id": proposal.candidate_id,
                            "intake_state": ResearchProposalIntakeState.PENDING_REVIEW.value,
                        },
                    ),
                )
                row = db.execute(
                    "SELECT * FROM research_proposal_intakes WHERE intake_id=?", (intake_id,)
                ).fetchone()
                if row is None:
                    raise ResearchProposalIntakeDatabaseError("intake insert was not visible")
                created.append(self._intake_from_row(row))
            return tuple(created)

    def _load_verified_intake(
        self, intake_id: str
    ) -> tuple[ResearchProposalIntakeV1, _VerifiedSource]:
        intake = self.get(intake_id)
        if intake is None:
            raise KeyError(intake_id)
        source = self._verified_source(intake.request_hash)
        ingestion = self.get_ingestion(intake.request_hash)
        if ingestion is None or not self._ingestion_matches_source(ingestion, source):
            raise ResearchProposalIntakeDatabaseError(
                "intake ingestion does not match authoritative source"
            )
        if not self._intake_matches_source(intake, source):
            raise ResearchProposalSourceError("intake no longer matches authoritative source")
        return intake, source

    @staticmethod
    def _candidate_binding(
        packet: SignalStatisticsPacketV2,
        candidate_id: str,
    ) -> dict[str, object]:
        matching = [
            dict(binding)
            for binding in packet.candidate_bindings
            if binding["candidate_id"] == candidate_id
        ]
        if len(matching) != 1:
            raise ResearchProposalSourceError("proposal candidate binding is not unique in Packet")
        return matching[0]

    @classmethod
    def _hypothesis_definitions(
        cls,
        intake: ResearchProposalIntakeV1,
        source: _VerifiedSource,
    ) -> tuple[dict[str, object], dict[str, object]]:
        binding = cls._candidate_binding(source.packet, intake.candidate_id)
        proposal = intake.proposal
        family_definition: dict[str, object] = {
            "schema_version": RESEARCH_PROPOSAL_HYPOTHESIS_SCHEMA_VERSION,
            "candidate": {
                "candidate_id": binding["candidate_id"],
                "symbol": binding["symbol"],
                "timeframe": binding["timeframe"],
                "feature": binding["feature"],
                "horizon": binding["horizon"],
            },
            "falsifiable_claim": proposal.falsifiable_claim,
            "proposed_test": proposal.proposed_test,
            "rejection_condition": proposal.rejection_condition,
        }
        content_definition: dict[str, object] = {
            "family_definition": family_definition,
            "proposal": proposal.to_payload(),
            "candidate_binding": binding,
            "model_metadata": {
                "qualitative_confidence": proposal.confidence.value,
                "interpretation": "external_model_self_assessment_only",
            },
            "provenance": {
                "intake_schema_version": RESEARCH_PROPOSAL_INTAKE_SCHEMA_VERSION,
                "intake_id": intake.intake_id,
                "execution_request_hash": intake.request_hash,
                "authorization_id": intake.authorization_id,
                "task_id": intake.task_id,
                "task_revision": intake.task_revision,
                "packet_artifact_hash_ref": intake.packet_artifact_hash_ref,
                "packet_semantic_hash": intake.packet_semantic_hash,
                "result_artifact_hash_ref": intake.result_artifact_hash_ref,
                "proposal_index": intake.proposal_index,
                "candidate_id": intake.candidate_id,
            },
        }
        canonical_json_bytes(family_definition)
        canonical_json_bytes(content_definition)
        return family_definition, content_definition

    def reject(
        self,
        intake_id: str,
        *,
        reviewer_id: str,
        reason_codes: tuple[ResearchProposalRejectionReason, ...],
    ) -> ResearchProposalIntakeV1:
        """Record one explicit trusted rejection without changing source evidence."""
        intake_id = self._validate_intake_id(intake_id)
        reviewer_id = self._reviewer_id(reviewer_id)
        reasons = self._reason_codes(reason_codes, require_nonempty=True)
        reason_json = canonical_json_bytes([reason.value for reason in reasons]).decode()
        before, source = self._load_verified_intake(intake_id)
        timestamp = ResearchExecutionControl._timestamp()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._validate_schema(db)
            self._verify_source_in_transaction(db, source)
            self._require_ingestion_in_transaction(db, source)
            row = db.execute(
                "SELECT * FROM research_proposal_intakes WHERE intake_id=?", (intake_id,)
            ).fetchone()
            if row is None:
                raise KeyError(intake_id)
            current = self._intake_from_row(row)
            if current != before:
                if (
                    current.state is ResearchProposalIntakeState.REJECTED
                    and current.reviewer_id == reviewer_id
                    and current.reason_codes == reasons
                ):
                    return current
                raise ResearchProposalReviewConflict("proposal review changed concurrently")
            if current.state is ResearchProposalIntakeState.REJECTED:
                if current.reviewer_id == reviewer_id and current.reason_codes == reasons:
                    return current
                raise ResearchProposalReviewConflict("proposal already has a different rejection")
            if current.state is not ResearchProposalIntakeState.PENDING_REVIEW:
                raise ResearchProposalReviewConflict(
                    "accepted proposal cannot be rewritten as rejected"
                )
            cursor = db.execute(
                """
                UPDATE research_proposal_intakes
                SET state='REJECTED', reviewer_id=?, reason_codes_json=?,
                    updated_at=?, reviewed_at=?
                WHERE intake_id=? AND state='PENDING_REVIEW'
                """,
                (reviewer_id, reason_json, timestamp, timestamp, intake_id),
            )
            if cursor.rowcount != 1:
                raise ResearchProposalReviewConflict("proposal review changed concurrently")
            AuditLog.append_in_transaction(
                db,
                AuditEvent(
                    timestamp=timestamp,
                    task_id=current.task_id,
                    revision=current.task_revision,
                    actor_role=Role.OPERATOR,
                    action="RESEARCH_PROPOSAL_REJECTED",
                    input_artifact_hashes=(current.result_artifact_hash_ref,),
                    from_state=TaskState.NEW,
                    to_state=TaskState.NEW,
                    policy_result=PolicyDecision.AUTO_ALLOWED_WITH_AUDIT,
                    metadata={
                        "intake_id": current.intake_id,
                        "request_hash": current.request_hash,
                        "proposal_index": current.proposal_index,
                        "candidate_id": current.candidate_id,
                        "reviewer_id": reviewer_id,
                        "reason_codes": [reason.value for reason in reasons],
                        "intake_from": ResearchProposalIntakeState.PENDING_REVIEW.value,
                        "intake_to": ResearchProposalIntakeState.REJECTED.value,
                    },
                ),
            )
            updated = db.execute(
                "SELECT * FROM research_proposal_intakes WHERE intake_id=?", (intake_id,)
            ).fetchone()
            if updated is None:
                raise ResearchProposalIntakeDatabaseError("rejected intake disappeared")
            return self._intake_from_row(updated)

    def accept_for_hypothesis(
        self,
        intake_id: str,
        *,
        reviewer_id: str,
    ) -> tuple[ResearchProposalIntakeV1, HypothesisRecord]:
        """Atomically bind one explicitly accepted proposal as a PROPOSED hypothesis."""
        intake_id = self._validate_intake_id(intake_id)
        reviewer_id = self._reviewer_id(reviewer_id)
        before, source = self._load_verified_intake(intake_id)
        family_definition, content_definition = self._hypothesis_definitions(before, source)
        expected_family_id = derive_hypothesis_family_id(family_definition)
        expected_content_hash = derive_content_hash(content_definition)
        timestamp = ResearchExecutionControl._timestamp()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._validate_schema(db)
            self._verify_source_in_transaction(db, source)
            self._require_ingestion_in_transaction(db, source)
            row = db.execute(
                "SELECT * FROM research_proposal_intakes WHERE intake_id=?", (intake_id,)
            ).fetchone()
            if row is None:
                raise KeyError(intake_id)
            current = self._intake_from_row(row)
            if current != before:
                if (
                    current.state is ResearchProposalIntakeState.ACCEPTED_FOR_HYPOTHESIS
                    and current.reviewer_id == reviewer_id
                ):
                    return current, self._accepted_hypothesis_in_transaction(
                        db,
                        current,
                        expected_family_id=expected_family_id,
                        expected_content_hash=expected_content_hash,
                    )
                raise ResearchProposalReviewConflict("proposal review changed concurrently")
            if current.state is ResearchProposalIntakeState.ACCEPTED_FOR_HYPOTHESIS:
                if current.reviewer_id == reviewer_id:
                    return current, self._accepted_hypothesis_in_transaction(
                        db,
                        current,
                        expected_family_id=expected_family_id,
                        expected_content_hash=expected_content_hash,
                    )
                raise ResearchProposalReviewConflict("proposal already has a different acceptance")
            if current.state is not ResearchProposalIntakeState.PENDING_REVIEW:
                raise ResearchProposalReviewConflict(
                    "rejected proposal cannot be rewritten as accepted"
                )

            hypothesis = self.hypothesis_registry.register_in_transaction(
                db,
                hypothesis_id=current.intake_id,
                family_definition=family_definition,
                content_definition=content_definition,
            )
            if hypothesis.state is not HypothesisState.PROPOSED:
                raise ResearchProposalIntakeDatabaseError(
                    "new hypothesis registry entry is not PROPOSED"
                )
            cursor = db.execute(
                """
                UPDATE research_proposal_intakes
                SET state='ACCEPTED_FOR_HYPOTHESIS', reviewer_id=?, hypothesis_id=?,
                    updated_at=?, reviewed_at=?
                WHERE intake_id=? AND state='PENDING_REVIEW'
                """,
                (reviewer_id, hypothesis.hypothesis_id, timestamp, timestamp, intake_id),
            )
            if cursor.rowcount != 1:
                raise ResearchProposalReviewConflict("proposal review changed concurrently")
            AuditLog.append_in_transaction(
                db,
                AuditEvent(
                    timestamp=timestamp,
                    task_id=current.task_id,
                    revision=current.task_revision,
                    actor_role=Role.OPERATOR,
                    action="RESEARCH_PROPOSAL_ACCEPTED_FOR_HYPOTHESIS",
                    input_artifact_hashes=(current.result_artifact_hash_ref,),
                    from_state=TaskState.NEW,
                    to_state=TaskState.NEW,
                    policy_result=PolicyDecision.AUTO_ALLOWED_WITH_AUDIT,
                    metadata={
                        "intake_id": current.intake_id,
                        "request_hash": current.request_hash,
                        "proposal_index": current.proposal_index,
                        "candidate_id": current.candidate_id,
                        "reviewer_id": reviewer_id,
                        "hypothesis_id": hypothesis.hypothesis_id,
                        "intake_from": ResearchProposalIntakeState.PENDING_REVIEW.value,
                        "intake_to": (ResearchProposalIntakeState.ACCEPTED_FOR_HYPOTHESIS.value),
                        "scientifically_validated": False,
                    },
                ),
            )
            AuditLog.append_in_transaction(
                db,
                AuditEvent(
                    timestamp=timestamp,
                    task_id=current.task_id,
                    revision=current.task_revision,
                    actor_role=Role.OPERATOR,
                    action="HYPOTHESIS_REGISTRY_BINDING_CREATED",
                    input_artifact_hashes=(
                        current.packet_artifact_hash_ref,
                        current.result_artifact_hash_ref,
                    ),
                    from_state=TaskState.NEW,
                    to_state=TaskState.NEW,
                    policy_result=PolicyDecision.AUTO_ALLOWED_WITH_AUDIT,
                    metadata={
                        "intake_id": current.intake_id,
                        "request_hash": current.request_hash,
                        "candidate_id": current.candidate_id,
                        "hypothesis_id": hypothesis.hypothesis_id,
                        "hypothesis_family_id": hypothesis.hypothesis_family_id,
                        "hypothesis_content_hash": hypothesis.content_hash,
                        "hypothesis_state": hypothesis.state.value,
                    },
                ),
            )
            updated = db.execute(
                "SELECT * FROM research_proposal_intakes WHERE intake_id=?", (intake_id,)
            ).fetchone()
            if updated is None:
                raise ResearchProposalIntakeDatabaseError("accepted intake disappeared")
            return self._intake_from_row(updated), hypothesis

    @staticmethod
    def _accepted_hypothesis_in_transaction(
        db: sqlite3.Connection,
        intake: ResearchProposalIntakeV1,
        *,
        expected_family_id: str,
        expected_content_hash: str,
    ) -> HypothesisRecord:
        row = db.execute(
            """
            SELECT h.*, f.definition_json AS family_definition_json
            FROM hypotheses h
            JOIN hypothesis_families f ON f.family_id=h.family_id
            WHERE h.hypothesis_id=?
            """,
            (intake.hypothesis_id,),
        ).fetchone()
        if row is None:
            raise ResearchProposalIntakeDatabaseError(
                "accepted intake has no hypothesis registry binding"
            )
        hypothesis = HypothesisRegistry._row(row)
        try:
            frozen_family = parse_json(row["family_definition_json"])
            frozen_content = parse_json(row["content_definition_json"])
            family_definition = json.loads(row["family_definition_json"])
            content_definition = json.loads(row["content_definition_json"])
            if (
                not isinstance(frozen_family, Mapping)
                or not isinstance(frozen_content, Mapping)
                or not isinstance(family_definition, dict)
                or not isinstance(content_definition, dict)
            ):
                raise TypeError
            stored_family_id = derive_hypothesis_family_id(family_definition)
            stored_content_hash = derive_content_hash(content_definition)
        except Exception as exc:
            raise ResearchProposalIntakeDatabaseError(
                "accepted hypothesis definitions failed strict verification"
            ) from exc
        if (
            hypothesis.hypothesis_id != intake.intake_id
            or hypothesis.hypothesis_family_id != expected_family_id
            or hypothesis.content_hash != expected_content_hash
            or stored_family_id != hypothesis.hypothesis_family_id
            or stored_content_hash != hypothesis.content_hash
        ):
            raise ResearchProposalIntakeDatabaseError(
                "accepted intake hypothesis binding is inconsistent"
            )
        return hypothesis

    def get_ingestion(self, request_hash: str) -> ResearchProposalIngestionV1 | None:
        request_hash = self._request_hash(request_hash)
        with self._connect() as db:
            self._validate_schema(db)
            row = db.execute(
                "SELECT * FROM research_proposal_ingestions WHERE request_hash=?",
                (request_hash,),
            ).fetchone()
            return self._ingestion_from_row(row) if row is not None else None

    def get(self, intake_id: str) -> ResearchProposalIntakeV1 | None:
        intake_id = self._validate_intake_id(intake_id)
        with self._connect() as db:
            self._validate_schema(db)
            row = db.execute(
                "SELECT * FROM research_proposal_intakes WHERE intake_id=?", (intake_id,)
            ).fetchone()
            return self._intake_from_row(row) if row is not None else None

    def list_for_execution(self, request_hash: str) -> tuple[ResearchProposalIntakeV1, ...]:
        request_hash = self._request_hash(request_hash)
        with self._connect() as db:
            self._validate_schema(db)
            rows = db.execute(
                """
                SELECT * FROM research_proposal_intakes
                WHERE request_hash=? ORDER BY proposal_index
                """,
                (request_hash,),
            ).fetchall()
            return tuple(self._intake_from_row(row) for row in rows)

    def reverify(self, intake_id: str) -> ResearchProposalIntakeV1:
        """Reverify exact result/Packet/execution provenance for a persisted intake."""
        intake, _source = self._load_verified_intake(self._validate_intake_id(intake_id))
        return intake


__all__ = [
    "MAX_REJECTION_REASONS",
    "MAX_REVIEWER_ID_LENGTH",
    "RESEARCH_PROPOSAL_HYPOTHESIS_SCHEMA_VERSION",
    "RESEARCH_PROPOSAL_INTAKE_SCHEMA_VERSION",
    "ResearchProposalIngestionV1",
    "ResearchProposalIntakeControl",
    "ResearchProposalIntakeDatabaseError",
    "ResearchProposalIntakeError",
    "ResearchProposalIntakeState",
    "ResearchProposalIntakeV1",
    "ResearchProposalRejectionReason",
    "ResearchProposalReviewConflict",
    "ResearchProposalSourceError",
]
