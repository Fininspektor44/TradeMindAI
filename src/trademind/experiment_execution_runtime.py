"""Deterministic Experiment Execution Runtime V1.

This layer actually executes one authoritative frozen ``ExperimentManifestV2``
phase and binds the outcome to the closed Experiment Execution Contract. It
introduces no new canonicalizer, registry, ArtifactStore, or hashing scheme;
every provenance-sensitive primitive is reused unchanged from the closed
layers below it:

* the manifest is loaded and reverified exclusively through
  ``HypothesisRegistry.load_bound_manifest_v2`` -- Verified CAS identity,
  dataset CAS identity, and registry identity are all reverified there. This
  runtime never accepts a caller-fabricated ``ExperimentManifestV2`` object;
* the precommitted chronological ``SplitPlan`` is obeyed exactly: only the
  public (discovery+validation) rows of the manifest's split dataset are ever
  read, their count and boundary timestamps are checked against the frozen
  plan, and any row at or after the frozen final-holdout boundary fails
  closed before an evaluator ever runs;
* evaluator-binding, friction-binding, code-provenance, criteria evaluation,
  result construction, and Verified CAS persistence all reuse
  ``trademind.experiment_execution_contract`` unchanged.

This layer does NOT train a model, fit statistics beyond what a caller-
supplied deterministic evaluator computes over the rows it is handed, execute
a final holdout (there is no FINAL_HOLDOUT/HOLDOUT execution phase; see
``ExecutionPhase`` in the closed contract), call any AI/provider/network/
broker API, place an order, or mutate orchestrator Task state. It also never
advances ``HypothesisRegistry`` lifecycle state: a hypothesis remains FROZEN
across both its DISCOVERY and VALIDATION executions, and any lifecycle
transition based on a result belongs to a future, separately governed
evidence layer.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Protocol, runtime_checkable

from trademind.discovery.hypothesis_registry import (
    HypothesisRegistry,
    HypothesisState,
    RegistryError,
)
from trademind.discovery.manifest import ExperimentManifestV2
from trademind.discovery.split_engine import SplitPlan
from trademind.experiment_execution_contract import (
    EvaluatorRegistry,
    ExecutionPhase,
    ExperimentExecutionContractError,
    ExperimentResultV1,
    ObservedMetricsV1,
    build_result_v1,
    persist_result_v1,
    verify_execution_code_provenance,
    verify_friction_support,
)
from trademind.orchestrator.artifact_store import ArtifactRef, ArtifactStore
from trademind.signal_statistics_provenance import CodeProvenance

EXPERIMENT_EXECUTION_RUNTIME_SCHEMA_VERSION = "experiment-execution-runtime-v1"

_SPLIT_DATASET_MEDIA_TYPE = "text/csv"
_TIME_COLUMN = "time"


class ExperimentExecutionRuntimeError(RuntimeError):
    """Raised when the deterministic runtime cannot execute safely."""


class SplitViolationError(ExperimentExecutionRuntimeError):
    """Raised when dataset rows do not exactly match the frozen chronological split."""


class HoldoutLeakageError(ExperimentExecutionRuntimeError):
    """Raised when an observed dataset row reaches the frozen final-holdout range."""


@dataclass(frozen=True, slots=True)
class DatasetRow:
    """One immutable, chronologically ordered public dataset observation."""

    time: datetime
    fields: Mapping[str, str]


@runtime_checkable
class ExecutionEvaluator(Protocol):
    """Trusted, caller-supplied, deterministic evaluator implementation.

    Constructed and owned entirely by trusted deployment code, exactly like
    ``FinalHoldoutRunner``'s evaluator. The runtime performs no dynamic
    import, no eval/exec, and reads no Python path from the manifest; the
    evaluator instance is wired in by the trusted caller of this runtime.
    """

    @property
    def evaluator_id(self) -> str: ...

    @property
    def evaluator_version(self) -> str: ...

    def evaluate(
        self,
        rows: tuple[DatasetRow, ...],
        *,
        manifest: ExperimentManifestV2,
        execution_phase: ExecutionPhase,
    ) -> Mapping[str, int | float]: ...


@dataclass(frozen=True, slots=True)
class ExperimentExecutionRuntimeResult:
    """Bundle of the immutable result and its exact Verified CAS placement."""

    result: ExperimentResultV1
    result_artifact: ArtifactRef
    manifest_artifact_hash_ref: str
    public_rows_used: int


def _boundary(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(timezone.utc)


def _parse_public_dataset_rows(raw_bytes: bytes, *, label: str) -> tuple[DatasetRow, ...]:
    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SplitViolationError(f"{label} must be valid UTF-8 CSV") from exc
    try:
        reader = csv.DictReader(io.StringIO(text))
        fieldnames = reader.fieldnames or ()
        if _TIME_COLUMN not in fieldnames:
            raise SplitViolationError(f"{label} must contain a {_TIME_COLUMN!r} column")
        rows: list[DatasetRow] = []
        for raw_row in reader:
            raw_time = (raw_row.get(_TIME_COLUMN) or "").strip()
            if not raw_time:
                raise SplitViolationError(f"{label} contains an empty time value")
            parsed = datetime.fromisoformat(raw_time)
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                raise SplitViolationError(f"{label} time values must be timezone-aware")
            rows.append(
                DatasetRow(
                    time=parsed.astimezone(timezone.utc),
                    fields=MappingProxyType(dict(raw_row)),
                )
            )
    except SplitViolationError:
        raise
    except (csv.Error, ValueError) as exc:
        raise SplitViolationError(f"cannot parse {label} safely") from exc
    if not rows:
        raise SplitViolationError(f"{label} must contain at least one data row")
    return tuple(rows)


def select_public_phase_rows(
    rows: tuple[DatasetRow, ...],
    *,
    plan: SplitPlan,
    execution_phase: ExecutionPhase,
) -> tuple[DatasetRow, ...]:
    """Slice exactly the frozen discovery/validation rows; never touch holdout.

    Fails closed on any row-count, ordering, or boundary deviation from the
    precommitted ``SplitPlan``, and on any row at or after ``holdout_start``.
    This check runs even though the referenced dataset artifact is expected to
    already be public-only (final-holdout sealing verifies that boundary
    separately); it is deliberate defense in depth, not redundant trust.
    """
    if type(plan) is not SplitPlan:
        raise ExperimentExecutionRuntimeError("plan must be SplitPlan")
    if type(execution_phase) is not ExecutionPhase:
        raise ExperimentExecutionRuntimeError("execution_phase must be ExecutionPhase")
    if len(rows) != plan.public_count:
        raise SplitViolationError(
            "dataset row count does not match the frozen split public row count: "
            f"got {len(rows)}, expected {plan.public_count}"
        )
    ordered = [row.time for row in rows]
    if ordered != sorted(ordered):
        raise SplitViolationError("dataset rows must be in non-decreasing chronological order")

    holdout_start = _boundary(plan.holdout_start)
    if any(row.time >= holdout_start for row in rows):
        raise HoldoutLeakageError(
            "dataset contains a row at or after the frozen final-holdout boundary"
        )

    discovery = rows[: plan.discovery_count]
    validation = rows[plan.discovery_count : plan.discovery_count + plan.validation_count]

    if discovery[0].time != _boundary(plan.discovery_start) or discovery[-1].time != _boundary(
        plan.discovery_end
    ):
        raise SplitViolationError("discovery rows do not match the frozen split boundaries")
    if validation[0].time != _boundary(plan.validation_start) or validation[-1].time != _boundary(
        plan.validation_end
    ):
        raise SplitViolationError("validation rows do not match the frozen split boundaries")

    if execution_phase is ExecutionPhase.DISCOVERY:
        return discovery
    return validation  # ExecutionPhase is closed to {DISCOVERY, VALIDATION}.


def _load_public_dataset_rows(
    manifest: ExperimentManifestV2,
    *,
    artifact_store: ArtifactStore,
) -> tuple[DatasetRow, ...]:
    dataset = next(
        (item for item in manifest.datasets if item.role == manifest.split_dataset_role),
        None,
    )
    if dataset is None:  # pragma: no cover - ExperimentManifestV2 already guarantees this.
        raise ExperimentExecutionRuntimeError("manifest split_dataset_role is unbound")
    if dataset.media_type != _SPLIT_DATASET_MEDIA_TYPE:
        raise SplitViolationError(
            f"split dataset media_type must be {_SPLIT_DATASET_MEDIA_TYPE!r}, "
            f"got {dataset.media_type!r}"
        )
    raw_bytes = artifact_store.read_verified(
        dataset.artifact_hash_ref,
        expected_media_type=dataset.media_type,
    )
    return _parse_public_dataset_rows(raw_bytes, label=f"dataset[{dataset.role}]")


class ExperimentExecutionRuntimeV1:
    """Deterministic runtime that executes one frozen manifest phase at a time.

    Construction binds one trusted, already-instantiated evaluator (exactly
    the ``FinalHoldoutRunner`` pattern) plus the closed declarative
    ``EvaluatorRegistry`` used to verify its binding. No dynamic import, eval,
    network, or broker call ever occurs in this class.
    """

    def __init__(
        self,
        *,
        registry: HypothesisRegistry,
        artifact_store: ArtifactStore,
        evaluator_registry: EvaluatorRegistry,
        evaluator: ExecutionEvaluator,
    ) -> None:
        if not isinstance(registry, HypothesisRegistry):
            raise TypeError("registry must be HypothesisRegistry")
        if not isinstance(artifact_store, ArtifactStore):
            raise TypeError("artifact_store must be ArtifactStore")
        if not isinstance(evaluator_registry, EvaluatorRegistry):
            raise TypeError("evaluator_registry must be EvaluatorRegistry")
        if not isinstance(evaluator, ExecutionEvaluator):
            raise TypeError("evaluator must implement the ExecutionEvaluator protocol")
        self.registry = registry
        self.artifact_store = artifact_store
        self.evaluator_registry = evaluator_registry
        self.evaluator = evaluator

    def _load_authoritative_manifest(
        self, hypothesis_id: str
    ) -> tuple[ExperimentManifestV2, str]:
        try:
            manifest = self.registry.load_bound_manifest_v2(
                hypothesis_id,
                artifact_store=self.artifact_store,
            )
        except (RegistryError, KeyError) as exc:
            raise ExperimentExecutionRuntimeError(
                "experiment execution requires an authoritative FROZEN manifest binding"
            ) from exc
        record = self.registry.get(hypothesis_id)
        if record.state is not HypothesisState.FROZEN or record.manifest_artifact_hash_ref is None:
            raise ExperimentExecutionRuntimeError(  # pragma: no cover - registry guarantees this.
                "frozen hypothesis has no exact manifest artifact binding"
            )
        return manifest, record.manifest_artifact_hash_ref

    def execute(
        self,
        hypothesis_id: str,
        *,
        execution_phase: ExecutionPhase,
        execution_code_provenance: CodeProvenance,
        evaluator_friction: tuple[str, str] | None,
        created_at: str,
        created_by: str,
    ) -> ExperimentExecutionRuntimeResult:
        """Execute one frozen manifest phase and persist its bound result.

        Deterministic and idempotent for identical inputs: identical arguments
        always compute an identical ``ObservedMetricsV1``/``CriteriaDecisionV1``
        pair (assuming a deterministic evaluator, as its binding requires) and,
        because Verified CAS persistence is itself content addressed, an
        identical exact result artifact -- re-running never creates a
        duplicate CAS object.
        """
        if type(execution_phase) is not ExecutionPhase:
            raise ExperimentExecutionRuntimeError("execution_phase must be ExecutionPhase")

        manifest, manifest_artifact_hash_ref = self._load_authoritative_manifest(hypothesis_id)

        # Fail fast on predeclared-contract mismatches before touching any data.
        binding = self.evaluator_registry.require_binding(
            manifest,
            evaluator_id=self.evaluator.evaluator_id,
            evaluator_version=self.evaluator.evaluator_version,
        )
        verify_execution_code_provenance(execution_code_provenance, manifest)
        verify_friction_support(manifest.trading_friction, binding)

        rows = _load_public_dataset_rows(manifest, artifact_store=self.artifact_store)
        selected = select_public_phase_rows(
            rows,
            plan=manifest.split_plan,
            execution_phase=execution_phase,
        )

        try:
            raw_metrics = self.evaluator.evaluate(
                selected,
                manifest=manifest,
                execution_phase=execution_phase,
            )
        except ExperimentExecutionRuntimeError:
            raise
        except Exception as exc:
            raise ExperimentExecutionRuntimeError(
                f"evaluator {self.evaluator.evaluator_id!r} failed during execution"
            ) from exc

        try:
            observed = ObservedMetricsV1(
                primary_metric=manifest.primary_metric,
                values=raw_metrics,
            )
        except ExperimentExecutionContractError as exc:
            raise ExperimentExecutionRuntimeError(
                "evaluator produced observed metrics that violate the execution contract"
            ) from exc

        try:
            result = build_result_v1(
                manifest=manifest,
                manifest_artifact_hash_ref=manifest_artifact_hash_ref,
                execution_phase=execution_phase,
                registry=self.evaluator_registry,
                evaluator_id=self.evaluator.evaluator_id,
                evaluator_version=self.evaluator.evaluator_version,
                execution_code_provenance=execution_code_provenance,
                observed=observed,
                evaluator_friction=evaluator_friction,
                created_at=created_at,
                created_by=created_by,
            )
        except ExperimentExecutionContractError as exc:
            raise ExperimentExecutionRuntimeError(
                "experiment execution contract rejected the computed result"
            ) from exc

        artifact = persist_result_v1(result, artifact_store=self.artifact_store)
        return ExperimentExecutionRuntimeResult(
            result=result,
            result_artifact=artifact,
            manifest_artifact_hash_ref=manifest_artifact_hash_ref,
            public_rows_used=len(selected),
        )


__all__ = [
    "EXPERIMENT_EXECUTION_RUNTIME_SCHEMA_VERSION",
    "DatasetRow",
    "ExecutionEvaluator",
    "ExperimentExecutionRuntimeError",
    "ExperimentExecutionRuntimeResult",
    "ExperimentExecutionRuntimeV1",
    "HoldoutLeakageError",
    "SplitViolationError",
    "select_public_phase_rows",
]
