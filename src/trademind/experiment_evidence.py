"""Experiment Evidence V1: an immutable record of what one execution showed.

This layer sits directly on top of the closed Experiment Execution Runtime V1
and Experiment Execution Contract. It does not run an experiment, fit a model,
compute a metric, call any AI/provider/network/broker API, place an order, or
mutate orchestrator Task state or ``HypothesisRegistry`` lifecycle state. It
introduces no new canonicalizer, CAS, or hashing scheme -- every provenance
primitive is reused unchanged from the closed layers below it.

Evidence exists to close one specific, real gap left open by the closed
Result contract: ``ExperimentResultV1.from_payload`` reconstructs a result's
``criteria_decision`` directly from wire bytes without ever recomputing it
from the predeclared manifest criteria and the observed metrics. A correctly
*built* result is always internally truthful (``build_result_v1`` calls
``evaluate_criteria`` itself), but nothing downstream reverifies that
truthfulness on reload. Evidence closes that gap by **always** recomputing
``evaluate_criteria(manifest.evaluation_criteria, result.observed_metrics)``
fresh and refusing to become evidence at all if that recomputation disagrees
with the result's stored decision -- fail closed, never silently trusted.

Evidence is sourced only from a completed ``ExperimentExecutionRuntimeResult``
(the closed runtime's own output type) plus the hypothesis's authoritative
frozen manifest, both reloaded and reverified through Verified CAS exactly as
the closed layers already do it (``HypothesisRegistry.load_bound_manifest_v2``
and ``experiment_execution_contract.load_result_v1``). It carries forward
exact provenance -- manifest, execution, datasets, code, split, friction, and
seed identities -- but never any final-holdout row, metric, or conclusion:
there is no FINAL_HOLDOUT/HOLDOUT ``ExecutionPhase`` anywhere in the closed
contract, so holdout data is structurally absent upstream of this layer, not
merely filtered here. Evidence also never claims ``scientifically_validated``
or ``trading_authorized``; synthesizing a hypothesis-level validation decision
across phases (and any resulting ``HypothesisRegistry`` transition) belongs to
a future, separately governed Validation Decision layer.
"""

from __future__ import annotations

import io
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone

from trademind.discovery.hypothesis_registry import HypothesisRegistry, HypothesisState
from trademind.discovery.manifest import ExperimentManifestV2
from trademind.experiment_execution_contract import (
    EvaluatorRegistry,
    ExecutionPhase,
    ExperimentResultV1,
    ObservedMetricsV1,
    CriteriaDecisionV1,
    evaluate_criteria,
    load_result_v1,
)
from trademind.experiment_execution_runtime import ExperimentExecutionRuntimeResult
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

EXPERIMENT_EVIDENCE_V1_SCHEMA_VERSION = "experiment-evidence-v1"
EXPERIMENT_EVIDENCE_V1_MEDIA_TYPE = "application/vnd.trademind.experiment-evidence-v1+json"
EVIDENCE_KIND = "EXPERIMENT_EVIDENCE_V1"

# Fixed by this contract, exactly like the closed Result's semantic markers:
# there is deliberately no way to set any of these true here. A hypothesis-
# level validation/trading decision belongs to a future, separately governed
# layer and can never be produced by Evidence construction.
EVIDENCE_SEMANTIC_MARKERS = {
    "scientifically_validated": False,
    "trading_authorized": False,
    "final_holdout_consumed": False,
    "provider_used": False,
    "ad_hoc_criteria_used": False,
}

_MACHINE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$")
_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")

_EVIDENCE_SEMANTIC_HASH_DOMAIN = b"trademind:discovery:experiment-evidence:v1"


class ExperimentEvidenceError(ValueError):
    """Raised when Experiment Evidence V1 is malformed, inconsistent, or unbuildable."""


class ExperimentEvidenceConflictError(ExperimentEvidenceError):
    """Raised when a recomputed criteria decision conflicts with the source result.

    This is the load-bearing failure mode of this layer: it fires whenever the
    predeclared manifest criteria, evaluated fresh against the result's own
    observed metrics, disagree with the decision already embedded in the
    result -- whether from tampering, a missing metric, or any other drift.
    Evidence refuses to exist rather than paper over the disagreement.
    """


class ExperimentEvidencePersistenceError(RuntimeError):
    """Raised when Verified CAS does not preserve the exact evidence identity."""


def _machine_identifier(value: object, *, field_name: str) -> str:
    if type(value) is not str or _MACHINE_IDENTIFIER.fullmatch(value) is None:
        raise ExperimentEvidenceError(f"{field_name} must be a bounded stable ASCII machine identifier")
    return value


def _sha256_ref(value: object, *, field_name: str) -> str:
    try:
        return validate_sha256_ref(value)
    except ProvenanceError as exc:
        raise ExperimentEvidenceError(f"{field_name} is invalid") from exc


def _sha256_hex(value: object, *, field_name: str) -> str:
    if type(value) is not str or _SHA256_HEX.fullmatch(value) is None:
        raise ExperimentEvidenceError(f"{field_name} must be 64 lowercase hexadecimal digits")
    return value


def _utc_timestamp(value: object, *, field_name: str) -> str:
    if type(value) is not str:
        raise ExperimentEvidenceError(f"{field_name} must be an exact UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ExperimentEvidenceError(f"{field_name} must be a canonical UTC timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ExperimentEvidenceError(f"{field_name} must include the UTC timezone")
    if parsed.isoformat() != value:
        raise ExperimentEvidenceError(f"{field_name} must use canonical datetime.isoformat() encoding")
    return value


def _audit_identity(value: object, *, field_name: str) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or len(value) > 256
        or any(ord(character) <= 0x1F or 0x7F <= ord(character) <= 0x9F for character in value)
    ):
        raise ExperimentEvidenceError(f"{field_name} must be a bounded audit identity")
    return value


def _exact_fields(payload: Mapping[str, object], *, required: frozenset[str], name: str) -> None:
    fields = frozenset(payload)
    missing = required - fields
    unknown = fields - required
    if missing:
        raise ExperimentEvidenceError(f"{name} is missing fields: {', '.join(sorted(missing))}")
    if unknown:
        raise ExperimentEvidenceError(f"{name} contains unknown fields: {', '.join(sorted(unknown))}")


_DIAGNOSTICS_FIELDS = frozenset({"created_at", "created_by"})


@dataclass(frozen=True, slots=True)
class ExperimentEvidenceDiagnostics:
    """Nonsemantic evidence metadata. Never affects semantic identity."""

    created_at: str
    created_by: str

    def __post_init__(self) -> None:
        _utc_timestamp(self.created_at, field_name="diagnostics.created_at")
        _audit_identity(self.created_by, field_name="diagnostics.created_by")
        canonical_json_bytes(self.to_payload())

    def to_payload(self) -> dict[str, object]:
        return {"created_at": self.created_at, "created_by": self.created_by}

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> "ExperimentEvidenceDiagnostics":
        frozen = freeze_json_object(payload, field_name="evidence_diagnostics")
        _exact_fields(frozen, required=_DIAGNOSTICS_FIELDS, name="evidence_diagnostics")
        return cls(created_at=frozen["created_at"], created_by=frozen["created_by"])


_EVIDENCE_FIELDS = frozenset(
    {
        "schema_version",
        "evidence_kind",
        "execution_identity",
        "execution_phase",
        "hypothesis",
        "manifest",
        "split",
        "evaluator",
        "datasets",
        "result",
        "deterministic_seed",
        "code_provenance",
        "friction",
        "observed_metrics",
        "criteria_decision",
        "evidence_semantic_markers",
        "evidence_semantic_identity",
        "diagnostics",
    }
)


@dataclass(frozen=True, slots=True)
class ExperimentEvidenceV1:
    """Immutable, machine-readable record of what one execution actually showed.

    ``evidence_semantic_markers`` is fixed by this contract (mirroring the
    closed Result's own ``semantic_markers``): it is always serialized as the
    same constant and never read back from the wire into any field, so
    editing it in stored bytes has no effect on the reconstructed object --
    trusted code always observes the true, hardcoded value.
    """

    schema_version: str
    evidence_kind: str
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
    result_artifact_hash_ref: str
    result_semantic_identity: str
    deterministic_seed: int | None
    code_provenance: CodeProvenance
    friction_absent: bool
    friction_model_id: str | None
    friction_unit: str | None
    observed_metrics: ObservedMetricsV1
    criteria_decision: CriteriaDecisionV1
    diagnostics: ExperimentEvidenceDiagnostics
    evidence_semantic_identity: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != EXPERIMENT_EVIDENCE_V1_SCHEMA_VERSION:
            raise ExperimentEvidenceError("unsupported experiment evidence schema_version")
        if self.evidence_kind != EVIDENCE_KIND:
            raise ExperimentEvidenceError("unsupported experiment evidence kind")
        _machine_identifier(self.execution_identity, field_name="evidence.execution_identity")
        if type(self.execution_phase) is not ExecutionPhase:
            raise ExperimentEvidenceError("evidence execution_phase must be ExecutionPhase")
        _sha256_hex(self.hypothesis_content_hash, field_name="evidence.hypothesis_content_hash")
        _sha256_ref(self.manifest_semantic_hash, field_name="evidence.manifest_semantic_hash")
        _sha256_ref(self.manifest_artifact_hash_ref, field_name="evidence.manifest_artifact_hash_ref")
        _sha256_ref(self.split_plan_semantic_hash, field_name="evidence.split_plan_semantic_hash")
        _machine_identifier(self.evaluator_id, field_name="evidence.evaluator_id")
        _machine_identifier(self.evaluator_version, field_name="evidence.evaluator_version")
        if type(self.dataset_artifact_hash_refs) is not tuple or not self.dataset_artifact_hash_refs:
            raise ExperimentEvidenceError("evidence.dataset_artifact_hash_refs must be a non-empty tuple")
        for ref in self.dataset_artifact_hash_refs:
            _sha256_ref(ref, field_name="evidence.dataset_artifact_hash_ref")
        _sha256_ref(self.result_artifact_hash_ref, field_name="evidence.result_artifact_hash_ref")
        _sha256_ref(self.result_semantic_identity, field_name="evidence.result_semantic_identity")
        if self.deterministic_seed is not None and (
            type(self.deterministic_seed) is not int
            or not 0 <= self.deterministic_seed <= MAX_JSON_INTEGER_ABS
        ):
            raise ExperimentEvidenceError("evidence deterministic_seed is invalid")
        if type(self.code_provenance) is not CodeProvenance:
            raise ExperimentEvidenceError("evidence code_provenance must be CodeProvenance")
        if type(self.friction_absent) is not bool:
            raise ExperimentEvidenceError("evidence friction_absent must be bool")
        if self.friction_absent:
            if self.friction_model_id is not None or self.friction_unit is not None:
                raise ExperimentEvidenceError("no-friction evidence must not carry friction model/unit")
        else:
            if not isinstance(self.friction_model_id, str) or not isinstance(self.friction_unit, str):
                raise ExperimentEvidenceError("friction evidence must carry exact model/unit")
            _machine_identifier(self.friction_model_id, field_name="evidence.friction_model_id")
            _machine_identifier(self.friction_unit, field_name="evidence.friction_unit")
        if type(self.observed_metrics) is not ObservedMetricsV1:
            raise ExperimentEvidenceError("evidence observed_metrics must be ObservedMetricsV1")
        if type(self.criteria_decision) is not CriteriaDecisionV1:
            raise ExperimentEvidenceError("evidence criteria_decision must be CriteriaDecisionV1")
        if self.criteria_decision.primary_metric != self.observed_metrics.primary_metric:
            raise ExperimentEvidenceError(
                "criteria decision primary metric must match observed metrics primary metric"
            )
        object.__setattr__(
            self,
            "evidence_semantic_identity",
            sha256_bytes(
                _EVIDENCE_SEMANTIC_HASH_DOMAIN + b"\x00" + canonical_json_bytes(self.semantic_projection())
            ),
        )
        canonical_json_bytes(self.to_payload())

    def semantic_projection(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "evidence_kind": self.evidence_kind,
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
            "evidence_semantic_markers": dict(EVIDENCE_SEMANTIC_MARKERS),
        }

    def to_payload(self) -> dict[str, object]:
        payload = self.semantic_projection()
        payload["evidence_semantic_identity"] = self.evidence_semantic_identity
        payload["diagnostics"] = self.diagnostics.to_payload()
        return payload

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_payload())

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> "ExperimentEvidenceV1":
        try:
            frozen = freeze_json_object(payload, field_name="experiment_evidence_v1")
        except ProvenanceError as exc:
            raise ExperimentEvidenceError("experiment evidence v1 must be strict bounded JSON") from exc
        _exact_fields(frozen, required=_EVIDENCE_FIELDS, name="experiment_evidence_v1")
        hypothesis = frozen["hypothesis"]
        manifest = frozen["manifest"]
        split = frozen["split"]
        evaluator = frozen["evaluator"]
        datasets = frozen["datasets"]
        result = frozen["result"]
        friction = frozen["friction"]
        if not all(
            isinstance(x, Mapping) for x in (hypothesis, manifest, split, evaluator, result, friction)
        ):
            raise ExperimentEvidenceError("evidence nested objects are malformed")
        if type(datasets) is not tuple:
            raise ExperimentEvidenceError("evidence datasets must be a JSON array")
        built = cls(
            schema_version=frozen["schema_version"],
            evidence_kind=frozen["evidence_kind"],
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
            result_artifact_hash_ref=result["artifact_hash_ref"],
            result_semantic_identity=result["semantic_identity"],
            deterministic_seed=frozen["deterministic_seed"],
            code_provenance=CodeProvenance.from_payload(frozen["code_provenance"]),
            friction_absent=friction["absent"],
            friction_model_id=friction["model_id"],
            friction_unit=friction["unit"],
            observed_metrics=ObservedMetricsV1.from_payload(frozen["observed_metrics"]),
            criteria_decision=CriteriaDecisionV1.from_payload(frozen["criteria_decision"]),
            diagnostics=ExperimentEvidenceDiagnostics.from_payload(frozen["diagnostics"]),
        )
        if built.evidence_semantic_identity != frozen["evidence_semantic_identity"]:
            raise ExperimentEvidenceError(
                "experiment evidence semantic identity does not match its semantic projection"
            )
        return built


def build_evidence_v1(
    *,
    manifest: ExperimentManifestV2,
    manifest_artifact_hash_ref: str,
    result: ExperimentResultV1,
    result_artifact_hash_ref: str,
    created_at: str,
    created_by: str,
) -> ExperimentEvidenceV1:
    """Build one immutable evidence record bound to an authoritative manifest+result.

    The caller must already have reverified ``manifest`` (e.g. via
    ``HypothesisRegistry.load_bound_manifest_v2``) and ``result`` (e.g. via
    ``load_result_v1`` against that same manifest); this function performs its
    own defense-in-depth cross-checks but does not repeat CAS I/O for either.

    Criteria are always recomputed here from the manifest's predeclared
    ``evaluation_criteria`` and the result's own ``observed_metrics`` -- never
    accepted as-is from the result -- and evidence refuses to exist if that
    recomputation disagrees with what the result already claims.
    """
    if type(manifest) is not ExperimentManifestV2:
        raise ExperimentEvidenceError("manifest must be ExperimentManifestV2")
    if type(result) is not ExperimentResultV1:
        raise ExperimentEvidenceError("result must be ExperimentResultV1")
    manifest_artifact_hash_ref = _sha256_ref(
        manifest_artifact_hash_ref, field_name="manifest_artifact_hash_ref"
    )
    result_artifact_hash_ref = _sha256_ref(
        result_artifact_hash_ref, field_name="result_artifact_hash_ref"
    )

    if (
        result.manifest_artifact_hash_ref != manifest_artifact_hash_ref
        or result.manifest_semantic_hash != manifest.manifest_semantic_hash
    ):
        raise ExperimentEvidenceError(
            "execution result manifest binding does not match the authoritative manifest"
        )
    if (
        result.hypothesis_id != manifest.hypothesis_id
        or result.hypothesis_family_id != manifest.hypothesis_family_id
        or result.hypothesis_content_hash != manifest.bound_hypothesis_content_hash
    ):
        raise ExperimentEvidenceError(
            "execution result hypothesis identity does not match the authoritative manifest"
        )
    expected_datasets = tuple(dataset.artifact_hash_ref for dataset in manifest.datasets)
    if result.dataset_artifact_hash_refs != expected_datasets:
        raise ExperimentEvidenceError(
            "execution result dataset bindings do not match the authoritative manifest"
        )

    # The load-bearing check: never trust a stored decision. Evaluate the
    # manifest's predeclared criteria fresh against the result's own observed
    # metrics; a missing metric fails closed here (ExperimentExecutionContractError),
    # and any disagreement with the stored decision is a hard conflict.
    recomputed = evaluate_criteria(manifest.evaluation_criteria, result.observed_metrics)
    if recomputed != result.criteria_decision:
        raise ExperimentEvidenceConflictError(
            "criteria decision recomputed from the predeclared manifest criteria conflicts "
            "with the execution result's own criteria decision"
        )

    return ExperimentEvidenceV1(
        schema_version=EXPERIMENT_EVIDENCE_V1_SCHEMA_VERSION,
        evidence_kind=EVIDENCE_KIND,
        execution_identity=result.execution_identity,
        execution_phase=result.execution_phase,
        hypothesis_id=result.hypothesis_id,
        hypothesis_family_id=result.hypothesis_family_id,
        hypothesis_content_hash=result.hypothesis_content_hash,
        manifest_semantic_hash=result.manifest_semantic_hash,
        manifest_artifact_hash_ref=manifest_artifact_hash_ref,
        split_plan_semantic_hash=manifest.split_plan.semantic_hash,
        evaluator_id=result.evaluator_id,
        evaluator_version=result.evaluator_version,
        dataset_artifact_hash_refs=result.dataset_artifact_hash_refs,
        result_artifact_hash_ref=result_artifact_hash_ref,
        result_semantic_identity=result.result_semantic_identity,
        deterministic_seed=result.deterministic_seed,
        code_provenance=result.code_provenance,
        friction_absent=result.friction_absent,
        friction_model_id=result.friction_model_id,
        friction_unit=result.friction_unit,
        observed_metrics=result.observed_metrics,
        criteria_decision=recomputed,
        diagnostics=ExperimentEvidenceDiagnostics(created_at=created_at, created_by=created_by),
    )


def persist_evidence_v1(
    evidence: ExperimentEvidenceV1,
    *,
    artifact_store: ArtifactStore,
) -> ArtifactRef:
    """Persist exact canonical evidence bytes under Verified CAS."""
    if type(evidence) is not ExperimentEvidenceV1:
        raise ExperimentEvidenceError("evidence must be ExperimentEvidenceV1")
    exact_bytes = evidence.canonical_bytes()
    artifact = artifact_store.import_snapshot(
        io.BytesIO(exact_bytes),
        media_type=EXPERIMENT_EVIDENCE_V1_MEDIA_TYPE,
    )
    if artifact.hash_ref != sha256_bytes(exact_bytes):
        raise ExperimentEvidencePersistenceError(
            "Verified CAS returned an unexpected experiment evidence hash"
        )
    return artifact


def verify_experiment_evidence_v1(encoded: str | bytes) -> ExperimentEvidenceV1:
    """Verify strict canonical wire bytes and the recomputed semantic identity."""
    if type(encoded) is bytes:
        exact_bytes = encoded
    elif type(encoded) is str:
        try:
            exact_bytes = encoded.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ExperimentEvidenceError("experiment evidence v1 must be valid UTF-8") from exc
    else:
        raise ExperimentEvidenceError("experiment evidence v1 wire payload must be exact str or bytes")
    try:
        parsed = parse_json(exact_bytes)
    except ProvenanceError as exc:
        raise ExperimentEvidenceError(f"experiment evidence v1 wire payload is invalid: {exc}") from exc
    if not isinstance(parsed, Mapping):
        raise ExperimentEvidenceError("experiment evidence v1 root must be a JSON object")
    if canonical_json_bytes(parsed) != exact_bytes:
        raise ExperimentEvidenceError("experiment evidence v1 wire payload must use canonical JSON bytes")
    return ExperimentEvidenceV1.from_payload(parsed)


def load_evidence_v1(
    evidence_artifact_hash_ref: str,
    *,
    artifact_store: ArtifactStore,
    manifest: ExperimentManifestV2,
    manifest_artifact_hash_ref: str,
    result: ExperimentResultV1,
    result_artifact_hash_ref: str,
) -> ExperimentEvidenceV1:
    """Load and reverify evidence against its authoritative manifest and result."""
    if type(manifest) is not ExperimentManifestV2:
        raise ExperimentEvidenceError("manifest must be ExperimentManifestV2")
    if type(result) is not ExperimentResultV1:
        raise ExperimentEvidenceError("result must be ExperimentResultV1")
    manifest_artifact_hash_ref = _sha256_ref(
        manifest_artifact_hash_ref, field_name="manifest_artifact_hash_ref"
    )
    result_artifact_hash_ref = _sha256_ref(
        result_artifact_hash_ref, field_name="result_artifact_hash_ref"
    )

    artifact = artifact_store.resolve_verified(
        evidence_artifact_hash_ref,
        expected_media_type=EXPERIMENT_EVIDENCE_V1_MEDIA_TYPE,
    )
    exact_bytes = artifact_store.read_verified(
        evidence_artifact_hash_ref,
        expected_media_type=EXPERIMENT_EVIDENCE_V1_MEDIA_TYPE,
    )
    if artifact.hash_ref != sha256_bytes(exact_bytes):
        raise ExperimentEvidencePersistenceError("Verified CAS evidence identity mismatch")
    evidence = verify_experiment_evidence_v1(exact_bytes)

    if (
        evidence.manifest_artifact_hash_ref != manifest_artifact_hash_ref
        or evidence.manifest_semantic_hash != manifest.manifest_semantic_hash
    ):
        raise ExperimentEvidenceError(
            "evidence manifest binding does not match the authoritative manifest"
        )
    if evidence.split_plan_semantic_hash != manifest.split_plan.semantic_hash:
        raise ExperimentEvidenceError("evidence split binding does not match the authoritative manifest")
    if (
        evidence.hypothesis_id != manifest.hypothesis_id
        or evidence.hypothesis_family_id != manifest.hypothesis_family_id
        or evidence.hypothesis_content_hash != manifest.bound_hypothesis_content_hash
    ):
        raise ExperimentEvidenceError("evidence hypothesis identity mismatch")
    expected_datasets = tuple(dataset.artifact_hash_ref for dataset in manifest.datasets)
    if evidence.dataset_artifact_hash_refs != expected_datasets:
        raise ExperimentEvidenceError("evidence dataset bindings do not match the authoritative manifest")

    if (
        evidence.result_artifact_hash_ref != result_artifact_hash_ref
        or evidence.result_semantic_identity != result.result_semantic_identity
        or evidence.execution_identity != result.execution_identity
        or evidence.execution_phase != result.execution_phase
        or evidence.evaluator_id != result.evaluator_id
        or evidence.evaluator_version != result.evaluator_version
        or evidence.deterministic_seed != result.deterministic_seed
        or evidence.code_provenance != result.code_provenance
        or evidence.friction_absent != result.friction_absent
        or evidence.friction_model_id != result.friction_model_id
        or evidence.friction_unit != result.friction_unit
        or evidence.observed_metrics != result.observed_metrics
    ):
        raise ExperimentEvidenceError("evidence does not match its authoritative execution result")

    # Never trust the stored decision on reload either.
    recomputed = evaluate_criteria(manifest.evaluation_criteria, result.observed_metrics)
    if recomputed != evidence.criteria_decision or recomputed != result.criteria_decision:
        raise ExperimentEvidenceConflictError(
            "criteria decision recomputed from the predeclared manifest criteria conflicts "
            "with the persisted evidence or its execution result"
        )

    return evidence


class ExperimentEvidenceBuilderV1:
    """Trusted assembly of Evidence from a closed runtime result, end to end.

    Construction binds the same registry, Verified CAS root, and declarative
    evaluator registry the runtime layer uses. ``build`` accepts only an
    ``ExperimentExecutionRuntimeResult`` -- the runtime's own output type --
    and independently reloads and reverifies both the authoritative frozen
    manifest and the execution result before any evidence is constructed.
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
            raise ExperimentEvidenceError(  # pragma: no cover - registry guarantees this.
                "experiment evidence requires an authoritative FROZEN hypothesis"
            )
        return manifest

    def build(
        self,
        hypothesis_id: str,
        *,
        execution: ExperimentExecutionRuntimeResult,
        created_at: str,
        created_by: str,
    ) -> tuple[ExperimentEvidenceV1, ArtifactRef]:
        if type(execution) is not ExperimentExecutionRuntimeResult:
            raise ExperimentEvidenceError("execution must be ExperimentExecutionRuntimeResult")
        manifest = self._load_authoritative_manifest(hypothesis_id)
        result = load_result_v1(
            execution.result_artifact.hash_ref,
            artifact_store=self.artifact_store,
            manifest=manifest,
            manifest_artifact_hash_ref=execution.manifest_artifact_hash_ref,
            registry=self.evaluator_registry,
        )
        evidence = build_evidence_v1(
            manifest=manifest,
            manifest_artifact_hash_ref=execution.manifest_artifact_hash_ref,
            result=result,
            result_artifact_hash_ref=execution.result_artifact.hash_ref,
            created_at=created_at,
            created_by=created_by,
        )
        artifact = persist_evidence_v1(evidence, artifact_store=self.artifact_store)
        return evidence, artifact

    def load(
        self,
        hypothesis_id: str,
        *,
        evidence_artifact_hash_ref: str,
        result_artifact_hash_ref: str,
        manifest_artifact_hash_ref: str,
    ) -> ExperimentEvidenceV1:
        manifest = self._load_authoritative_manifest(hypothesis_id)
        result = load_result_v1(
            result_artifact_hash_ref,
            artifact_store=self.artifact_store,
            manifest=manifest,
            manifest_artifact_hash_ref=manifest_artifact_hash_ref,
            registry=self.evaluator_registry,
        )
        return load_evidence_v1(
            evidence_artifact_hash_ref,
            artifact_store=self.artifact_store,
            manifest=manifest,
            manifest_artifact_hash_ref=manifest_artifact_hash_ref,
            result=result,
            result_artifact_hash_ref=result_artifact_hash_ref,
        )


__all__ = [
    "EVIDENCE_KIND",
    "EVIDENCE_SEMANTIC_MARKERS",
    "EXPERIMENT_EVIDENCE_V1_MEDIA_TYPE",
    "EXPERIMENT_EVIDENCE_V1_SCHEMA_VERSION",
    "ExperimentEvidenceBuilderV1",
    "ExperimentEvidenceConflictError",
    "ExperimentEvidenceDiagnostics",
    "ExperimentEvidenceError",
    "ExperimentEvidencePersistenceError",
    "ExperimentEvidenceV1",
    "build_evidence_v1",
    "load_evidence_v1",
    "persist_evidence_v1",
    "verify_experiment_evidence_v1",
]
