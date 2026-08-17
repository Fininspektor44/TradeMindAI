"""Trusted creation boundary for immutable ExperimentManifest v2 artifacts.

This module joins an explicitly accepted Research Proposal Intake item to the
closed ExperimentManifest v2 and HypothesisRegistry contracts.  It does not run
an experiment, invoke a provider, read final-holdout observations, or mutate an
orchestrator Task.  ``TrustedExperimentSpecificationV1`` is control-plane input;
constructing one validates data but does not authenticate or authorize its
caller.  Deployments must expose this operation only through a trusted operator
boundary.
"""

from __future__ import annotations

import math
import re
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta

from trademind.discovery.hypothesis_registry import (
    HypothesisRecord,
    HypothesisState,
    RegistryError,
    derive_content_hash,
    derive_hypothesis_family_id,
)
from trademind.discovery.manifest import (
    MAX_FRICTION_VALUE,
    MAX_MANIFEST_DATASETS,
    MAX_MANIFEST_IDENTIFIER_LENGTH,
    MAX_MANIFEST_PARAMETERS,
    MAX_MANIFEST_TESTS,
    DatasetArtifactV2,
    EvaluationCriteriaV1,
    ExperimentManifestV2,
    ProposalIntakeProvenanceV1,
    TradingFrictionV1,
    build_experiment_manifest_v2,
    load_experiment_manifest_v2,
    persist_experiment_manifest_v2,
    verify_experiment_manifest_v2,
)
from trademind.discovery.split_engine import SplitPlan
from trademind.orchestrator.artifact_store import ArtifactStore
from trademind.orchestrator.audit_log import AuditLog
from trademind.orchestrator.models import AuditEvent, PolicyDecision, Role, TaskState
from trademind.research_execution import ResearchExecutionControl
from trademind.research_proposal_intake import (
    ResearchProposalIntakeControl,
    ResearchProposalIntakeDatabaseError,
    ResearchProposalIntakeState,
    ResearchProposalIntakeV1,
    ResearchProposalSourceError,
)
from trademind.signal_statistics_provenance import (
    MAX_JSON_INTEGER_ABS,
    CodeProvenance,
    FrozenJsonValue,
    ProvenanceError,
    freeze_json_object,
)


TRUSTED_EXPERIMENT_SPECIFICATION_SCHEMA_VERSION = "trusted-experiment-specification-v1"

_MACHINE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$")
_PROHIBITED_RESULT_KEYS = frozenset(
    {
        "actual",
        "actual_value",
        "confidence_interval",
        "evidence",
        "experiment_result",
        "experiment_results",
        "holdout_metrics",
        "p_value",
        "passed",
        "result",
        "results",
        "scientifically_validated",
        "validated",
    }
)


class ExperimentManifestCreationError(RuntimeError):
    """Base error for trusted manifest creation failures."""


class ExperimentManifestCreationSourceError(ExperimentManifestCreationError):
    """The accepted intake or authoritative hypothesis cannot be reverified."""


class ExperimentManifestCreationConflict(ExperimentManifestCreationError):
    """A different scientific manifest already owns the frozen hypothesis."""


def _machine_identifier(value: object, *, field_name: str) -> str:
    if type(value) is not str or _MACHINE_IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a bounded stable ASCII identifier")
    return value


def _audit_identity(value: object, *, field_name: str) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or len(value) > MAX_MANIFEST_IDENTIFIER_LENGTH
        or any(ord(character) <= 0x1F or 0x7F <= ord(character) <= 0x9F for character in value)
    ):
        raise ValueError(f"{field_name} must be a bounded audit identity")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{field_name} must be valid UTF-8") from exc
    return value


def _utc_timestamp(value: object, *, field_name: str) -> str:
    if type(value) is not str:
        raise ValueError(f"{field_name} must be an exact canonical UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an exact canonical UTC timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0) or parsed.isoformat() != value:
        raise ValueError(f"{field_name} must be an exact canonical UTC timestamp")
    return value


def _finite_float(
    value: object,
    *,
    field_name: str,
    minimum: float,
    maximum: float,
    minimum_exclusive: bool = False,
) -> float:
    if type(value) is not float or not math.isfinite(value):
        raise ValueError(f"{field_name} must be an exact finite float")
    valid = minimum < value <= maximum if minimum_exclusive else minimum <= value <= maximum
    if not valid:
        raise ValueError(f"{field_name} is outside the closed V2 numeric bounds")
    return value


def _reject_observed_results(value: FrozenJsonValue, *, path: str = "semantic_parameters") -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            normalized = key.casefold().replace("-", "_")
            if (
                normalized.startswith("observed_")
                or normalized.startswith("actual_")
                or normalized in _PROHIBITED_RESULT_KEYS
            ):
                raise ValueError(f"{path} contains prohibited observed-result field: {key}")
            _reject_observed_results(nested, path=f"{path}.{key}")
    elif isinstance(value, tuple):
        for index, nested in enumerate(value):
            _reject_observed_results(nested, path=f"{path}[{index}]")
    elif isinstance(value, str):
        _reject_result_identifier(value, field_name=path)


def _reject_result_identifier(value: str, *, field_name: str) -> None:
    normalized = value.casefold().replace("-", "_")
    if (
        normalized.startswith("observed_")
        or normalized.startswith("actual_")
        or normalized in _PROHIBITED_RESULT_KEYS
    ):
        raise ValueError(f"{field_name} contains a prohibited observed-result identifier")


@dataclass(frozen=True, slots=True)
class TrustedExperimentSpecificationV1:
    """Strict immutable scientific input supplied by a trusted control plane.

    Natural-language provider output is deliberately absent.  Every numeric
    value, split boundary, dataset binding, criterion, and friction assumption is
    explicit before creation.  The final holdout is represented only by the
    closed ``SplitPlan`` boundaries/counts and is never read by this workflow.
    """

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
    schema_version: str = TRUSTED_EXPERIMENT_SPECIFICATION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != TRUSTED_EXPERIMENT_SPECIFICATION_SCHEMA_VERSION:
            raise ValueError("unsupported trusted experiment specification schema_version")
        if type(self.datasets) is not tuple or not 1 <= len(self.datasets) <= MAX_MANIFEST_DATASETS:
            raise ValueError("datasets must satisfy the closed V2 collection bounds")
        if not all(type(item) is DatasetArtifactV2 for item in self.datasets):
            raise ValueError("datasets must contain exact DatasetArtifactV2 values")
        if type(self.split_plan) is not SplitPlan:
            raise ValueError("split_plan must be an exact SplitPlan")
        _machine_identifier(self.split_dataset_role, field_name="split_dataset_role")
        _machine_identifier(self.test_family, field_name="test_family")
        _machine_identifier(self.primary_metric, field_name="primary_metric")
        _reject_result_identifier(self.split_dataset_role, field_name="split_dataset_role")
        _reject_result_identifier(self.test_family, field_name="test_family")
        _reject_result_identifier(self.primary_metric, field_name="primary_metric")
        if type(self.evaluation_criteria) is not EvaluationCriteriaV1:
            raise ValueError("evaluation_criteria must be exact EvaluationCriteriaV1")
        for criterion in self.evaluation_criteria.criteria:
            _reject_result_identifier(criterion.metric, field_name="evaluation_criteria.metric")
        _finite_float(
            self.alpha,
            field_name="alpha",
            minimum=0.0,
            maximum=1.0,
            minimum_exclusive=True,
        )
        _finite_float(
            self.q,
            field_name="q",
            minimum=0.0,
            maximum=1.0,
            minimum_exclusive=True,
        )
        _finite_float(
            self.minimum_effect_size,
            field_name="minimum_effect_size",
            minimum=0.0,
            maximum=MAX_FRICTION_VALUE,
        )
        if (
            type(self.max_hypotheses_tests) is not int
            or not 1 <= self.max_hypotheses_tests <= MAX_MANIFEST_TESTS
        ):
            raise ValueError("max_hypotheses_tests must satisfy the closed V2 integer bounds")
        if (
            self.trading_friction is not None
            and type(self.trading_friction) is not TradingFrictionV1
        ):
            raise ValueError("trading_friction must be exact TradingFrictionV1 or None")
        if self.deterministic_seed is not None and (
            type(self.deterministic_seed) is not int
            or not 0 <= self.deterministic_seed <= MAX_JSON_INTEGER_ABS
        ):
            raise ValueError("deterministic_seed must satisfy the closed V2 integer bounds")
        if type(self.code_provenance) is not CodeProvenance:
            raise ValueError("code_provenance must be exact CodeProvenance")
        try:
            parameters = freeze_json_object(
                self.semantic_parameters,
                field_name="semantic_parameters",
            )
        except ProvenanceError as exc:
            raise ValueError("semantic_parameters must be strict bounded JSON") from exc
        if len(parameters) > MAX_MANIFEST_PARAMETERS:
            raise ValueError("semantic_parameters exceeds the closed V2 key bound")
        _reject_observed_results(parameters)
        object.__setattr__(self, "semantic_parameters", parameters)
        _utc_timestamp(self.created_at, field_name="created_at")
        _audit_identity(self.created_by, field_name="created_by")


@dataclass(frozen=True, slots=True)
class ExperimentManifestCreationResultV1:
    manifest: ExperimentManifestV2
    hypothesis: HypothesisRecord
    manifest_artifact_hash_ref: str
    created: bool


class ExperimentManifestCreationControl:
    """Trusted, provider-neutral manifest build and atomic registry-freeze boundary."""

    def __init__(
        self,
        *,
        intake_control: ResearchProposalIntakeControl,
        artifact_store: ArtifactStore,
    ) -> None:
        if not isinstance(intake_control, ResearchProposalIntakeControl):
            raise TypeError("intake_control must be ResearchProposalIntakeControl")
        if not isinstance(artifact_store, ArtifactStore):
            raise TypeError("artifact_store must be ArtifactStore")
        source_store = intake_control.execution_control.artifact_store
        if source_store.root != artifact_store.root:
            raise ExperimentManifestCreationSourceError(
                "manifest workflow must share the authoritative execution Verified CAS"
            )
        self.path = intake_control.path.resolve()
        self.intake_control = intake_control
        self.hypothesis_registry = intake_control.hypothesis_registry
        self.artifact_store = artifact_store

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30.0)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        return db

    @staticmethod
    def _begin_transaction(db: sqlite3.Connection) -> None:
        db.execute("BEGIN IMMEDIATE")

    @staticmethod
    def _commit_transaction(db: sqlite3.Connection) -> None:
        db.commit()

    @staticmethod
    def _proposal_provenance(intake: ResearchProposalIntakeV1) -> ProposalIntakeProvenanceV1:
        return ProposalIntakeProvenanceV1(
            intake_id=intake.intake_id,
            execution_request_hash=intake.request_hash,
            authorization_id=intake.authorization_id,
            task_id=intake.task_id,
            task_revision=intake.task_revision,
            packet_artifact_hash_ref=intake.packet_artifact_hash_ref,
            packet_semantic_hash=intake.packet_semantic_hash,
            result_artifact_hash_ref=intake.result_artifact_hash_ref,
            proposal_index=intake.proposal_index,
            candidate_id=intake.candidate_id,
        )

    @staticmethod
    def _require_accepted(intake: ResearchProposalIntakeV1) -> None:
        if intake.state is not ResearchProposalIntakeState.ACCEPTED_FOR_HYPOTHESIS:
            raise ExperimentManifestCreationSourceError(
                "manifest creation requires ACCEPTED_FOR_HYPOTHESIS intake"
            )
        if intake.hypothesis_id != intake.intake_id:
            raise ExperimentManifestCreationSourceError(
                "accepted intake has an invalid authoritative hypothesis binding"
            )

    @staticmethod
    def _require_hypothesis_binding(
        intake: ResearchProposalIntakeV1,
        hypothesis: HypothesisRecord,
        *,
        expected_family_id: str,
        expected_content_hash: str,
    ) -> None:
        if (
            hypothesis.hypothesis_id != intake.intake_id
            or hypothesis.hypothesis_family_id != expected_family_id
            or hypothesis.content_hash != expected_content_hash
        ):
            raise ExperimentManifestCreationSourceError(
                "accepted intake and hypothesis registry identities conflict"
            )
        if hypothesis.state not in {HypothesisState.PROPOSED, HypothesisState.FROZEN}:
            raise ExperimentManifestCreationConflict(
                "manifest creation requires PROPOSED or idempotently FROZEN hypothesis"
            )

    @staticmethod
    def _audit_event(
        *,
        intake: ResearchProposalIntakeV1,
        hypothesis: HypothesisRecord,
        manifest: ExperimentManifestV2,
        manifest_artifact_hash_ref: str,
    ) -> AuditEvent:
        return AuditEvent(
            timestamp=ResearchExecutionControl._timestamp(),
            task_id=intake.task_id,
            revision=intake.task_revision,
            actor_role=Role.OPERATOR,
            action="EXPERIMENT_MANIFEST_V1_CREATED_AND_HYPOTHESIS_FROZEN",
            input_artifact_hashes=(
                intake.packet_artifact_hash_ref,
                intake.result_artifact_hash_ref,
                *(dataset.artifact_hash_ref for dataset in manifest.datasets),
            ),
            output_artifact_hashes=(manifest_artifact_hash_ref,),
            from_state=TaskState.NEW,
            to_state=TaskState.NEW,
            policy_result=PolicyDecision.AUTO_ALLOWED_WITH_AUDIT,
            metadata={
                "intake_id": intake.intake_id,
                "creation_status": "REQUESTED_AND_COMPLETED",
                "reviewer_id": intake.reviewer_id,
                "created_by": manifest.created_by,
                "candidate_id": intake.candidate_id,
                "execution_request_hash": intake.request_hash,
                "packet_semantic_hash": intake.packet_semantic_hash,
                "hypothesis_id": hypothesis.hypothesis_id,
                "hypothesis_family_id": hypothesis.hypothesis_family_id,
                "hypothesis_content_hash": hypothesis.content_hash,
                "manifest_semantic_hash": manifest.manifest_semantic_hash,
                "manifest_artifact_hash_ref": manifest_artifact_hash_ref,
                "dataset_artifact_hash_refs": [
                    dataset.artifact_hash_ref for dataset in manifest.datasets
                ],
                "split_plan_semantic_hash": manifest.split_plan.semantic_hash,
                "code_provenance_claim": manifest.code_provenance.to_payload(),
                "registry_state": HypothesisState.FROZEN.value,
                "scientifically_validated": False,
                "experiment_executed": False,
            },
        )

    def create_experiment_manifest_v1(
        self,
        intake_id: str,
        *,
        specification: TrustedExperimentSpecificationV1,
    ) -> ExperimentManifestCreationResultV1:
        """Build CAS first, then atomically freeze the authoritative hypothesis.

        A DB rollback may leave an unreferenced immutable CAS object.  The
        registry can never commit a binding until that exact object and every
        dataset have been reloaded and verified inside the transaction.
        """
        if type(specification) is not TrustedExperimentSpecificationV1:
            raise TypeError("specification must be exact TrustedExperimentSpecificationV1")
        try:
            before, source = self.intake_control._load_verified_intake(intake_id)
            self._require_accepted(before)
            family_definition, content_definition = self.intake_control._hypothesis_definitions(
                before, source
            )
            expected_family_id = derive_hypothesis_family_id(family_definition)
            expected_content_hash = derive_content_hash(content_definition)
            with self._connect() as verification_db:
                self.intake_control._validate_schema(verification_db)
                hypothesis_before = self.intake_control._accepted_hypothesis_in_transaction(
                    verification_db,
                    before,
                    expected_family_id=expected_family_id,
                    expected_content_hash=expected_content_hash,
                )
            self._require_hypothesis_binding(
                before,
                hypothesis_before,
                expected_family_id=expected_family_id,
                expected_content_hash=expected_content_hash,
            )
        except (ExperimentManifestCreationError, KeyError):
            raise
        except (ResearchProposalIntakeDatabaseError, ResearchProposalSourceError) as exc:
            raise ExperimentManifestCreationSourceError(
                "accepted proposal lineage failed authoritative verification"
            ) from exc

        manifest = build_experiment_manifest_v2(
            artifact_store=self.artifact_store,
            hypothesis_id=hypothesis_before.hypothesis_id,
            hypothesis_family_id=hypothesis_before.hypothesis_family_id,
            bound_hypothesis_content_hash=hypothesis_before.content_hash,
            proposal_provenance=self._proposal_provenance(before),
            datasets=specification.datasets,
            split_plan=specification.split_plan,
            split_dataset_role=specification.split_dataset_role,
            test_family=specification.test_family,
            primary_metric=specification.primary_metric,
            evaluation_criteria=specification.evaluation_criteria,
            alpha=specification.alpha,
            q=specification.q,
            minimum_effect_size=specification.minimum_effect_size,
            max_hypotheses_tests=specification.max_hypotheses_tests,
            trading_friction=specification.trading_friction,
            deterministic_seed=specification.deterministic_seed,
            code_provenance=specification.code_provenance,
            semantic_parameters=specification.semantic_parameters,
            created_at=specification.created_at,
            created_by=specification.created_by,
        )
        verified = verify_experiment_manifest_v2(manifest.canonical_bytes())
        if verified != manifest:
            raise ExperimentManifestCreationError("manifest v2 semantic verification diverged")
        artifact = persist_experiment_manifest_v2(manifest, artifact_store=self.artifact_store)

        db = self._connect()
        freeze_result = None
        try:
            self._begin_transaction(db)
            self.intake_control._validate_schema(db)
            self.intake_control._verify_source_in_transaction(db, source)
            self.intake_control._require_ingestion_in_transaction(db, source)
            row = db.execute(
                "SELECT * FROM research_proposal_intakes WHERE intake_id=?",
                (before.intake_id,),
            ).fetchone()
            if row is None:
                raise ExperimentManifestCreationSourceError("accepted intake disappeared")
            current = self.intake_control._intake_from_row(row)
            if current != before:
                raise ExperimentManifestCreationSourceError(
                    "accepted intake changed before manifest binding"
                )
            self._require_accepted(current)
            current_hypothesis = self.intake_control._accepted_hypothesis_in_transaction(
                db,
                current,
                expected_family_id=expected_family_id,
                expected_content_hash=expected_content_hash,
            )
            self._require_hypothesis_binding(
                current,
                current_hypothesis,
                expected_family_id=expected_family_id,
                expected_content_hash=expected_content_hash,
            )
            loaded = load_experiment_manifest_v2(
                artifact.hash_ref,
                artifact_store=self.artifact_store,
            )
            if loaded != manifest:
                raise ExperimentManifestCreationError(
                    "persisted manifest differs from the verified build"
                )
            try:
                freeze_result = self.hypothesis_registry.freeze_manifest_v2_in_transaction(
                    db,
                    manifest_artifact_hash_ref=artifact.hash_ref,
                    artifact_store=self.artifact_store,
                )
            except RegistryError as exc:
                raise ExperimentManifestCreationConflict(
                    "hypothesis cannot be frozen to this scientific manifest"
                ) from exc
            if freeze_result.created:
                AuditLog.append_in_transaction(
                    db,
                    self._audit_event(
                        intake=current,
                        hypothesis=freeze_result.record,
                        manifest=manifest,
                        manifest_artifact_hash_ref=artifact.hash_ref,
                    ),
                )
            self._commit_transaction(db)
        except Exception:
            if db.in_transaction:
                db.rollback()
            raise
        finally:
            db.close()

        if freeze_result is None:
            raise ExperimentManifestCreationError("manifest freeze produced no result")
        bound_ref = freeze_result.record.manifest_artifact_hash_ref
        if bound_ref is None:
            raise ExperimentManifestCreationError("frozen hypothesis has no exact manifest binding")
        bound_manifest = load_experiment_manifest_v2(
            bound_ref,
            artifact_store=self.artifact_store,
        )
        return ExperimentManifestCreationResultV1(
            manifest=bound_manifest,
            hypothesis=freeze_result.record,
            manifest_artifact_hash_ref=bound_ref,
            created=freeze_result.created,
        )


__all__ = [
    "TRUSTED_EXPERIMENT_SPECIFICATION_SCHEMA_VERSION",
    "ExperimentManifestCreationConflict",
    "ExperimentManifestCreationControl",
    "ExperimentManifestCreationError",
    "ExperimentManifestCreationResultV1",
    "ExperimentManifestCreationSourceError",
    "TrustedExperimentSpecificationV1",
]
