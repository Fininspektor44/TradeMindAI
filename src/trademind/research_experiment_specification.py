"""Trusted experiment-specification boundary for an accepted, PROPOSED hypothesis.

This additive sidecar closes the gap between "a trusted reviewer approved an
external-model research proposal into a PROPOSED HypothesisRegistry entry"
(``trademind.research_proposal_intake``) and "the technical parameters a
future ``trademind.discovery.manifest.ExperimentManifest`` needs are fully
and explicitly determined, so the manifest could later be safely frozen
without any of its fields being silently guessed."

It performs no freeze, no train/test, no validation, no final-holdout access,
no provider/network/broker action. It is provider-neutral: nothing in this
module reads model/proposal confidence, prose, or provider identity to
decide anything. Every experiment-defining technical field (test_family,
primary_metric, alpha, q, minimum_effect_size, max_hypotheses_tests,
parameters, dataset bindings) is a mandatory, explicit keyword argument with
no default -- there is no code path by which untrusted proposal content can
populate them. ``reviewer_id`` is bounded audit identity, not authentication;
deployments must expose specification creation only through a trusted
operator/application boundary, exactly like
``ResearchProposalIntakeControl.accept_for_hypothesis``.

Scope boundary (deliberate, not an oversight): this module STOPS at
producing an immutable, cryptographically bound experiment specification.
It never calls ``HypothesisRegistry.freeze``/``transition``, never builds or
persists an ``ExperimentManifest``, and never advances a hypothesis past
``PROPOSED``. Turning an approved specification into a frozen
``ExperimentManifest`` is a separate, later, explicitly out-of-scope layer.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

from trademind.discovery.hypothesis_registry import (
    HypothesisRecord,
    HypothesisRegistry,
    HypothesisState,
    derive_hypothesis_family_id,
)
from trademind.discovery.manifest import DatasetArtifact
from trademind.orchestrator.audit_log import AuditLog
from trademind.orchestrator.models import AuditEvent, PolicyDecision, Role, TaskState
from trademind.research_proposal_intake import (
    ResearchProposalIntakeControl,
    ResearchProposalIntakeState,
    ResearchProposalIntakeV1,
)
from trademind.signal_statistics_provenance import (
    ProvenanceError,
    canonical_json_bytes,
    parse_json,
)

RESEARCH_EXPERIMENT_SPECIFICATION_SCHEMA_VERSION = "research-experiment-specification-v1"

MAX_REVIEWER_ID_LENGTH = 256
MAX_TEXT_FIELD_LENGTH = 128
MAX_DATASETS = 16
MAX_PARAMETERS_BYTES = 16_384

_INTAKE_ID = re.compile(r"^rpi-v1:sha256:[0-9a-f]{64}:[0-7]$")
_HASH_REF = re.compile(r"^sha256:[0-9a-f]{64}$")
_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
_REQUEST_HASH = re.compile(r"^[0-9a-f]{64}$")
_CANDIDATE_ID = re.compile(r"^ssc-v2-[0-9a-f]{64}$")
_SPECIFICATION_ID = re.compile(
    r"^res-v1:rpi-v1:sha256:[0-9a-f]{64}:[0-7]:[0-9a-f]{64}$"
)
_TEXT_FIELD = re.compile(r"^\S(?:.{0,126}\S)?$")
# Best-effort, defense-in-depth naming guard. The PRIMARY guarantee that final
# holdout data can never enter this layer is structural: this module imports
# nothing from trademind.discovery.holdout_sealer / holdout_store /
# orchestrator_bridge / holdout_crypto, and the upstream precondition (bound
# hypothesis must be exactly PROPOSED, manifest_hash None) means no holdout
# seal/isolation activity for this hypothesis's family could have occurred
# yet under the registry's own one-way state machine. This filename check is
# an additional, narrower belt-and-suspenders check, not the boundary itself.
_FORBIDDEN_DATASET_PATH_SUBSTRING = "holdout"


class ResearchExperimentSpecificationError(RuntimeError):
    """Base error for experiment-specification failures."""


class ResearchExperimentSpecificationDatabaseError(ResearchExperimentSpecificationError):
    """The specification schema, topology, or persisted record is not trustworthy."""


class ResearchExperimentSpecificationSourceError(ResearchExperimentSpecificationError):
    """The authoritative accepted intake / PROPOSED hypothesis source cannot be reverified."""


class ResearchExperimentSpecificationConflict(ResearchExperimentSpecificationError):
    """A requested specification conflicts with an immutable prior record."""


@dataclass(frozen=True, slots=True)
class ResearchExperimentSpecificationV1:
    specification_id: str
    hypothesis_id: str
    hypothesis_family_id: str
    hypothesis_content_hash: str
    family_definition: Mapping[str, object]
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
    reviewer_id: str
    test_family: str
    primary_metric: str
    alpha: float
    q: float
    minimum_effect_size: float
    max_hypotheses_tests: int
    parameters: Mapping[str, object]
    datasets: tuple[DatasetArtifact, ...]
    content_hash: str
    created_at: str


@dataclass(frozen=True, slots=True)
class _VerifiedSpecificationSource:
    intake: ResearchProposalIntakeV1
    hypothesis: HypothesisRecord
    family_definition: dict[str, object]


class ResearchExperimentSpecificationControl:
    """Trusted, provider-neutral experiment-specification boundary."""

    def __init__(
        self,
        *,
        intake_control: ResearchProposalIntakeControl,
        hypothesis_registry: HypothesisRegistry,
    ) -> None:
        if not isinstance(intake_control, ResearchProposalIntakeControl):
            raise TypeError("intake_control must be ResearchProposalIntakeControl")
        if not isinstance(hypothesis_registry, HypothesisRegistry):
            raise TypeError("hypothesis_registry must be HypothesisRegistry")
        intake_path = intake_control.path.resolve()
        registry_path = hypothesis_registry.path.resolve()
        if intake_path != registry_path or intake_control.hypothesis_registry is not hypothesis_registry:
            raise ResearchExperimentSpecificationDatabaseError(
                "ResearchProposalIntakeControl and HypothesisRegistry must share one database "
                "and one bound registry instance"
            )
        self.path = intake_path
        self.intake_control = intake_control
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
                    'research_experiment_specification_meta',
                    'research_experiment_specifications'
                )
                """
            ).fetchall()
        }

    @staticmethod
    def _validate_schema(db: sqlite3.Connection) -> None:
        expected_tables = {
            "research_experiment_specification_meta",
            "research_experiment_specifications",
        }
        if ResearchExperimentSpecificationControl._table_names(db) != expected_tables:
            raise ResearchExperimentSpecificationDatabaseError(
                "incomplete research experiment specification schema cannot be adopted"
            )
        try:
            meta = db.execute(
                "SELECT id, schema_version FROM research_experiment_specification_meta"
            ).fetchall()
        except sqlite3.Error as exc:
            raise ResearchExperimentSpecificationDatabaseError(
                "invalid research experiment specification metadata schema"
            ) from exc
        if (
            len(meta) != 1
            or meta[0]["id"] != 1
            or meta[0]["schema_version"] != RESEARCH_EXPERIMENT_SPECIFICATION_SCHEMA_VERSION
        ):
            raise ResearchExperimentSpecificationDatabaseError(
                "unsupported research experiment specification schema version"
            )
        expected_columns = {
            "research_experiment_specification_meta": {"id", "schema_version"},
            "research_experiment_specifications": {
                "specification_id",
                "schema_version",
                "hypothesis_id",
                "hypothesis_family_id",
                "hypothesis_content_hash",
                "family_definition_json",
                "intake_id",
                "request_hash",
                "authorization_id",
                "task_id",
                "task_revision",
                "packet_artifact_hash_ref",
                "packet_semantic_hash",
                "result_artifact_hash_ref",
                "proposal_index",
                "candidate_id",
                "reviewer_id",
                "test_family",
                "primary_metric",
                "alpha",
                "q",
                "minimum_effect_size",
                "max_hypotheses_tests",
                "parameters_json",
                "datasets_json",
                "content_hash",
                "created_at",
            },
        }
        for table, expected in expected_columns.items():
            try:
                actual = {
                    row["name"] for row in db.execute(f"PRAGMA table_info({table})").fetchall()
                }
            except sqlite3.Error as exc:
                raise ResearchExperimentSpecificationDatabaseError(
                    f"invalid research experiment specification table: {table}"
                ) from exc
            if actual != expected:
                raise ResearchExperimentSpecificationDatabaseError(
                    f"unsupported research experiment specification table shape: {table}"
                )

    def _init_schema(self) -> None:
        with self._connect() as db:
            existing = self._table_names(db)
            if existing and len(existing) != 2:
                raise ResearchExperimentSpecificationDatabaseError(
                    "incomplete research experiment specification schema cannot be adopted"
                )
            if existing:
                self._validate_schema(db)
                return
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS research_experiment_specification_meta (
                    id INTEGER PRIMARY KEY CHECK(id = 1),
                    schema_version TEXT NOT NULL
                        CHECK(schema_version = 'research-experiment-specification-v1')
                );

                CREATE TABLE IF NOT EXISTS research_experiment_specifications (
                    specification_id TEXT PRIMARY KEY,
                    schema_version TEXT NOT NULL
                        CHECK(schema_version = 'research-experiment-specification-v1'),
                    hypothesis_id TEXT NOT NULL UNIQUE,
                    hypothesis_family_id TEXT NOT NULL,
                    hypothesis_content_hash TEXT NOT NULL
                        CHECK(length(hypothesis_content_hash)=64),
                    family_definition_json TEXT NOT NULL,
                    intake_id TEXT NOT NULL,
                    request_hash TEXT NOT NULL CHECK(length(request_hash)=64),
                    authorization_id INTEGER NOT NULL,
                    task_id TEXT NOT NULL,
                    task_revision INTEGER NOT NULL CHECK(task_revision >= 1),
                    packet_artifact_hash_ref TEXT NOT NULL
                        CHECK(length(packet_artifact_hash_ref)=71),
                    packet_semantic_hash TEXT NOT NULL CHECK(length(packet_semantic_hash)=71),
                    result_artifact_hash_ref TEXT NOT NULL
                        CHECK(length(result_artifact_hash_ref)=71),
                    proposal_index INTEGER NOT NULL CHECK(proposal_index BETWEEN 0 AND 7),
                    candidate_id TEXT NOT NULL CHECK(length(candidate_id)=71),
                    reviewer_id TEXT NOT NULL,
                    test_family TEXT NOT NULL,
                    primary_metric TEXT NOT NULL,
                    alpha REAL NOT NULL CHECK(alpha > 0 AND alpha <= 1),
                    q REAL NOT NULL CHECK(q > 0 AND q <= 1),
                    minimum_effect_size REAL NOT NULL CHECK(minimum_effect_size >= 0),
                    max_hypotheses_tests INTEGER NOT NULL CHECK(max_hypotheses_tests >= 1),
                    parameters_json TEXT NOT NULL,
                    datasets_json TEXT NOT NULL,
                    content_hash TEXT NOT NULL CHECK(length(content_hash)=64),
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(hypothesis_id) REFERENCES hypotheses(hypothesis_id),
                    FOREIGN KEY(intake_id) REFERENCES research_proposal_intakes(intake_id)
                );
                """
            )
            db.execute(
                """
                INSERT INTO research_experiment_specification_meta(id, schema_version)
                VALUES (1, ?)
                """,
                (RESEARCH_EXPERIMENT_SPECIFICATION_SCHEMA_VERSION,),
            )
            self._validate_schema(db)

    # --- small validation primitives (deliberately mirror, not import,
    # trademind.research_proposal_intake's private helpers -- same house
    # convention as that module already uses relative to hypothesis_registry). ---

    @staticmethod
    def _intake_id(value: object) -> str:
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
    def _text_field(value: object, *, field: str) -> str:
        if (
            type(value) is not str
            or _TEXT_FIELD.fullmatch(value) is None
            or len(value) > MAX_TEXT_FIELD_LENGTH
        ):
            raise ValueError(f"{field} must be a bounded, trimmed, non-empty string")
        return value

    @staticmethod
    def _unit_interval(value: object, *, field: str) -> float:
        if type(value) not in (int, float) or type(value) is bool:
            raise ValueError(f"{field} must be a real number")
        number = float(value)
        if not 0 < number <= 1:
            raise ValueError(f"{field} must be in (0, 1]")
        return number

    @staticmethod
    def _non_negative_float(value: object, *, field: str) -> float:
        if type(value) not in (int, float) or type(value) is bool:
            raise ValueError(f"{field} must be a real number")
        number = float(value)
        if number < 0:
            raise ValueError(f"{field} must be non-negative")
        return number

    @staticmethod
    def _positive_int(value: object, *, field: str) -> int:
        if type(value) is not int or type(value) is bool or value < 1:
            raise ValueError(f"{field} must be an exact positive integer")
        return value

    @staticmethod
    def _parameters(value: object) -> dict[str, object]:
        if value is None:
            return {}
        if not isinstance(value, Mapping):
            raise ValueError("parameters must be a mapping")
        try:
            encoded = canonical_json_bytes(dict(value))
        except ProvenanceError as exc:
            raise ValueError("parameters must be canonical JSON data") from exc
        if len(encoded) > MAX_PARAMETERS_BYTES:
            raise ValueError("parameters exceeds the v1 size bound")
        return dict(value)

    @staticmethod
    def _datasets(value: object) -> tuple[DatasetArtifact, ...]:
        if type(value) is not tuple or not value:
            raise ValueError("datasets must be a non-empty tuple of DatasetArtifact")
        if len(value) > MAX_DATASETS:
            raise ValueError("datasets exceeds the v1 bound")
        verified: list[DatasetArtifact] = []
        seen_paths: set[str] = set()
        for entry in value:
            if type(entry) is not DatasetArtifact:
                raise ValueError("every dataset entry must be an exact DatasetArtifact")
            lowered = entry.file_path.lower()
            if _FORBIDDEN_DATASET_PATH_SUBSTRING in lowered:
                raise ValueError(
                    "dataset file_path must not reference final-holdout-shaped paths"
                )
            if entry.file_path in seen_paths:
                raise ValueError("duplicate dataset file_path")
            seen_paths.add(entry.file_path)
            # Re-verify now, at binding time: the caller may have hashed the
            # artifact earlier, and the file must still match exactly. Reuses
            # DatasetArtifact.verify() unchanged -- no second hashing scheme.
            try:
                entry.verify()
            except Exception as exc:
                raise ValueError(f"dataset artifact failed verification: {exc}") from exc
            verified.append(entry)
        return tuple(verified)

    @staticmethod
    def _timestamp(value: object, *, field: str) -> str:
        if type(value) is not str:
            raise ResearchExperimentSpecificationDatabaseError(f"persisted {field} is invalid")
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise ResearchExperimentSpecificationDatabaseError(f"persisted {field} is invalid") from exc
        if parsed.utcoffset() is None:
            raise ResearchExperimentSpecificationDatabaseError(f"persisted {field} is not timezone-aware")
        return value

    @staticmethod
    def _hash_ref(value: object, *, field: str) -> str:
        if type(value) is not str or _HASH_REF.fullmatch(value) is None:
            raise ResearchExperimentSpecificationDatabaseError(f"persisted {field} is invalid")
        return value

    @staticmethod
    def _sha256_hex(value: object, *, field: str) -> str:
        if type(value) is not str or _SHA256_HEX.fullmatch(value) is None:
            raise ResearchExperimentSpecificationDatabaseError(f"persisted {field} is invalid")
        return value

    @staticmethod
    def _positive_id(value: object, *, field: str) -> int:
        if type(value) is not int or not 1 <= value <= 2**63 - 1:
            raise ResearchExperimentSpecificationDatabaseError(f"persisted {field} is invalid")
        return value

    # --- deterministic content identity. ---

    @staticmethod
    def _content_payload(
        *,
        hypothesis_id: str,
        test_family: str,
        primary_metric: str,
        alpha: float,
        q: float,
        minimum_effect_size: float,
        max_hypotheses_tests: int,
        parameters: Mapping[str, object],
        datasets: tuple[DatasetArtifact, ...],
    ) -> dict[str, object]:
        return {
            "schema_version": RESEARCH_EXPERIMENT_SPECIFICATION_SCHEMA_VERSION,
            "hypothesis_id": hypothesis_id,
            "test_family": test_family,
            "primary_metric": primary_metric,
            "alpha": alpha,
            "q": q,
            "minimum_effect_size": minimum_effect_size,
            "max_hypotheses_tests": max_hypotheses_tests,
            "parameters": dict(parameters),
            "datasets": [asdict(item) for item in datasets],
        }

    @classmethod
    def _content_hash(cls, payload: Mapping[str, object]) -> str:
        return hashlib.sha256(canonical_json_bytes(dict(payload))).hexdigest()

    @classmethod
    def _specification_id(cls, hypothesis_id: str, content_hash: str) -> str:
        return f"res-v1:{hypothesis_id}:{content_hash}"

    # --- authoritative source verification. ---

    def _verified_specification_source(self, intake_id: str) -> _VerifiedSpecificationSource:
        intake_id = self._intake_id(intake_id)
        try:
            intake = self.intake_control.get(intake_id)
            if intake is None:
                raise ResearchExperimentSpecificationSourceError("accepted intake does not exist")
            if intake.state is not ResearchProposalIntakeState.ACCEPTED_FOR_HYPOTHESIS:
                raise ResearchExperimentSpecificationSourceError(
                    "intake is not ACCEPTED_FOR_HYPOTHESIS"
                )
            if intake.hypothesis_id is None:
                raise ResearchExperimentSpecificationSourceError(
                    "accepted intake has no hypothesis binding"
                )
            # Reverify the intake's own full authoritative source chain
            # (result CAS / Packet / ResearchExecution) via intake's own
            # public API -- this module never reimplements that
            # verification, and any failure there (including intake's own
            # closed-layer integrity errors) is caught below and re-raised
            # in this module's own error type.
            reverified = self.intake_control.reverify(intake_id)
            if reverified != intake:
                raise ResearchExperimentSpecificationSourceError(
                    "accepted intake changed during reverification"
                )

            with self._connect() as db:
                row = db.execute(
                    """
                    SELECT h.*, f.definition_json AS family_definition_json,
                           f.holdout_consumed AS family_holdout_consumed,
                           f.terminal_state AS family_terminal_state
                    FROM hypotheses h
                    JOIN hypothesis_families f ON f.family_id = h.family_id
                    WHERE h.hypothesis_id = ?
                    """,
                    (intake.hypothesis_id,),
                ).fetchone()
            if row is None:
                raise ResearchExperimentSpecificationSourceError(
                    "accepted intake's hypothesis is missing from the registry"
                )
            hypothesis = HypothesisRegistry._row(row)
            if hypothesis.hypothesis_id != intake.hypothesis_id:
                raise ResearchExperimentSpecificationSourceError("hypothesis identity mismatch")
            if hypothesis.hypothesis_family_id != row["family_id"]:
                raise ResearchExperimentSpecificationSourceError(
                    "hypothesis family binding mismatch"
                )
            if hypothesis.state is not HypothesisState.PROPOSED:
                raise ResearchExperimentSpecificationSourceError(
                    "bound hypothesis is not exactly PROPOSED"
                )
            if hypothesis.manifest_hash is not None:
                raise ResearchExperimentSpecificationSourceError(
                    "bound hypothesis already has a manifest_hash"
                )
            if row["family_terminal_state"] is not None:
                raise ResearchExperimentSpecificationSourceError("hypothesis family is terminal")
            if int(row["family_holdout_consumed"]):
                raise ResearchExperimentSpecificationSourceError(
                    "hypothesis family has already consumed final holdout"
                )
            frozen_family_definition = parse_json(row["family_definition_json"])
            if not isinstance(frozen_family_definition, Mapping):
                raise TypeError("persisted family_definition must be a JSON object")
            # parse_json above is the strict-canonical-JSON validation gate;
            # json.loads below yields a plain, mutable structure compatible
            # with derive_hypothesis_family_id's own json.dumps -- the exact
            # dual-parse pattern trademind.research_proposal_intake already
            # uses for the same reason.
            family_definition = json.loads(row["family_definition_json"])
            if not isinstance(family_definition, dict):
                raise TypeError("persisted family_definition must decode to a dict")
            if derive_hypothesis_family_id(family_definition) != hypothesis.hypothesis_family_id:
                raise ResearchExperimentSpecificationSourceError(
                    "family_definition does not derive the bound hypothesis_family_id"
                )
        except ResearchExperimentSpecificationSourceError:
            raise
        except Exception as exc:
            raise ResearchExperimentSpecificationSourceError(
                "authoritative accepted intake / PROPOSED hypothesis failed verification"
            ) from exc

        return _VerifiedSpecificationSource(
            intake=reverified, hypothesis=hypothesis, family_definition=family_definition
        )

    @classmethod
    def _from_row(cls, row: sqlite3.Row) -> ResearchExperimentSpecificationV1:
        if row["schema_version"] != RESEARCH_EXPERIMENT_SPECIFICATION_SCHEMA_VERSION:
            raise ResearchExperimentSpecificationDatabaseError(
                "unsupported persisted specification version"
            )
        hypothesis_id = row["hypothesis_id"]
        if type(hypothesis_id) is not str or _INTAKE_ID.fullmatch(hypothesis_id) is None:
            raise ResearchExperimentSpecificationDatabaseError("persisted hypothesis_id is invalid")
        content_hash = cls._sha256_hex(row["content_hash"], field="content_hash")
        specification_id = row["specification_id"]
        if specification_id != cls._specification_id(hypothesis_id, content_hash):
            raise ResearchExperimentSpecificationDatabaseError(
                "persisted specification_id is not derived from its content"
            )
        try:
            frozen_family_definition = parse_json(row["family_definition_json"])
            frozen_parameters = parse_json(row["parameters_json"])
            if not isinstance(frozen_family_definition, Mapping) or not isinstance(
                frozen_parameters, Mapping
            ):
                raise TypeError
            # Plain, fully JSON-native structures (not the frozen
            # mappingproxy/tuple values parse_json returns above) -- this is
            # what makes spec.family_definition/spec.parameters directly
            # reusable, unmodified, by a future ExperimentManifest.new()
            # call, which itself uses plain json.dumps internally.
            family_definition = json.loads(row["family_definition_json"])
            parameters = json.loads(row["parameters_json"])
            if not isinstance(family_definition, dict) or not isinstance(parameters, dict):
                raise TypeError
            raw_datasets = parse_json(row["datasets_json"])
            if type(raw_datasets) is not tuple:
                raise TypeError
            datasets = tuple(
                DatasetArtifact(
                    file_path=str(item["file_path"]),
                    sha256=str(item["sha256"]),
                    size_bytes=int(item["size_bytes"]),
                )
                for item in raw_datasets
            )
        except (ProvenanceError, TypeError, KeyError, ValueError) as exc:
            raise ResearchExperimentSpecificationDatabaseError(
                "persisted specification content failed strict parsing"
            ) from exc

        expected_content_hash = cls._content_hash(
            cls._content_payload(
                hypothesis_id=hypothesis_id,
                test_family=row["test_family"],
                primary_metric=row["primary_metric"],
                alpha=row["alpha"],
                q=row["q"],
                minimum_effect_size=row["minimum_effect_size"],
                max_hypotheses_tests=row["max_hypotheses_tests"],
                parameters=parameters,
                datasets=datasets,
            )
        )
        if expected_content_hash != content_hash:
            raise ResearchExperimentSpecificationDatabaseError(
                "persisted specification content_hash does not match its content"
            )

        return ResearchExperimentSpecificationV1(
            specification_id=specification_id,
            hypothesis_id=hypothesis_id,
            hypothesis_family_id=row["hypothesis_family_id"],
            hypothesis_content_hash=cls._sha256_hex(
                row["hypothesis_content_hash"], field="hypothesis_content_hash"
            ),
            family_definition=family_definition,
            intake_id=cls._intake_id(row["intake_id"]),
            request_hash=cls._request_hash(row["request_hash"]),
            authorization_id=cls._positive_id(row["authorization_id"], field="authorization_id"),
            task_id=str(row["task_id"]),
            task_revision=cls._positive_id(row["task_revision"], field="task_revision"),
            packet_artifact_hash_ref=cls._hash_ref(
                row["packet_artifact_hash_ref"], field="packet_artifact_hash_ref"
            ),
            packet_semantic_hash=cls._hash_ref(
                row["packet_semantic_hash"], field="packet_semantic_hash"
            ),
            result_artifact_hash_ref=cls._hash_ref(
                row["result_artifact_hash_ref"], field="result_artifact_hash_ref"
            ),
            proposal_index=int(row["proposal_index"]),
            candidate_id=str(row["candidate_id"]),
            reviewer_id=cls._reviewer_id(row["reviewer_id"]),
            test_family=str(row["test_family"]),
            primary_metric=str(row["primary_metric"]),
            alpha=float(row["alpha"]),
            q=float(row["q"]),
            minimum_effect_size=float(row["minimum_effect_size"]),
            max_hypotheses_tests=int(row["max_hypotheses_tests"]),
            parameters=parameters,
            datasets=datasets,
            content_hash=content_hash,
            created_at=cls._timestamp(row["created_at"], field="created_at"),
        )

    @staticmethod
    def _request_hash(value: object) -> str:
        if type(value) is not str or _REQUEST_HASH.fullmatch(value) is None:
            raise ResearchExperimentSpecificationDatabaseError("persisted request_hash is invalid")
        return value

    # --- public API. ---

    def create_specification(
        self,
        intake_id: str,
        *,
        reviewer_id: str,
        test_family: str,
        primary_metric: str,
        alpha: float,
        q: float,
        minimum_effect_size: float,
        max_hypotheses_tests: int,
        datasets: tuple[DatasetArtifact, ...],
        parameters: Mapping[str, object] | None = None,
    ) -> ResearchExperimentSpecificationV1:
        """Trusted, immutable-on-creation experiment specification for one
        accepted, still-PROPOSED hypothesis.

        Every technical field is a mandatory explicit keyword argument
        supplied by the trusted caller; none is derived from the proposal's
        own content. Idempotent: an exact repeat (same intake_id, same
        resulting content) returns the existing record unchanged. A repeat
        with different content for a hypothesis that already has a
        specification fails closed with
        ``ResearchExperimentSpecificationConflict``.
        """
        source = self._verified_specification_source(intake_id)
        reviewer_id = self._reviewer_id(reviewer_id)
        test_family = self._text_field(test_family, field="test_family")
        primary_metric = self._text_field(primary_metric, field="primary_metric")
        alpha = self._unit_interval(alpha, field="alpha")
        q = self._unit_interval(q, field="q")
        minimum_effect_size = self._non_negative_float(
            minimum_effect_size, field="minimum_effect_size"
        )
        max_hypotheses_tests = self._positive_int(
            max_hypotheses_tests, field="max_hypotheses_tests"
        )
        datasets = self._datasets(datasets)
        parameters = self._parameters(parameters)

        hypothesis_id = source.hypothesis.hypothesis_id
        content_payload = self._content_payload(
            hypothesis_id=hypothesis_id,
            test_family=test_family,
            primary_metric=primary_metric,
            alpha=alpha,
            q=q,
            minimum_effect_size=minimum_effect_size,
            max_hypotheses_tests=max_hypotheses_tests,
            parameters=parameters,
            datasets=datasets,
        )
        content_hash = self._content_hash(content_payload)
        specification_id = self._specification_id(hypothesis_id, content_hash)
        timestamp = datetime.now(timezone.utc).isoformat()

        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._validate_schema(db)
            self.hypothesis_registry._validate_transaction_connection(db)
            self._reverify_source_in_transaction(db, source)

            existing_row = db.execute(
                "SELECT * FROM research_experiment_specifications WHERE hypothesis_id=?",
                (hypothesis_id,),
            ).fetchone()
            if existing_row is not None:
                existing = self._from_row(existing_row)
                if existing.specification_id == specification_id:
                    return existing
                raise ResearchExperimentSpecificationConflict(
                    "hypothesis already has a different immutable experiment specification"
                )

            db.execute(
                """
                INSERT INTO research_experiment_specifications(
                    specification_id, schema_version, hypothesis_id, hypothesis_family_id,
                    hypothesis_content_hash, family_definition_json, intake_id, request_hash,
                    authorization_id, task_id, task_revision, packet_artifact_hash_ref,
                    packet_semantic_hash, result_artifact_hash_ref, proposal_index,
                    candidate_id, reviewer_id, test_family, primary_metric, alpha, q,
                    minimum_effect_size, max_hypotheses_tests, parameters_json, datasets_json,
                    content_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    specification_id,
                    RESEARCH_EXPERIMENT_SPECIFICATION_SCHEMA_VERSION,
                    hypothesis_id,
                    source.hypothesis.hypothesis_family_id,
                    source.hypothesis.content_hash,
                    canonical_json_bytes(source.family_definition).decode("utf-8"),
                    source.intake.intake_id,
                    source.intake.request_hash,
                    source.intake.authorization_id,
                    source.intake.task_id,
                    source.intake.task_revision,
                    source.intake.packet_artifact_hash_ref,
                    source.intake.packet_semantic_hash,
                    source.intake.result_artifact_hash_ref,
                    source.intake.proposal_index,
                    source.intake.candidate_id,
                    reviewer_id,
                    test_family,
                    primary_metric,
                    alpha,
                    q,
                    minimum_effect_size,
                    max_hypotheses_tests,
                    canonical_json_bytes(parameters).decode("utf-8"),
                    canonical_json_bytes(
                        [asdict(item) for item in datasets]
                    ).decode("utf-8"),
                    content_hash,
                    timestamp,
                ),
            )
            AuditLog.append_in_transaction(
                db,
                AuditEvent(
                    timestamp=timestamp,
                    task_id=source.intake.task_id,
                    revision=source.intake.task_revision,
                    actor_role=Role.OPERATOR,
                    action="RESEARCH_EXPERIMENT_SPECIFICATION_CREATED",
                    input_artifact_hashes=(
                        source.intake.packet_artifact_hash_ref,
                        source.intake.result_artifact_hash_ref,
                    ),
                    from_state=TaskState.NEW,
                    to_state=TaskState.NEW,
                    policy_result=PolicyDecision.AUTO_ALLOWED_WITH_AUDIT,
                    metadata={
                        "specification_id": specification_id,
                        "hypothesis_id": hypothesis_id,
                        "intake_id": source.intake.intake_id,
                        "reviewer_id": reviewer_id,
                        "test_family": test_family,
                        "primary_metric": primary_metric,
                        "content_hash": content_hash,
                        "manifest_created": False,
                        "manifest_frozen": False,
                        "hypothesis_state_after": HypothesisState.PROPOSED.value,
                    },
                ),
            )
            row = db.execute(
                "SELECT * FROM research_experiment_specifications WHERE specification_id=?",
                (specification_id,),
            ).fetchone()
            if row is None:
                raise ResearchExperimentSpecificationDatabaseError(
                    "specification insert was not visible"
                )
            created = self._from_row(row)

        self._assert_hypothesis_unchanged(source.hypothesis)
        return created

    def _reverify_source_in_transaction(
        self, db: sqlite3.Connection, source: _VerifiedSpecificationSource
    ) -> None:
        row = db.execute(
            """
            SELECT h.*, f.definition_json AS family_definition_json,
                   f.holdout_consumed AS family_holdout_consumed,
                   f.terminal_state AS family_terminal_state
            FROM hypotheses h
            JOIN hypothesis_families f ON f.family_id = h.family_id
            WHERE h.hypothesis_id = ?
            """,
            (source.hypothesis.hypothesis_id,),
        ).fetchone()
        if row is None:
            raise ResearchExperimentSpecificationSourceError(
                "hypothesis vanished before specification commit"
            )
        current = HypothesisRegistry._row(row)
        if current != source.hypothesis:
            raise ResearchExperimentSpecificationSourceError(
                "bound hypothesis changed before specification commit"
            )
        if row["family_terminal_state"] is not None:
            raise ResearchExperimentSpecificationSourceError(
                "hypothesis family became terminal before specification commit"
            )
        if int(row["family_holdout_consumed"]):
            raise ResearchExperimentSpecificationSourceError(
                "hypothesis family consumed final holdout before specification commit"
            )
        intake_row = db.execute(
            "SELECT * FROM research_proposal_intakes WHERE intake_id=?",
            (source.intake.intake_id,),
        ).fetchone()
        if intake_row is None or intake_row["state"] != ResearchProposalIntakeState.ACCEPTED_FOR_HYPOTHESIS.value:
            raise ResearchExperimentSpecificationSourceError(
                "accepted intake changed before specification commit"
            )

    def _assert_hypothesis_unchanged(self, before: HypothesisRecord) -> None:
        after = self.hypothesis_registry.get(before.hypothesis_id)
        if after.state is not HypothesisState.PROPOSED or after.manifest_hash is not None:
            raise ResearchExperimentSpecificationDatabaseError(
                "hypothesis advanced past PROPOSED during specification creation"
            )

    def get(self, specification_id: str) -> ResearchExperimentSpecificationV1 | None:
        if type(specification_id) is not str or _SPECIFICATION_ID.fullmatch(specification_id) is None:
            raise ValueError("specification_id is not a Research Experiment Specification v1 identity")
        with self._connect() as db:
            self._validate_schema(db)
            row = db.execute(
                "SELECT * FROM research_experiment_specifications WHERE specification_id=?",
                (specification_id,),
            ).fetchone()
            return self._from_row(row) if row is not None else None

    def get_for_hypothesis(self, hypothesis_id: str) -> ResearchExperimentSpecificationV1 | None:
        hypothesis_id = self._intake_id(hypothesis_id)
        with self._connect() as db:
            self._validate_schema(db)
            row = db.execute(
                "SELECT * FROM research_experiment_specifications WHERE hypothesis_id=?",
                (hypothesis_id,),
            ).fetchone()
            return self._from_row(row) if row is not None else None

    def reverify(self, specification_id: str) -> ResearchExperimentSpecificationV1:
        """Reverify a persisted specification's own content self-consistency
        and its upstream hypothesis/intake binding against the live registry."""
        specification = self.get(specification_id)
        if specification is None:
            raise KeyError(specification_id)
        source = self._verified_specification_source(specification.intake_id)
        if source.hypothesis.hypothesis_id != specification.hypothesis_id:
            raise ResearchExperimentSpecificationSourceError(
                "specification no longer matches its authoritative hypothesis binding"
            )
        if source.family_definition != dict(specification.family_definition):
            raise ResearchExperimentSpecificationSourceError(
                "specification family_definition no longer matches the registry"
            )
        return specification


__all__ = [
    "MAX_DATASETS",
    "MAX_PARAMETERS_BYTES",
    "MAX_REVIEWER_ID_LENGTH",
    "RESEARCH_EXPERIMENT_SPECIFICATION_SCHEMA_VERSION",
    "ResearchExperimentSpecificationConflict",
    "ResearchExperimentSpecificationControl",
    "ResearchExperimentSpecificationDatabaseError",
    "ResearchExperimentSpecificationError",
    "ResearchExperimentSpecificationSourceError",
    "ResearchExperimentSpecificationV1",
]
