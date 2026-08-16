"""Validation Decision V1: a deterministic PASS/FAIL verdict on VALIDATION evidence.

This layer sits directly on top of the closed Experiment Evidence V1 (which
itself sits on the closed Experiment Execution Runtime V1 and Contract). It
does not run an experiment, fit a model, compute a metric, call any
AI/provider/network/broker API, place an order, or mutate orchestrator Task
state or ``HypothesisRegistry`` lifecycle state. It introduces no new
canonicalizer, CAS, or hashing scheme -- every provenance primitive below it
is reused unchanged.

Evidence records what one execution showed for *either* phase. A Validation
Decision is specifically the verdict on the VALIDATION phase -- using
DISCOVERY-phase evidence to answer "did validation pass?" would silently
reuse the same data twice (the exact multiple-comparisons/overfitting failure
mode the discovery/validation/holdout split exists to prevent), so this layer
adds one new, load-bearing guard the closed layers below it do not have:
``build_validation_decision_v1`` refuses to run at all unless
``evidence.execution_phase is ExecutionPhase.VALIDATION``.

As with Evidence-over-Result, this layer never trusts a decision handed to it
-- it always recomputes ``evaluate_criteria(manifest.evaluation_criteria,
evidence.observed_metrics)`` fresh from the predeclared manifest criteria and
refuses to produce a decision if that recomputation disagrees with the
evidence's own stored criteria decision.

A PASS here is a necessary, not sufficient, condition to proceed: it never
claims ``scientifically_validated`` or ``trading_authorized``, and it never
transitions ``HypothesisRegistry`` state. Whether (and how) a PASS actually
unlocks the final holdout is a distinct, separately governed act that belongs
to a future Final Holdout Decision Gate layer.
"""

from __future__ import annotations

import io
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum

from trademind.discovery.hypothesis_registry import HypothesisRegistry, HypothesisState
from trademind.discovery.manifest import ExperimentManifestV2
from trademind.experiment_evidence import ExperimentEvidenceV1, load_evidence_v1
from trademind.experiment_execution_contract import (
    CriteriaDecisionV1,
    EvaluatorRegistry,
    ExecutionPhase,
    ObservedMetricsV1,
    evaluate_criteria,
    load_result_v1,
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

VALIDATION_DECISION_V1_SCHEMA_VERSION = "validation-decision-v1"
VALIDATION_DECISION_V1_MEDIA_TYPE = "application/vnd.trademind.validation-decision-v1+json"
VALIDATION_DECISION_KIND = "VALIDATION_DECISION_V1"

# Fixed by this contract, exactly like the closed Result/Evidence semantic
# markers: there is deliberately no way to set any of these true here. A PASS
# is eligibility to proceed, not a scientific-validation or trading claim, and
# this layer never mutates HypothesisRegistry state.
VALIDATION_DECISION_SEMANTIC_MARKERS = {
    "scientifically_validated": False,
    "trading_authorized": False,
    "final_holdout_consumed": False,
    "provider_used": False,
    "ad_hoc_criteria_used": False,
    "hypothesis_registry_mutated": False,
}

_MACHINE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$")
_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")

_VALIDATION_DECISION_HASH_DOMAIN = b"trademind:discovery:validation-decision:v1"


class ValidationDecisionError(ValueError):
    """Raised when a Validation Decision V1 is malformed, unbindable, or unbuildable."""


class ValidationDecisionPhaseError(ValidationDecisionError):
    """Raised when the source evidence is not for the VALIDATION execution phase."""


class ValidationDecisionConflictError(ValidationDecisionError):
    """Raised when a recomputed criteria decision conflicts with the source evidence.

    Mirrors the Evidence-over-Result conflict check one layer up: fires
    whenever the predeclared manifest criteria, evaluated fresh against the
    evidence's own observed metrics, disagree with the decision already
    embedded in that evidence.
    """


class ValidationDecisionPersistenceError(RuntimeError):
    """Raised when Verified CAS does not preserve the exact decision identity."""


class ValidationOutcome(StrEnum):
    """Deterministic, machine-readable verdict. Closed to exactly these two values."""

    PASS = "PASS"
    FAIL = "FAIL"

    @classmethod
    def from_value(cls, value: object) -> "ValidationOutcome":
        try:
            return cls(value)
        except (TypeError, ValueError) as exc:
            raise ValidationDecisionError("outcome must be PASS or FAIL") from exc


def _machine_identifier(value: object, *, field_name: str) -> str:
    if type(value) is not str or _MACHINE_IDENTIFIER.fullmatch(value) is None:
        raise ValidationDecisionError(f"{field_name} must be a bounded stable ASCII machine identifier")
    return value


def _sha256_ref(value: object, *, field_name: str) -> str:
    try:
        return validate_sha256_ref(value)
    except ProvenanceError as exc:
        raise ValidationDecisionError(f"{field_name} is invalid") from exc


def _sha256_hex(value: object, *, field_name: str) -> str:
    if type(value) is not str or _SHA256_HEX.fullmatch(value) is None:
        raise ValidationDecisionError(f"{field_name} must be 64 lowercase hexadecimal digits")
    return value


def _utc_timestamp(value: object, *, field_name: str) -> str:
    if type(value) is not str:
        raise ValidationDecisionError(f"{field_name} must be an exact UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValidationDecisionError(f"{field_name} must be a canonical UTC timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValidationDecisionError(f"{field_name} must include the UTC timezone")
    if parsed.isoformat() != value:
        raise ValidationDecisionError(f"{field_name} must use canonical datetime.isoformat() encoding")
    return value


def _audit_identity(value: object, *, field_name: str) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or len(value) > 256
        or any(ord(character) <= 0x1F or 0x7F <= ord(character) <= 0x9F for character in value)
    ):
        raise ValidationDecisionError(f"{field_name} must be a bounded audit identity")
    return value


def _exact_fields(payload: Mapping[str, object], *, required: frozenset[str], name: str) -> None:
    fields = frozenset(payload)
    missing = required - fields
    unknown = fields - required
    if missing:
        raise ValidationDecisionError(f"{name} is missing fields: {', '.join(sorted(missing))}")
    if unknown:
        raise ValidationDecisionError(f"{name} contains unknown fields: {', '.join(sorted(unknown))}")


_DIAGNOSTICS_FIELDS = frozenset({"created_at", "created_by"})


@dataclass(frozen=True, slots=True)
class ValidationDecisionDiagnostics:
    """Nonsemantic decision metadata. Never affects semantic identity."""

    created_at: str
    created_by: str

    def __post_init__(self) -> None:
        _utc_timestamp(self.created_at, field_name="diagnostics.created_at")
        _audit_identity(self.created_by, field_name="diagnostics.created_by")
        canonical_json_bytes(self.to_payload())

    def to_payload(self) -> dict[str, object]:
        return {"created_at": self.created_at, "created_by": self.created_by}

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> "ValidationDecisionDiagnostics":
        frozen = freeze_json_object(payload, field_name="decision_diagnostics")
        _exact_fields(frozen, required=_DIAGNOSTICS_FIELDS, name="decision_diagnostics")
        return cls(created_at=frozen["created_at"], created_by=frozen["created_by"])


_DECISION_FIELDS = frozenset(
    {
        "schema_version",
        "decision_kind",
        "outcome",
        "execution_identity",
        "execution_phase",
        "hypothesis",
        "manifest",
        "split",
        "evaluator",
        "datasets",
        "evidence",
        "result",
        "deterministic_seed",
        "code_provenance",
        "friction",
        "observed_metrics",
        "criteria_decision",
        "decision_semantic_markers",
        "decision_semantic_identity",
        "diagnostics",
    }
)


@dataclass(frozen=True, slots=True)
class ValidationDecisionV1:
    """Immutable, machine-readable PASS/FAIL verdict on one VALIDATION-phase evidence.

    ``decision_semantic_markers`` is fixed by this contract exactly like the
    closed Result/Evidence markers: always serialized as the same constant
    and never read back from the wire into any field, so editing it in stored
    bytes has no effect on the reconstructed object.
    """

    schema_version: str
    decision_kind: str
    outcome: ValidationOutcome
    execution_identity: str
    execution_phase: ExecutionPhase
    hypothesis_id: str
    hypothesis_family_id: str
    hypothesis_content_hash: str
    manifest_semantic_hash: str
    manifest_artifact_hash_ref: str
    split_plan_semantic_hash: str
    evaluator_id: str
    evaluator_version: str
    dataset_artifact_hash_refs: tuple[str, ...]
    evidence_artifact_hash_ref: str
    evidence_semantic_identity: str
    result_artifact_hash_ref: str
    result_semantic_identity: str
    deterministic_seed: int | None
    code_provenance: CodeProvenance
    friction_absent: bool
    friction_model_id: str | None
    friction_unit: str | None
    observed_metrics: ObservedMetricsV1
    criteria_decision: CriteriaDecisionV1
    diagnostics: ValidationDecisionDiagnostics
    decision_semantic_identity: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != VALIDATION_DECISION_V1_SCHEMA_VERSION:
            raise ValidationDecisionError("unsupported validation decision schema_version")
        if self.decision_kind != VALIDATION_DECISION_KIND:
            raise ValidationDecisionError("unsupported validation decision kind")
        if type(self.outcome) is not ValidationOutcome:
            raise ValidationDecisionError("decision outcome must be ValidationOutcome")
        _machine_identifier(self.execution_identity, field_name="decision.execution_identity")
        if self.execution_phase is not ExecutionPhase.VALIDATION:
            raise ValidationDecisionPhaseError(
                "validation decision execution_phase must be VALIDATION"
            )
        if self.outcome is ValidationOutcome.PASS:
            if self.criteria_decision.passed is not True:
                raise ValidationDecisionError("PASS outcome requires a passed criteria decision")
        elif self.criteria_decision.passed is not False:
            raise ValidationDecisionError("FAIL outcome requires a failed criteria decision")
        _sha256_hex(self.hypothesis_content_hash, field_name="decision.hypothesis_content_hash")
        _sha256_ref(self.manifest_semantic_hash, field_name="decision.manifest_semantic_hash")
        _sha256_ref(self.manifest_artifact_hash_ref, field_name="decision.manifest_artifact_hash_ref")
        _sha256_ref(self.split_plan_semantic_hash, field_name="decision.split_plan_semantic_hash")
        _machine_identifier(self.evaluator_id, field_name="decision.evaluator_id")
        _machine_identifier(self.evaluator_version, field_name="decision.evaluator_version")
        if type(self.dataset_artifact_hash_refs) is not tuple or not self.dataset_artifact_hash_refs:
            raise ValidationDecisionError("decision.dataset_artifact_hash_refs must be a non-empty tuple")
        for ref in self.dataset_artifact_hash_refs:
            _sha256_ref(ref, field_name="decision.dataset_artifact_hash_ref")
        _sha256_ref(self.evidence_artifact_hash_ref, field_name="decision.evidence_artifact_hash_ref")
        _sha256_ref(self.evidence_semantic_identity, field_name="decision.evidence_semantic_identity")
        _sha256_ref(self.result_artifact_hash_ref, field_name="decision.result_artifact_hash_ref")
        _sha256_ref(self.result_semantic_identity, field_name="decision.result_semantic_identity")
        if self.deterministic_seed is not None and (
            type(self.deterministic_seed) is not int
            or not 0 <= self.deterministic_seed <= MAX_JSON_INTEGER_ABS
        ):
            raise ValidationDecisionError("decision deterministic_seed is invalid")
        if type(self.code_provenance) is not CodeProvenance:
            raise ValidationDecisionError("decision code_provenance must be CodeProvenance")
        if type(self.friction_absent) is not bool:
            raise ValidationDecisionError("decision friction_absent must be bool")
        if self.friction_absent:
            if self.friction_model_id is not None or self.friction_unit is not None:
                raise ValidationDecisionError("no-friction decision must not carry friction model/unit")
        else:
            if not isinstance(self.friction_model_id, str) or not isinstance(self.friction_unit, str):
                raise ValidationDecisionError("friction decision must carry exact model/unit")
            _machine_identifier(self.friction_model_id, field_name="decision.friction_model_id")
            _machine_identifier(self.friction_unit, field_name="decision.friction_unit")
        if type(self.observed_metrics) is not ObservedMetricsV1:
            raise ValidationDecisionError("decision observed_metrics must be ObservedMetricsV1")
        if type(self.criteria_decision) is not CriteriaDecisionV1:
            raise ValidationDecisionError("decision criteria_decision must be CriteriaDecisionV1")
        if self.criteria_decision.primary_metric != self.observed_metrics.primary_metric:
            raise ValidationDecisionError(
                "criteria decision primary metric must match observed metrics primary metric"
            )
        object.__setattr__(
            self,
            "decision_semantic_identity",
            sha256_bytes(
                _VALIDATION_DECISION_HASH_DOMAIN
                + b"\x00"
                + canonical_json_bytes(self.semantic_projection())
            ),
        )
        canonical_json_bytes(self.to_payload())

    def semantic_projection(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "decision_kind": self.decision_kind,
            "outcome": self.outcome.value,
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
            "split": {"semantic_hash": self.split_plan_semantic_hash},
            "evaluator": {
                "evaluator_id": self.evaluator_id,
                "evaluator_version": self.evaluator_version,
            },
            "datasets": list(self.dataset_artifact_hash_refs),
            "evidence": {
                "artifact_hash_ref": self.evidence_artifact_hash_ref,
                "semantic_identity": self.evidence_semantic_identity,
            },
            "result": {
                "artifact_hash_ref": self.result_artifact_hash_ref,
                "semantic_identity": self.result_semantic_identity,
            },
            "deterministic_seed": self.deterministic_seed,
            "code_provenance": self.code_provenance.to_payload(),
            "friction": {
                "absent": self.friction_absent,
                "model_id": self.friction_model_id,
                "unit": self.friction_unit,
            },
            "observed_metrics": self.observed_metrics.to_payload(),
            "criteria_decision": self.criteria_decision.to_payload(),
            "decision_semantic_markers": dict(VALIDATION_DECISION_SEMANTIC_MARKERS),
        }

    def to_payload(self) -> dict[str, object]:
        payload = self.semantic_projection()
        payload["decision_semantic_identity"] = self.decision_semantic_identity
        payload["diagnostics"] = self.diagnostics.to_payload()
        return payload

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_payload())

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> "ValidationDecisionV1":
        try:
            frozen = freeze_json_object(payload, field_name="validation_decision_v1")
        except ProvenanceError as exc:
            raise ValidationDecisionError("validation decision v1 must be strict bounded JSON") from exc
        _exact_fields(frozen, required=_DECISION_FIELDS, name="validation_decision_v1")
        hypothesis = frozen["hypothesis"]
        manifest = frozen["manifest"]
        split = frozen["split"]
        evaluator = frozen["evaluator"]
        datasets = frozen["datasets"]
        evidence = frozen["evidence"]
        result = frozen["result"]
        friction = frozen["friction"]
        if not all(
            isinstance(x, Mapping)
            for x in (hypothesis, manifest, split, evaluator, evidence, result, friction)
        ):
            raise ValidationDecisionError("decision nested objects are malformed")
        if type(datasets) is not tuple:
            raise ValidationDecisionError("decision datasets must be a JSON array")
        built = cls(
            schema_version=frozen["schema_version"],
            decision_kind=frozen["decision_kind"],
            outcome=ValidationOutcome.from_value(frozen["outcome"]),
            execution_identity=frozen["execution_identity"],
            execution_phase=ExecutionPhase.from_value(frozen["execution_phase"]),
            hypothesis_id=hypothesis["hypothesis_id"],
            hypothesis_family_id=hypothesis["hypothesis_family_id"],
            hypothesis_content_hash=hypothesis["content_hash"],
            manifest_semantic_hash=manifest["semantic_hash"],
            manifest_artifact_hash_ref=manifest["artifact_hash_ref"],
            split_plan_semantic_hash=split["semantic_hash"],
            evaluator_id=evaluator["evaluator_id"],
            evaluator_version=evaluator["evaluator_version"],
            dataset_artifact_hash_refs=tuple(datasets),
            evidence_artifact_hash_ref=evidence["artifact_hash_ref"],
            evidence_semantic_identity=evidence["semantic_identity"],
            result_artifact_hash_ref=result["artifact_hash_ref"],
            result_semantic_identity=result["semantic_identity"],
            deterministic_seed=frozen["deterministic_seed"],
            code_provenance=CodeProvenance.from_payload(frozen["code_provenance"]),
            friction_absent=friction["absent"],
            friction_model_id=friction["model_id"],
            friction_unit=friction["unit"],
            observed_metrics=ObservedMetricsV1.from_payload(frozen["observed_metrics"]),
            criteria_decision=CriteriaDecisionV1.from_payload(frozen["criteria_decision"]),
            diagnostics=ValidationDecisionDiagnostics.from_payload(frozen["diagnostics"]),
        )
        if built.decision_semantic_identity != frozen["decision_semantic_identity"]:
            raise ValidationDecisionError(
                "validation decision semantic identity does not match its semantic projection"
            )
        return built


def build_validation_decision_v1(
    *,
    manifest: ExperimentManifestV2,
    evidence: ExperimentEvidenceV1,
    evidence_artifact_hash_ref: str,
    created_at: str,
    created_by: str,
) -> ValidationDecisionV1:
    """Build one immutable PASS/FAIL decision bound to authoritative VALIDATION evidence.

    The caller must already have reverified ``manifest`` (e.g. via
    ``HypothesisRegistry.load_bound_manifest_v2``) and ``evidence`` (e.g. via
    ``load_evidence_v1`` against that same manifest, using the very same
    ``evidence_artifact_hash_ref`` passed here); this function performs its
    own defense-in-depth cross-checks but does not repeat CAS I/O.

    Refuses to run for anything but VALIDATION-phase evidence, and always
    recomputes the manifest's predeclared criteria fresh against the
    evidence's own observed metrics rather than trusting its stored decision.
    """
    if type(manifest) is not ExperimentManifestV2:
        raise ValidationDecisionError("manifest must be ExperimentManifestV2")
    if type(evidence) is not ExperimentEvidenceV1:
        raise ValidationDecisionError("evidence must be ExperimentEvidenceV1")
    evidence_artifact_hash_ref = _sha256_ref(
        evidence_artifact_hash_ref, field_name="evidence_artifact_hash_ref"
    )

    if evidence.execution_phase is not ExecutionPhase.VALIDATION:
        raise ValidationDecisionPhaseError(
            "a validation decision can be built only from VALIDATION-phase evidence, "
            f"got {evidence.execution_phase.value}"
        )

    if evidence.manifest_semantic_hash != manifest.manifest_semantic_hash:
        raise ValidationDecisionError(
            "evidence manifest binding does not match the authoritative manifest"
        )
    if (
        evidence.hypothesis_id != manifest.hypothesis_id
        or evidence.hypothesis_family_id != manifest.hypothesis_family_id
        or evidence.hypothesis_content_hash != manifest.bound_hypothesis_content_hash
    ):
        raise ValidationDecisionError(
            "evidence hypothesis identity does not match the authoritative manifest"
        )
    if evidence.split_plan_semantic_hash != manifest.split_plan.semantic_hash:
        raise ValidationDecisionError(
            "evidence split binding does not match the authoritative manifest"
        )
    expected_datasets = tuple(dataset.artifact_hash_ref for dataset in manifest.datasets)
    if evidence.dataset_artifact_hash_refs != expected_datasets:
        raise ValidationDecisionError(
            "evidence dataset bindings do not match the authoritative manifest"
        )

    # The load-bearing check: never trust a stored decision. Evaluate the
    # manifest's predeclared criteria fresh against the evidence's own
    # observed metrics; any disagreement with the stored decision is a hard
    # conflict, regardless of whether it stems from a missing metric or an
    # outright falsified pass/fail.
    recomputed = evaluate_criteria(manifest.evaluation_criteria, evidence.observed_metrics)
    if recomputed != evidence.criteria_decision:
        raise ValidationDecisionConflictError(
            "criteria decision recomputed from the predeclared manifest criteria conflicts "
            "with the source evidence's own criteria decision"
        )

    outcome = ValidationOutcome.PASS if recomputed.passed else ValidationOutcome.FAIL

    return ValidationDecisionV1(
        schema_version=VALIDATION_DECISION_V1_SCHEMA_VERSION,
        decision_kind=VALIDATION_DECISION_KIND,
        outcome=outcome,
        execution_identity=evidence.execution_identity,
        execution_phase=evidence.execution_phase,
        hypothesis_id=evidence.hypothesis_id,
        hypothesis_family_id=evidence.hypothesis_family_id,
        hypothesis_content_hash=evidence.hypothesis_content_hash,
        manifest_semantic_hash=evidence.manifest_semantic_hash,
        manifest_artifact_hash_ref=evidence.manifest_artifact_hash_ref,
        split_plan_semantic_hash=evidence.split_plan_semantic_hash,
        evaluator_id=evidence.evaluator_id,
        evaluator_version=evidence.evaluator_version,
        dataset_artifact_hash_refs=evidence.dataset_artifact_hash_refs,
        evidence_artifact_hash_ref=evidence_artifact_hash_ref,
        evidence_semantic_identity=evidence.evidence_semantic_identity,
        result_artifact_hash_ref=evidence.result_artifact_hash_ref,
        result_semantic_identity=evidence.result_semantic_identity,
        deterministic_seed=evidence.deterministic_seed,
        code_provenance=evidence.code_provenance,
        friction_absent=evidence.friction_absent,
        friction_model_id=evidence.friction_model_id,
        friction_unit=evidence.friction_unit,
        observed_metrics=evidence.observed_metrics,
        criteria_decision=recomputed,
        diagnostics=ValidationDecisionDiagnostics(created_at=created_at, created_by=created_by),
    )


def persist_validation_decision_v1(
    decision: ValidationDecisionV1,
    *,
    artifact_store: ArtifactStore,
) -> ArtifactRef:
    """Persist exact canonical decision bytes under Verified CAS."""
    if type(decision) is not ValidationDecisionV1:
        raise ValidationDecisionError("decision must be ValidationDecisionV1")
    exact_bytes = decision.canonical_bytes()
    artifact = artifact_store.import_snapshot(
        io.BytesIO(exact_bytes),
        media_type=VALIDATION_DECISION_V1_MEDIA_TYPE,
    )
    if artifact.hash_ref != sha256_bytes(exact_bytes):
        raise ValidationDecisionPersistenceError(
            "Verified CAS returned an unexpected validation decision hash"
        )
    return artifact


def verify_validation_decision_v1(encoded: str | bytes) -> ValidationDecisionV1:
    """Verify strict canonical wire bytes and the recomputed semantic identity."""
    if type(encoded) is bytes:
        exact_bytes = encoded
    elif type(encoded) is str:
        try:
            exact_bytes = encoded.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ValidationDecisionError("validation decision v1 must be valid UTF-8") from exc
    else:
        raise ValidationDecisionError("validation decision v1 wire payload must be exact str or bytes")
    try:
        parsed = parse_json(exact_bytes)
    except ProvenanceError as exc:
        raise ValidationDecisionError(f"validation decision v1 wire payload is invalid: {exc}") from exc
    if not isinstance(parsed, Mapping):
        raise ValidationDecisionError("validation decision v1 root must be a JSON object")
    if canonical_json_bytes(parsed) != exact_bytes:
        raise ValidationDecisionError(
            "validation decision v1 wire payload must use canonical JSON bytes"
        )
    return ValidationDecisionV1.from_payload(parsed)


def load_validation_decision_v1(
    decision_artifact_hash_ref: str,
    *,
    artifact_store: ArtifactStore,
    manifest: ExperimentManifestV2,
    evidence: ExperimentEvidenceV1,
    evidence_artifact_hash_ref: str,
) -> ValidationDecisionV1:
    """Load and reverify a decision against its authoritative manifest and evidence."""
    if type(manifest) is not ExperimentManifestV2:
        raise ValidationDecisionError("manifest must be ExperimentManifestV2")
    if type(evidence) is not ExperimentEvidenceV1:
        raise ValidationDecisionError("evidence must be ExperimentEvidenceV1")
    evidence_artifact_hash_ref = _sha256_ref(
        evidence_artifact_hash_ref, field_name="evidence_artifact_hash_ref"
    )

    artifact = artifact_store.resolve_verified(
        decision_artifact_hash_ref,
        expected_media_type=VALIDATION_DECISION_V1_MEDIA_TYPE,
    )
    exact_bytes = artifact_store.read_verified(
        decision_artifact_hash_ref,
        expected_media_type=VALIDATION_DECISION_V1_MEDIA_TYPE,
    )
    if artifact.hash_ref != sha256_bytes(exact_bytes):
        raise ValidationDecisionPersistenceError("Verified CAS decision identity mismatch")
    decision = verify_validation_decision_v1(exact_bytes)

    if decision.manifest_semantic_hash != manifest.manifest_semantic_hash:
        raise ValidationDecisionError(
            "decision manifest binding does not match the authoritative manifest"
        )
    if decision.split_plan_semantic_hash != manifest.split_plan.semantic_hash:
        raise ValidationDecisionError(
            "decision split binding does not match the authoritative manifest"
        )
    if (
        decision.hypothesis_id != manifest.hypothesis_id
        or decision.hypothesis_family_id != manifest.hypothesis_family_id
        or decision.hypothesis_content_hash != manifest.bound_hypothesis_content_hash
    ):
        raise ValidationDecisionError("decision hypothesis identity mismatch")
    expected_datasets = tuple(dataset.artifact_hash_ref for dataset in manifest.datasets)
    if decision.dataset_artifact_hash_refs != expected_datasets:
        raise ValidationDecisionError(
            "decision dataset bindings do not match the authoritative manifest"
        )

    if (
        decision.evidence_artifact_hash_ref != evidence_artifact_hash_ref
        or decision.evidence_semantic_identity != evidence.evidence_semantic_identity
        or decision.execution_identity != evidence.execution_identity
        or decision.execution_phase != evidence.execution_phase
        or decision.evaluator_id != evidence.evaluator_id
        or decision.evaluator_version != evidence.evaluator_version
        or decision.result_artifact_hash_ref != evidence.result_artifact_hash_ref
        or decision.result_semantic_identity != evidence.result_semantic_identity
        or decision.deterministic_seed != evidence.deterministic_seed
        or decision.code_provenance != evidence.code_provenance
        or decision.friction_absent != evidence.friction_absent
        or decision.friction_model_id != evidence.friction_model_id
        or decision.friction_unit != evidence.friction_unit
        or decision.observed_metrics != evidence.observed_metrics
    ):
        raise ValidationDecisionError("decision does not match its authoritative source evidence")

    if evidence.execution_phase is not ExecutionPhase.VALIDATION:
        raise ValidationDecisionPhaseError(
            "a validation decision can be reverified only against VALIDATION-phase evidence"
        )

    # Never trust the stored decision on reload either.
    recomputed = evaluate_criteria(manifest.evaluation_criteria, evidence.observed_metrics)
    if recomputed != decision.criteria_decision or recomputed != evidence.criteria_decision:
        raise ValidationDecisionConflictError(
            "criteria decision recomputed from the predeclared manifest criteria conflicts "
            "with the persisted decision or its source evidence"
        )
    expected_outcome = ValidationOutcome.PASS if recomputed.passed else ValidationOutcome.FAIL
    if decision.outcome is not expected_outcome:
        raise ValidationDecisionConflictError(
            "persisted decision outcome conflicts with the recomputed criteria decision"
        )

    return decision


class ValidationDecisionBuilderV1:
    """Trusted assembly of a Validation Decision from closed layers, end to end.

    Construction binds the same registry, Verified CAS root, and declarative
    evaluator registry the runtime and evidence layers use. ``decide`` accepts
    only explicit CAS references and independently reloads and reverifies the
    authoritative frozen manifest, the execution result, and the evidence --
    in that order, each through its own closed reverification primitive --
    before any decision is constructed. It never mutates HypothesisRegistry
    state; that remains the province of a future, separately governed layer.
    """

    def __init__(
        self,
        *,
        registry: HypothesisRegistry,
        artifact_store: ArtifactStore,
        evaluator_registry: EvaluatorRegistry,
    ) -> None:
        if not isinstance(registry, HypothesisRegistry):
            raise TypeError("registry must be HypothesisRegistry")
        if not isinstance(artifact_store, ArtifactStore):
            raise TypeError("artifact_store must be ArtifactStore")
        if not isinstance(evaluator_registry, EvaluatorRegistry):
            raise TypeError("evaluator_registry must be EvaluatorRegistry")
        self.registry = registry
        self.artifact_store = artifact_store
        self.evaluator_registry = evaluator_registry

    def _load_authoritative_manifest(self, hypothesis_id: str) -> ExperimentManifestV2:
        manifest = self.registry.load_bound_manifest_v2(
            hypothesis_id,
            artifact_store=self.artifact_store,
        )
        record = self.registry.get(hypothesis_id)
        if record.state is not HypothesisState.FROZEN:
            raise ValidationDecisionError(  # pragma: no cover - registry guarantees this.
                "validation decision requires an authoritative FROZEN hypothesis"
            )
        return manifest

    def _load_authoritative_evidence(
        self,
        hypothesis_id: str,
        *,
        evidence_artifact_hash_ref: str,
        result_artifact_hash_ref: str,
        manifest: ExperimentManifestV2,
    ) -> ExperimentEvidenceV1:
        record = self.registry.get(hypothesis_id)
        result = load_result_v1(
            result_artifact_hash_ref,
            artifact_store=self.artifact_store,
            manifest=manifest,
            manifest_artifact_hash_ref=record.manifest_artifact_hash_ref,
            registry=self.evaluator_registry,
        )
        return load_evidence_v1(
            evidence_artifact_hash_ref,
            artifact_store=self.artifact_store,
            manifest=manifest,
            manifest_artifact_hash_ref=record.manifest_artifact_hash_ref,
            result=result,
            result_artifact_hash_ref=result_artifact_hash_ref,
        )

    def decide(
        self,
        hypothesis_id: str,
        *,
        evidence_artifact_hash_ref: str,
        result_artifact_hash_ref: str,
        created_at: str,
        created_by: str,
    ) -> tuple[ValidationDecisionV1, ArtifactRef]:
        manifest = self._load_authoritative_manifest(hypothesis_id)
        evidence = self._load_authoritative_evidence(
            hypothesis_id,
            evidence_artifact_hash_ref=evidence_artifact_hash_ref,
            result_artifact_hash_ref=result_artifact_hash_ref,
            manifest=manifest,
        )
        decision = build_validation_decision_v1(
            manifest=manifest,
            evidence=evidence,
            evidence_artifact_hash_ref=evidence_artifact_hash_ref,
            created_at=created_at,
            created_by=created_by,
        )
        artifact = persist_validation_decision_v1(decision, artifact_store=self.artifact_store)
        return decision, artifact

    def load(
        self,
        hypothesis_id: str,
        *,
        decision_artifact_hash_ref: str,
        evidence_artifact_hash_ref: str,
        result_artifact_hash_ref: str,
    ) -> ValidationDecisionV1:
        manifest = self._load_authoritative_manifest(hypothesis_id)
        evidence = self._load_authoritative_evidence(
            hypothesis_id,
            evidence_artifact_hash_ref=evidence_artifact_hash_ref,
            result_artifact_hash_ref=result_artifact_hash_ref,
            manifest=manifest,
        )
        return load_validation_decision_v1(
            decision_artifact_hash_ref,
            artifact_store=self.artifact_store,
            manifest=manifest,
            evidence=evidence,
            evidence_artifact_hash_ref=evidence_artifact_hash_ref,
        )


__all__ = [
    "VALIDATION_DECISION_KIND",
    "VALIDATION_DECISION_SEMANTIC_MARKERS",
    "VALIDATION_DECISION_V1_MEDIA_TYPE",
    "VALIDATION_DECISION_V1_SCHEMA_VERSION",
    "ValidationDecisionBuilderV1",
    "ValidationDecisionConflictError",
    "ValidationDecisionDiagnostics",
    "ValidationDecisionError",
    "ValidationDecisionPersistenceError",
    "ValidationDecisionPhaseError",
    "ValidationDecisionV1",
    "ValidationOutcome",
    "build_validation_decision_v1",
    "load_validation_decision_v1",
    "persist_validation_decision_v1",
    "verify_validation_decision_v1",
]
