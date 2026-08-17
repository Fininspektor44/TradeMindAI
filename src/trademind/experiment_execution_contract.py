"""Deterministic Experiment Execution V1 contract boundaries.

This layer defines the immutable, deterministic execution/result contract only.
It does NOT run experiments, train models, fit XGBoost, evaluate scientific
specifications, walk forward, execute a final holdout, place orders, call
providers/models/brokers, or mutate HypothesisRegistry lifecycle state.

All canonicalization, numeric validation, and hashing reuse the closed Provenance
Core and Verified ArtifactStore contracts. No second canonicalizer, registry, or
ArtifactStore is introduced.
"""

from __future__ import annotations

import io
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum

from trademind.discovery.manifest import (
    CriterionOperator,
    CriteriaMode,
    EvaluationCriteriaV1,
    ExperimentManifestV2,
    TradingFrictionV1,
)
from trademind.orchestrator.artifact_store import ArtifactRef, ArtifactStore
from trademind.signal_statistics_provenance import (
    MAX_JSON_INTEGER_ABS,
    CodeProvenance,
    ProvenanceError,
    canonical_json_bytes,
    freeze_json_object,
    parse_json,
    sha256_bytes,
    validate_sha256_ref,
)

EXPERIMENT_EXECUTION_CONTRACT_SCHEMA_VERSION = "experiment-execution-contract-v1"
EXPERIMENT_RESULT_V1_SCHEMA_VERSION = "experiment-result-v1"
EXPERIMENT_RESULT_V1_MEDIA_TYPE = "application/vnd.trademind.experiment-result-v1+json"
EVALUATOR_BINDING_SCHEMA_VERSION = "experiment-execution-evaluator-binding-v1"
OBSERVED_METRICS_SCHEMA_VERSION = "experiment-execution-observed-metrics-v1"
CRITERIA_DECISION_SCHEMA_VERSION = "experiment-execution-criteria-decision-v1"

RESULT_KIND = "EXPERIMENT_RESULT_V1"

MAX_OBSERVED_METRICS = 128
MAX_EVALUATOR_SUPPORTED_METRICS = 256
MAX_EVALUATOR_SUPPORTED_FRICTION_MODELS = 64

# Semantic markers fixed by this contract. There is deliberately NO way to set
# any of these true here; that authorization would belong to a future trusted
# lifecycle boundary and can never be introduced by this result contract.
RESULT_SEMANTIC_MARKERS = {
    "scientifically_validated": False,
    "trading_authorized": False,
    "final_holdout_consumed": False,
    "provider_used": False,
}

_MACHINE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$")
_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")

_EXECUTION_IDENTITY_DOMAIN = b"trademind:discovery:experiment-execution:v1"
_RESULT_SEMANTIC_HASH_DOMAIN = b"trademind:discovery:experiment-result:v1"


class ExperimentExecutionContractError(ValueError):
    """Raised when an execution/result contract is malformed or inconsistent."""


class ExperimentExecutionContractPersistenceError(RuntimeError):
    """Raised when Verified CAS does not preserve the exact result identity."""


class ExecutionPhase(StrEnum):
    """Only public (non-holdout) execution phases exist in this contract.

    There is deliberately NO FINAL_HOLDOUT/HOLDOUT member. The schema itself
    makes final-holdout execution impossible within this contract.
    """

    DISCOVERY = "DISCOVERY"
    VALIDATION = "VALIDATION"

    @classmethod
    def from_value(cls, value: object) -> "ExecutionPhase":
        try:
            return cls(value)
        except (TypeError, ValueError) as exc:
            raise ExperimentExecutionContractError(
                "execution_phase must be DISCOVERY or VALIDATION"
            ) from exc


def _machine_identifier(value: object, *, field_name: str) -> str:
    if type(value) is not str or _MACHINE_IDENTIFIER.fullmatch(value) is None:
        raise ExperimentExecutionContractError(
            f"{field_name} must be a bounded stable ASCII machine identifier"
        )
    return value


def _sha256_ref(value: object, *, field_name: str) -> str:
    try:
        return validate_sha256_ref(value)
    except ProvenanceError as exc:
        raise ExperimentExecutionContractError(f"{field_name} is invalid") from exc


def _sha256_hex(value: object, *, field_name: str) -> str:
    if type(value) is not str or _SHA256_HEX.fullmatch(value) is None:
        raise ExperimentExecutionContractError(
            f"{field_name} must be 64 lowercase hexadecimal digits"
        )
    return value


def _finite_scalar(value: object, *, field_name: str) -> int | float:
    if type(value) is bool:
        raise ExperimentExecutionContractError(f"{field_name} must not be a boolean")
    if type(value) is int:
        if abs(value) > MAX_JSON_INTEGER_ABS:
            raise ExperimentExecutionContractError(f"{field_name} is outside JSON integer bounds")
        return value
    if type(value) is float and math.isfinite(value):
        return value
    raise ExperimentExecutionContractError(f"{field_name} must be an exact finite number")


def _finite_float(value: object, *, field_name: str) -> float:
    scalar = _finite_scalar(value, field_name=field_name)
    return float(scalar)


def _threshold(value: object, *, field_name: str) -> int | float:
    if type(value) is int and abs(value) <= MAX_JSON_INTEGER_ABS:
        return value
    if type(value) is float and math.isfinite(value):
        return value
    raise ExperimentExecutionContractError(f"{field_name} must be an exact finite int or float")


def _utc_timestamp(value: object, *, field_name: str) -> str:
    if type(value) is not str:
        raise ExperimentExecutionContractError(f"{field_name} must be an exact UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ExperimentExecutionContractError(
            f"{field_name} must be a canonical UTC timestamp"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ExperimentExecutionContractError(f"{field_name} must include the UTC timezone")
    if parsed.isoformat() != value:
        raise ExperimentExecutionContractError(
            f"{field_name} must use canonical datetime.isoformat() encoding"
        )
    return value


def _audit_identity(value: object, *, field_name: str) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or len(value) > 256
        or any(ord(character) <= 0x1F or 0x7F <= ord(character) <= 0x9F for character in value)
    ):
        raise ExperimentExecutionContractError(f"{field_name} must be a bounded audit identity")
    return value


def _exact_fields(payload: Mapping[str, object], *, required: frozenset[str], name: str) -> None:
    fields = frozenset(payload)
    missing = required - fields
    unknown = fields - required
    if missing:
        raise ExperimentExecutionContractError(
            f"{name} is missing fields: {', '.join(sorted(missing))}"
        )
    if unknown:
        raise ExperimentExecutionContractError(
            f"{name} contains unknown fields: {', '.join(sorted(unknown))}"
        )


# ---------------------------------------------------------------------------
# B. Evaluator binding / supported vocabulary
# ---------------------------------------------------------------------------

_EVALUATOR_BINDING_FIELDS = frozenset(
    {
        "schema_version",
        "test_family",
        "evaluator_id",
        "evaluator_version",
        "supported_metrics",
        "supported_friction_models",
        "deterministic",
    }
)


@dataclass(frozen=True, slots=True)
class EvaluatorBinding:
    """Explicit trusted descriptor binding one test_family to one evaluator."""

    test_family: str
    evaluator_id: str
    evaluator_version: str
    supported_metrics: tuple[str, ...]
    supported_friction_models: tuple[tuple[str, str], ...]
    deterministic: bool
    schema_version: str = EVALUATOR_BINDING_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != EVALUATOR_BINDING_SCHEMA_VERSION:
            raise ExperimentExecutionContractError("unsupported evaluator binding schema_version")
        _machine_identifier(self.test_family, field_name="evaluator_binding.test_family")
        _machine_identifier(self.evaluator_id, field_name="evaluator_binding.evaluator_id")
        _machine_identifier(
            self.evaluator_version, field_name="evaluator_binding.evaluator_version"
        )
        if (
            type(self.supported_metrics) is not tuple
            or not 1 <= len(self.supported_metrics) <= MAX_EVALUATOR_SUPPORTED_METRICS
        ):
            raise ExperimentExecutionContractError(
                "supported_metrics must be a bounded non-empty tuple"
            )
        if not all(type(metric) is str for metric in self.supported_metrics):
            raise ExperimentExecutionContractError("supported_metrics must contain identifiers")
        for metric in self.supported_metrics:
            _machine_identifier(metric, field_name="evaluator_binding.supported_metric")
        if len(set(self.supported_metrics)) != len(self.supported_metrics):
            raise ExperimentExecutionContractError("supported_metrics must be unique")
        if (
            type(self.supported_friction_models) is not tuple
            or len(self.supported_friction_models) > MAX_EVALUATOR_SUPPORTED_FRICTION_MODELS
        ):
            raise ExperimentExecutionContractError("supported_friction_models is out of bounds")
        seen: set[tuple[str, str]] = set()
        for pair in self.supported_friction_models:
            if type(pair) is not tuple or len(pair) != 2 or not all(type(x) is str for x in pair):
                raise ExperimentExecutionContractError(
                    "supported_friction_models must contain (model_id, unit) pairs"
                )
            model_id, unit = pair
            _machine_identifier(model_id, field_name="friction.model_id")
            _machine_identifier(unit, field_name="friction.unit")
            if pair in seen:
                raise ExperimentExecutionContractError("supported_friction_models must be unique")
            seen.add(pair)
        if self.deterministic is not True:
            raise ExperimentExecutionContractError(
                "evaluator binding must declare deterministic execution capability"
            )
        canonical_json_bytes(self.to_payload())

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "test_family": self.test_family,
            "evaluator_id": self.evaluator_id,
            "evaluator_version": self.evaluator_version,
            "supported_metrics": list(self.supported_metrics),
            "supported_friction_models": [list(pair) for pair in self.supported_friction_models],
            "deterministic": self.deterministic,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> "EvaluatorBinding":
        frozen = freeze_json_object(payload, field_name="evaluator_binding")
        _exact_fields(frozen, required=_EVALUATOR_BINDING_FIELDS, name="evaluator_binding")
        if frozen["schema_version"] != EVALUATOR_BINDING_SCHEMA_VERSION:
            raise ExperimentExecutionContractError("unsupported evaluator binding schema_version")
        metrics = frozen["supported_metrics"]
        if type(metrics) is not tuple:
            raise ExperimentExecutionContractError("supported_metrics must be a JSON array")
        friction = frozen["supported_friction_models"]
        if type(friction) is not tuple:
            raise ExperimentExecutionContractError("supported_friction_models must be a JSON array")
        friction_pairs: list[tuple[str, str]] = []
        for index, item in enumerate(friction):
            if type(item) is not tuple or len(item) != 2 or not all(type(x) is str for x in item):
                raise ExperimentExecutionContractError(
                    f"supported_friction_models[{index}] must be a [model_id, unit] pair"
                )
            friction_pairs.append((item[0], item[1]))
        return cls(
            schema_version=frozen["schema_version"],
            test_family=frozen["test_family"],
            evaluator_id=frozen["evaluator_id"],
            evaluator_version=frozen["evaluator_version"],
            supported_metrics=tuple(metrics),
            supported_friction_models=tuple(friction_pairs),
            deterministic=frozen["deterministic"],
        )


class EvaluatorRegistry:
    """Explicit trusted mapping test_family -> evaluator implementation.

    This is the fail-closed registry the future runtime must use. It performs no
    dynamic import, no eval/exec, and no Python path supplied by the manifest.
    """

    def __init__(self, bindings: Mapping[str, EvaluatorBinding]) -> None:
        if not isinstance(bindings, Mapping):
            raise ExperimentExecutionContractError("bindings must be a mapping")
        stored: dict[str, EvaluatorBinding] = {}
        for family, binding in bindings.items():
            if type(binding) is not EvaluatorBinding:
                raise ExperimentExecutionContractError("registry values must be EvaluatorBinding")
            stored[family] = binding
        self._bindings = dict(sorted(stored.items()))

    def require_binding(
        self,
        manifest: ExperimentManifestV2,
        *,
        evaluator_id: str,
        evaluator_version: str,
    ) -> EvaluatorBinding:
        if type(manifest) is not ExperimentManifestV2:
            raise ExperimentExecutionContractError("manifest must be ExperimentManifestV2")
        binding = self._bindings.get(manifest.test_family)
        if binding is None:
            raise ExperimentExecutionContractError(
                f"unknown or unregistered test_family {manifest.test_family!r}"
            )
        if binding.evaluator_id != evaluator_id:
            raise ExperimentExecutionContractError(
                f"unknown evaluator id {evaluator_id!r} for test_family {manifest.test_family!r}"
            )
        if binding.evaluator_version != evaluator_version:
            raise ExperimentExecutionContractError(
                f"unknown evaluator version {evaluator_version!r} for "
                f"evaluator {binding.evaluator_id!r}"
            )
        return binding


# ---------------------------------------------------------------------------
# C. Observed metrics contract
# ---------------------------------------------------------------------------

_OBSERVED_METRICS_FIELDS = frozenset({"schema_version", "primary_metric", "values"})


@dataclass(frozen=True, slots=True)
class ObservedMetricsV1:
    """Bounded, finite, machine-readable observed scalar metrics."""

    primary_metric: str
    values: Mapping[str, int | float]
    schema_version: str = OBSERVED_METRICS_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != OBSERVED_METRICS_SCHEMA_VERSION:
            raise ExperimentExecutionContractError("unsupported observed metrics schema_version")
        _machine_identifier(self.primary_metric, field_name="observed_metrics.primary_metric")
        if not isinstance(self.values, Mapping) or not self.values:
            raise ExperimentExecutionContractError(
                "observed metrics values must be a non-empty mapping"
            )
        if len(self.values) > MAX_OBSERVED_METRICS:
            raise ExperimentExecutionContractError(
                f"observed metrics exceed maximum key count {MAX_OBSERVED_METRICS}"
            )
        for key in self.values:
            _machine_identifier(key, field_name="observed_metrics.metric_id")
        for key, value in self.values.items():
            _finite_scalar(value, field_name=f"metric {key!r}")
        if self.primary_metric not in self.values:
            raise ExperimentExecutionContractError(
                f"primary metric {self.primary_metric!r} must be present in observed metrics"
            )
        canonical_json_bytes(self.to_payload())

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "primary_metric": self.primary_metric,
            "values": dict(self.values),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> "ObservedMetricsV1":
        frozen = freeze_json_object(payload, field_name="observed_metrics")
        _exact_fields(frozen, required=_OBSERVED_METRICS_FIELDS, name="observed_metrics")
        raw_values = frozen["values"]
        if not isinstance(raw_values, Mapping):
            raise ExperimentExecutionContractError("observed_metrics.values must be a JSON object")
        return cls(
            schema_version=frozen["schema_version"],
            primary_metric=frozen["primary_metric"],
            values=raw_values,
        )


def assert_metrics_cover_criteria(
    metrics: ObservedMetricsV1,
    criteria: EvaluationCriteriaV1,
) -> None:
    if type(metrics) is not ObservedMetricsV1:
        raise ExperimentExecutionContractError("metrics must be ObservedMetricsV1")
    if type(criteria) is not EvaluationCriteriaV1:
        raise ExperimentExecutionContractError("criteria must be EvaluationCriteriaV1")
    for criterion in criteria.criteria:
        if criterion.metric not in metrics.values:
            raise ExperimentExecutionContractError(
                f"criteria reference metric {criterion.metric!r} missing from observed metrics"
            )


def assert_metrics_within_vocabulary(
    metrics: ObservedMetricsV1,
    binding: EvaluatorBinding,
) -> None:
    if type(metrics) is not ObservedMetricsV1:
        raise ExperimentExecutionContractError("metrics must be ObservedMetricsV1")
    if type(binding) is not EvaluatorBinding:
        raise ExperimentExecutionContractError("binding must be EvaluatorBinding")
    supported = set(binding.supported_metrics)
    extra = set(metrics.values) - supported
    if extra:
        raise ExperimentExecutionContractError(
            "evaluator emitted metric IDs outside its declared supported vocabulary: "
            f"{', '.join(sorted(extra))}"
        )


# ---------------------------------------------------------------------------
# D. Deterministic criteria evaluator
# ---------------------------------------------------------------------------

_CRITERION_EVALUATION_FIELDS = frozenset({"metric", "operator", "threshold", "observed", "passed"})
_CRITERIA_DECISION_FIELDS = frozenset(
    {"schema_version", "mode", "passed", "primary_metric", "primary_metric_value", "evaluations"}
)


@dataclass(frozen=True, slots=True)
class CriterionEvaluation:
    metric: str
    operator: CriterionOperator
    threshold: int | float
    observed: float
    passed: bool

    def __post_init__(self) -> None:
        _machine_identifier(self.metric, field_name="criterion_evaluation.metric")
        if type(self.operator) is not CriterionOperator:
            raise ExperimentExecutionContractError(
                "criterion_evaluation.operator must be CriterionOperator"
            )
        _threshold(self.threshold, field_name="criterion_evaluation.threshold")
        if not math.isfinite(self.observed):
            raise ExperimentExecutionContractError("criterion_evaluation.observed must be finite")
        if type(self.passed) is not bool:
            raise ExperimentExecutionContractError("criterion_evaluation.passed must be bool")
        canonical_json_bytes(self.to_payload())

    def to_payload(self) -> dict[str, object]:
        return {
            "metric": self.metric,
            "operator": self.operator.value,
            "threshold": self.threshold,
            "observed": self.observed,
            "passed": self.passed,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> "CriterionEvaluation":
        frozen = freeze_json_object(payload, field_name="criterion_evaluation")
        _exact_fields(frozen, required=_CRITERION_EVALUATION_FIELDS, name="criterion_evaluation")
        try:
            operator = CriterionOperator(frozen["operator"])
        except (TypeError, ValueError) as exc:
            raise ExperimentExecutionContractError("unsupported criterion operator") from exc
        return cls(
            metric=frozen["metric"],
            operator=operator,
            threshold=frozen["threshold"],
            observed=float(_finite_scalar(frozen["observed"], field_name="observed")),
            passed=frozen["passed"],
        )


@dataclass(frozen=True, slots=True)
class CriteriaDecisionV1:
    schema_version: str
    mode: CriteriaMode
    passed: bool
    primary_metric: str
    primary_metric_value: float
    evaluations: tuple[CriterionEvaluation, ...]

    def __post_init__(self) -> None:
        if self.schema_version != CRITERIA_DECISION_SCHEMA_VERSION:
            raise ExperimentExecutionContractError("unsupported criteria decision schema_version")
        if type(self.mode) is not CriteriaMode:
            raise ExperimentExecutionContractError("criteria decision mode must be CriteriaMode")
        if type(self.passed) is not bool:
            raise ExperimentExecutionContractError("criteria decision passed must be bool")
        _machine_identifier(self.primary_metric, field_name="criteria_decision.primary_metric")
        if not math.isfinite(self.primary_metric_value):
            raise ExperimentExecutionContractError("primary_metric_value must be finite")
        if type(self.evaluations) is not tuple or not self.evaluations:
            raise ExperimentExecutionContractError(
                "criteria decision must contain evaluator evaluations"
            )
        if not all(type(item) is CriterionEvaluation for item in self.evaluations):
            raise ExperimentExecutionContractError("criteria decision contains invalid evaluation")
        canonical_json_bytes(self.to_payload())

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "mode": self.mode.value,
            "passed": self.passed,
            "primary_metric": self.primary_metric,
            "primary_metric_value": self.primary_metric_value,
            "evaluations": [item.to_payload() for item in self.evaluations],
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> "CriteriaDecisionV1":
        frozen = freeze_json_object(payload, field_name="criteria_decision")
        _exact_fields(frozen, required=_CRITERIA_DECISION_FIELDS, name="criteria_decision")
        try:
            mode = CriteriaMode(frozen["mode"])
        except (TypeError, ValueError) as exc:
            raise ExperimentExecutionContractError("unsupported criteria mode") from exc
        raw = frozen["evaluations"]
        if type(raw) is not tuple:
            raise ExperimentExecutionContractError("criteria_decision.evaluations must be an array")
        evaluations = tuple(CriterionEvaluation.from_payload(item) for item in raw)
        return cls(
            schema_version=frozen["schema_version"],
            mode=mode,
            passed=frozen["passed"],
            primary_metric=frozen["primary_metric"],
            primary_metric_value=float(frozen["primary_metric_value"]),
            evaluations=evaluations,
        )


def evaluate_criteria(
    criteria: EvaluationCriteriaV1,
    metrics: ObservedMetricsV1,
) -> CriteriaDecisionV1:
    """Pure deterministic pass/fail from frozen criteria + observed metrics.

    No AI, no network, no registry mutation, no holdout inspection, no inferred
    thresholds, no criteria changes.
    """
    if type(criteria) is not EvaluationCriteriaV1:
        raise ExperimentExecutionContractError("criteria must be EvaluationCriteriaV1")
    if type(metrics) is not ObservedMetricsV1:
        raise ExperimentExecutionContractError("metrics must be ObservedMetricsV1")
    for criterion in criteria.criteria:
        if criterion.metric not in metrics.values:
            raise ExperimentExecutionContractError(
                f"criteria reference metric {criterion.metric!r} missing from observed metrics"
            )

    evaluations: list[CriterionEvaluation] = []
    for criterion in criteria.criteria:
        observed = float(metrics.values[criterion.metric])
        threshold = criterion.threshold
        operator = criterion.operator
        if operator is CriterionOperator.GREATER_THAN_OR_EQUAL:
            passed = observed >= float(threshold)
        elif operator is CriterionOperator.GREATER_THAN:
            passed = observed > float(threshold)
        elif operator is CriterionOperator.LESS_THAN_OR_EQUAL:
            passed = observed <= float(threshold)
        elif operator is CriterionOperator.LESS_THAN:
            passed = observed < float(threshold)
        else:  # pragma: no cover - enum is bounded
            raise ExperimentExecutionContractError("unsupported criterion operator")
        evaluations.append(
            CriterionEvaluation(
                metric=criterion.metric,
                operator=operator,
                threshold=threshold,
                observed=observed,
                passed=passed,
            )
        )

    if criteria.mode is CriteriaMode.ALL:
        aggregate = all(item.passed for item in evaluations)
    elif criteria.mode is CriteriaMode.ANY:
        aggregate = any(item.passed for item in evaluations)
    else:  # pragma: no cover - enum is bounded
        raise ExperimentExecutionContractError("unsupported criteria mode")

    return CriteriaDecisionV1(
        schema_version=CRITERIA_DECISION_SCHEMA_VERSION,
        mode=criteria.mode,
        passed=aggregate,
        primary_metric=metrics.primary_metric,
        primary_metric_value=float(metrics.values[metrics.primary_metric]),
        evaluations=tuple(evaluations),
    )


# ---------------------------------------------------------------------------
# E/F. Friction + code provenance binding helpers
# ---------------------------------------------------------------------------


def verify_friction_support(
    manifest_friction: TradingFrictionV1 | None,
    binding: EvaluatorBinding,
) -> None:
    """Require the evaluator to declare support for the exact frozen friction."""
    if type(binding) is not EvaluatorBinding:
        raise ExperimentExecutionContractError("binding must be EvaluatorBinding")
    if manifest_friction is None:
        return
    if type(manifest_friction) is not TradingFrictionV1:
        raise ExperimentExecutionContractError("manifest friction must be TradingFrictionV1")
    if (
        manifest_friction.model_id,
        manifest_friction.unit,
    ) not in binding.supported_friction_models:
        raise ExperimentExecutionContractError(
            "evaluator does not declare support for frozen friction model "
            f"{manifest_friction.model_id!r}/{manifest_friction.unit!r}"
        )


def verify_execution_code_provenance(
    execution: CodeProvenance,
    manifest: ExperimentManifestV2,
) -> None:
    """Require the caller-supplied execution provenance claim to match the frozen one."""
    if type(execution) is not CodeProvenance:
        raise ExperimentExecutionContractError("execution code provenance must be CodeProvenance")
    if type(manifest) is not ExperimentManifestV2:
        raise ExperimentExecutionContractError("manifest must be ExperimentManifestV2")
    if execution != manifest.code_provenance:
        raise ExperimentExecutionContractError(
            "execution code provenance does not match the frozen manifest code provenance"
        )


def _logic_execution_identity(
    *,
    manifest_semantic_hash: str,
    phase: ExecutionPhase,
    evaluator_id: str,
    evaluator_version: str,
) -> str:
    projection = {
        "manifest_semantic_hash": manifest_semantic_hash,
        "execution_phase": phase.value,
        "evaluator_id": evaluator_id,
        "evaluator_version": evaluator_version,
    }
    return sha256_bytes(_EXECUTION_IDENTITY_DOMAIN + b"\x00" + canonical_json_bytes(projection))


def logical_execution_identity(
    *,
    manifest_semantic_hash: str,
    phase: ExecutionPhase,
    evaluator_id: str,
    evaluator_version: str,
) -> str:
    return _logic_execution_identity(
        manifest_semantic_hash=manifest_semantic_hash,
        phase=phase,
        evaluator_id=evaluator_id,
        evaluator_version=evaluator_version,
    )


# ---------------------------------------------------------------------------
# H. Immutable Experiment Result V1
# ---------------------------------------------------------------------------

_DIAGNOSTICS_FIELDS = frozenset({"created_at", "created_by"})
_RESULT_FIELDS = frozenset(
    {
        "schema_version",
        "result_kind",
        "execution_identity",
        "execution_phase",
        "hypothesis",
        "manifest",
        "evaluator",
        "datasets",
        "deterministic_seed",
        "code_provenance",
        "friction",
        "observed_metrics",
        "criteria_decision",
        "semantic_markers",
        "result_semantic_identity",
        "diagnostics",
    }
)


@dataclass(frozen=True, slots=True)
class ExperimentResultDiagnostics:
    """Nonsemantic result metadata. Never affects semantic identity."""

    created_at: str
    created_by: str

    def __post_init__(self) -> None:
        _utc_timestamp(self.created_at, field_name="diagnostics.created_at")
        _audit_identity(self.created_by, field_name="diagnostics.created_by")
        canonical_json_bytes(self.to_payload())

    def to_payload(self) -> dict[str, object]:
        return {"created_at": self.created_at, "created_by": self.created_by}

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> "ExperimentResultDiagnostics":
        frozen = freeze_json_object(payload, field_name="result_diagnostics")
        _exact_fields(frozen, required=_DIAGNOSTICS_FIELDS, name="result_diagnostics")
        return cls(created_at=frozen["created_at"], created_by=frozen["created_by"])


@dataclass(frozen=True, slots=True)
class ExperimentResultV1:
    """Immutable, machine-readable, deterministic execution result."""

    schema_version: str
    result_kind: str
    execution_identity: str
    execution_phase: ExecutionPhase
    hypothesis_id: str
    hypothesis_family_id: str
    hypothesis_content_hash: str
    manifest_semantic_hash: str
    manifest_artifact_hash_ref: str
    evaluator_id: str
    evaluator_version: str
    dataset_artifact_hash_refs: tuple[str, ...]
    deterministic_seed: int | None
    code_provenance: CodeProvenance
    friction_absent: bool
    friction_model_id: str | None
    friction_unit: str | None
    observed_metrics: ObservedMetricsV1
    criteria_decision: CriteriaDecisionV1
    diagnostics: ExperimentResultDiagnostics
    result_semantic_identity: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != EXPERIMENT_RESULT_V1_SCHEMA_VERSION:
            raise ExperimentExecutionContractError("unsupported experiment result schema_version")
        if self.result_kind != RESULT_KIND:
            raise ExperimentExecutionContractError("unsupported experiment result kind")
        _machine_identifier(self.execution_identity, field_name="result.execution_identity")
        if type(self.execution_phase) is not ExecutionPhase:
            raise ExperimentExecutionContractError("result execution_phase must be ExecutionPhase")
        _sha256_hex(self.hypothesis_content_hash, field_name="result.hypothesis_content_hash")
        _sha256_ref(self.manifest_semantic_hash, field_name="result.manifest_semantic_hash")
        _sha256_ref(self.manifest_artifact_hash_ref, field_name="result.manifest_artifact_hash_ref")
        _machine_identifier(self.evaluator_id, field_name="result.evaluator_id")
        _machine_identifier(self.evaluator_version, field_name="result.evaluator_version")
        if (
            type(self.dataset_artifact_hash_refs) is not tuple
            or not self.dataset_artifact_hash_refs
        ):
            raise ExperimentExecutionContractError(
                "result.dataset_artifact_hash_refs must be a non-empty tuple"
            )
        for ref in self.dataset_artifact_hash_refs:
            _sha256_ref(ref, field_name="result.dataset_artifact_hash_ref")
        if self.deterministic_seed is not None and (
            type(self.deterministic_seed) is not int
            or not 0 <= self.deterministic_seed <= MAX_JSON_INTEGER_ABS
        ):
            raise ExperimentExecutionContractError("result deterministic_seed is invalid")
        if type(self.code_provenance) is not CodeProvenance:
            raise ExperimentExecutionContractError("result code_provenance must be CodeProvenance")
        if type(self.friction_absent) is not bool:
            raise ExperimentExecutionContractError("result friction_absent must be bool")
        if self.friction_absent:
            if self.friction_model_id is not None or self.friction_unit is not None:
                raise ExperimentExecutionContractError(
                    "no-friction result must not carry friction model/unit"
                )
        else:
            if not isinstance(self.friction_model_id, str) or not isinstance(
                self.friction_unit, str
            ):
                raise ExperimentExecutionContractError(
                    "friction result must carry exact model/unit"
                )
            _machine_identifier(self.friction_model_id, field_name="result.friction_model_id")
            _machine_identifier(self.friction_unit, field_name="result.friction_unit")
        if type(self.observed_metrics) is not ObservedMetricsV1:
            raise ExperimentExecutionContractError(
                "result observed_metrics must be ObservedMetricsV1"
            )
        if type(self.criteria_decision) is not CriteriaDecisionV1:
            raise ExperimentExecutionContractError(
                "result criteria_decision must be CriteriaDecisionV1"
            )
        if self.criteria_decision.primary_metric != self.observed_metrics.primary_metric:
            raise ExperimentExecutionContractError(
                "criteria decision primary metric must match observed metrics primary metric"
            )
        object.__setattr__(
            self,
            "result_semantic_identity",
            sha256_bytes(
                _RESULT_SEMANTIC_HASH_DOMAIN
                + b"\x00"
                + canonical_json_bytes(self.semantic_projection())
            ),
        )
        canonical_json_bytes(self.to_payload())

    def semantic_projection(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "result_kind": self.result_kind,
            "execution_identity": self.execution_identity,
            "execution_phase": self.execution_phase.value,
            "hypothesis": {
                "hypothesis_id": self.hypothesis_id,
                "hypothesis_family_id": self.hypothesis_family_id,
                "content_hash": self.hypothesis_content_hash,
            },
            "manifest": {
                "semantic_hash": self.manifest_semantic_hash,
                "artifact_hash_ref": self.manifest_artifact_hash_ref,
            },
            "evaluator": {
                "evaluator_id": self.evaluator_id,
                "evaluator_version": self.evaluator_version,
            },
            "datasets": list(self.dataset_artifact_hash_refs),
            "deterministic_seed": self.deterministic_seed,
            "code_provenance": self.code_provenance.to_payload(),
            "friction": {
                "absent": self.friction_absent,
                "model_id": self.friction_model_id,
                "unit": self.friction_unit,
            },
            "observed_metrics": self.observed_metrics.to_payload(),
            "criteria_decision": self.criteria_decision.to_payload(),
            "semantic_markers": dict(RESULT_SEMANTIC_MARKERS),
        }

    def to_payload(self) -> dict[str, object]:
        payload = self.semantic_projection()
        payload["result_semantic_identity"] = self.result_semantic_identity
        payload["diagnostics"] = self.diagnostics.to_payload()
        return payload

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_payload())

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> "ExperimentResultV1":
        try:
            frozen = freeze_json_object(payload, field_name="experiment_result_v1")
        except ProvenanceError as exc:
            raise ExperimentExecutionContractError(
                "experiment result v1 must be strict bounded JSON"
            ) from exc
        _exact_fields(frozen, required=_RESULT_FIELDS, name="experiment_result_v1")
        hypothesis = frozen["hypothesis"]
        manifest = frozen["manifest"]
        evaluator = frozen["evaluator"]
        datasets = frozen["datasets"]
        friction = frozen["friction"]
        if not all(isinstance(x, Mapping) for x in (hypothesis, manifest, evaluator, friction)):
            raise ExperimentExecutionContractError("result nested objects are malformed")
        if type(datasets) is not tuple:
            raise ExperimentExecutionContractError("result datasets must be a JSON array")
        result = cls(
            schema_version=frozen["schema_version"],
            result_kind=frozen["result_kind"],
            execution_identity=frozen["execution_identity"],
            execution_phase=ExecutionPhase.from_value(frozen["execution_phase"]),
            hypothesis_id=hypothesis["hypothesis_id"],
            hypothesis_family_id=hypothesis["hypothesis_family_id"],
            hypothesis_content_hash=hypothesis["content_hash"],
            manifest_semantic_hash=manifest["semantic_hash"],
            manifest_artifact_hash_ref=manifest["artifact_hash_ref"],
            evaluator_id=evaluator["evaluator_id"],
            evaluator_version=evaluator["evaluator_version"],
            dataset_artifact_hash_refs=tuple(datasets),
            deterministic_seed=frozen["deterministic_seed"],
            code_provenance=CodeProvenance.from_payload(frozen["code_provenance"]),
            friction_absent=friction["absent"],
            friction_model_id=friction["model_id"],
            friction_unit=friction["unit"],
            observed_metrics=ObservedMetricsV1.from_payload(frozen["observed_metrics"]),
            criteria_decision=CriteriaDecisionV1.from_payload(frozen["criteria_decision"]),
            diagnostics=ExperimentResultDiagnostics.from_payload(frozen["diagnostics"]),
        )
        if result.result_semantic_identity != frozen["result_semantic_identity"]:
            raise ExperimentExecutionContractError(
                "experiment result semantic identity does not match its semantic projection"
            )
        return result


# ---------------------------------------------------------------------------
# I. Verified CAS persistence
# ---------------------------------------------------------------------------


def build_result_v1(
    *,
    manifest: ExperimentManifestV2,
    manifest_artifact_hash_ref: str,
    execution_phase: ExecutionPhase,
    registry: EvaluatorRegistry,
    evaluator_id: str,
    evaluator_version: str,
    execution_code_provenance: CodeProvenance,
    observed: ObservedMetricsV1,
    evaluator_friction: tuple[str, str] | None,
    created_at: str,
    created_by: str,
) -> ExperimentResultV1:
    """Build one immutable result bound to the authoritative frozen manifest."""
    if type(manifest) is not ExperimentManifestV2:
        raise ExperimentExecutionContractError("manifest must be ExperimentManifestV2")
    manifest_artifact_hash_ref = _sha256_ref(
        manifest_artifact_hash_ref, field_name="manifest_artifact_hash_ref"
    )
    if type(execution_phase) is not ExecutionPhase:
        raise ExperimentExecutionContractError("execution_phase must be ExecutionPhase")
    binding = registry.require_binding(
        manifest,
        evaluator_id=evaluator_id,
        evaluator_version=evaluator_version,
    )
    verify_execution_code_provenance(execution_code_provenance, manifest)

    if observed.primary_metric != manifest.primary_metric:
        raise ExperimentExecutionContractError(
            "observed primary metric does not match the frozen manifest primary metric"
        )
    assert_metrics_cover_criteria(observed, manifest.evaluation_criteria)
    assert_metrics_within_vocabulary(observed, binding)

    manifest_friction = manifest.trading_friction
    verify_friction_support(manifest_friction, binding)
    if manifest_friction is None:
        if evaluator_friction is not None:
            raise ExperimentExecutionContractError(
                "manifest declares no friction but evaluator attested a friction binding"
            )
        friction_absent, friction_model_id, friction_unit = True, None, None
    else:
        expected = (manifest_friction.model_id, manifest_friction.unit)
        if evaluator_friction != expected:
            raise ExperimentExecutionContractError(
                "evaluator must explicitly attest the exact frozen friction binding "
                f"{expected!r}; silent omission or mismatch is forbidden"
            )
        friction_absent, friction_model_id, friction_unit = False, expected[0], expected[1]

    decision = evaluate_criteria(manifest.evaluation_criteria, observed)
    identity = logical_execution_identity(
        manifest_semantic_hash=manifest.manifest_semantic_hash,
        phase=execution_phase,
        evaluator_id=binding.evaluator_id,
        evaluator_version=binding.evaluator_version,
    )
    dataset_refs = tuple(dataset.artifact_hash_ref for dataset in manifest.datasets)
    return ExperimentResultV1(
        schema_version=EXPERIMENT_RESULT_V1_SCHEMA_VERSION,
        result_kind=RESULT_KIND,
        execution_identity=identity,
        execution_phase=execution_phase,
        hypothesis_id=manifest.hypothesis_id,
        hypothesis_family_id=manifest.hypothesis_family_id,
        hypothesis_content_hash=manifest.bound_hypothesis_content_hash,
        manifest_semantic_hash=manifest.manifest_semantic_hash,
        manifest_artifact_hash_ref=manifest_artifact_hash_ref,
        evaluator_id=binding.evaluator_id,
        evaluator_version=binding.evaluator_version,
        dataset_artifact_hash_refs=dataset_refs,
        deterministic_seed=manifest.deterministic_seed,
        code_provenance=manifest.code_provenance,
        friction_absent=friction_absent,
        friction_model_id=friction_model_id,
        friction_unit=friction_unit,
        observed_metrics=observed,
        criteria_decision=decision,
        diagnostics=ExperimentResultDiagnostics(created_at=created_at, created_by=created_by),
    )


def persist_result_v1(
    result: ExperimentResultV1,
    *,
    artifact_store: ArtifactStore,
) -> ArtifactRef:
    """Persist exact canonical result bytes under Verified CAS."""
    if type(result) is not ExperimentResultV1:
        raise ExperimentExecutionContractError("result must be ExperimentResultV1")
    exact_bytes = result.canonical_bytes()
    artifact = artifact_store.import_snapshot(
        io.BytesIO(exact_bytes),
        media_type=EXPERIMENT_RESULT_V1_MEDIA_TYPE,
    )
    if artifact.hash_ref != sha256_bytes(exact_bytes):
        raise ExperimentExecutionContractPersistenceError(
            "Verified CAS returned an unexpected experiment result hash"
        )
    return artifact


def load_result_v1(
    result_artifact_hash_ref: str,
    *,
    artifact_store: ArtifactStore,
    manifest: ExperimentManifestV2,
    manifest_artifact_hash_ref: str,
    registry: EvaluatorRegistry,
) -> ExperimentResultV1:
    """Load and reverify a result against the authoritative manifest and registry."""
    if type(manifest) is not ExperimentManifestV2:
        raise ExperimentExecutionContractError("manifest must be ExperimentManifestV2")
    _sha256_ref(manifest_artifact_hash_ref, field_name="manifest_artifact_hash_ref")
    if type(registry) is not EvaluatorRegistry:
        raise ExperimentExecutionContractError("registry must be EvaluatorRegistry")

    artifact = artifact_store.resolve_verified(
        result_artifact_hash_ref,
        expected_media_type=EXPERIMENT_RESULT_V1_MEDIA_TYPE,
    )
    exact_bytes = artifact_store.read_verified(
        result_artifact_hash_ref,
        expected_media_type=EXPERIMENT_RESULT_V1_MEDIA_TYPE,
    )
    if artifact.hash_ref != sha256_bytes(exact_bytes):
        raise ExperimentExecutionContractPersistenceError("Verified CAS result identity mismatch")
    result = verify_experiment_result_v1(exact_bytes)

    # Manifest identity and exact CAS artifact identity.
    if result.manifest_artifact_hash_ref != manifest_artifact_hash_ref:
        raise ExperimentExecutionContractError(
            "result manifest CAS artifact ref does not match the authoritative manifest"
        )
    if result.manifest_semantic_hash != manifest.manifest_semantic_hash:
        raise ExperimentExecutionContractError(
            "result manifest semantic identity does not match the authoritative manifest"
        )
    # Hypothesis identities.
    if (
        result.hypothesis_id != manifest.hypothesis_id
        or result.hypothesis_family_id != manifest.hypothesis_family_id
        or result.hypothesis_content_hash != manifest.bound_hypothesis_content_hash
    ):
        raise ExperimentExecutionContractError("result hypothesis identity mismatch")

    # Dataset CAS bindings (reverify each through Verified CAS).
    expected_datasets = tuple(dataset.artifact_hash_ref for dataset in manifest.datasets)
    if result.dataset_artifact_hash_refs != expected_datasets:
        raise ExperimentExecutionContractError("result dataset CAS bindings do not match manifest")
    manifest.verify_datasets(artifact_store)

    # Evaluator binding (unknown family/version fails closed).
    binding = registry.require_binding(
        manifest,
        evaluator_id=result.evaluator_id,
        evaluator_version=result.evaluator_version,
    )

    # Friction binding.
    manifest_friction = manifest.trading_friction
    verify_friction_support(manifest_friction, binding)
    if manifest_friction is None:
        if not result.friction_absent:
            raise ExperimentExecutionContractError(
                "result friction binding conflicts with a no-friction manifest"
            )
    else:
        expected = (manifest_friction.model_id, manifest_friction.unit)
        if (
            result.friction_absent
            or result.friction_model_id != expected[0]
            or result.friction_unit != expected[1]
        ):
            raise ExperimentExecutionContractError(
                "result friction binding does not match the frozen manifest friction"
            )

    # Code provenance equality.
    if result.code_provenance != manifest.code_provenance:
        raise ExperimentExecutionContractError(
            "result code provenance does not match the frozen manifest code provenance"
        )

    # Execution identity determinism.
    recomputed = logical_execution_identity(
        manifest_semantic_hash=manifest.manifest_semantic_hash,
        phase=result.execution_phase,
        evaluator_id=result.evaluator_id,
        evaluator_version=result.evaluator_version,
    )
    if recomputed != result.execution_identity:
        raise ExperimentExecutionContractError("result execution identity is not deterministic")

    return result


def verify_experiment_result_v1(encoded: str | bytes) -> ExperimentResultV1:
    """Verify strict canonical wire bytes and the recomputed semantic identity."""
    if type(encoded) is bytes:
        exact_bytes = encoded
    elif type(encoded) is str:
        try:
            exact_bytes = encoded.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ExperimentExecutionContractError(
                "experiment result v1 must be valid UTF-8"
            ) from exc
    else:
        raise ExperimentExecutionContractError(
            "experiment result v1 wire payload must be exact str or bytes"
        )
    try:
        parsed = parse_json(exact_bytes)
    except ProvenanceError as exc:
        raise ExperimentExecutionContractError(
            f"experiment result v1 wire payload is invalid: {exc}"
        ) from exc
    if not isinstance(parsed, Mapping):
        raise ExperimentExecutionContractError("experiment result v1 root must be a JSON object")
    if canonical_json_bytes(parsed) != exact_bytes:
        raise ExperimentExecutionContractError(
            "experiment result v1 wire payload must use canonical JSON bytes"
        )
    return ExperimentResultV1.from_payload(parsed)


__all__ = [
    "CRITERIA_DECISION_SCHEMA_VERSION",
    "EVALUATOR_BINDING_SCHEMA_VERSION",
    "EXPERIMENT_EXECUTION_CONTRACT_SCHEMA_VERSION",
    "EXPERIMENT_RESULT_V1_MEDIA_TYPE",
    "EXPERIMENT_RESULT_V1_SCHEMA_VERSION",
    "OBSERVED_METRICS_SCHEMA_VERSION",
    "RESULT_KIND",
    "RESULT_SEMANTIC_MARKERS",
    "CriteriaDecisionV1",
    "CriterionEvaluation",
    "EvaluatorBinding",
    "EvaluatorRegistry",
    "ExecutionPhase",
    "ExperimentExecutionContractError",
    "ExperimentExecutionContractPersistenceError",
    "ExperimentResultDiagnostics",
    "ExperimentResultV1",
    "ObservedMetricsV1",
    "assert_metrics_cover_criteria",
    "assert_metrics_within_vocabulary",
    "build_result_v1",
    "evaluate_criteria",
    "load_result_v1",
    "logical_execution_identity",
    "persist_result_v1",
    "verify_execution_code_provenance",
    "verify_experiment_result_v1",
    "verify_friction_support",
]
