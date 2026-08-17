"""Immutable experiment manifests for Discovery Engine research."""

from __future__ import annotations

import hashlib
import io
import json
import math
import os
import re
import tempfile
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any

from trademind.orchestrator.artifact_store import ArtifactRef, ArtifactStore
from trademind.signal_statistics_provenance import (
    MAX_JSON_INTEGER_ABS,
    CodeProvenance,
    FrozenJsonValue,
    ProvenanceError,
    canonical_json_bytes,
    freeze_json_object,
    parse_json,
    sha256_bytes,
    validate_sha256_ref,
)

from .data_layer import sha256_file
from .hypothesis_registry import derive_content_hash, derive_hypothesis_family_id
from .split_engine import SplitPlan


class ManifestIntegrityError(RuntimeError):
    pass


def _canonical(payload: Any) -> str:
    try:
        return json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("manifest fields must be canonical JSON data") from exc


def _canonical_mapping(payload: Mapping[str, Any], *, label: str, allow_empty: bool) -> str:
    if not isinstance(payload, Mapping) or (not payload and not allow_empty):
        qualifier = "mapping" if allow_empty else "non-empty mapping"
        raise ValueError(f"{label} must be a {qualifier}")
    return _canonical(dict(payload))


def _content_definition(
    *,
    family_definition: dict[str, Any],
    test_family: str,
    primary_metric: str,
    parameters: dict[str, Any],
) -> dict[str, Any]:
    return {
        "family_definition": family_definition,
        "test_family": test_family,
        "primary_metric": primary_metric,
        "parameters": parameters,
    }


@dataclass(frozen=True, slots=True)
class DatasetArtifact:
    file_path: str
    sha256: str
    size_bytes: int

    @classmethod
    def from_path(cls, path: str | Path) -> "DatasetArtifact":
        source = Path(path)
        if not source.is_file():
            raise FileNotFoundError(source)
        return cls(
            file_path=str(source),
            sha256=sha256_file(source),
            size_bytes=source.stat().st_size,
        )

    def verify(self) -> None:
        source = Path(self.file_path)
        if not source.is_file():
            raise ManifestIntegrityError(f"dataset artifact missing: {source}")
        if source.stat().st_size != self.size_bytes:
            raise ManifestIntegrityError(f"dataset artifact size changed: {source}")
        actual = sha256_file(source)
        if actual != self.sha256:
            raise ManifestIntegrityError(f"dataset artifact hash changed: {source}")


@dataclass(frozen=True, slots=True)
class ExperimentManifest:
    hypothesis_id: str
    hypothesis_family_id: str
    content_hash: str
    test_family: str
    primary_metric: str
    alpha: float
    q: float
    minimum_effect_size: float
    max_hypotheses_tests: int
    schema_version: str
    git_commit: str
    datasets: tuple[DatasetArtifact, ...]
    _family_definition_json: str
    _parameters_json: str
    created_at: str

    @classmethod
    def new(
        cls,
        *,
        hypothesis_id: str,
        family_definition: Mapping[str, Any],
        test_family: str,
        primary_metric: str,
        alpha: float,
        q: float,
        minimum_effect_size: float,
        max_hypotheses_tests: int,
        schema_version: str,
        git_commit: str,
        datasets: tuple[DatasetArtifact, ...],
        parameters: Mapping[str, Any] | None = None,
    ) -> "ExperimentManifest":
        values = {
            "hypothesis_id": hypothesis_id,
            "test_family": test_family,
            "primary_metric": primary_metric,
            "schema_version": schema_version,
            "git_commit": git_commit,
        }
        if any(not value.strip() for value in values.values()):
            raise ValueError("manifest identifiers and metric/version fields must not be empty")
        if not 0 < alpha <= 1 or not 0 < q <= 1:
            raise ValueError("alpha and q must be in (0, 1]")
        if minimum_effect_size < 0:
            raise ValueError("minimum_effect_size must be non-negative")
        if max_hypotheses_tests < 1:
            raise ValueError("max_hypotheses_tests must be positive")
        if not datasets:
            raise ValueError("at least one dataset artifact is required")

        family_json = _canonical_mapping(
            family_definition,
            label="family_definition",
            allow_empty=False,
        )
        parameters_json = _canonical_mapping(
            parameters or {},
            label="parameters",
            allow_empty=True,
        )
        family_payload = json.loads(family_json)
        parameter_payload = json.loads(parameters_json)
        family_id = derive_hypothesis_family_id(family_payload)
        content_hash = derive_content_hash(
            _content_definition(
                family_definition=family_payload,
                test_family=test_family.strip(),
                primary_metric=primary_metric.strip(),
                parameters=parameter_payload,
            )
        )
        return cls(
            hypothesis_id=hypothesis_id.strip(),
            hypothesis_family_id=family_id,
            content_hash=content_hash,
            test_family=test_family.strip(),
            primary_metric=primary_metric.strip(),
            alpha=float(alpha),
            q=float(q),
            minimum_effect_size=float(minimum_effect_size),
            max_hypotheses_tests=int(max_hypotheses_tests),
            schema_version=schema_version.strip(),
            git_commit=git_commit.strip(),
            datasets=tuple(datasets),
            _family_definition_json=family_json,
            _parameters_json=parameters_json,
            created_at=datetime.now(timezone.utc).isoformat(),
        )

    @property
    def family_definition(self) -> dict[str, Any]:
        return json.loads(self._family_definition_json)

    @property
    def parameters(self) -> dict[str, Any]:
        return json.loads(self._parameters_json)

    @property
    def content_definition(self) -> dict[str, Any]:
        return _content_definition(
            family_definition=self.family_definition,
            test_family=self.test_family,
            primary_metric=self.primary_metric,
            parameters=self.parameters,
        )

    def payload(self) -> dict[str, Any]:
        return {
            "hypothesis_id": self.hypothesis_id,
            "hypothesis_family_id": self.hypothesis_family_id,
            "content_hash": self.content_hash,
            "family_definition": self.family_definition,
            "test_family": self.test_family,
            "primary_metric": self.primary_metric,
            "alpha": self.alpha,
            "q": self.q,
            "minimum_effect_size": self.minimum_effect_size,
            "max_hypotheses_tests": self.max_hypotheses_tests,
            "schema_version": self.schema_version,
            "git_commit": self.git_commit,
            "datasets": [asdict(item) for item in self.datasets],
            "parameters": self.parameters,
            "created_at": self.created_at,
        }

    @property
    def manifest_hash(self) -> str:
        return hashlib.sha256(_canonical(self.payload()).encode("utf-8")).hexdigest()

    def freeze(self, path: str | Path) -> str:
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            raise FileExistsError(f"frozen manifest already exists: {destination}")
        for artifact in self.datasets:
            artifact.verify()
        document = {"manifest_hash": self.manifest_hash, "manifest": self.payload()}
        encoded = (_canonical(document) + "\n").encode("utf-8")
        fd, temporary_name = tempfile.mkstemp(prefix=".manifest-", dir=destination.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
        finally:
            if temporary.exists():
                temporary.unlink()
        return self.manifest_hash


def verify_frozen_manifest(
    path: str | Path,
    *,
    expected_manifest_hash: str,
    verify_datasets: bool = True,
) -> dict[str, Any]:
    """Verify a frozen manifest against an externally persisted registry hash."""
    expected_manifest_hash = expected_manifest_hash.strip().lower()
    if len(expected_manifest_hash) != 64 or any(
        char not in "0123456789abcdef" for char in expected_manifest_hash
    ):
        raise ValueError("expected_manifest_hash must be a SHA-256 hex digest")

    source = Path(path)
    try:
        document = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ManifestIntegrityError(f"cannot read frozen manifest: {source}") from exc
    if not isinstance(document, dict) or not isinstance(document.get("manifest"), dict):
        raise ManifestIntegrityError("invalid frozen manifest document")

    payload = document["manifest"]
    stored = document.get("manifest_hash")
    actual = hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()
    if stored != actual:
        raise ManifestIntegrityError("frozen manifest self-hash mismatch")
    if actual != expected_manifest_hash:
        raise ManifestIntegrityError(
            "frozen manifest does not match externally frozen manifest_hash"
        )

    try:
        family_definition = payload["family_definition"]
        if not isinstance(family_definition, dict) or not family_definition:
            raise TypeError
        parameters = payload["parameters"]
        if not isinstance(parameters, dict):
            raise TypeError
        derived_family_id = derive_hypothesis_family_id(family_definition)
        derived_content_hash = derive_content_hash(
            _content_definition(
                family_definition=family_definition,
                test_family=str(payload["test_family"]),
                primary_metric=str(payload["primary_metric"]),
                parameters=parameters,
            )
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ManifestIntegrityError("manifest identity fields are invalid") from exc
    if payload.get("hypothesis_family_id") != derived_family_id:
        raise ManifestIntegrityError(
            "manifest hypothesis_family_id is not derived from family_definition"
        )
    if payload.get("content_hash") != derived_content_hash:
        raise ManifestIntegrityError(
            "manifest content_hash is not derived from frozen hypothesis content"
        )

    raw_datasets = payload.get("datasets")
    if not isinstance(raw_datasets, list) or not raw_datasets:
        raise ManifestIntegrityError("manifest must contain at least one dataset artifact")
    if verify_datasets:
        for raw in raw_datasets:
            try:
                artifact = DatasetArtifact(
                    file_path=str(raw["file_path"]),
                    sha256=str(raw["sha256"]),
                    size_bytes=int(raw["size_bytes"]),
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise ManifestIntegrityError("invalid dataset artifact in manifest") from exc
            artifact.verify()
    return document

# ExperimentManifest v2 is an additive scientific contract. The legacy classes
# and functions above deliberately retain their historical hashes and path-based
# persistence behavior for existing discovery consumers.
EXPERIMENT_MANIFEST_V2_SCHEMA_VERSION = "experiment-manifest-v2"
EXPERIMENT_MANIFEST_V2_MEDIA_TYPE = "application/vnd.trademind.experiment-manifest-v2+json"
PROPOSAL_INTAKE_PROVENANCE_SCHEMA_VERSION = "experiment-manifest-proposal-intake-provenance-v1"
EVALUATION_CRITERIA_SCHEMA_VERSION = "experiment-evaluation-criteria-v1"
TRADING_FRICTION_SCHEMA_VERSION = "experiment-trading-friction-v1"
FINAL_HOLDOUT_CRITERIA_SCHEMA_VERSION = "experiment-final-holdout-criteria-v1"

MAX_MANIFEST_DATASETS = 32
MAX_MANIFEST_CRITERIA = 16
MAX_MANIFEST_PARAMETERS = 64
MAX_MANIFEST_IDENTIFIER_LENGTH = 256
MAX_MANIFEST_TESTS = 1_000_000
MAX_FRICTION_VALUE = 1_000_000.0

_MANIFEST_V2_HASH_DOMAIN = b"trademind:discovery:experiment-manifest:v2"
_FINAL_HOLDOUT_CRITERIA_HASH_DOMAIN = b"trademind:discovery:final-holdout-criteria:v1"
_MACHINE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$")
_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
_FAMILY_ID = re.compile(r"^hf_[0-9a-f]{64}$")
_CANDIDATE_ID = re.compile(r"^ssc-v2-[0-9a-f]{64}$")
_INTAKE_ID = re.compile(r"^rpi-v1:sha256:[0-9a-f]{64}:[0-7]$")


class ManifestV2ValidationError(ValueError):
    """Raised when a V2 scientific manifest is malformed or inconsistent."""


class ManifestV2PersistenceError(RuntimeError):
    """Raised when Verified CAS does not preserve the exact V2 artifact identity."""


def _v2_exact_fields(
    payload: Mapping[str, object],
    *,
    required: frozenset[str],
    field_name: str,
) -> None:
    fields = frozenset(payload)
    missing = required - fields
    unknown = fields - required
    if missing:
        raise ManifestV2ValidationError(
            f"{field_name} is missing fields: {', '.join(sorted(missing))}"
        )
    if unknown:
        raise ManifestV2ValidationError(
            f"{field_name} contains unknown fields: {', '.join(sorted(unknown))}"
        )


def _v2_machine_identifier(value: object, *, field_name: str) -> str:
    if type(value) is not str or _MACHINE_IDENTIFIER.fullmatch(value) is None:
        raise ManifestV2ValidationError(
            f"{field_name} must be a bounded stable ASCII machine identifier"
        )
    return value


def _v2_audit_identity(value: object, *, field_name: str) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or len(value) > MAX_MANIFEST_IDENTIFIER_LENGTH
        or any(ord(character) <= 0x1F or 0x7F <= ord(character) <= 0x9F for character in value)
    ):
        raise ManifestV2ValidationError(f"{field_name} must be a bounded audit identity")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ManifestV2ValidationError(f"{field_name} must be valid UTF-8") from exc
    return value


def _v2_sha256_hex(value: object, *, field_name: str) -> str:
    if type(value) is not str or _SHA256_HEX.fullmatch(value) is None:
        raise ManifestV2ValidationError(f"{field_name} must be 64 lowercase hexadecimal digits")
    return value


def _v2_positive_integer(
    value: object,
    *,
    field_name: str,
    maximum: int = MAX_JSON_INTEGER_ABS,
) -> int:
    if type(value) is not int or not 1 <= value <= maximum:
        raise ManifestV2ValidationError(
            f"{field_name} must be an exact positive integer no greater than {maximum}"
        )
    return value


def _v2_finite_float(
    value: object,
    *,
    field_name: str,
    minimum: float,
    maximum: float,
    minimum_exclusive: bool = False,
) -> float:
    if type(value) is not float or not math.isfinite(value):
        raise ManifestV2ValidationError(f"{field_name} must be an exact finite float")
    if minimum_exclusive:
        valid = minimum < value <= maximum
    else:
        valid = minimum <= value <= maximum
    if not valid:
        boundary = "(" if minimum_exclusive else "["
        raise ManifestV2ValidationError(f"{field_name} must be in {boundary}{minimum}, {maximum}]")
    return value


def _v2_threshold(value: object) -> int | float:
    if type(value) is int:
        if abs(value) > MAX_JSON_INTEGER_ABS:
            raise ManifestV2ValidationError("criterion threshold integer is outside JSON bounds")
        return value
    if type(value) is float and math.isfinite(value):
        return value
    raise ManifestV2ValidationError("criterion threshold must be an exact finite int or float")


def _v2_utc_timestamp(value: object, *, field_name: str) -> str:
    if type(value) is not str:
        raise ManifestV2ValidationError(f"{field_name} must be an exact UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ManifestV2ValidationError(f"{field_name} must be a canonical UTC timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ManifestV2ValidationError(f"{field_name} must include the UTC timezone")
    if parsed.isoformat() != value:
        raise ManifestV2ValidationError(
            f"{field_name} must use canonical datetime.isoformat() encoding"
        )
    return value


def _v2_json_compatible(value: FrozenJsonValue) -> object:
    if isinstance(value, Mapping):
        return {key: _v2_json_compatible(nested) for key, nested in value.items()}
    if isinstance(value, tuple):
        return [_v2_json_compatible(nested) for nested in value]
    return value


def _v2_semantic_hash(projection: Mapping[str, object]) -> str:
    return sha256_bytes(_MANIFEST_V2_HASH_DOMAIN + b"\x00" + canonical_json_bytes(projection))


@dataclass(frozen=True, slots=True)
class DatasetArtifactV2:
    """One immutable dataset role bound only by Verified CAS identity."""

    role: str
    artifact_hash_ref: str
    media_type: str
    size_bytes: int

    def __post_init__(self) -> None:
        _v2_machine_identifier(self.role, field_name="dataset.role")
        try:
            validate_sha256_ref(self.artifact_hash_ref)
        except ProvenanceError as exc:
            raise ManifestV2ValidationError("dataset artifact_hash_ref is invalid") from exc
        if (
            type(self.media_type) is not str
            or not self.media_type
            or len(self.media_type) > 255
            or self.media_type != self.media_type.strip()
        ):
            raise ManifestV2ValidationError("dataset media_type must be bounded exact ASCII")
        try:
            encoded = self.media_type.encode("ascii")
        except UnicodeEncodeError as exc:
            raise ManifestV2ValidationError("dataset media_type must be ASCII") from exc
        if any(byte < 0x20 or byte > 0x7E for byte in encoded):
            raise ManifestV2ValidationError("dataset media_type contains unsafe characters")
        _v2_positive_integer(self.size_bytes, field_name="dataset.size_bytes")
        canonical_json_bytes(self.to_payload())

    def to_payload(self) -> dict[str, object]:
        return {
            "role": self.role,
            "artifact_hash_ref": self.artifact_hash_ref,
            "media_type": self.media_type,
            "size_bytes": self.size_bytes,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> DatasetArtifactV2:
        _v2_exact_fields(
            payload,
            required=frozenset({"role", "artifact_hash_ref", "media_type", "size_bytes"}),
            field_name="dataset",
        )
        return cls(
            role=payload["role"],
            artifact_hash_ref=payload["artifact_hash_ref"],
            media_type=payload["media_type"],
            size_bytes=payload["size_bytes"],
        )

    def verify(self, artifact_store: ArtifactStore) -> ArtifactRef:
        if not isinstance(artifact_store, ArtifactStore):
            raise TypeError("artifact_store must be ArtifactStore")
        artifact = artifact_store.resolve_verified(
            self.artifact_hash_ref,
            expected_media_type=self.media_type,
        )
        if artifact.size_bytes != self.size_bytes:
            raise ManifestV2ValidationError("dataset CAS metadata size does not match manifest")
        return artifact


@dataclass(frozen=True, slots=True)
class ProposalIntakeProvenanceV1:
    """Validated lineage claim from Proposal Intake to the source Packet/result.

    Construction proves field consistency, not that the intake is accepted or
    that the referenced execution/Packet exists. The future trusted creation
    boundary must reverify those records before injecting this claim.
    """

    intake_id: str
    execution_request_hash: str
    authorization_id: int
    task_id: str
    task_revision: int
    packet_artifact_hash_ref: str
    packet_semantic_hash: str
    result_artifact_hash_ref: str
    proposal_index: int
    candidate_id: str
    schema_version: str = PROPOSAL_INTAKE_PROVENANCE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != PROPOSAL_INTAKE_PROVENANCE_SCHEMA_VERSION:
            raise ManifestV2ValidationError("unsupported proposal provenance schema_version")
        if type(self.intake_id) is not str or _INTAKE_ID.fullmatch(self.intake_id) is None:
            raise ManifestV2ValidationError(
                "intake_id is not a Research Proposal Intake v1 identity"
            )
        _v2_sha256_hex(self.execution_request_hash, field_name="execution_request_hash")
        _v2_positive_integer(self.authorization_id, field_name="authorization_id")
        _v2_machine_identifier(self.task_id, field_name="task_id")
        _v2_positive_integer(self.task_revision, field_name="task_revision")
        for field_name in (
            "packet_artifact_hash_ref",
            "packet_semantic_hash",
            "result_artifact_hash_ref",
        ):
            try:
                validate_sha256_ref(getattr(self, field_name))
            except ProvenanceError as exc:
                raise ManifestV2ValidationError(f"{field_name} is invalid") from exc
        if type(self.proposal_index) is not int or not 0 <= self.proposal_index <= 7:
            raise ManifestV2ValidationError("proposal_index is outside Proposal Response v1 bounds")
        expected_intake_id = f"rpi-v1:{self.result_artifact_hash_ref}:{self.proposal_index}"
        if self.intake_id != expected_intake_id:
            raise ManifestV2ValidationError(
                "intake_id does not match result artifact and proposal index"
            )
        if type(self.candidate_id) is not str or _CANDIDATE_ID.fullmatch(self.candidate_id) is None:
            raise ManifestV2ValidationError(
                "candidate_id is not a Candidate Definition v2 identity"
            )
        canonical_json_bytes(self.to_payload())

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "intake_id": self.intake_id,
            "execution_request_hash": self.execution_request_hash,
            "authorization_id": self.authorization_id,
            "task_id": self.task_id,
            "task_revision": self.task_revision,
            "packet_artifact_hash_ref": self.packet_artifact_hash_ref,
            "packet_semantic_hash": self.packet_semantic_hash,
            "result_artifact_hash_ref": self.result_artifact_hash_ref,
            "proposal_index": self.proposal_index,
            "candidate_id": self.candidate_id,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> ProposalIntakeProvenanceV1:
        _v2_exact_fields(
            payload,
            required=frozenset(
                {
                    "schema_version",
                    "intake_id",
                    "execution_request_hash",
                    "authorization_id",
                    "task_id",
                    "task_revision",
                    "packet_artifact_hash_ref",
                    "packet_semantic_hash",
                    "result_artifact_hash_ref",
                    "proposal_index",
                    "candidate_id",
                }
            ),
            field_name="proposal_provenance",
        )
        return cls(
            schema_version=payload["schema_version"],
            intake_id=payload["intake_id"],
            execution_request_hash=payload["execution_request_hash"],
            authorization_id=payload["authorization_id"],
            task_id=payload["task_id"],
            task_revision=payload["task_revision"],
            packet_artifact_hash_ref=payload["packet_artifact_hash_ref"],
            packet_semantic_hash=payload["packet_semantic_hash"],
            result_artifact_hash_ref=payload["result_artifact_hash_ref"],
            proposal_index=payload["proposal_index"],
            candidate_id=payload["candidate_id"],
        )


class CriterionOperator(StrEnum):
    GREATER_THAN_OR_EQUAL = ">="
    GREATER_THAN = ">"
    LESS_THAN_OR_EQUAL = "<="
    LESS_THAN = "<"


class CriteriaMode(StrEnum):
    ALL = "ALL"
    ANY = "ANY"


@dataclass(frozen=True, slots=True)
class EvaluationCriterionV1:
    metric: str
    operator: CriterionOperator
    threshold: int | float

    def __post_init__(self) -> None:
        _v2_machine_identifier(self.metric, field_name="criterion.metric")
        if type(self.operator) is not CriterionOperator:
            raise ManifestV2ValidationError("criterion.operator must be CriterionOperator")
        _v2_threshold(self.threshold)
        canonical_json_bytes(self.to_payload())

    def to_payload(self) -> dict[str, object]:
        return {
            "metric": self.metric,
            "operator": self.operator.value,
            "threshold": self.threshold,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> EvaluationCriterionV1:
        _v2_exact_fields(
            payload,
            required=frozenset({"metric", "operator", "threshold"}),
            field_name="criterion",
        )
        try:
            operator = CriterionOperator(payload["operator"])
        except (TypeError, ValueError) as exc:
            raise ManifestV2ValidationError("unsupported criterion operator") from exc
        return cls(
            metric=payload["metric"],
            operator=operator,
            threshold=payload["threshold"],
        )


@dataclass(frozen=True, slots=True)
class EvaluationCriteriaV1:
    mode: CriteriaMode
    criteria: tuple[EvaluationCriterionV1, ...]
    schema_version: str = EVALUATION_CRITERIA_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != EVALUATION_CRITERIA_SCHEMA_VERSION:
            raise ManifestV2ValidationError("unsupported evaluation criteria schema_version")
        if type(self.mode) is not CriteriaMode:
            raise ManifestV2ValidationError("criteria mode must be CriteriaMode")
        if type(self.criteria) is not tuple or not 1 <= len(self.criteria) <= MAX_MANIFEST_CRITERIA:
            raise ManifestV2ValidationError(
                f"criteria must contain between 1 and {MAX_MANIFEST_CRITERIA} entries"
            )
        if not all(type(item) is EvaluationCriterionV1 for item in self.criteria):
            raise ManifestV2ValidationError("criteria contains an invalid criterion")
        identities = [canonical_json_bytes(item.to_payload()) for item in self.criteria]
        if len(set(identities)) != len(identities):
            raise ManifestV2ValidationError("criteria contains duplicate entries")
        ordered = tuple(
            sorted(self.criteria, key=lambda item: canonical_json_bytes(item.to_payload()))
        )
        object.__setattr__(self, "criteria", ordered)
        canonical_json_bytes(self.to_payload())

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "mode": self.mode.value,
            "criteria": [item.to_payload() for item in self.criteria],
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> EvaluationCriteriaV1:
        _v2_exact_fields(
            payload,
            required=frozenset({"schema_version", "mode", "criteria"}),
            field_name="evaluation_criteria",
        )
        try:
            mode = CriteriaMode(payload["mode"])
        except (TypeError, ValueError) as exc:
            raise ManifestV2ValidationError("unsupported evaluation criteria mode") from exc
        raw_criteria = payload["criteria"]
        if type(raw_criteria) is not tuple:
            raise ManifestV2ValidationError("criteria must be a JSON array")
        parsed: list[EvaluationCriterionV1] = []
        for index, item in enumerate(raw_criteria):
            if not isinstance(item, Mapping):
                raise ManifestV2ValidationError(f"criteria[{index}] must be a JSON object")
            parsed.append(EvaluationCriterionV1.from_payload(item))
        return cls(
            schema_version=payload["schema_version"],
            mode=mode,
            criteria=tuple(parsed),
        )


@dataclass(frozen=True, slots=True)
class FinalHoldoutCriteriaV1:
    """Explicit, stage-scoped decision contract for the FINAL HOLDOUT stage
    only.

    Structurally identical in shape to ``EvaluationCriteriaV1`` -- it reuses
    the same ``CriterionOperator``/``CriteriaMode``/``EvaluationCriterionV1``
    vocabulary, since ``>``, ``>=``, ``<``, ``<=``, ``ALL``, and ``ANY``
    carry no stage-specific meaning -- but is a SEPARATE field, a SEPARATE
    schema, and a SEPARATE Python type from ``evaluation_criteria``. The two
    are never interchangeable, and building or reading one never assumes or
    derives the other. This is the only manifest-level declaration of which
    metric names a ``HoldoutEvaluator`` (discovery/holdout_runner.py) is
    contractually required to return, and the threshold rule that governs
    them; it does not itself apply that rule to anything -- consuming it to
    compute a final research verdict is the explicitly out-of-scope
    responsibility of a future Final Verdict / Acceptance Control layer.
    ``verify_final_holdout_metric_vocabulary`` below is the one piece of
    consumption logic this module provides: a pure structural check that
    observed metrics satisfy the declared NAME vocabulary, never an
    evaluation of the operators/thresholds themselves and never a verdict.

    ``ExperimentManifestV2.alpha``/``q``/``minimum_effect_size``/
    ``max_hypotheses_tests`` are validation-stage significance/effect-size
    controls (consumed today only by ``ValidationExecutionControl``) and are
    NOT implicitly reapplied to the final-holdout decision by this contract
    or by anything that reads it. A manifest that wants an equivalent
    significance/effect-size gate at the holdout stage too must express it
    explicitly as one or more criteria here -- reusing those validation-stage
    numeric fields a second time, unchanged, for an entirely different
    metric space would silently assume a correspondence this contract makes
    no promise of.
    """

    mode: CriteriaMode
    criteria: tuple[EvaluationCriterionV1, ...]
    schema_version: str = FINAL_HOLDOUT_CRITERIA_SCHEMA_VERSION
    semantic_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != FINAL_HOLDOUT_CRITERIA_SCHEMA_VERSION:
            raise ManifestV2ValidationError(
                "unsupported final holdout criteria schema_version"
            )
        if type(self.mode) is not CriteriaMode:
            raise ManifestV2ValidationError("final holdout criteria mode must be CriteriaMode")
        if type(self.criteria) is not tuple or not 1 <= len(self.criteria) <= MAX_MANIFEST_CRITERIA:
            raise ManifestV2ValidationError(
                f"final holdout criteria must contain between 1 and {MAX_MANIFEST_CRITERIA} entries"
            )
        if not all(type(item) is EvaluationCriterionV1 for item in self.criteria):
            raise ManifestV2ValidationError("final holdout criteria contains an invalid criterion")
        identities = [canonical_json_bytes(item.to_payload()) for item in self.criteria]
        if len(set(identities)) != len(identities):
            raise ManifestV2ValidationError("final holdout criteria contains duplicate entries")
        ordered = tuple(
            sorted(self.criteria, key=lambda item: canonical_json_bytes(item.to_payload()))
        )
        object.__setattr__(self, "criteria", ordered)
        object.__setattr__(
            self,
            "semantic_hash",
            sha256_bytes(
                _FINAL_HOLDOUT_CRITERIA_HASH_DOMAIN
                + b"\x00"
                + canonical_json_bytes(self._semantic_payload())
            ),
        )
        canonical_json_bytes(self.to_payload())

    def _semantic_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "mode": self.mode.value,
            "criteria": [item.to_payload() for item in self.criteria],
        }

    @property
    def required_metric_names(self) -> frozenset[str]:
        """The closed set of metric names a HoldoutEvaluator must return for
        this contract to be checkable at all."""
        return frozenset(item.metric for item in self.criteria)

    def to_payload(self) -> dict[str, object]:
        payload = self._semantic_payload()
        payload["semantic_hash"] = self.semantic_hash
        return payload

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> FinalHoldoutCriteriaV1:
        _v2_exact_fields(
            payload,
            required=frozenset({"schema_version", "mode", "criteria", "semantic_hash"}),
            field_name="final_holdout_criteria",
        )
        try:
            mode = CriteriaMode(payload["mode"])
        except (TypeError, ValueError) as exc:
            raise ManifestV2ValidationError("unsupported final holdout criteria mode") from exc
        raw_criteria = payload["criteria"]
        if type(raw_criteria) is not tuple:
            raise ManifestV2ValidationError("final holdout criteria.criteria must be a JSON array")
        parsed: list[EvaluationCriterionV1] = []
        for index, item in enumerate(raw_criteria):
            if not isinstance(item, Mapping):
                raise ManifestV2ValidationError(
                    f"final_holdout_criteria.criteria[{index}] must be a JSON object"
                )
            parsed.append(EvaluationCriterionV1.from_payload(item))
        built = cls(
            schema_version=payload["schema_version"],
            mode=mode,
            criteria=tuple(parsed),
        )
        try:
            claimed_hash = validate_sha256_ref(payload["semantic_hash"])
        except ProvenanceError as exc:
            raise ManifestV2ValidationError(
                "final_holdout_criteria.semantic_hash is invalid"
            ) from exc
        if claimed_hash != built.semantic_hash:
            raise ManifestV2ValidationError("final_holdout_criteria hash identity mismatch")
        return built


def verify_final_holdout_metric_vocabulary(
    criteria: FinalHoldoutCriteriaV1,
    metrics: Mapping[str, object],
) -> None:
    """Pure, structural check: every metric name ``criteria`` declares must
    be present in ``metrics`` (e.g. a ``HoldoutRunReceipt.aggregate_metrics``).

    Does not evaluate operators/thresholds and does not produce a verdict --
    applying the decision rule to compute a final research outcome remains
    the responsibility of a future Final Verdict / Acceptance Control layer.
    Fails closed (raises ``ManifestV2ValidationError``) if any required
    metric name is absent, exactly the failure mode a caller-forged or
    incomplete metrics mapping must trigger.
    """
    if type(criteria) is not FinalHoldoutCriteriaV1:
        raise ManifestV2ValidationError("criteria must be an exact FinalHoldoutCriteriaV1")
    if not isinstance(metrics, Mapping):
        raise ManifestV2ValidationError("metrics must be a mapping")
    missing = criteria.required_metric_names - frozenset(metrics)
    if missing:
        raise ManifestV2ValidationError(
            f"final holdout metrics are missing required names: {', '.join(sorted(missing))}"
        )


@dataclass(frozen=True, slots=True)
class TradingFrictionV1:
    """Provider-neutral predeclared friction assumptions in one explicit unit."""

    model_id: str
    unit: str
    spread: float
    commission: float
    slippage: float
    fees: float
    schema_version: str = TRADING_FRICTION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != TRADING_FRICTION_SCHEMA_VERSION:
            raise ManifestV2ValidationError("unsupported trading friction schema_version")
        _v2_machine_identifier(self.model_id, field_name="friction.model_id")
        _v2_machine_identifier(self.unit, field_name="friction.unit")
        for field_name in ("spread", "commission", "slippage", "fees"):
            _v2_finite_float(
                getattr(self, field_name),
                field_name=f"friction.{field_name}",
                minimum=0.0,
                maximum=MAX_FRICTION_VALUE,
            )
        canonical_json_bytes(self.to_payload())

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "model_id": self.model_id,
            "unit": self.unit,
            "spread": self.spread,
            "commission": self.commission,
            "slippage": self.slippage,
            "fees": self.fees,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> TradingFrictionV1:
        _v2_exact_fields(
            payload,
            required=frozenset(
                {
                    "schema_version",
                    "model_id",
                    "unit",
                    "spread",
                    "commission",
                    "slippage",
                    "fees",
                }
            ),
            field_name="trading_friction",
        )
        return cls(
            schema_version=payload["schema_version"],
            model_id=payload["model_id"],
            unit=payload["unit"],
            spread=payload["spread"],
            commission=payload["commission"],
            slippage=payload["slippage"],
            fees=payload["fees"],
        )


@dataclass(frozen=True, slots=True)
class ExperimentManifestV2:
    """Immutable pre-execution scientific specification.

    ``manifest_semantic_hash`` binds only the scientific projection. Diagnostic
    creation metadata is preserved in exact CAS bytes but cannot change that
    semantic identity. ``CodeProvenance`` remains a validated caller-supplied
    claim, not a Git or build attestation.

    ``created_by`` is audit identity, not an authentication credential. Building
    this value does not authorize execution or freeze a registry hypothesis.
    Test-family and metric identifiers are predeclared names, not proof that a
    future runner supports them; the trusted creation/execution boundary must
    bind them to its explicit supported vocabulary before running anything.

    ``evaluation_criteria`` (together with ``alpha``/``q``/
    ``minimum_effect_size``/``max_hypotheses_tests``) is the VALIDATION-stage
    decision contract, consumed by ``ValidationExecutionControl``. It is never
    assumed to also govern the final-holdout decision. ``final_holdout_criteria``
    (``FinalHoldoutCriteriaV1`` or ``None``) is the separate, explicit,
    optional contract for that later stage -- see its own docstring. A
    manifest with ``final_holdout_criteria=None`` declares no automated
    final-holdout decision rule at all; nothing in this dataclass infers one
    from ``evaluation_criteria`` in that case.
    """

    hypothesis_id: str
    hypothesis_family_id: str
    bound_hypothesis_content_hash: str
    proposal_provenance: ProposalIntakeProvenanceV1
    datasets: tuple[DatasetArtifactV2, ...]
    split_plan: SplitPlan
    split_dataset_role: str
    test_family: str
    primary_metric: str
    evaluation_criteria: EvaluationCriteriaV1
    alpha: float
    q: float
    minimum_effect_size: float
    max_hypotheses_tests: int
    trading_friction: TradingFrictionV1 | None
    deterministic_seed: int | None
    code_provenance: CodeProvenance
    semantic_parameters: Mapping[str, FrozenJsonValue]
    created_at: str
    created_by: str
    final_holdout_criteria: FinalHoldoutCriteriaV1 | None = None
    schema_version: str = EXPERIMENT_MANIFEST_V2_SCHEMA_VERSION
    manifest_semantic_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != EXPERIMENT_MANIFEST_V2_SCHEMA_VERSION:
            raise ManifestV2ValidationError("unsupported experiment manifest schema_version")
        if type(self.hypothesis_id) is not str or _INTAKE_ID.fullmatch(self.hypothesis_id) is None:
            raise ManifestV2ValidationError(
                "hypothesis_id must be the accepted Proposal Intake v1 identity"
            )
        if (
            type(self.hypothesis_family_id) is not str
            or _FAMILY_ID.fullmatch(self.hypothesis_family_id) is None
        ):
            raise ManifestV2ValidationError("hypothesis_family_id is invalid")
        _v2_sha256_hex(
            self.bound_hypothesis_content_hash,
            field_name="bound_hypothesis_content_hash",
        )
        if type(self.proposal_provenance) is not ProposalIntakeProvenanceV1:
            raise ManifestV2ValidationError(
                "proposal_provenance must be ProposalIntakeProvenanceV1"
            )
        if self.hypothesis_id != self.proposal_provenance.intake_id:
            raise ManifestV2ValidationError("hypothesis_id does not match proposal intake identity")

        if type(self.datasets) is not tuple or not 1 <= len(self.datasets) <= MAX_MANIFEST_DATASETS:
            raise ManifestV2ValidationError(
                f"datasets must contain between 1 and {MAX_MANIFEST_DATASETS} entries"
            )
        if not all(type(item) is DatasetArtifactV2 for item in self.datasets):
            raise ManifestV2ValidationError("datasets contains an invalid dataset binding")
        roles = [item.role for item in self.datasets]
        if len(set(roles)) != len(roles):
            raise ManifestV2ValidationError("dataset roles must be unique")
        object.__setattr__(
            self, "datasets", tuple(sorted(self.datasets, key=lambda item: item.role))
        )

        if type(self.split_plan) is not SplitPlan:
            raise ManifestV2ValidationError("split_plan must be SplitPlan")
        _v2_machine_identifier(self.split_dataset_role, field_name="split_dataset_role")
        if self.split_dataset_role not in roles:
            raise ManifestV2ValidationError(
                "split_dataset_role must identify one immutable dataset binding"
            )
        _v2_machine_identifier(self.test_family, field_name="test_family")
        _v2_machine_identifier(self.primary_metric, field_name="primary_metric")
        if type(self.evaluation_criteria) is not EvaluationCriteriaV1:
            raise ManifestV2ValidationError("evaluation_criteria must be EvaluationCriteriaV1")
        if not any(
            criterion.metric == self.primary_metric
            for criterion in self.evaluation_criteria.criteria
        ):
            raise ManifestV2ValidationError(
                "evaluation_criteria must predeclare the primary_metric threshold"
            )
        _v2_finite_float(
            self.alpha,
            field_name="alpha",
            minimum=0.0,
            maximum=1.0,
            minimum_exclusive=True,
        )
        _v2_finite_float(
            self.q,
            field_name="q",
            minimum=0.0,
            maximum=1.0,
            minimum_exclusive=True,
        )
        _v2_finite_float(
            self.minimum_effect_size,
            field_name="minimum_effect_size",
            minimum=0.0,
            maximum=MAX_FRICTION_VALUE,
        )
        _v2_positive_integer(
            self.max_hypotheses_tests,
            field_name="max_hypotheses_tests",
            maximum=MAX_MANIFEST_TESTS,
        )
        if (
            self.trading_friction is not None
            and type(self.trading_friction) is not TradingFrictionV1
        ):
            raise ManifestV2ValidationError("trading_friction must be TradingFrictionV1 or null")
        if (
            self.final_holdout_criteria is not None
            and type(self.final_holdout_criteria) is not FinalHoldoutCriteriaV1
        ):
            raise ManifestV2ValidationError(
                "final_holdout_criteria must be FinalHoldoutCriteriaV1 or null"
            )
        if self.deterministic_seed is not None and (
            type(self.deterministic_seed) is not int
            or not 0 <= self.deterministic_seed <= MAX_JSON_INTEGER_ABS
        ):
            raise ManifestV2ValidationError(
                "deterministic_seed must be null or an exact bounded non-negative integer"
            )
        if type(self.code_provenance) is not CodeProvenance:
            raise ManifestV2ValidationError("code_provenance must be CodeProvenance")
        try:
            frozen_parameters = freeze_json_object(
                self.semantic_parameters,
                field_name="semantic_parameters",
            )
        except ProvenanceError as exc:
            raise ManifestV2ValidationError(
                "semantic_parameters must be strict bounded JSON"
            ) from exc
        if len(frozen_parameters) > MAX_MANIFEST_PARAMETERS:
            raise ManifestV2ValidationError(
                f"semantic_parameters exceeds maximum keys {MAX_MANIFEST_PARAMETERS}"
            )
        object.__setattr__(self, "semantic_parameters", frozen_parameters)
        _v2_utc_timestamp(self.created_at, field_name="created_at")
        _v2_audit_identity(self.created_by, field_name="created_by")

        object.__setattr__(
            self,
            "manifest_semantic_hash",
            _v2_semantic_hash(self.semantic_projection()),
        )
        canonical_json_bytes(self.to_payload())

    def semantic_projection(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "read_only": True,
            "orders_enabled": False,
            "hypothesis": {
                "hypothesis_id": self.hypothesis_id,
                "hypothesis_family_id": self.hypothesis_family_id,
                "content_hash": self.bound_hypothesis_content_hash,
            },
            "proposal_provenance": self.proposal_provenance.to_payload(),
            "datasets": [item.to_payload() for item in self.datasets],
            "split": {
                "semantic_hash": self.split_plan.semantic_hash,
                "dataset_role": self.split_dataset_role,
                "plan": self.split_plan.to_payload(),
            },
            "experiment": {
                "test_family": self.test_family,
                "primary_metric": self.primary_metric,
                "evaluation_criteria": self.evaluation_criteria.to_payload(),
                "alpha": self.alpha,
                "q": self.q,
                "minimum_effect_size": self.minimum_effect_size,
                "max_hypotheses_tests": self.max_hypotheses_tests,
                "trading_friction": (
                    self.trading_friction.to_payload()
                    if self.trading_friction is not None
                    else None
                ),
                "deterministic_seed": self.deterministic_seed,
                "parameters": _v2_json_compatible(self.semantic_parameters),
            },
            "code_provenance": self.code_provenance.to_payload(),
            "final_holdout_criteria": (
                self.final_holdout_criteria.to_payload()
                if self.final_holdout_criteria is not None
                else None
            ),
        }

    def to_payload(self) -> dict[str, object]:
        payload = self.semantic_projection()
        payload["manifest_semantic_hash"] = self.manifest_semantic_hash
        payload["diagnostics"] = {
            "created_at": self.created_at,
            "created_by": self.created_by,
        }
        return payload

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_payload())

    def verify_datasets(self, artifact_store: ArtifactStore) -> None:
        for dataset in self.datasets:
            dataset.verify(artifact_store)

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> ExperimentManifestV2:
        try:
            frozen = freeze_json_object(payload, field_name="experiment_manifest_v2")
        except ProvenanceError as exc:
            raise ManifestV2ValidationError("manifest v2 must be strict bounded JSON") from exc
        _v2_exact_fields(
            frozen,
            required=frozenset(
                {
                    "schema_version",
                    "manifest_semantic_hash",
                    "read_only",
                    "orders_enabled",
                    "hypothesis",
                    "proposal_provenance",
                    "datasets",
                    "split",
                    "experiment",
                    "code_provenance",
                    "diagnostics",
                    "final_holdout_criteria",
                }
            ),
            field_name="experiment_manifest_v2",
        )
        if frozen["schema_version"] != EXPERIMENT_MANIFEST_V2_SCHEMA_VERSION:
            raise ManifestV2ValidationError("unsupported experiment manifest schema_version")
        if frozen["read_only"] is not True or frozen["orders_enabled"] is not False:
            raise ManifestV2ValidationError(
                "manifest v2 must be read-only with orders permanently disabled"
            )

        hypothesis = frozen["hypothesis"]
        provenance = frozen["proposal_provenance"]
        raw_datasets = frozen["datasets"]
        split = frozen["split"]
        experiment = frozen["experiment"]
        code_provenance = frozen["code_provenance"]
        diagnostics = frozen["diagnostics"]
        raw_final_holdout_criteria = frozen["final_holdout_criteria"]
        for field_name, value in (
            ("hypothesis", hypothesis),
            ("proposal_provenance", provenance),
            ("split", split),
            ("experiment", experiment),
            ("code_provenance", code_provenance),
            ("diagnostics", diagnostics),
        ):
            if not isinstance(value, Mapping):
                raise ManifestV2ValidationError(f"{field_name} must be a JSON object")
        if type(raw_datasets) is not tuple:
            raise ManifestV2ValidationError("datasets must be a JSON array")
        if raw_final_holdout_criteria is not None and not isinstance(
            raw_final_holdout_criteria, Mapping
        ):
            raise ManifestV2ValidationError(
                "final_holdout_criteria must be a JSON object or null"
            )

        _v2_exact_fields(
            hypothesis,
            required=frozenset({"hypothesis_id", "hypothesis_family_id", "content_hash"}),
            field_name="hypothesis",
        )
        _v2_exact_fields(
            split,
            required=frozenset({"semantic_hash", "dataset_role", "plan"}),
            field_name="split",
        )
        _v2_exact_fields(
            experiment,
            required=frozenset(
                {
                    "test_family",
                    "primary_metric",
                    "evaluation_criteria",
                    "alpha",
                    "q",
                    "minimum_effect_size",
                    "max_hypotheses_tests",
                    "trading_friction",
                    "deterministic_seed",
                    "parameters",
                }
            ),
            field_name="experiment",
        )
        _v2_exact_fields(
            diagnostics,
            required=frozenset({"created_at", "created_by"}),
            field_name="diagnostics",
        )
        raw_plan = split["plan"]
        raw_criteria = experiment["evaluation_criteria"]
        raw_friction = experiment["trading_friction"]
        raw_parameters = experiment["parameters"]
        if not isinstance(raw_plan, Mapping):
            raise ManifestV2ValidationError("split.plan must be a JSON object")
        if not isinstance(raw_criteria, Mapping):
            raise ManifestV2ValidationError("evaluation_criteria must be a JSON object")
        if raw_friction is not None and not isinstance(raw_friction, Mapping):
            raise ManifestV2ValidationError("trading_friction must be a JSON object or null")
        if not isinstance(raw_parameters, Mapping):
            raise ManifestV2ValidationError("experiment.parameters must be a JSON object")
        if not isinstance(provenance, Mapping) or not isinstance(code_provenance, Mapping):
            raise ManifestV2ValidationError("manifest provenance must be JSON objects")

        datasets: list[DatasetArtifactV2] = []
        for index, raw in enumerate(raw_datasets):
            if not isinstance(raw, Mapping):
                raise ManifestV2ValidationError(f"datasets[{index}] must be a JSON object")
            datasets.append(DatasetArtifactV2.from_payload(raw))
        plan = SplitPlan.from_payload(raw_plan)
        try:
            claimed_split_hash = validate_sha256_ref(split["semantic_hash"])
        except ProvenanceError as exc:
            raise ManifestV2ValidationError("split semantic_hash is invalid") from exc
        if claimed_split_hash != plan.semantic_hash:
            raise ManifestV2ValidationError("split semantic identity mismatch")
        try:
            claimed_manifest_hash = validate_sha256_ref(frozen["manifest_semantic_hash"])
        except ProvenanceError as exc:
            raise ManifestV2ValidationError("manifest_semantic_hash is invalid") from exc

        manifest = cls(
            schema_version=frozen["schema_version"],
            hypothesis_id=hypothesis["hypothesis_id"],
            hypothesis_family_id=hypothesis["hypothesis_family_id"],
            bound_hypothesis_content_hash=hypothesis["content_hash"],
            proposal_provenance=ProposalIntakeProvenanceV1.from_payload(provenance),
            datasets=tuple(datasets),
            split_plan=plan,
            split_dataset_role=split["dataset_role"],
            test_family=experiment["test_family"],
            primary_metric=experiment["primary_metric"],
            evaluation_criteria=EvaluationCriteriaV1.from_payload(raw_criteria),
            alpha=experiment["alpha"],
            q=experiment["q"],
            minimum_effect_size=experiment["minimum_effect_size"],
            max_hypotheses_tests=experiment["max_hypotheses_tests"],
            trading_friction=(
                TradingFrictionV1.from_payload(raw_friction)
                if isinstance(raw_friction, Mapping)
                else None
            ),
            deterministic_seed=experiment["deterministic_seed"],
            code_provenance=CodeProvenance.from_payload(code_provenance),
            semantic_parameters=raw_parameters,
            created_at=diagnostics["created_at"],
            created_by=diagnostics["created_by"],
            final_holdout_criteria=(
                FinalHoldoutCriteriaV1.from_payload(raw_final_holdout_criteria)
                if isinstance(raw_final_holdout_criteria, Mapping)
                else None
            ),
        )
        if manifest.manifest_semantic_hash != claimed_manifest_hash:
            raise ManifestV2ValidationError("manifest semantic identity mismatch")
        if canonical_json_bytes(frozen) != manifest.canonical_bytes():
            raise ManifestV2ValidationError("manifest payload is not in canonical semantic order")
        return manifest


def build_experiment_manifest_v2(
    *,
    artifact_store: ArtifactStore,
    hypothesis_id: str,
    hypothesis_family_id: str,
    bound_hypothesis_content_hash: str,
    proposal_provenance: ProposalIntakeProvenanceV1,
    datasets: tuple[DatasetArtifactV2, ...],
    split_plan: SplitPlan,
    split_dataset_role: str,
    test_family: str,
    primary_metric: str,
    evaluation_criteria: EvaluationCriteriaV1,
    alpha: float,
    q: float,
    minimum_effect_size: float,
    max_hypotheses_tests: int,
    trading_friction: TradingFrictionV1 | None,
    deterministic_seed: int | None,
    code_provenance: CodeProvenance,
    semantic_parameters: Mapping[str, object],
    created_at: str,
    created_by: str,
    final_holdout_criteria: FinalHoldoutCriteriaV1 | None = None,
) -> ExperimentManifestV2:
    """Build a V2 contract and verify dataset identities without reading rows.

    This pure prerequisite does not verify Proposal Intake acceptance, authorize
    execution, write audit, or freeze the hypothesis. Those controls belong to
    the future trusted Experiment Manifest Creation workflow.

    ``final_holdout_criteria`` defaults to ``None`` (no automated final-holdout
    decision rule declared) for backward compatibility with every existing
    caller; it is never derived from ``evaluation_criteria``.
    """
    manifest = ExperimentManifestV2(
        hypothesis_id=hypothesis_id,
        hypothesis_family_id=hypothesis_family_id,
        bound_hypothesis_content_hash=bound_hypothesis_content_hash,
        proposal_provenance=proposal_provenance,
        datasets=datasets,
        split_plan=split_plan,
        split_dataset_role=split_dataset_role,
        test_family=test_family,
        primary_metric=primary_metric,
        evaluation_criteria=evaluation_criteria,
        alpha=alpha,
        q=q,
        minimum_effect_size=minimum_effect_size,
        max_hypotheses_tests=max_hypotheses_tests,
        trading_friction=trading_friction,
        deterministic_seed=deterministic_seed,
        code_provenance=code_provenance,
        semantic_parameters=semantic_parameters,
        created_at=created_at,
        created_by=created_by,
        final_holdout_criteria=final_holdout_criteria,
    )
    manifest.verify_datasets(artifact_store)
    return manifest


def verify_experiment_manifest_v2(encoded: str | bytes) -> ExperimentManifestV2:
    """Verify strict canonical wire bytes and the domain-bound semantic identity."""
    if type(encoded) is bytes:
        exact_bytes = encoded
    elif type(encoded) is str:
        try:
            exact_bytes = encoded.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ManifestV2ValidationError("manifest v2 must be valid UTF-8") from exc
    else:
        raise ManifestV2ValidationError("manifest v2 wire payload must be exact str or bytes")
    try:
        parsed = parse_json(exact_bytes)
    except ProvenanceError as exc:
        raise ManifestV2ValidationError("manifest v2 wire payload is invalid") from exc
    if not isinstance(parsed, Mapping):
        raise ManifestV2ValidationError("manifest v2 root must be a JSON object")
    if canonical_json_bytes(parsed) != exact_bytes:
        raise ManifestV2ValidationError("manifest v2 wire payload must use canonical JSON bytes")
    return ExperimentManifestV2.from_payload(parsed)


def persist_experiment_manifest_v2(
    manifest: ExperimentManifestV2,
    *,
    artifact_store: ArtifactStore,
) -> ArtifactRef:
    """Verify dataset CAS bindings, then persist exact canonical manifest bytes."""
    if type(manifest) is not ExperimentManifestV2:
        raise ManifestV2ValidationError("manifest must be ExperimentManifestV2")
    manifest.verify_datasets(artifact_store)
    exact_bytes = manifest.canonical_bytes()
    artifact = artifact_store.import_snapshot(
        io.BytesIO(exact_bytes),
        media_type=EXPERIMENT_MANIFEST_V2_MEDIA_TYPE,
    )
    if artifact.hash_ref != sha256_bytes(exact_bytes):
        raise ManifestV2PersistenceError("Verified CAS returned an unexpected manifest hash")
    return artifact


def load_experiment_manifest_v2(
    artifact_hash_ref: str,
    *,
    artifact_store: ArtifactStore,
) -> ExperimentManifestV2:
    """Load exact manifest CAS bytes, reverify semantics, and verify every dataset CAS."""
    artifact = artifact_store.resolve_verified(
        artifact_hash_ref,
        expected_media_type=EXPERIMENT_MANIFEST_V2_MEDIA_TYPE,
    )
    exact_bytes = artifact_store.read_verified(
        artifact_hash_ref,
        expected_media_type=EXPERIMENT_MANIFEST_V2_MEDIA_TYPE,
    )
    manifest = verify_experiment_manifest_v2(exact_bytes)
    if artifact.hash_ref != sha256_bytes(manifest.canonical_bytes()):
        raise ManifestV2PersistenceError("manifest exact CAS identity mismatch")
    manifest.verify_datasets(artifact_store)
    return manifest
