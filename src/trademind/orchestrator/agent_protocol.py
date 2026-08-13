"""Vendor-neutral structured contract for orchestrator AI workers."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import KW_ONLY, dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Protocol, TypeAlias

from .models import Role


V1_SCHEMA_VERSION = "orchestrator-agent-v1"
V2_SCHEMA_VERSION = "orchestrator-agent-v2"
SCHEMA_VERSION = V2_SCHEMA_VERSION
SUPPORTED_SCHEMA_VERSIONS = frozenset({V1_SCHEMA_VERSION, V2_SCHEMA_VERSION})

# Discovery hypotheses are expected to be compact, human-auditable objects. These
# limits leave ample room for rationales and feature lists while bounding the CPU,
# memory, cache, and artifact cost of an untrusted future provider response.
MAX_JSON_DEPTH = 32
MAX_WIRE_JSON_DEPTH = MAX_JSON_DEPTH + 8
MAX_JSON_NODES = 10_000
MAX_JSON_MAPPING_ENTRIES = 1_024
MAX_JSON_SEQUENCE_LENGTH = 4_096
MAX_JSON_STRING_LENGTH = 65_536
MAX_JSON_TOTAL_STRING_BYTES = 262_144
MAX_JSON_INTEGER_ABS = 9_007_199_254_740_991  # Largest interoperable IEEE-754 integer.
MAX_STRUCTURED_JSON_BYTES = 262_144
MAX_CANONICAL_JSON_BYTES = 1_048_576

JsonPrimitive: TypeAlias = None | bool | int | float | str
JsonValue: TypeAlias = (
    JsonPrimitive | Mapping[str, "JsonValue"] | list["JsonValue"] | tuple["JsonValue", ...]
)
FrozenJsonValue: TypeAlias = (
    JsonPrimitive | Mapping[str, "FrozenJsonValue"] | tuple["FrozenJsonValue", ...]
)


class AgentProtocolError(RuntimeError):
    pass


class WirePayloadError(AgentProtocolError):
    pass


class JsonPayloadError(ValueError):
    pass


@dataclass(slots=True)
class _JsonValidationState:
    nodes: int = 0
    string_bytes: int = 0

    def consume_node(self, *, path: str) -> None:
        self.nodes += 1
        if self.nodes > MAX_JSON_NODES:
            raise JsonPayloadError(f"{path} exceeds maximum JSON node count {MAX_JSON_NODES}")

    def consume_string(self, value: str, *, path: str) -> None:
        if len(value) > MAX_JSON_STRING_LENGTH:
            raise JsonPayloadError(
                f"{path} exceeds maximum JSON string length {MAX_JSON_STRING_LENGTH}"
            )
        self.string_bytes += len(value.encode("utf-8"))
        if self.string_bytes > MAX_JSON_TOTAL_STRING_BYTES:
            raise JsonPayloadError(
                f"{path} exceeds maximum total JSON string bytes "
                f"{MAX_JSON_TOTAL_STRING_BYTES}"
            )


def _freeze_json_value(
    value: object,
    *,
    path: str,
    depth: int,
    max_depth: int,
    state: _JsonValidationState,
    active_containers: set[int],
) -> FrozenJsonValue:
    if depth > max_depth:
        raise JsonPayloadError(f"{path} exceeds maximum JSON depth {max_depth}")
    state.consume_node(path=path)

    if value is None or type(value) is bool:
        return value
    if type(value) is int:
        if abs(value) > MAX_JSON_INTEGER_ABS:
            raise JsonPayloadError(
                f"{path} exceeds maximum JSON integer magnitude {MAX_JSON_INTEGER_ABS}"
            )
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise JsonPayloadError(f"{path} contains a non-finite float")
        return value
    if type(value) is str:
        state.consume_string(value, path=path)
        return value

    if isinstance(value, Mapping):
        identity = id(value)
        if identity in active_containers:
            raise JsonPayloadError(f"{path} contains a recursive mapping")
        active_containers.add(identity)
        try:
            snapshot: list[tuple[str, object]] = []
            seen_keys: set[str] = set()
            try:
                for key, nested in value.items():
                    if len(snapshot) >= MAX_JSON_MAPPING_ENTRIES:
                        raise JsonPayloadError(
                            f"{path} exceeds maximum mapping entries "
                            f"{MAX_JSON_MAPPING_ENTRIES}"
                        )
                    if type(key) is not str:
                        raise JsonPayloadError(f"{path} mapping keys must be strings")
                    if key in seen_keys:
                        raise JsonPayloadError(f"{path} contains duplicate mapping key {key!r}")
                    state.consume_string(key, path=f"{path}.<key>")
                    seen_keys.add(key)
                    snapshot.append((key, nested))
            except JsonPayloadError:
                raise
            except Exception as exc:
                raise JsonPayloadError(f"{path} mapping iteration failed") from exc

            frozen = {
                key: _freeze_json_value(
                    nested,
                    path=f"{path}.{key}",
                    depth=depth + 1,
                    max_depth=max_depth,
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
            raise JsonPayloadError(f"{path} contains a recursive sequence")
        active_containers.add(identity)
        try:
            frozen_items: list[FrozenJsonValue] = []
            try:
                for index, item in enumerate(value):
                    if index >= MAX_JSON_SEQUENCE_LENGTH:
                        raise JsonPayloadError(
                            f"{path} exceeds maximum sequence length "
                            f"{MAX_JSON_SEQUENCE_LENGTH}"
                        )
                    frozen_items.append(
                        _freeze_json_value(
                            item,
                            path=f"{path}[{index}]",
                            depth=depth + 1,
                            max_depth=max_depth,
                            state=state,
                            active_containers=active_containers,
                        )
                    )
            except JsonPayloadError:
                raise
            except Exception as exc:
                raise JsonPayloadError(f"{path} sequence iteration failed") from exc
            return tuple(frozen_items)
        finally:
            active_containers.remove(identity)

    raise JsonPayloadError(f"{path} contains unsupported JSON type {type(value).__name__}")


def json_compatible(value: FrozenJsonValue) -> JsonValue:
    """Return a detached JSON-compatible copy of an already validated frozen value."""
    if isinstance(value, Mapping):
        return {key: json_compatible(nested) for key, nested in value.items()}
    if isinstance(value, tuple):
        return [json_compatible(nested) for nested in value]
    return value


def _encoded_frozen_json(value: FrozenJsonValue) -> str:
    try:
        return json.dumps(
            json_compatible(value),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:  # Defensive: validated values must serialize.
        raise JsonPayloadError("validated JSON payload could not be serialized") from exc


def _freeze_json_object_with_limits(
    value: object,
    *,
    field_name: str,
    max_depth: int,
    max_bytes: int,
) -> Mapping[str, FrozenJsonValue]:
    """Validate an object root and return a detached, deeply immutable representation."""
    if not isinstance(value, Mapping):
        raise JsonPayloadError(f"{field_name} must be a JSON object")
    frozen = _freeze_json_value(
        value,
        path=field_name,
        depth=0,
        max_depth=max_depth,
        state=_JsonValidationState(),
        active_containers=set(),
    )
    if not isinstance(frozen, Mapping):  # Defensive type narrowing.
        raise JsonPayloadError(f"{field_name} must be a JSON object")
    encoded_size = len(_encoded_frozen_json(frozen).encode("utf-8"))
    if encoded_size > max_bytes:
        raise JsonPayloadError(f"{field_name} exceeds maximum canonical JSON bytes {max_bytes}")
    return frozen


def freeze_json_object(value: object, *, field_name: str) -> Mapping[str, FrozenJsonValue]:
    """Validate a bounded object-root structured payload and deeply freeze it."""
    return _freeze_json_object_with_limits(
        value,
        field_name=field_name,
        max_depth=MAX_JSON_DEPTH,
        max_bytes=MAX_STRUCTURED_JSON_BYTES,
    )


def canonical_json_dumps(value: object) -> str:
    """Validate and serialize a bounded JSON wire value deterministically."""
    frozen = _freeze_json_value(
        value,
        path="payload",
        depth=0,
        max_depth=MAX_WIRE_JSON_DEPTH,
        state=_JsonValidationState(),
        active_containers=set(),
    )
    encoded = _encoded_frozen_json(frozen)
    if len(encoded.encode("utf-8")) > MAX_CANONICAL_JSON_BYTES:
        raise JsonPayloadError(
            f"payload exceeds maximum canonical JSON bytes {MAX_CANONICAL_JSON_BYTES}"
        )
    return encoded


def canonical_json_loads(encoded: str) -> JsonValue:
    """Parse strict JSON, rejecting extensions, duplicate keys, and oversized values."""
    if type(encoded) is not str:
        raise JsonPayloadError("encoded JSON payload must be a string")
    if len(encoded.encode("utf-8")) > MAX_CANONICAL_JSON_BYTES:
        raise JsonPayloadError(
            f"payload exceeds maximum canonical JSON bytes {MAX_CANONICAL_JSON_BYTES}"
        )

    def reject_constant(value: str) -> None:
        raise JsonPayloadError(f"payload contains non-standard JSON constant {value}")

    def strict_object(pairs: list[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
        result: dict[str, JsonValue] = {}
        for key, value in pairs:
            if key in result:
                raise JsonPayloadError(f"payload contains duplicate JSON object key {key!r}")
            result[key] = value
        return result

    try:
        parsed = json.loads(
            encoded,
            parse_constant=reject_constant,
            object_pairs_hook=strict_object,
        )
    except JsonPayloadError:
        raise
    except (ValueError, RecursionError) as exc:
        raise JsonPayloadError("payload contains malformed JSON") from exc

    frozen = _freeze_json_value(
        parsed,
        path="payload",
        depth=0,
        max_depth=MAX_WIRE_JSON_DEPTH,
        state=_JsonValidationState(),
        active_containers=set(),
    )
    return json_compatible(frozen)


def _wire_object(payload: object, *, name: str) -> Mapping[str, FrozenJsonValue]:
    try:
        return _freeze_json_object_with_limits(
            payload,
            field_name=name,
            max_depth=MAX_WIRE_JSON_DEPTH,
            max_bytes=MAX_CANONICAL_JSON_BYTES,
        )
    except JsonPayloadError as exc:
        raise WirePayloadError(str(exc)) from exc


def _check_wire_fields(
    payload: Mapping[str, FrozenJsonValue],
    *,
    name: str,
    required: frozenset[str],
    allowed: frozenset[str],
) -> None:
    fields = frozenset(payload)
    missing = required - fields
    if missing:
        raise WirePayloadError(f"{name} is missing required fields: {', '.join(sorted(missing))}")
    unknown = fields - allowed
    if unknown:
        raise WirePayloadError(f"{name} contains unknown fields: {', '.join(sorted(unknown))}")


def _wire_string(value: object, *, field: str, allow_none: bool = False) -> str | None:
    if value is None and allow_none:
        return None
    if type(value) is not str:
        raise WirePayloadError(f"{field} must be a string")
    return value


def _wire_integer(value: object, *, field: str) -> int:
    if type(value) is not int:
        raise WirePayloadError(f"{field} must be an integer")
    return value


def _wire_bool(value: object, *, field: str) -> bool:
    if type(value) is not bool:
        raise WirePayloadError(f"{field} must be a boolean")
    return value


def _wire_number(value: object, *, field: str) -> int | float:
    if type(value) not in {int, float}:
        raise WirePayloadError(f"{field} must be a finite number")
    if not math.isfinite(value):
        raise WirePayloadError(f"{field} must be a finite number")
    return value


def _wire_string_tuple(value: object, *, field: str) -> tuple[str, ...]:
    if not isinstance(value, tuple) or any(type(item) is not str for item in value):
        raise WirePayloadError(f"{field} must be an array of strings")
    return value


def _supported_version(value: object, *, field: str) -> str:
    version = _wire_string(value, field=field)
    if version not in SUPPORTED_SCHEMA_VERSIONS:
        raise WirePayloadError(f"unsupported protocol schema version: {version}")
    return version


class AgentDecision(StrEnum):
    CONTINUE = "CONTINUE"
    APPROVE = "APPROVE"
    REJECT = "REJECT"


_ENVELOPE_BASE_FIELDS = frozenset(
    {
        "schema_version",
        "task_id",
        "revision",
        "role",
        "goal",
        "scope",
        "forbidden_actions",
        "acceptance_criteria",
        "artifacts",
        "required_output_schema",
    }
)
_ENVELOPE_V2_FIELDS = _ENVELOPE_BASE_FIELDS | {"structured_input", "input_schema"}


@dataclass(frozen=True, slots=True)
class AgentEnvelope:
    """Versioned provider request; frozen does not promise hashability for object payloads."""

    task_id: str
    revision: int
    role: Role
    goal: str
    scope: tuple[str, ...]
    forbidden_actions: tuple[str, ...]
    acceptance_criteria: tuple[str, ...]
    artifact_refs: tuple[str, ...]
    required_output_schema: str
    schema_version: str = SCHEMA_VERSION
    _: KW_ONLY
    structured_input: Mapping[str, FrozenJsonValue] | None = None
    input_schema: str | None = None

    def __post_init__(self) -> None:
        if type(self.task_id) is not str or not self.task_id.strip():
            raise ValueError("task_id must not be empty")
        if type(self.revision) is not int or self.revision < 1:
            raise ValueError("revision must be a positive integer")
        if not isinstance(self.role, Role):
            raise ValueError("role must be a Role")
        if type(self.goal) is not str or not self.goal.strip():
            raise ValueError("goal must not be empty")
        for field_name, values in (
            ("scope", self.scope),
            ("forbidden_actions", self.forbidden_actions),
            ("acceptance_criteria", self.acceptance_criteria),
            ("artifact_refs", self.artifact_refs),
        ):
            if not isinstance(values, tuple) or any(type(item) is not str for item in values):
                raise ValueError(f"{field_name} must be a tuple of strings")
        if type(self.required_output_schema) is not str or not self.required_output_schema.strip():
            raise ValueError("required_output_schema must not be empty")
        if type(self.schema_version) is not str or self.schema_version not in SUPPORTED_SCHEMA_VERSIONS:
            raise ValueError(f"unsupported protocol schema version: {self.schema_version!r}")
        if self.schema_version == V1_SCHEMA_VERSION:
            if self.structured_input is not None or self.input_schema is not None:
                raise ValueError("v1 agent envelope cannot contain structured input fields")
        elif self.structured_input is None:
            if self.input_schema is not None:
                raise ValueError("input_schema requires structured_input")
        else:
            if type(self.input_schema) is not str or not self.input_schema.strip():
                raise ValueError("structured_input requires input_schema")
            object.__setattr__(
                self,
                "structured_input",
                freeze_json_object(self.structured_input, field_name="structured_input"),
            )
        canonical_json_dumps(self.to_payload())

    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema_version": self.schema_version,
            "task_id": self.task_id,
            "revision": self.revision,
            "role": self.role.value,
            "goal": self.goal,
            "scope": list(self.scope),
            "forbidden_actions": list(self.forbidden_actions),
            "acceptance_criteria": list(self.acceptance_criteria),
            "artifacts": list(self.artifact_refs),
            "required_output_schema": self.required_output_schema,
        }
        if self.structured_input is not None:
            payload["input_schema"] = self.input_schema
            payload["structured_input"] = json_compatible(self.structured_input)
        return payload

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> AgentEnvelope:
        """Restore an explicitly versioned v1 or v2 envelope using exact wire types."""
        frozen = _wire_object(payload, name="agent envelope")
        if "schema_version" not in frozen:
            raise WirePayloadError("agent envelope is missing required field: schema_version")
        version = _supported_version(frozen["schema_version"], field="schema_version")
        allowed = _ENVELOPE_BASE_FIELDS if version == V1_SCHEMA_VERSION else _ENVELOPE_V2_FIELDS
        _check_wire_fields(
            frozen,
            name="agent envelope",
            required=_ENVELOPE_BASE_FIELDS,
            allowed=allowed,
        )
        if version == V1_SCHEMA_VERSION and (
            "structured_input" in frozen or "input_schema" in frozen
        ):
            raise WirePayloadError("v1 agent envelope cannot contain structured input fields")
        if version == V2_SCHEMA_VERSION and (
            ("structured_input" in frozen) != ("input_schema" in frozen)
        ):
            raise WirePayloadError(
                "v2 agent envelope structured_input and input_schema must appear together"
            )
        role_value = _wire_string(frozen["role"], field="role")
        try:
            role = Role(role_value)
        except ValueError as exc:
            raise WirePayloadError(f"unsupported agent role: {role_value}") from exc
        try:
            return cls(
                task_id=_wire_string(frozen["task_id"], field="task_id"),
                revision=_wire_integer(frozen["revision"], field="revision"),
                role=role,
                goal=_wire_string(frozen["goal"], field="goal"),
                scope=_wire_string_tuple(frozen["scope"], field="scope"),
                forbidden_actions=_wire_string_tuple(
                    frozen["forbidden_actions"], field="forbidden_actions"
                ),
                acceptance_criteria=_wire_string_tuple(
                    frozen["acceptance_criteria"], field="acceptance_criteria"
                ),
                artifact_refs=_wire_string_tuple(frozen["artifacts"], field="artifacts"),
                required_output_schema=_wire_string(
                    frozen["required_output_schema"], field="required_output_schema"
                ),
                schema_version=version,
                structured_input=frozen.get("structured_input"),
                input_schema=(
                    _wire_string(frozen["input_schema"], field="input_schema")
                    if "input_schema" in frozen
                    else None
                ),
            )
        except JsonPayloadError:
            raise
        except (TypeError, ValueError) as exc:
            raise WirePayloadError(f"invalid agent envelope: {exc}") from exc


_RESULT_BASE_FIELDS = frozenset(
    {
        "success",
        "summary",
        "artifact_refs",
        "output_schema",
        "tokens",
        "cost",
        "error",
        "decision",
    }
)
_RESULT_V1_FIELDS = _RESULT_BASE_FIELDS | {"schema_version"}
_RESULT_V2_FIELDS = _RESULT_V1_FIELDS | {"structured_output"}


@dataclass(frozen=True, slots=True)
class AgentResult:
    """Versioned provider result with an optional object-root machine payload.

    Frozen semantics prevent mutation. Structured instances intentionally make no
    promise that ``hash(result)`` is supported.
    """

    success: bool
    summary: str
    artifact_refs: tuple[str, ...] = ()
    output_schema: str = ""
    tokens: int = 0
    cost: float = 0.0
    error: str | None = None
    decision: AgentDecision = AgentDecision.CONTINUE
    _: KW_ONLY
    schema_version: str = SCHEMA_VERSION
    structured_output: Mapping[str, FrozenJsonValue] | None = None

    def __post_init__(self) -> None:
        if type(self.success) is not bool:
            raise ValueError("success must be a boolean")
        if type(self.summary) is not str:
            raise ValueError("summary must be a string")
        if not isinstance(self.artifact_refs, tuple) or any(
            type(item) is not str for item in self.artifact_refs
        ):
            raise ValueError("artifact_refs must be a tuple of strings")
        if type(self.output_schema) is not str:
            raise ValueError("output_schema must be a string")
        if type(self.tokens) is not int or self.tokens < 0:
            raise ValueError("tokens must be a non-negative integer")
        if type(self.cost) not in {int, float} or not math.isfinite(self.cost) or self.cost < 0:
            raise ValueError("cost must be a non-negative finite number")
        if self.error is not None and type(self.error) is not str:
            raise ValueError("error must be a string or null")
        if not isinstance(self.decision, AgentDecision):
            raise ValueError("decision must be an AgentDecision")
        if type(self.schema_version) is not str or self.schema_version not in SUPPORTED_SCHEMA_VERSIONS:
            raise ValueError(f"unsupported protocol schema version: {self.schema_version!r}")
        if self.success and self.error:
            raise ValueError("successful agent result cannot contain an error")
        if self.success and not self.output_schema.strip():
            raise ValueError("successful agent result must declare output_schema")
        if not self.success and not self.error:
            raise ValueError("failed agent result must contain an error")
        if not self.success and self.decision is not AgentDecision.CONTINUE:
            raise ValueError("failed transport/result cannot carry approve/reject decision")
        if not self.success and self.structured_output is not None:
            raise ValueError("failed agent result cannot contain structured_output")
        if self.schema_version == V1_SCHEMA_VERSION and self.structured_output is not None:
            raise ValueError("v1 agent result cannot contain structured_output")
        if self.structured_output is not None:
            object.__setattr__(
                self,
                "structured_output",
                freeze_json_object(self.structured_output, field_name="structured_output"),
            )
        canonical_json_dumps(self.to_payload())

    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "success": self.success,
            "summary": self.summary,
            "artifact_refs": list(self.artifact_refs),
            "output_schema": self.output_schema,
            "tokens": self.tokens,
            "cost": self.cost,
            "error": self.error,
            "decision": self.decision.value,
        }
        payload["schema_version"] = self.schema_version
        if self.structured_output is not None:
            payload["structured_output"] = json_compatible(self.structured_output)
        return payload

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> AgentResult:
        """Restore an exact legacy-v1 or explicitly versioned v1/v2 result."""
        frozen = _wire_object(payload, name="agent result")
        if "schema_version" in frozen:
            version = _supported_version(frozen["schema_version"], field="schema_version")
        else:
            # Proven historical compatibility: v1 AgentResult cache rows had no version field.
            version = V1_SCHEMA_VERSION
        allowed = _RESULT_V1_FIELDS if version == V1_SCHEMA_VERSION else _RESULT_V2_FIELDS
        _check_wire_fields(
            frozen,
            name="agent result",
            required=_RESULT_BASE_FIELDS,
            allowed=allowed,
        )
        if version == V1_SCHEMA_VERSION and "structured_output" in frozen:
            raise WirePayloadError("v1 agent result cannot contain structured_output")
        decision_value = _wire_string(frozen["decision"], field="decision")
        try:
            decision = AgentDecision(decision_value)
        except ValueError as exc:
            raise WirePayloadError(f"unsupported agent decision: {decision_value}") from exc
        try:
            return cls(
                success=_wire_bool(frozen["success"], field="success"),
                summary=_wire_string(frozen["summary"], field="summary"),
                artifact_refs=_wire_string_tuple(
                    frozen["artifact_refs"], field="artifact_refs"
                ),
                output_schema=_wire_string(frozen["output_schema"], field="output_schema"),
                tokens=_wire_integer(frozen["tokens"], field="tokens"),
                cost=_wire_number(frozen["cost"], field="cost"),
                error=_wire_string(frozen["error"], field="error", allow_none=True),
                decision=decision,
                schema_version=version,
                structured_output=frozen.get("structured_output"),
            )
        except JsonPayloadError:
            raise
        except (TypeError, ValueError) as exc:
            raise WirePayloadError(f"invalid agent result: {exc}") from exc


class AgentProvider(Protocol):
    """Replaceable model-provider adapter. No vendor is hard-coded into workflow logic."""

    @property
    def provider_name(self) -> str: ...

    @property
    def model_name(self) -> str: ...

    def execute(self, envelope: AgentEnvelope) -> AgentResult: ...


def validate_result(envelope: AgentEnvelope, result: AgentResult) -> AgentResult:
    """Reject a response whose protocol or output schema differs from the request."""
    if result.schema_version != envelope.schema_version:
        raise AgentProtocolError(
            "agent protocol schema mismatch: "
            f"expected {envelope.schema_version}, got {result.schema_version}"
        )
    if result.success and result.output_schema != envelope.required_output_schema:
        raise AgentProtocolError(
            "agent output schema mismatch: "
            f"expected {envelope.required_output_schema}, got {result.output_schema}"
        )
    return result
