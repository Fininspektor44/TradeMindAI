"""Final Holdout Decision Gate V1: authorization to proceed toward holdout evaluation.

CRITICAL: this layer MUST NOT open, read, inspect, evaluate, summarize, or
expose final holdout data. It imports nothing from ``holdout_crypto``,
``holdout_keys``, ``holdout_sealer``, ``holdout_runner``, or ``holdout_store``,
and it never constructs, decrypts, or reads a sealed holdout envelope. It only
decides whether an already-authoritative, already-PASSing Validation Decision
V1, for one frozen manifest, is authorized to be handed to a future, separate
Final Holdout Evaluation V1 layer -- the only layer that may ever open the
seal. It introduces no new canonicalizer, CAS, or hashing scheme; every
provenance primitive below it (Manifest, Result, Evidence, Validation
Decision) is reused unchanged.

Every closed layer below this one persists both PASS and FAIL outcomes as
faithful records of what happened. This layer does not: a FAIL validation
decision produces no authorization artifact at all -- not even a "denied"
record -- because there is nothing to authorize.  ``authorized`` is not a
settable field anywhere in this schema; the mere existence of a persisted
``FinalHoldoutAuthorizationV1`` *is* the authorization, and it is refused
before construction (``FinalHoldoutAuthorizationRejectedError``) rather than
represented as a false value inside one.

"Exactly one authoritative holdout authorization per scientific experiment"
is a cross-invocation invariant content-addressing alone cannot express (two
authorizations built moments apart, differing only in diagnostic timestamp,
would be two distinct-but-both-valid CAS objects). ``FinalHoldoutAuthorizationStore``
closes that gap with a small persistent table -- one row per hypothesis,
enforced the same way ``HypothesisRegistry.freeze_manifest_v2_in_transaction``
enforces one frozen manifest per hypothesis: idempotent retry when the new
grant is semantically identical to the one on file, a hard conflict when it
is not.
"""

from __future__ import annotations

import io
import re
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from trademind.discovery.hypothesis_registry import HypothesisRegistry, HypothesisState
from trademind.discovery.manifest import ExperimentManifestV2
from trademind.experiment_evidence import load_evidence_v1
from trademind.experiment_execution_contract import (
    CriteriaDecisionV1,
    EvaluatorRegistry,
    ExecutionPhase,
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
from trademind.validation_decision import (
    ValidationDecisionV1,
    ValidationOutcome,
    load_validation_decision_v1,
)

FINAL_HOLDOUT_AUTHORIZATION_V1_SCHEMA_VERSION = "final-holdout-authorization-v1"
FINAL_HOLDOUT_AUTHORIZATION_V1_MEDIA_TYPE = (
    "application/vnd.trademind.final-holdout-authorization-v1+json"
)
FINAL_HOLDOUT_AUTHORIZATION_KIND = "FINAL_HOLDOUT_AUTHORIZATION_V1"

# Fixed by this contract, exactly like every closed marker below it: there is
# deliberately no way to set any of these true here. Authorization to proceed
# is not a scientific-validation or trading claim, this layer never mutates
# HypothesisRegistry state, and it never reads final-holdout content.
FINAL_HOLDOUT_AUTHORIZATION_SEMANTIC_MARKERS = {
    "scientifically_validated": False,
    "trading_authorized": False,
    "final_holdout_consumed": False,
    "final_holdout_content_read": False,
    "provider_used": False,
    "ad_hoc_criteria_used": False,
    "hypothesis_registry_mutated": False,
}

_MACHINE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$")
_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")

_FINAL_HOLDOUT_AUTHORIZATION_HASH_DOMAIN = b"trademind:discovery:final-holdout-authorization:v1"


class FinalHoldoutDecisionGateError(ValueError):
    """Raised when a Final Holdout Authorization V1 is malformed, unbindable, or unbuildable."""


class FinalHoldoutAuthorizationRejectedError(FinalHoldoutDecisionGateError):
    """Raised when the source validation decision does not authorize holdout evaluation.

    Fires whenever ``validation_decision.outcome`` is not PASS. A FAIL can
    never authorize holdout evaluation, no matter how it is retried.
    """


class FinalHoldoutAuthorizationConflictError(FinalHoldoutDecisionGateError):
    """Raised when a recomputed criteria decision conflicts with the source validation decision."""


class FinalHoldoutAuthorizationPersistenceError(RuntimeError):
    """Raised when Verified CAS does not preserve the exact authorization identity."""


class FinalHoldoutAuthorizationStoreError(RuntimeError):
    """Raised when the one-authorization-per-hypothesis store cannot be used safely."""


class FinalHoldoutAuthorizationAlreadyGrantedError(FinalHoldoutAuthorizationStoreError):
    """Raised when a hypothesis already has a different final-holdout authorization on file."""


def _machine_identifier(value: object, *, field_name: str) -> str:
    if type(value) is not str or _MACHINE_IDENTIFIER.fullmatch(value) is None:
        raise FinalHoldoutDecisionGateError(
            f"{field_name} must be a bounded stable ASCII machine identifier"
        )
    return value


def _sha256_ref(value: object, *, field_name: str) -> str:
    try:
        return validate_sha256_ref(value)
    except ProvenanceError as exc:
        raise FinalHoldoutDecisionGateError(f"{field_name} is invalid") from exc


def _sha256_hex(value: object, *, field_name: str) -> str:
    if type(value) is not str or _SHA256_HEX.fullmatch(value) is None:
        raise FinalHoldoutDecisionGateError(f"{field_name} must be 64 lowercase hexadecimal digits")
    return value


def _utc_timestamp(value: object, *, field_name: str) -> str:
    if type(value) is not str:
        raise FinalHoldoutDecisionGateError(f"{field_name} must be an exact UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise FinalHoldoutDecisionGateError(f"{field_name} must be a canonical UTC timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise FinalHoldoutDecisionGateError(f"{field_name} must include the UTC timezone")
    if parsed.isoformat() != value:
        raise FinalHoldoutDecisionGateError(
            f"{field_name} must use canonical datetime.isoformat() encoding"
        )
    return value


def _positive_count(value: object, *, field_name: str) -> int:
    if type(value) is not int or not 1 <= value <= MAX_JSON_INTEGER_ABS:
        raise FinalHoldoutDecisionGateError(f"{field_name} must be an exact bounded positive integer")
    return value


def _audit_identity(value: object, *, field_name: str) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or len(value) > 256
        or any(ord(character) <= 0x1F or 0x7F <= ord(character) <= 0x9F for character in value)
    ):
        raise FinalHoldoutDecisionGateError(f"{field_name} must be a bounded audit identity")
    return value


def _exact_fields(payload: Mapping[str, object], *, required: frozenset[str], name: str) -> None:
    fields = frozenset(payload)
    missing = required - fields
    unknown = fields - required
    if missing:
        raise FinalHoldoutDecisionGateError(f"{name} is missing fields: {', '.join(sorted(missing))}")
    if unknown:
        raise FinalHoldoutDecisionGateError(f"{name} contains unknown fields: {', '.join(sorted(unknown))}")


_DIAGNOSTICS_FIELDS = frozenset({"created_at", "created_by"})


@dataclass(frozen=True, slots=True)
class FinalHoldoutAuthorizationDiagnostics:
    """Nonsemantic authorization metadata. Never affects semantic identity."""

    created_at: str
    created_by: str

    def __post_init__(self) -> None:
        _utc_timestamp(self.created_at, field_name="diagnostics.created_at")
        _audit_identity(self.created_by, field_name="diagnostics.created_by")
        canonical_json_bytes(self.to_payload())

    def to_payload(self) -> dict[str, object]:
        return {"created_at": self.created_at, "created_by": self.created_by}

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> "FinalHoldoutAuthorizationDiagnostics":
        frozen = freeze_json_object(payload, field_name="authorization_diagnostics")
        _exact_fields(frozen, required=_DIAGNOSTICS_FIELDS, name="authorization_diagnostics")
        return cls(created_at=frozen["created_at"], created_by=frozen["created_by"])


_AUTHORIZATION_FIELDS = frozenset(
    {
        "schema_version",
        "authorization_kind",
        "execution_identity",
        "execution_phase",
        "hypothesis",
        "manifest",
        "split",
        "holdout_window",
        "evaluator",
        "datasets",
        "validation_decision",
        "evidence",
        "result",
        "deterministic_seed",
        "code_provenance",
        "friction",
        "criteria_decision",
        "authorization_semantic_markers",
        "authorization_semantic_identity",
        "diagnostics",
    }
)


@dataclass(frozen=True, slots=True)
class FinalHoldoutAuthorizationV1:
    """Immutable authorization to proceed to a future Final Holdout Evaluation V1.

    Carries only identifiers, hashes, and the precommitted holdout *boundary*
    metadata (start/end timestamps and row count) already public within the
    frozen manifest's ``SplitPlan`` -- never a holdout row, metric, or result.
    ``authorization_semantic_markers`` is fixed by this contract exactly like
    every closed marker below it: always serialized as the same constant and
    never read back from the wire into any field.
    """

    schema_version: str
    authorization_kind: str
    execution_identity: str
    execution_phase: ExecutionPhase
    hypothesis_id: str
    hypothesis_family_id: str
    hypothesis_content_hash: str
    manifest_semantic_hash: str
    manifest_artifact_hash_ref: str
    split_plan_semantic_hash: str
    holdout_start: str
    holdout_end: str
    holdout_row_count: int
    evaluator_id: str
    evaluator_version: str
    dataset_artifact_hash_refs: tuple[str, ...]
    validation_decision_artifact_hash_ref: str
    validation_decision_semantic_identity: str
    evidence_artifact_hash_ref: str
    evidence_semantic_identity: str
    result_artifact_hash_ref: str
    result_semantic_identity: str
    deterministic_seed: int | None
    code_provenance: CodeProvenance
    friction_absent: bool
    friction_model_id: str | None
    friction_unit: str | None
    criteria_decision: CriteriaDecisionV1
    diagnostics: FinalHoldoutAuthorizationDiagnostics
    authorization_semantic_identity: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != FINAL_HOLDOUT_AUTHORIZATION_V1_SCHEMA_VERSION:
            raise FinalHoldoutDecisionGateError("unsupported final holdout authorization schema_version")
        if self.authorization_kind != FINAL_HOLDOUT_AUTHORIZATION_KIND:
            raise FinalHoldoutDecisionGateError("unsupported final holdout authorization kind")
        _machine_identifier(self.execution_identity, field_name="authorization.execution_identity")
        if self.execution_phase is not ExecutionPhase.VALIDATION:
            raise FinalHoldoutDecisionGateError(
                "final holdout authorization execution_phase must be VALIDATION"
            )
        _sha256_hex(self.hypothesis_content_hash, field_name="authorization.hypothesis_content_hash")
        _sha256_ref(self.manifest_semantic_hash, field_name="authorization.manifest_semantic_hash")
        _sha256_ref(
            self.manifest_artifact_hash_ref, field_name="authorization.manifest_artifact_hash_ref"
        )
        _sha256_ref(
            self.split_plan_semantic_hash, field_name="authorization.split_plan_semantic_hash"
        )
        _utc_timestamp(self.holdout_start, field_name="authorization.holdout_start")
        _utc_timestamp(self.holdout_end, field_name="authorization.holdout_end")
        if datetime.fromisoformat(self.holdout_start) > datetime.fromisoformat(self.holdout_end):
            raise FinalHoldoutDecisionGateError("authorization.holdout_start must not be after holdout_end")
        _positive_count(self.holdout_row_count, field_name="authorization.holdout_row_count")
        _machine_identifier(self.evaluator_id, field_name="authorization.evaluator_id")
        _machine_identifier(self.evaluator_version, field_name="authorization.evaluator_version")
        if type(self.dataset_artifact_hash_refs) is not tuple or not self.dataset_artifact_hash_refs:
            raise FinalHoldoutDecisionGateError(
                "authorization.dataset_artifact_hash_refs must be a non-empty tuple"
            )
        for ref in self.dataset_artifact_hash_refs:
            _sha256_ref(ref, field_name="authorization.dataset_artifact_hash_ref")
        _sha256_ref(
            self.validation_decision_artifact_hash_ref,
            field_name="authorization.validation_decision_artifact_hash_ref",
        )
        _sha256_ref(
            self.validation_decision_semantic_identity,
            field_name="authorization.validation_decision_semantic_identity",
        )
        _sha256_ref(self.evidence_artifact_hash_ref, field_name="authorization.evidence_artifact_hash_ref")
        _sha256_ref(
            self.evidence_semantic_identity, field_name="authorization.evidence_semantic_identity"
        )
        _sha256_ref(self.result_artifact_hash_ref, field_name="authorization.result_artifact_hash_ref")
        _sha256_ref(self.result_semantic_identity, field_name="authorization.result_semantic_identity")
        if self.deterministic_seed is not None and (
            type(self.deterministic_seed) is not int
            or not 0 <= self.deterministic_seed <= MAX_JSON_INTEGER_ABS
        ):
            raise FinalHoldoutDecisionGateError("authorization deterministic_seed is invalid")
        if type(self.code_provenance) is not CodeProvenance:
            raise FinalHoldoutDecisionGateError("authorization code_provenance must be CodeProvenance")
        if type(self.friction_absent) is not bool:
            raise FinalHoldoutDecisionGateError("authorization friction_absent must be bool")
        if self.friction_absent:
            if self.friction_model_id is not None or self.friction_unit is not None:
                raise FinalHoldoutDecisionGateError(
                    "no-friction authorization must not carry friction model/unit"
                )
        else:
            if not isinstance(self.friction_model_id, str) or not isinstance(self.friction_unit, str):
                raise FinalHoldoutDecisionGateError("friction authorization must carry exact model/unit")
            _machine_identifier(self.friction_model_id, field_name="authorization.friction_model_id")
            _machine_identifier(self.friction_unit, field_name="authorization.friction_unit")
        if type(self.criteria_decision) is not CriteriaDecisionV1:
            raise FinalHoldoutDecisionGateError(
                "authorization criteria_decision must be CriteriaDecisionV1"
            )
        if self.criteria_decision.passed is not True:
            raise FinalHoldoutAuthorizationRejectedError(
                "final holdout authorization requires a passed criteria decision"
            )
        object.__setattr__(
            self,
            "authorization_semantic_identity",
            sha256_bytes(
                _FINAL_HOLDOUT_AUTHORIZATION_HASH_DOMAIN
                + b"\x00"
                + canonical_json_bytes(self.semantic_projection())
            ),
        )
        canonical_json_bytes(self.to_payload())

    def semantic_projection(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "authorization_kind": self.authorization_kind,
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
            "holdout_window": {
                "holdout_start": self.holdout_start,
                "holdout_end": self.holdout_end,
                "holdout_row_count": self.holdout_row_count,
            },
            "evaluator": {
                "evaluator_id": self.evaluator_id,
                "evaluator_version": self.evaluator_version,
            },
            "datasets": list(self.dataset_artifact_hash_refs),
            "validation_decision": {
                "artifact_hash_ref": self.validation_decision_artifact_hash_ref,
                "semantic_identity": self.validation_decision_semantic_identity,
            },
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
            "criteria_decision": self.criteria_decision.to_payload(),
            "authorization_semantic_markers": dict(FINAL_HOLDOUT_AUTHORIZATION_SEMANTIC_MARKERS),
        }

    def to_payload(self) -> dict[str, object]:
        payload = self.semantic_projection()
        payload["authorization_semantic_identity"] = self.authorization_semantic_identity
        payload["diagnostics"] = self.diagnostics.to_payload()
        return payload

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_payload())

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> "FinalHoldoutAuthorizationV1":
        try:
            frozen = freeze_json_object(payload, field_name="final_holdout_authorization_v1")
        except ProvenanceError as exc:
            raise FinalHoldoutDecisionGateError(
                "final holdout authorization v1 must be strict bounded JSON"
            ) from exc
        _exact_fields(frozen, required=_AUTHORIZATION_FIELDS, name="final_holdout_authorization_v1")
        hypothesis = frozen["hypothesis"]
        manifest = frozen["manifest"]
        split = frozen["split"]
        holdout_window = frozen["holdout_window"]
        evaluator = frozen["evaluator"]
        datasets = frozen["datasets"]
        validation_decision = frozen["validation_decision"]
        evidence = frozen["evidence"]
        result = frozen["result"]
        friction = frozen["friction"]
        if not all(
            isinstance(x, Mapping)
            for x in (
                hypothesis,
                manifest,
                split,
                holdout_window,
                evaluator,
                validation_decision,
                evidence,
                result,
                friction,
            )
        ):
            raise FinalHoldoutDecisionGateError("authorization nested objects are malformed")
        if type(datasets) is not tuple:
            raise FinalHoldoutDecisionGateError("authorization datasets must be a JSON array")
        built = cls(
            schema_version=frozen["schema_version"],
            authorization_kind=frozen["authorization_kind"],
            execution_identity=frozen["execution_identity"],
            execution_phase=ExecutionPhase.from_value(frozen["execution_phase"]),
            hypothesis_id=hypothesis["hypothesis_id"],
            hypothesis_family_id=hypothesis["hypothesis_family_id"],
            hypothesis_content_hash=hypothesis["content_hash"],
            manifest_semantic_hash=manifest["semantic_hash"],
            manifest_artifact_hash_ref=manifest["artifact_hash_ref"],
            split_plan_semantic_hash=split["semantic_hash"],
            holdout_start=holdout_window["holdout_start"],
            holdout_end=holdout_window["holdout_end"],
            holdout_row_count=holdout_window["holdout_row_count"],
            evaluator_id=evaluator["evaluator_id"],
            evaluator_version=evaluator["evaluator_version"],
            dataset_artifact_hash_refs=tuple(datasets),
            validation_decision_artifact_hash_ref=validation_decision["artifact_hash_ref"],
            validation_decision_semantic_identity=validation_decision["semantic_identity"],
            evidence_artifact_hash_ref=evidence["artifact_hash_ref"],
            evidence_semantic_identity=evidence["semantic_identity"],
            result_artifact_hash_ref=result["artifact_hash_ref"],
            result_semantic_identity=result["semantic_identity"],
            deterministic_seed=frozen["deterministic_seed"],
            code_provenance=CodeProvenance.from_payload(frozen["code_provenance"]),
            friction_absent=friction["absent"],
            friction_model_id=friction["model_id"],
            friction_unit=friction["unit"],
            criteria_decision=CriteriaDecisionV1.from_payload(frozen["criteria_decision"]),
            diagnostics=FinalHoldoutAuthorizationDiagnostics.from_payload(frozen["diagnostics"]),
        )
        if built.authorization_semantic_identity != frozen["authorization_semantic_identity"]:
            raise FinalHoldoutDecisionGateError(
                "final holdout authorization semantic identity does not match its semantic projection"
            )
        return built


def build_final_holdout_authorization_v1(
    *,
    manifest: ExperimentManifestV2,
    validation_decision: ValidationDecisionV1,
    validation_decision_artifact_hash_ref: str,
    created_at: str,
    created_by: str,
) -> FinalHoldoutAuthorizationV1:
    """Build one immutable authorization bound to an authoritative PASS decision.

    The caller must already have reverified ``manifest`` (e.g. via
    ``HypothesisRegistry.load_bound_manifest_v2``) and ``validation_decision``
    (e.g. via ``load_validation_decision_v1`` against that same manifest,
    using the very same ``validation_decision_artifact_hash_ref`` passed
    here); this function performs its own defense-in-depth cross-checks but
    does not repeat CAS I/O and never touches final-holdout content.

    Refuses to run for anything but a PASS outcome, and always recomputes the
    manifest's predeclared criteria fresh against the decision's own observed
    metrics rather than trusting its stored decision.
    """
    if type(manifest) is not ExperimentManifestV2:
        raise FinalHoldoutDecisionGateError("manifest must be ExperimentManifestV2")
    if type(validation_decision) is not ValidationDecisionV1:
        raise FinalHoldoutDecisionGateError("validation_decision must be ValidationDecisionV1")
    validation_decision_artifact_hash_ref = _sha256_ref(
        validation_decision_artifact_hash_ref,
        field_name="validation_decision_artifact_hash_ref",
    )

    if validation_decision.execution_phase is not ExecutionPhase.VALIDATION:
        raise FinalHoldoutDecisionGateError(
            "final holdout authorization requires a VALIDATION-phase decision"
        )
    if validation_decision.outcome is not ValidationOutcome.PASS:
        raise FinalHoldoutAuthorizationRejectedError(
            "final holdout evaluation cannot be authorized from a non-PASS validation decision, "
            f"got {validation_decision.outcome.value}"
        )

    if validation_decision.manifest_semantic_hash != manifest.manifest_semantic_hash:
        raise FinalHoldoutDecisionGateError(
            "validation decision manifest binding does not match the authoritative manifest"
        )
    if (
        validation_decision.hypothesis_id != manifest.hypothesis_id
        or validation_decision.hypothesis_family_id != manifest.hypothesis_family_id
        or validation_decision.hypothesis_content_hash != manifest.bound_hypothesis_content_hash
    ):
        raise FinalHoldoutDecisionGateError(
            "validation decision hypothesis identity does not match the authoritative manifest"
        )
    if validation_decision.split_plan_semantic_hash != manifest.split_plan.semantic_hash:
        raise FinalHoldoutDecisionGateError(
            "validation decision split binding does not match the authoritative manifest"
        )
    expected_datasets = tuple(dataset.artifact_hash_ref for dataset in manifest.datasets)
    if validation_decision.dataset_artifact_hash_refs != expected_datasets:
        raise FinalHoldoutDecisionGateError(
            "validation decision dataset bindings do not match the authoritative manifest"
        )

    # The load-bearing check: never trust a stored decision. Evaluate the
    # manifest's predeclared criteria fresh against the decision's own
    # observed metrics; any disagreement is a hard conflict, and a decision
    # that no longer recomputes to PASS can never authorize anything.
    recomputed = evaluate_criteria(manifest.evaluation_criteria, validation_decision.observed_metrics)
    if recomputed != validation_decision.criteria_decision:
        raise FinalHoldoutAuthorizationConflictError(
            "criteria decision recomputed from the predeclared manifest criteria conflicts "
            "with the source validation decision's own criteria decision"
        )
    if not recomputed.passed:
        raise FinalHoldoutAuthorizationRejectedError(
            "recomputed criteria decision did not pass; final holdout evaluation cannot be authorized"
        )

    plan = manifest.split_plan
    return FinalHoldoutAuthorizationV1(
        schema_version=FINAL_HOLDOUT_AUTHORIZATION_V1_SCHEMA_VERSION,
        authorization_kind=FINAL_HOLDOUT_AUTHORIZATION_KIND,
        execution_identity=validation_decision.execution_identity,
        execution_phase=validation_decision.execution_phase,
        hypothesis_id=validation_decision.hypothesis_id,
        hypothesis_family_id=validation_decision.hypothesis_family_id,
        hypothesis_content_hash=validation_decision.hypothesis_content_hash,
        manifest_semantic_hash=validation_decision.manifest_semantic_hash,
        manifest_artifact_hash_ref=validation_decision.manifest_artifact_hash_ref,
        split_plan_semantic_hash=validation_decision.split_plan_semantic_hash,
        holdout_start=plan.holdout_start,
        holdout_end=plan.holdout_end,
        holdout_row_count=plan.holdout_count,
        evaluator_id=validation_decision.evaluator_id,
        evaluator_version=validation_decision.evaluator_version,
        dataset_artifact_hash_refs=validation_decision.dataset_artifact_hash_refs,
        validation_decision_artifact_hash_ref=validation_decision_artifact_hash_ref,
        validation_decision_semantic_identity=validation_decision.decision_semantic_identity,
        evidence_artifact_hash_ref=validation_decision.evidence_artifact_hash_ref,
        evidence_semantic_identity=validation_decision.evidence_semantic_identity,
        result_artifact_hash_ref=validation_decision.result_artifact_hash_ref,
        result_semantic_identity=validation_decision.result_semantic_identity,
        deterministic_seed=validation_decision.deterministic_seed,
        code_provenance=validation_decision.code_provenance,
        friction_absent=validation_decision.friction_absent,
        friction_model_id=validation_decision.friction_model_id,
        friction_unit=validation_decision.friction_unit,
        criteria_decision=recomputed,
        diagnostics=FinalHoldoutAuthorizationDiagnostics(created_at=created_at, created_by=created_by),
    )


def persist_final_holdout_authorization_v1(
    authorization: FinalHoldoutAuthorizationV1,
    *,
    artifact_store: ArtifactStore,
) -> ArtifactRef:
    """Persist exact canonical authorization bytes under Verified CAS."""
    if type(authorization) is not FinalHoldoutAuthorizationV1:
        raise FinalHoldoutDecisionGateError("authorization must be FinalHoldoutAuthorizationV1")
    exact_bytes = authorization.canonical_bytes()
    artifact = artifact_store.import_snapshot(
        io.BytesIO(exact_bytes),
        media_type=FINAL_HOLDOUT_AUTHORIZATION_V1_MEDIA_TYPE,
    )
    if artifact.hash_ref != sha256_bytes(exact_bytes):
        raise FinalHoldoutAuthorizationPersistenceError(
            "Verified CAS returned an unexpected final holdout authorization hash"
        )
    return artifact


def verify_final_holdout_authorization_v1(encoded: str | bytes) -> FinalHoldoutAuthorizationV1:
    """Verify strict canonical wire bytes and the recomputed semantic identity."""
    if type(encoded) is bytes:
        exact_bytes = encoded
    elif type(encoded) is str:
        try:
            exact_bytes = encoded.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise FinalHoldoutDecisionGateError(
                "final holdout authorization v1 must be valid UTF-8"
            ) from exc
    else:
        raise FinalHoldoutDecisionGateError(
            "final holdout authorization v1 wire payload must be exact str or bytes"
        )
    try:
        parsed = parse_json(exact_bytes)
    except ProvenanceError as exc:
        raise FinalHoldoutDecisionGateError(
            f"final holdout authorization v1 wire payload is invalid: {exc}"
        ) from exc
    if not isinstance(parsed, Mapping):
        raise FinalHoldoutDecisionGateError("final holdout authorization v1 root must be a JSON object")
    if canonical_json_bytes(parsed) != exact_bytes:
        raise FinalHoldoutDecisionGateError(
            "final holdout authorization v1 wire payload must use canonical JSON bytes"
        )
    return FinalHoldoutAuthorizationV1.from_payload(parsed)


def load_final_holdout_authorization_v1(
    authorization_artifact_hash_ref: str,
    *,
    artifact_store: ArtifactStore,
    manifest: ExperimentManifestV2,
    validation_decision: ValidationDecisionV1,
    validation_decision_artifact_hash_ref: str,
) -> FinalHoldoutAuthorizationV1:
    """Load and reverify an authorization against its authoritative manifest and decision."""
    if type(manifest) is not ExperimentManifestV2:
        raise FinalHoldoutDecisionGateError("manifest must be ExperimentManifestV2")
    if type(validation_decision) is not ValidationDecisionV1:
        raise FinalHoldoutDecisionGateError("validation_decision must be ValidationDecisionV1")
    validation_decision_artifact_hash_ref = _sha256_ref(
        validation_decision_artifact_hash_ref,
        field_name="validation_decision_artifact_hash_ref",
    )

    artifact = artifact_store.resolve_verified(
        authorization_artifact_hash_ref,
        expected_media_type=FINAL_HOLDOUT_AUTHORIZATION_V1_MEDIA_TYPE,
    )
    exact_bytes = artifact_store.read_verified(
        authorization_artifact_hash_ref,
        expected_media_type=FINAL_HOLDOUT_AUTHORIZATION_V1_MEDIA_TYPE,
    )
    if artifact.hash_ref != sha256_bytes(exact_bytes):
        raise FinalHoldoutAuthorizationPersistenceError("Verified CAS authorization identity mismatch")
    authorization = verify_final_holdout_authorization_v1(exact_bytes)

    if authorization.manifest_semantic_hash != manifest.manifest_semantic_hash:
        raise FinalHoldoutDecisionGateError(
            "authorization manifest binding does not match the authoritative manifest"
        )
    if authorization.split_plan_semantic_hash != manifest.split_plan.semantic_hash:
        raise FinalHoldoutDecisionGateError(
            "authorization split binding does not match the authoritative manifest"
        )
    if (
        authorization.holdout_start != manifest.split_plan.holdout_start
        or authorization.holdout_end != manifest.split_plan.holdout_end
        or authorization.holdout_row_count != manifest.split_plan.holdout_count
    ):
        raise FinalHoldoutDecisionGateError(
            "authorization holdout window does not match the authoritative manifest split plan"
        )
    if (
        authorization.hypothesis_id != manifest.hypothesis_id
        or authorization.hypothesis_family_id != manifest.hypothesis_family_id
        or authorization.hypothesis_content_hash != manifest.bound_hypothesis_content_hash
    ):
        raise FinalHoldoutDecisionGateError("authorization hypothesis identity mismatch")
    expected_datasets = tuple(dataset.artifact_hash_ref for dataset in manifest.datasets)
    if authorization.dataset_artifact_hash_refs != expected_datasets:
        raise FinalHoldoutDecisionGateError(
            "authorization dataset bindings do not match the authoritative manifest"
        )

    if (
        authorization.validation_decision_artifact_hash_ref != validation_decision_artifact_hash_ref
        or authorization.validation_decision_semantic_identity
        != validation_decision.decision_semantic_identity
        or authorization.execution_identity != validation_decision.execution_identity
        or authorization.execution_phase != validation_decision.execution_phase
        or authorization.evaluator_id != validation_decision.evaluator_id
        or authorization.evaluator_version != validation_decision.evaluator_version
        or authorization.evidence_artifact_hash_ref != validation_decision.evidence_artifact_hash_ref
        or authorization.evidence_semantic_identity != validation_decision.evidence_semantic_identity
        or authorization.result_artifact_hash_ref != validation_decision.result_artifact_hash_ref
        or authorization.result_semantic_identity != validation_decision.result_semantic_identity
        or authorization.deterministic_seed != validation_decision.deterministic_seed
        or authorization.code_provenance != validation_decision.code_provenance
        or authorization.friction_absent != validation_decision.friction_absent
        or authorization.friction_model_id != validation_decision.friction_model_id
        or authorization.friction_unit != validation_decision.friction_unit
    ):
        raise FinalHoldoutDecisionGateError(
            "authorization does not match its authoritative source validation decision"
        )

    if validation_decision.outcome is not ValidationOutcome.PASS:
        raise FinalHoldoutAuthorizationRejectedError(
            "a final holdout authorization can be reverified only against a PASS validation decision"
        )

    # Never trust the stored decision on reload either.
    recomputed = evaluate_criteria(manifest.evaluation_criteria, validation_decision.observed_metrics)
    if (
        recomputed != authorization.criteria_decision
        or recomputed != validation_decision.criteria_decision
        or not recomputed.passed
    ):
        raise FinalHoldoutAuthorizationConflictError(
            "criteria decision recomputed from the predeclared manifest criteria conflicts "
            "with the persisted authorization or its source validation decision"
        )

    return authorization


# ---------------------------------------------------------------------------
# Persistent one-authorization-per-hypothesis store
# ---------------------------------------------------------------------------

_AUTHORIZATION_TABLE_COLUMNS = frozenset(
    {
        "hypothesis_id",
        "family_id",
        "manifest_semantic_hash",
        "authorization_semantic_identity",
        "authorization_artifact_hash_ref",
        "validation_decision_semantic_identity",
        "created_at",
    }
)


@dataclass(frozen=True, slots=True)
class FinalHoldoutAuthorizationRecord:
    hypothesis_id: str
    hypothesis_family_id: str
    manifest_semantic_hash: str
    authorization_semantic_identity: str
    authorization_artifact_hash_ref: str
    validation_decision_semantic_identity: str
    created_at: str


class FinalHoldoutAuthorizationStore:
    """Enforce exactly one authoritative final-holdout authorization per hypothesis.

    A DB rollback after a Verified CAS write may leave an unreferenced
    immutable CAS object, exactly like ``ExperimentManifestCreationControl``'s
    own documented behavior; the registry never commits a binding until the
    exact authorization content has been compared inside the transaction.
    """

    def __init__(self, registry: HypothesisRegistry) -> None:
        if not isinstance(registry, HypothesisRegistry):
            raise TypeError("registry must be HypothesisRegistry")
        self.registry = registry
        self.path = Path(registry.path)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30.0)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        return db

    def _init_schema(self) -> None:
        with self._connect() as db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS final_holdout_authorizations (
                    hypothesis_id TEXT PRIMARY KEY,
                    family_id TEXT NOT NULL,
                    manifest_semantic_hash TEXT NOT NULL,
                    authorization_semantic_identity TEXT NOT NULL,
                    authorization_artifact_hash_ref TEXT NOT NULL,
                    validation_decision_semantic_identity TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(hypothesis_id) REFERENCES hypotheses(hypothesis_id),
                    FOREIGN KEY(family_id) REFERENCES hypothesis_families(family_id)
                )
                """
            )
            columns = {
                row["name"]
                for row in db.execute("PRAGMA table_info(final_holdout_authorizations)").fetchall()
            }
            if not _AUTHORIZATION_TABLE_COLUMNS.issubset(columns):
                raise FinalHoldoutAuthorizationStoreError(
                    "legacy final_holdout_authorizations schema detected; explicit migration is required"
                )

    @staticmethod
    def _record(row: sqlite3.Row) -> FinalHoldoutAuthorizationRecord:
        return FinalHoldoutAuthorizationRecord(
            hypothesis_id=row["hypothesis_id"],
            hypothesis_family_id=row["family_id"],
            manifest_semantic_hash=row["manifest_semantic_hash"],
            authorization_semantic_identity=row["authorization_semantic_identity"],
            authorization_artifact_hash_ref=row["authorization_artifact_hash_ref"],
            validation_decision_semantic_identity=row["validation_decision_semantic_identity"],
            created_at=row["created_at"],
        )

    def get(self, hypothesis_id: str) -> FinalHoldoutAuthorizationRecord:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM final_holdout_authorizations WHERE hypothesis_id=?",
                (hypothesis_id,),
            ).fetchone()
        if row is None:
            raise KeyError(hypothesis_id)
        return self._record(row)

    def register(
        self,
        authorization: FinalHoldoutAuthorizationV1,
        *,
        authorization_artifact_hash_ref: str,
    ) -> FinalHoldoutAuthorizationRecord:
        """Register the one authoritative grant for this hypothesis, or confirm it exists.

        Idempotent when the new grant is semantically identical (same
        manifest, same validation decision, same authorization content) to
        the one already on file; a hard conflict otherwise.
        """
        if type(authorization) is not FinalHoldoutAuthorizationV1:
            raise FinalHoldoutAuthorizationStoreError("authorization must be FinalHoldoutAuthorizationV1")
        authorization_artifact_hash_ref = _sha256_ref(
            authorization_artifact_hash_ref, field_name="authorization_artifact_hash_ref"
        )
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT * FROM final_holdout_authorizations WHERE hypothesis_id=?",
                (authorization.hypothesis_id,),
            ).fetchone()
            if existing is not None:
                current = self._record(existing)
                if (
                    current.manifest_semantic_hash != authorization.manifest_semantic_hash
                    or current.authorization_semantic_identity
                    != authorization.authorization_semantic_identity
                    or current.validation_decision_semantic_identity
                    != authorization.validation_decision_semantic_identity
                ):
                    raise FinalHoldoutAuthorizationAlreadyGrantedError(
                        "hypothesis already has a different final-holdout authorization on file"
                    )
                return current
            try:
                db.execute(
                    """
                    INSERT INTO final_holdout_authorizations(
                        hypothesis_id, family_id, manifest_semantic_hash,
                        authorization_semantic_identity, authorization_artifact_hash_ref,
                        validation_decision_semantic_identity, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        authorization.hypothesis_id,
                        authorization.hypothesis_family_id,
                        authorization.manifest_semantic_hash,
                        authorization.authorization_semantic_identity,
                        authorization_artifact_hash_ref,
                        authorization.validation_decision_semantic_identity,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise FinalHoldoutAuthorizationAlreadyGrantedError(
                    "hypothesis final-holdout authorization changed concurrently"
                ) from exc
        return self.get(authorization.hypothesis_id)


# ---------------------------------------------------------------------------
# End-to-end gate
# ---------------------------------------------------------------------------


class FinalHoldoutDecisionGateV1:
    """Trusted assembly of a Final Holdout Authorization from closed layers, end to end.

    Construction binds the same registry, Verified CAS root, and declarative
    evaluator registry the runtime/evidence/decision layers use, plus the
    persistent ``FinalHoldoutAuthorizationStore``. ``authorize`` accepts only
    explicit CAS references and independently reloads and reverifies the
    authoritative frozen manifest, execution result, evidence, and validation
    decision -- in that order, each through its own closed reverification
    primitive -- before any authorization is constructed. It never opens,
    decrypts, or reads final-holdout content, and it never mutates
    HypothesisRegistry state; that remains the province of a future,
    separately governed layer.
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
        self.store = FinalHoldoutAuthorizationStore(registry)

    def _load_authoritative_manifest(self, hypothesis_id: str) -> ExperimentManifestV2:
        manifest = self.registry.load_bound_manifest_v2(
            hypothesis_id,
            artifact_store=self.artifact_store,
        )
        record = self.registry.get(hypothesis_id)
        if record.state is not HypothesisState.FROZEN:
            raise FinalHoldoutDecisionGateError(  # pragma: no cover - registry guarantees this.
                "final holdout authorization requires an authoritative FROZEN hypothesis"
            )
        return manifest

    def _load_authoritative_decision(
        self,
        hypothesis_id: str,
        *,
        validation_decision_artifact_hash_ref: str,
        evidence_artifact_hash_ref: str,
        result_artifact_hash_ref: str,
        manifest: ExperimentManifestV2,
    ) -> ValidationDecisionV1:
        record = self.registry.get(hypothesis_id)
        result = load_result_v1(
            result_artifact_hash_ref,
            artifact_store=self.artifact_store,
            manifest=manifest,
            manifest_artifact_hash_ref=record.manifest_artifact_hash_ref,
            registry=self.evaluator_registry,
        )
        evidence = load_evidence_v1(
            evidence_artifact_hash_ref,
            artifact_store=self.artifact_store,
            manifest=manifest,
            manifest_artifact_hash_ref=record.manifest_artifact_hash_ref,
            result=result,
            result_artifact_hash_ref=result_artifact_hash_ref,
        )
        return load_validation_decision_v1(
            validation_decision_artifact_hash_ref,
            artifact_store=self.artifact_store,
            manifest=manifest,
            evidence=evidence,
            evidence_artifact_hash_ref=evidence_artifact_hash_ref,
        )

    def authorize(
        self,
        hypothesis_id: str,
        *,
        validation_decision_artifact_hash_ref: str,
        evidence_artifact_hash_ref: str,
        result_artifact_hash_ref: str,
        created_at: str,
        created_by: str,
    ) -> tuple[FinalHoldoutAuthorizationV1, ArtifactRef]:
        manifest = self._load_authoritative_manifest(hypothesis_id)
        decision = self._load_authoritative_decision(
            hypothesis_id,
            validation_decision_artifact_hash_ref=validation_decision_artifact_hash_ref,
            evidence_artifact_hash_ref=evidence_artifact_hash_ref,
            result_artifact_hash_ref=result_artifact_hash_ref,
            manifest=manifest,
        )
        authorization = build_final_holdout_authorization_v1(
            manifest=manifest,
            validation_decision=decision,
            validation_decision_artifact_hash_ref=validation_decision_artifact_hash_ref,
            created_at=created_at,
            created_by=created_by,
        )
        artifact = persist_final_holdout_authorization_v1(authorization, artifact_store=self.artifact_store)
        self.store.register(authorization, authorization_artifact_hash_ref=artifact.hash_ref)
        return authorization, artifact

    def load(
        self,
        hypothesis_id: str,
        *,
        authorization_artifact_hash_ref: str,
        validation_decision_artifact_hash_ref: str,
        evidence_artifact_hash_ref: str,
        result_artifact_hash_ref: str,
    ) -> FinalHoldoutAuthorizationV1:
        manifest = self._load_authoritative_manifest(hypothesis_id)
        decision = self._load_authoritative_decision(
            hypothesis_id,
            validation_decision_artifact_hash_ref=validation_decision_artifact_hash_ref,
            evidence_artifact_hash_ref=evidence_artifact_hash_ref,
            result_artifact_hash_ref=result_artifact_hash_ref,
            manifest=manifest,
        )
        authorization = load_final_holdout_authorization_v1(
            authorization_artifact_hash_ref,
            artifact_store=self.artifact_store,
            manifest=manifest,
            validation_decision=decision,
            validation_decision_artifact_hash_ref=validation_decision_artifact_hash_ref,
        )
        record = self.store.get(hypothesis_id)
        if (
            record.authorization_semantic_identity != authorization.authorization_semantic_identity
            or record.authorization_artifact_hash_ref != authorization_artifact_hash_ref
        ):
            raise FinalHoldoutAuthorizationAlreadyGrantedError(
                "persisted authorization does not match the one-authorization-per-hypothesis store"
            )
        return authorization


__all__ = [
    "FINAL_HOLDOUT_AUTHORIZATION_KIND",
    "FINAL_HOLDOUT_AUTHORIZATION_SEMANTIC_MARKERS",
    "FINAL_HOLDOUT_AUTHORIZATION_V1_MEDIA_TYPE",
    "FINAL_HOLDOUT_AUTHORIZATION_V1_SCHEMA_VERSION",
    "FinalHoldoutAuthorizationAlreadyGrantedError",
    "FinalHoldoutAuthorizationConflictError",
    "FinalHoldoutAuthorizationDiagnostics",
    "FinalHoldoutAuthorizationPersistenceError",
    "FinalHoldoutAuthorizationRecord",
    "FinalHoldoutAuthorizationRejectedError",
    "FinalHoldoutAuthorizationStore",
    "FinalHoldoutAuthorizationStoreError",
    "FinalHoldoutAuthorizationV1",
    "FinalHoldoutDecisionGateError",
    "FinalHoldoutDecisionGateV1",
    "build_final_holdout_authorization_v1",
    "load_final_holdout_authorization_v1",
    "persist_final_holdout_authorization_v1",
    "verify_final_holdout_authorization_v1",
]
