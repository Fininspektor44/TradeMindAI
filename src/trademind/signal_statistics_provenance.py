"""Strict immutable provenance primitives for signal-statistics v2.

This module is deliberately independent from report, packet, orchestration, and
Git-process concerns. Callers supply already resolved code provenance and explicit
semantic hash projections; this module validates, freezes, and hashes them.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from types import MappingProxyType
from typing import TypeAlias

CODE_PROVENANCE_SCHEMA_VERSION = "signal-statistics-code-provenance-v1"
CANDIDATE_DEFINITION_SCHEMA_VERSION = "signal-statistics-candidate-definition-v2"
CANDIDATE_CONTENT_SCHEMA_VERSION = "signal-statistics-candidate-content-v2"
NUMERIC_CANONICALIZATION_VERSION = "trademind-provenance-numeric-v1"

MAX_JSON_DEPTH = 32
MAX_JSON_NODES = 10_000
MAX_JSON_MAPPING_ENTRIES = 1_024
MAX_JSON_SEQUENCE_LENGTH = 4_096
MAX_JSON_STRING_LENGTH = 65_536
MAX_JSON_TOTAL_STRING_BYTES = 196_608
MAX_JSON_INTEGER_ABS = 9_007_199_254_740_991
MAX_CANONICAL_JSON_BYTES = 262_144

JsonPrimitive: TypeAlias = None | bool | int | float | str
JsonValue: TypeAlias = (
    JsonPrimitive | Mapping[str, "JsonValue"] | list["JsonValue"] | tuple["JsonValue", ...]
)
FrozenJsonValue: TypeAlias = (
    JsonPrimitive | Mapping[str, "FrozenJsonValue"] | tuple["FrozenJsonValue", ...]
)

_SHA256_REF = re.compile(r"^sha256:[0-9a-f]{64}$")
_FULL_GIT_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_MACHINE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_REVISION_SOURCES = frozenset({"git_worktree", "embedded_build"})
_CANDIDATE_DEFINITION_HASH_DOMAIN = b"trademind:signal-statistics:candidate-definition:v2"
_CANDIDATE_CONTENT_HASH_DOMAIN = b"trademind:signal-statistics:candidate-content:v2"
_REPORT_CONTENT_HASH_DOMAIN = b"trademind:signal-statistics:report-content:v2"
_PACKET_CONTENT_HASH_DOMAIN = b"trademind:signal-statistics:packet-content:v2"


class ProvenanceError(ValueError):
    """Raised when provenance data is not strict, canonical, or bounded."""


@dataclass(slots=True)
class _ValidationState:
    nodes: int = 0
    string_bytes: int = 0

    def consume_node(self, *, path: str) -> None:
        self.nodes += 1
        if self.nodes > MAX_JSON_NODES:
            raise ProvenanceError(f"{path} exceeds maximum JSON node count {MAX_JSON_NODES}")

    def consume_string(self, value: str, *, path: str) -> None:
        if len(value) > MAX_JSON_STRING_LENGTH:
            raise ProvenanceError(
                f"{path} exceeds maximum JSON string length {MAX_JSON_STRING_LENGTH}"
            )
        try:
            encoded = value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ProvenanceError(f"{path} is not valid UTF-8 text") from exc
        self.string_bytes += len(encoded)
        if self.string_bytes > MAX_JSON_TOTAL_STRING_BYTES:
            raise ProvenanceError(
                f"{path} exceeds maximum aggregate JSON string bytes {MAX_JSON_TOTAL_STRING_BYTES}"
            )


def _freeze(
    value: object,
    *,
    path: str,
    depth: int,
    state: _ValidationState,
    active_containers: set[int],
) -> FrozenJsonValue:
    if depth > MAX_JSON_DEPTH:
        raise ProvenanceError(f"{path} exceeds maximum JSON depth {MAX_JSON_DEPTH}")
    state.consume_node(path=path)

    if value is None or type(value) is bool:
        return value
    if type(value) is int:
        if abs(value) > MAX_JSON_INTEGER_ABS:
            raise ProvenanceError(
                f"{path} exceeds maximum JSON integer magnitude {MAX_JSON_INTEGER_ABS}"
            )
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ProvenanceError(f"{path} contains a non-finite float")
        if value == 0.0:
            return 0.0
        return value
    if type(value) is str:
        state.consume_string(value, path=path)
        return value
    if isinstance(value, (bytes, bytearray, memoryview)):
        raise ProvenanceError(f"{path} contains unsupported binary data")

    if isinstance(value, Mapping):
        identity = id(value)
        if identity in active_containers:
            raise ProvenanceError(f"{path} contains a recursive mapping")
        active_containers.add(identity)
        try:
            snapshot: list[tuple[str, object]] = []
            seen: set[str] = set()
            try:
                for key, nested in value.items():
                    if len(snapshot) >= MAX_JSON_MAPPING_ENTRIES:
                        raise ProvenanceError(
                            f"{path} exceeds maximum mapping entries {MAX_JSON_MAPPING_ENTRIES}"
                        )
                    if type(key) is not str:
                        raise ProvenanceError(f"{path} mapping keys must be exact strings")
                    if key in seen:
                        raise ProvenanceError(f"{path} contains duplicate mapping key {key!r}")
                    state.consume_string(key, path=f"{path}.<key>")
                    seen.add(key)
                    snapshot.append((key, nested))
            except ProvenanceError:
                raise
            except Exception as exc:
                raise ProvenanceError(f"{path} mapping snapshot failed") from exc

            frozen = {
                key: _freeze(
                    nested,
                    path=f"{path}.{key}",
                    depth=depth + 1,
                    state=state,
                    active_containers=active_containers,
                )
                for key, nested in sorted(snapshot, key=lambda item: item[0])
            }
            return MappingProxyType(frozen)
        finally:
            active_containers.remove(identity)

    if isinstance(value, (list, tuple)):
        identity = id(value)
        if identity in active_containers:
            raise ProvenanceError(f"{path} contains a recursive sequence")
        active_containers.add(identity)
        try:
            frozen_items: list[FrozenJsonValue] = []
            try:
                for index, nested in enumerate(value):
                    if index >= MAX_JSON_SEQUENCE_LENGTH:
                        raise ProvenanceError(
                            f"{path} exceeds maximum sequence length {MAX_JSON_SEQUENCE_LENGTH}"
                        )
                    frozen_items.append(
                        _freeze(
                            nested,
                            path=f"{path}[{index}]",
                            depth=depth + 1,
                            state=state,
                            active_containers=active_containers,
                        )
                    )
            except ProvenanceError:
                raise
            except Exception as exc:
                raise ProvenanceError(f"{path} sequence snapshot failed") from exc
            return tuple(frozen_items)
        finally:
            active_containers.remove(identity)

    raise ProvenanceError(f"{path} contains unsupported JSON type {type(value).__name__}")


def _json_compatible(value: FrozenJsonValue) -> JsonValue:
    if isinstance(value, Mapping):
        return {key: _json_compatible(nested) for key, nested in value.items()}
    if isinstance(value, tuple):
        return [_json_compatible(nested) for nested in value]
    return value


def _canonical_float(value: float) -> bytes:
    """Encode an exact finite binary64 value using numeric canonicalization v1.

    The representation is fixed-point and never delegates number formatting to
    the JSON encoder. Signed zero is normalized, while the trailing ``.0`` keeps
    integral floats distinct from exact integers.
    """
    if value == 0.0:
        return b"0.0"
    rendered = format(Decimal.from_float(value), "f")
    if "." not in rendered:
        rendered = f"{rendered}.0"
    return rendered.encode("ascii")


@dataclass(slots=True)
class _CanonicalWriter:
    parts: list[bytes] = field(default_factory=list)
    size: int = 0

    def append(self, value: bytes) -> None:
        next_size = self.size + len(value)
        if next_size > MAX_CANONICAL_JSON_BYTES:
            raise ProvenanceError(
                f"payload exceeds maximum canonical JSON bytes {MAX_CANONICAL_JSON_BYTES}"
            )
        self.parts.append(value)
        self.size = next_size


def _write_frozen(value: FrozenJsonValue, writer: _CanonicalWriter) -> None:
    if value is None:
        writer.append(b"null")
        return
    if type(value) is bool:
        writer.append(b"true" if value else b"false")
        return
    if type(value) is int:
        writer.append(str(value).encode("ascii"))
        return
    if type(value) is float:
        writer.append(_canonical_float(value))
        return
    if type(value) is str:
        try:
            encoded = json.dumps(value, ensure_ascii=False).encode("utf-8")
        except (TypeError, ValueError, UnicodeEncodeError) as exc:
            raise ProvenanceError("validated provenance string could not be serialized") from exc
        writer.append(encoded)
        return
    if isinstance(value, Mapping):
        writer.append(b"{")
        for index, key in enumerate(sorted(value)):
            if index:
                writer.append(b",")
            _write_frozen(key, writer)
            writer.append(b":")
            _write_frozen(value[key], writer)
        writer.append(b"}")
        return
    if isinstance(value, tuple):
        writer.append(b"[")
        for index, nested in enumerate(value):
            if index:
                writer.append(b",")
            _write_frozen(nested, writer)
        writer.append(b"]")
        return
    raise ProvenanceError("validated provenance JSON contains an unsupported frozen value")


def _encode_frozen(value: FrozenJsonValue) -> bytes:
    writer = _CanonicalWriter()
    _write_frozen(value, writer)
    return b"".join(writer.parts)


def _freeze_snapshot(value: object, *, field_name: str) -> FrozenJsonValue:
    if type(field_name) is not str or not field_name:
        raise ProvenanceError("field_name must be a non-empty string")
    return _freeze(
        value,
        path=field_name,
        depth=0,
        state=_ValidationState(),
        active_containers=set(),
    )


def freeze_json(value: object, *, field_name: str = "payload") -> FrozenJsonValue:
    """Take one detached, deeply immutable, bounded JSON snapshot."""
    frozen = _freeze_snapshot(value, field_name=field_name)
    _encode_frozen(frozen)
    return frozen


def freeze_json_object(
    value: object,
    *,
    field_name: str = "payload",
) -> Mapping[str, FrozenJsonValue]:
    """Freeze a JSON object root; scalar and array roots are rejected."""
    frozen = freeze_json(value, field_name=field_name)
    if not isinstance(frozen, Mapping):
        raise ProvenanceError(f"{field_name} must be a JSON object")
    return frozen


def canonical_json_bytes(value: object) -> bytes:
    """Return deterministic UTF-8 bytes for one strict bounded JSON value."""
    return _encode_frozen(_freeze_snapshot(value, field_name="payload"))


def parse_json(value: str | bytes) -> FrozenJsonValue:
    """Parse strict bounded JSON and reject duplicate keys and extensions."""
    if type(value) is bytes:
        if len(value) > MAX_CANONICAL_JSON_BYTES:
            raise ProvenanceError(
                f"payload exceeds maximum canonical JSON bytes {MAX_CANONICAL_JSON_BYTES}"
            )
        try:
            text = value.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ProvenanceError("payload must be valid UTF-8") from exc
    elif type(value) is str:
        try:
            encoded_size = len(value.encode("utf-8"))
        except UnicodeEncodeError as exc:
            raise ProvenanceError("payload must be valid UTF-8") from exc
        if encoded_size > MAX_CANONICAL_JSON_BYTES:
            raise ProvenanceError(
                f"payload exceeds maximum canonical JSON bytes {MAX_CANONICAL_JSON_BYTES}"
            )
        text = value
    else:
        raise ProvenanceError("encoded JSON must be an exact str or bytes value")

    def reject_constant(constant: str) -> None:
        raise ProvenanceError(f"payload contains non-standard JSON constant {constant}")

    def strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, nested in pairs:
            if key in result:
                raise ProvenanceError(f"payload contains duplicate JSON key {key!r}")
            result[key] = nested
        return result

    try:
        parsed = json.loads(
            text,
            parse_constant=reject_constant,
            object_pairs_hook=strict_object,
        )
    except ProvenanceError:
        raise
    except (ValueError, RecursionError) as exc:
        raise ProvenanceError("payload contains malformed JSON") from exc
    return freeze_json(parsed)


def validate_sha256_ref(value: object) -> str:
    """Return an exact lowercase SHA-256 reference or fail closed."""
    if type(value) is not str or _SHA256_REF.fullmatch(value) is None:
        raise ProvenanceError("value must be sha256:<64 lowercase hex>")
    return value


def sha256_bytes(value: bytes) -> str:
    """Hash exact bytes using the canonical provenance reference format."""
    if type(value) is not bytes:
        raise ProvenanceError("sha256_bytes requires exact bytes")
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


def _domain_hash(domain: bytes, projection: object) -> str:
    digest = hashlib.sha256()
    digest.update(domain)
    digest.update(b"\x00")
    digest.update(canonical_json_bytes(projection))
    return f"sha256:{digest.hexdigest()}"


def _strict_machine_identifier(value: object, *, field_name: str) -> str:
    if type(value) is not str or _MACHINE_IDENTIFIER.fullmatch(value) is None:
        raise ProvenanceError(f"{field_name} must be a non-empty stable ASCII machine identifier")
    return value


def _strict_fields(
    payload: Mapping[str, object],
    *,
    required: frozenset[str],
    name: str,
) -> None:
    fields = frozenset(payload)
    missing = required - fields
    if missing:
        raise ProvenanceError(f"{name} is missing fields: {', '.join(sorted(missing))}")
    unknown = fields - required
    if unknown:
        raise ProvenanceError(f"{name} contains unknown fields: {', '.join(sorted(unknown))}")


@dataclass(frozen=True, slots=True)
class CodeProvenance:
    """A validated provenance claim, not an attestation.

    Construction validates only field form and allowed values. It does not prove
    that a commit exists, a worktree is clean, or an embedded build is trusted.
    A trusted external build/CLI boundary must establish those facts before
    injecting this claim; this core deliberately performs no Git, filesystem, or
    environment lookup.
    """

    producer_name: str
    producer_version: str
    git_commit: str
    revision_source: str
    tree_state: str = "CLEAN"
    schema_version: str = CODE_PROVENANCE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _strict_machine_identifier(self.producer_name, field_name="producer_name")
        _strict_machine_identifier(self.producer_version, field_name="producer_version")
        if (
            type(self.schema_version) is not str
            or self.schema_version != CODE_PROVENANCE_SCHEMA_VERSION
        ):
            raise ProvenanceError("unsupported code provenance schema_version")
        if type(self.git_commit) is not str or _FULL_GIT_COMMIT.fullmatch(self.git_commit) is None:
            raise ProvenanceError("git_commit must be 40 lowercase hexadecimal characters")
        if type(self.revision_source) is not str or self.revision_source not in _REVISION_SOURCES:
            raise ProvenanceError("unsupported revision_source")
        if type(self.tree_state) is not str or self.tree_state != "CLEAN":
            raise ProvenanceError("tree_state must be CLEAN")
        canonical_json_bytes(self.to_payload())

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "producer_name": self.producer_name,
            "producer_version": self.producer_version,
            "git_commit": self.git_commit,
            "revision_source": self.revision_source,
            "tree_state": self.tree_state,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> CodeProvenance:
        frozen = freeze_json_object(payload, field_name="code_provenance")
        required = frozenset(
            {
                "schema_version",
                "producer_name",
                "producer_version",
                "git_commit",
                "revision_source",
                "tree_state",
            }
        )
        _strict_fields(frozen, required=required, name="code provenance")
        return cls(
            schema_version=frozen["schema_version"],
            producer_name=frozen["producer_name"],
            producer_version=frozen["producer_version"],
            git_commit=frozen["git_commit"],
            revision_source=frozen["revision_source"],
            tree_state=frozen["tree_state"],
        )


@dataclass(frozen=True, slots=True)
class CandidateDefinitionV2:
    source_kind: str
    source_namespace: str
    symbol: str
    timeframe: str
    feature: str
    horizon: int
    action_scope: str
    evaluation_method_version: str
    schema_version: str = CANDIDATE_DEFINITION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if (
            type(self.schema_version) is not str
            or self.schema_version != CANDIDATE_DEFINITION_SCHEMA_VERSION
        ):
            raise ProvenanceError("unsupported candidate definition schema_version")
        for field_name in (
            "source_kind",
            "source_namespace",
            "symbol",
            "timeframe",
            "feature",
            "action_scope",
            "evaluation_method_version",
        ):
            _strict_machine_identifier(getattr(self, field_name), field_name=field_name)
        if type(self.horizon) is not int or self.horizon < 1:
            raise ProvenanceError("horizon must be an exact positive integer")
        if self.horizon > MAX_JSON_INTEGER_ABS:
            raise ProvenanceError("horizon exceeds maximum JSON integer magnitude")
        canonical_json_bytes(self.to_payload())

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "source_kind": self.source_kind,
            "source_namespace": self.source_namespace,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "feature": self.feature,
            "horizon": self.horizon,
            "action_scope": self.action_scope,
            "evaluation_method_version": self.evaluation_method_version,
        }

    @property
    def candidate_id(self) -> str:
        digest = _domain_hash(
            _CANDIDATE_DEFINITION_HASH_DOMAIN,
            self.to_payload(),
        ).removeprefix("sha256:")
        return f"ssc-v2-{digest}"

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> CandidateDefinitionV2:
        frozen = freeze_json_object(payload, field_name="candidate_definition")
        required = frozenset(
            {
                "schema_version",
                "source_kind",
                "source_namespace",
                "symbol",
                "timeframe",
                "feature",
                "horizon",
                "action_scope",
                "evaluation_method_version",
            }
        )
        _strict_fields(frozen, required=required, name="candidate definition")
        return cls(
            schema_version=frozen["schema_version"],
            source_kind=frozen["source_kind"],
            source_namespace=frozen["source_namespace"],
            symbol=frozen["symbol"],
            timeframe=frozen["timeframe"],
            feature=frozen["feature"],
            horizon=frozen["horizon"],
            action_scope=frozen["action_scope"],
            evaluation_method_version=frozen["evaluation_method_version"],
        )


@dataclass(frozen=True, slots=True)
class CandidateContentV2:
    """Immutable candidate evidence with set semantics for reason codes.

    Reason-code order has no meaning. Duplicates are rejected and accepted codes
    are sorted before serialization so equal sets always have equal content hashes.
    """

    candidate_definition: CandidateDefinitionV2
    evaluation_policy_hash: str
    metrics: Mapping[str, FrozenJsonValue]
    status: str
    reason_codes: tuple[str, ...]
    schema_version: str = CANDIDATE_CONTENT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if type(self.candidate_definition) is not CandidateDefinitionV2:
            raise ProvenanceError("candidate_definition must be CandidateDefinitionV2")
        if (
            type(self.schema_version) is not str
            or self.schema_version != CANDIDATE_CONTENT_SCHEMA_VERSION
        ):
            raise ProvenanceError("unsupported candidate content schema_version")
        validate_sha256_ref(self.evaluation_policy_hash)
        _strict_machine_identifier(self.status, field_name="status")
        if type(self.reason_codes) is not tuple:
            raise ProvenanceError("reason_codes must be an immutable tuple")
        seen: set[str] = set()
        for code in self.reason_codes:
            _strict_machine_identifier(code, field_name="reason_code")
            if code in seen:
                raise ProvenanceError(f"duplicate reason_code: {code}")
            seen.add(code)
        object.__setattr__(self, "reason_codes", tuple(sorted(self.reason_codes)))
        frozen_metrics = freeze_json_object(self.metrics, field_name="metrics")
        object.__setattr__(self, "metrics", frozen_metrics)
        canonical_json_bytes(self.to_payload())

    @property
    def candidate_id(self) -> str:
        return self.candidate_definition.candidate_id

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "candidate_definition": self.candidate_definition.to_payload(),
            "evaluation_policy_hash": self.evaluation_policy_hash,
            "metrics": _json_compatible(self.metrics),
            "status": self.status,
            "reason_codes": list(self.reason_codes),
        }

    @property
    def content_hash(self) -> str:
        return _domain_hash(_CANDIDATE_CONTENT_HASH_DOMAIN, self.to_payload())

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> CandidateContentV2:
        frozen = freeze_json_object(payload, field_name="candidate_content")
        required = frozenset(
            {
                "schema_version",
                "candidate_definition",
                "evaluation_policy_hash",
                "metrics",
                "status",
                "reason_codes",
            }
        )
        _strict_fields(frozen, required=required, name="candidate content")
        definition = frozen["candidate_definition"]
        metrics = frozen["metrics"]
        reason_codes = frozen["reason_codes"]
        if not isinstance(definition, Mapping):
            raise ProvenanceError("candidate_definition must be a JSON object")
        if not isinstance(metrics, Mapping):
            raise ProvenanceError("metrics must be a JSON object")
        if type(reason_codes) is not tuple or not all(type(code) is str for code in reason_codes):
            raise ProvenanceError("reason_codes must be an array of exact strings")
        return cls(
            schema_version=frozen["schema_version"],
            candidate_definition=CandidateDefinitionV2.from_payload(definition),
            evaluation_policy_hash=frozen["evaluation_policy_hash"],
            metrics=metrics,
            status=frozen["status"],
            reason_codes=reason_codes,
        )


@dataclass(frozen=True, slots=True)
class ReportContentHashProjection:
    """Explicit semantic report projection prepared by a future report v2 builder."""

    content: Mapping[str, FrozenJsonValue]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "content",
            freeze_json_object(self.content, field_name="report_content_projection"),
        )


@dataclass(frozen=True, slots=True)
class PacketContentHashProjection:
    """Explicit semantic packet projection prepared by a future packet v2 builder."""

    content: Mapping[str, FrozenJsonValue]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "content",
            freeze_json_object(self.content, field_name="packet_content_projection"),
        )


def report_content_hash(projection: ReportContentHashProjection) -> str:
    if type(projection) is not ReportContentHashProjection:
        raise ProvenanceError("projection must be ReportContentHashProjection")
    return _domain_hash(_REPORT_CONTENT_HASH_DOMAIN, projection.content)


def packet_content_hash(projection: PacketContentHashProjection) -> str:
    if type(projection) is not PacketContentHashProjection:
        raise ProvenanceError("projection must be PacketContentHashProjection")
    return _domain_hash(_PACKET_CONTENT_HASH_DOMAIN, projection.content)


def task_id_from_packet_hash(packet_hash: object) -> str:
    """Derive a safe task ID without truncating the packet digest."""
    validated = validate_sha256_ref(packet_hash)
    return f"signal-stats-v2-{validated.removeprefix('sha256:')}"
