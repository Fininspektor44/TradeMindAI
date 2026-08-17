"""Final Holdout Evaluation V1: one-shot consumption and terminal verdict.

This is the top layer of the discovery/validation/holdout pipeline. It
consumes an authoritative Final Holdout Authorization V1 and the EXISTING
holdout infrastructure -- unchanged -- to produce one immutable, deterministic
PASS/FAIL verdict from the truly-final holdout, and to carry
``HypothesisRegistry`` through its existing lifecycle to a terminal state.

It deliberately creates NO new holdout runner, sealer, ledger, registry, or
canonicalization system. The one-shot consumption boundary, envelope
integrity, key authentication, and side-effect-guarded evaluator invocation
are entirely delegated to the closed ``FinalHoldoutRunner.run_once`` -- this
module never decrypts, never touches plaintext, and never re-implements any
part of that boundary. Sealing and plaintext-isolation attestation
(``FinalHoldoutSealer``, ``HoldoutSealStore.mark_isolated``) are an earlier,
separate, out-of-band operational step this module assumes has already
happened -- exactly the same precondition ``FinalHoldoutRunner`` itself
assumes. This module never calls ``FinalHoldoutSealer`` or
``HoldoutSealStore.mark_isolated``.

``HypothesisRegistry.load_bound_manifest_v2`` requires FROZEN state, but this
module's whole job is to drive a hypothesis *through* FROZEN -> TRAIN_TESTED
-> VALIDATION_PASSED -> HOLDOUT_CONSUMED -> {ACCEPTED, REJECTED_FINAL}, so it
cannot be the loader used past the first transition. This module instead
reuses the exact same closed primitive that wrapper calls internally
(``load_experiment_manifest_v2``) plus the same identity cross-checks, state-
independently -- precisely how ``FinalHoldoutSealer``/``FinalHoldoutRunner``
themselves already operate on non-FROZEN hypotheses via
``record.manifest_hash`` rather than that wrapper. Nothing in
``hypothesis_registry.py`` or ``manifest.py`` is modified.

Every registry transition this module performs is the existing
``HypothesisRegistry.transition`` with its existing legality and
holdout-isolation guards, unmodified -- this module supplies no bypass.
"""

from __future__ import annotations

import io
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from trademind.discovery.holdout_runner import FinalHoldoutRunner
from trademind.discovery.hypothesis_registry import (
    HypothesisRecord,
    HypothesisRegistry,
    HypothesisState,
)
from trademind.discovery.manifest import (
    EXPERIMENT_MANIFEST_V2_SCHEMA_VERSION,
    ExperimentManifestV2,
    load_experiment_manifest_v2,
)
from trademind.experiment_evidence import load_evidence_v1
from trademind.experiment_execution_contract import (
    CriteriaDecisionV1,
    EvaluatorRegistry,
    ObservedMetricsV1,
    evaluate_criteria,
    load_result_v1,
)
from trademind.final_holdout_decision_gate import (
    FinalHoldoutAuthorizationStore,
    FinalHoldoutAuthorizationV1,
    load_final_holdout_authorization_v1,
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
from trademind.validation_decision import ValidationOutcome, load_validation_decision_v1

FINAL_HOLDOUT_RESULT_V1_SCHEMA_VERSION = "final-holdout-result-v1"
FINAL_HOLDOUT_RESULT_V1_MEDIA_TYPE = "application/vnd.trademind.final-holdout-result-v1+json"
FINAL_HOLDOUT_RESULT_KIND = "FINAL_HOLDOUT_RESULT_V1"

# Ledger record type this module appends to the SAME ResultLedger FinalHoldoutRunner
# already writes to; the record type string is namespaced away from the runner's own
# FINAL_HOLDOUT_INTENT/CLAIM/RESULT/RUN_FAILED types to avoid any ambiguity when scanning.
_EVALUATION_LEDGER_RECORD_TYPE = "FINAL_HOLDOUT_EVALUATION_V1_RESULT"
# The runner's own ledger record type this module reads (never writes) for crash recovery.
_RUNNER_RESULT_LEDGER_RECORD_TYPE = "FINAL_HOLDOUT_RESULT"

_MACHINE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$")
_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")

_FINAL_HOLDOUT_RESULT_HASH_DOMAIN = b"trademind:discovery:final-holdout-result:v1"


class FinalHoldoutEvaluationError(ValueError):
    """Raised when a Final Holdout Result V1 is malformed, unbindable, or unbuildable."""


class FinalHoldoutEvaluationConflictError(FinalHoldoutEvaluationError):
    """Raised when supplied lineage, a recomputed decision, or registry state conflicts."""


class FinalHoldoutEvaluationPersistenceError(RuntimeError):
    """Raised when Verified CAS does not preserve the exact final holdout result identity."""


def _machine_identifier(value: object, *, field_name: str) -> str:
    if type(value) is not str or _MACHINE_IDENTIFIER.fullmatch(value) is None:
        raise FinalHoldoutEvaluationError(
            f"{field_name} must be a bounded stable ASCII machine identifier"
        )
    return value


def _sha256_ref(value: object, *, field_name: str) -> str:
    try:
        return validate_sha256_ref(value)
    except ProvenanceError as exc:
        raise FinalHoldoutEvaluationError(f"{field_name} is invalid") from exc


def _sha256_hex(value: object, *, field_name: str) -> str:
    if type(value) is not str or _SHA256_HEX.fullmatch(value) is None:
        raise FinalHoldoutEvaluationError(f"{field_name} must be 64 lowercase hexadecimal digits")
    return value


def _utc_timestamp(value: object, *, field_name: str) -> str:
    if type(value) is not str:
        raise FinalHoldoutEvaluationError(f"{field_name} must be an exact UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise FinalHoldoutEvaluationError(f"{field_name} must be a canonical UTC timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise FinalHoldoutEvaluationError(f"{field_name} must include the UTC timezone")
    if parsed.isoformat() != value:
        raise FinalHoldoutEvaluationError(
            f"{field_name} must use canonical datetime.isoformat() encoding"
        )
    return value


def _positive_count(value: object, *, field_name: str) -> int:
    if type(value) is not int or not 1 <= value <= MAX_JSON_INTEGER_ABS:
        raise FinalHoldoutEvaluationError(f"{field_name} must be an exact bounded positive integer")
    return value


def _audit_identity(value: object, *, field_name: str) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or len(value) > 256
        or any(ord(character) <= 0x1F or 0x7F <= ord(character) <= 0x9F for character in value)
    ):
        raise FinalHoldoutEvaluationError(f"{field_name} must be a bounded audit identity")
    return value


def _exact_fields(payload: Mapping[str, object], *, required: frozenset[str], name: str) -> None:
    fields = frozenset(payload)
    missing = required - fields
    unknown = fields - required
    if missing:
        raise FinalHoldoutEvaluationError(f"{name} is missing fields: {', '.join(sorted(missing))}")
    if unknown:
        raise FinalHoldoutEvaluationError(f"{name} contains unknown fields: {', '.join(sorted(unknown))}")


_DIAGNOSTICS_FIELDS = frozenset({"created_at", "created_by"})


@dataclass(frozen=True, slots=True)
class FinalHoldoutResultDiagnostics:
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
    def from_payload(cls, payload: Mapping[str, object]) -> "FinalHoldoutResultDiagnostics":
        frozen = freeze_json_object(payload, field_name="final_holdout_result_diagnostics")
        _exact_fields(frozen, required=_DIAGNOSTICS_FIELDS, name="final_holdout_result_diagnostics")
        return cls(created_at=frozen["created_at"], created_by=frozen["created_by"])


_FINAL_HOLDOUT_RESULT_FIELDS = frozenset(
    {
        "schema_version",
        "result_kind",
        "outcome",
        "hypothesis",
        "manifest",
        "split",
        "holdout_window",
        "datasets",
        "authorization",
        "validation_decision",
        "evidence",
        "result",
        "seal",
        "deterministic_seed",
        "code_provenance",
        "friction",
        "observed_metrics",
        "criteria_decision",
        "final_holdout_semantic_markers",
        "final_holdout_result_semantic_identity",
        "diagnostics",
    }
)


@dataclass(frozen=True, slots=True)
class FinalHoldoutResultV1:
    """Immutable, machine-readable terminal verdict from the truly-final holdout.

    Carries the full provenance chain (manifest, authorization, validation
    decision, evidence, execution result, code provenance, and the holdout
    seal's envelope/evaluator identity) plus only the *aggregate* holdout
    metrics the trusted ``HoldoutEvaluator`` returned -- never a row, never
    plaintext. ``final_holdout_semantic_markers`` is the first outcome-
    dependent marker set in this pipeline: ``scientifically_validated`` is
    finally allowed to be true here, exactly when ``outcome`` is PASS, since
    this is the one point where discovery, validation, and the untouched
    holdout have all agreed. ``trading_authorized`` remains permanently
    false regardless of outcome -- operational trading authorization is a
    distinct, separately governed concern outside this pipeline.
    """

    schema_version: str
    result_kind: str
    outcome: ValidationOutcome
    hypothesis_id: str
    hypothesis_family_id: str
    hypothesis_content_hash: str
    manifest_semantic_hash: str
    manifest_artifact_hash_ref: str
    split_plan_semantic_hash: str
    holdout_start: str
    holdout_end: str
    holdout_row_count: int
    dataset_artifact_hash_refs: tuple[str, ...]
    authorization_artifact_hash_ref: str
    authorization_semantic_identity: str
    validation_decision_artifact_hash_ref: str
    validation_decision_semantic_identity: str
    evidence_artifact_hash_ref: str
    evidence_semantic_identity: str
    result_artifact_hash_ref: str
    result_semantic_identity: str
    envelope_hash: str
    holdout_evaluator_id: str
    holdout_evaluator_hash: str
    deterministic_seed: int | None
    code_provenance: CodeProvenance
    friction_absent: bool
    friction_model_id: str | None
    friction_unit: str | None
    observed_metrics: ObservedMetricsV1
    criteria_decision: CriteriaDecisionV1
    diagnostics: FinalHoldoutResultDiagnostics
    final_holdout_result_semantic_identity: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != FINAL_HOLDOUT_RESULT_V1_SCHEMA_VERSION:
            raise FinalHoldoutEvaluationError("unsupported final holdout result schema_version")
        if self.result_kind != FINAL_HOLDOUT_RESULT_KIND:
            raise FinalHoldoutEvaluationError("unsupported final holdout result kind")
        if type(self.outcome) is not ValidationOutcome:
            raise FinalHoldoutEvaluationError("final holdout result outcome must be ValidationOutcome")
        _sha256_hex(self.hypothesis_content_hash, field_name="result.hypothesis_content_hash")
        _sha256_ref(self.manifest_semantic_hash, field_name="result.manifest_semantic_hash")
        _sha256_ref(self.manifest_artifact_hash_ref, field_name="result.manifest_artifact_hash_ref")
        _sha256_ref(self.split_plan_semantic_hash, field_name="result.split_plan_semantic_hash")
        _utc_timestamp(self.holdout_start, field_name="result.holdout_start")
        _utc_timestamp(self.holdout_end, field_name="result.holdout_end")
        if datetime.fromisoformat(self.holdout_start) > datetime.fromisoformat(self.holdout_end):
            raise FinalHoldoutEvaluationError("result.holdout_start must not be after holdout_end")
        _positive_count(self.holdout_row_count, field_name="result.holdout_row_count")
        if type(self.dataset_artifact_hash_refs) is not tuple or not self.dataset_artifact_hash_refs:
            raise FinalHoldoutEvaluationError("result.dataset_artifact_hash_refs must be a non-empty tuple")
        for ref in self.dataset_artifact_hash_refs:
            _sha256_ref(ref, field_name="result.dataset_artifact_hash_ref")
        _sha256_ref(
            self.authorization_artifact_hash_ref, field_name="result.authorization_artifact_hash_ref"
        )
        _sha256_ref(
            self.authorization_semantic_identity, field_name="result.authorization_semantic_identity"
        )
        _sha256_ref(
            self.validation_decision_artifact_hash_ref,
            field_name="result.validation_decision_artifact_hash_ref",
        )
        _sha256_ref(
            self.validation_decision_semantic_identity,
            field_name="result.validation_decision_semantic_identity",
        )
        _sha256_ref(self.evidence_artifact_hash_ref, field_name="result.evidence_artifact_hash_ref")
        _sha256_ref(self.evidence_semantic_identity, field_name="result.evidence_semantic_identity")
        _sha256_ref(self.result_artifact_hash_ref, field_name="result.result_artifact_hash_ref")
        _sha256_ref(self.result_semantic_identity, field_name="result.result_semantic_identity")
        _sha256_hex(self.envelope_hash, field_name="result.envelope_hash")
        _machine_identifier(self.holdout_evaluator_id, field_name="result.holdout_evaluator_id")
        _sha256_hex(self.holdout_evaluator_hash, field_name="result.holdout_evaluator_hash")
        if self.deterministic_seed is not None and (
            type(self.deterministic_seed) is not int
            or not 0 <= self.deterministic_seed <= MAX_JSON_INTEGER_ABS
        ):
            raise FinalHoldoutEvaluationError("result deterministic_seed is invalid")
        if type(self.code_provenance) is not CodeProvenance:
            raise FinalHoldoutEvaluationError("result code_provenance must be CodeProvenance")
        if type(self.friction_absent) is not bool:
            raise FinalHoldoutEvaluationError("result friction_absent must be bool")
        if self.friction_absent:
            if self.friction_model_id is not None or self.friction_unit is not None:
                raise FinalHoldoutEvaluationError("no-friction result must not carry friction model/unit")
        else:
            if not isinstance(self.friction_model_id, str) or not isinstance(self.friction_unit, str):
                raise FinalHoldoutEvaluationError("friction result must carry exact model/unit")
            _machine_identifier(self.friction_model_id, field_name="result.friction_model_id")
            _machine_identifier(self.friction_unit, field_name="result.friction_unit")
        if type(self.observed_metrics) is not ObservedMetricsV1:
            raise FinalHoldoutEvaluationError("result observed_metrics must be ObservedMetricsV1")
        if type(self.criteria_decision) is not CriteriaDecisionV1:
            raise FinalHoldoutEvaluationError("result criteria_decision must be CriteriaDecisionV1")
        if self.criteria_decision.primary_metric != self.observed_metrics.primary_metric:
            raise FinalHoldoutEvaluationError(
                "criteria decision primary metric must match observed metrics primary metric"
            )
        if (self.outcome is ValidationOutcome.PASS) != self.criteria_decision.passed:
            raise FinalHoldoutEvaluationError("result outcome must match its criteria decision")
        object.__setattr__(
            self,
            "final_holdout_result_semantic_identity",
            sha256_bytes(
                _FINAL_HOLDOUT_RESULT_HASH_DOMAIN
                + b"\x00"
                + canonical_json_bytes(self.semantic_projection())
            ),
        )
        canonical_json_bytes(self.to_payload())

    def semantic_projection(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "result_kind": self.result_kind,
            "outcome": self.outcome.value,
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
            "datasets": list(self.dataset_artifact_hash_refs),
            "authorization": {
                "artifact_hash_ref": self.authorization_artifact_hash_ref,
                "semantic_identity": self.authorization_semantic_identity,
            },
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
            "seal": {
                "envelope_hash": self.envelope_hash,
                "holdout_evaluator_id": self.holdout_evaluator_id,
                "holdout_evaluator_hash": self.holdout_evaluator_hash,
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
            "final_holdout_semantic_markers": {
                "scientifically_validated": self.outcome is ValidationOutcome.PASS,
                "trading_authorized": False,
                "final_holdout_consumed": True,
                "provider_used": False,
                "ad_hoc_criteria_used": False,
            },
        }

    def to_payload(self) -> dict[str, object]:
        payload = self.semantic_projection()
        payload["final_holdout_result_semantic_identity"] = self.final_holdout_result_semantic_identity
        payload["diagnostics"] = self.diagnostics.to_payload()
        return payload

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_payload())

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> "FinalHoldoutResultV1":
        try:
            frozen = freeze_json_object(payload, field_name="final_holdout_result_v1")
        except ProvenanceError as exc:
            raise FinalHoldoutEvaluationError(
                "final holdout result v1 must be strict bounded JSON"
            ) from exc
        _exact_fields(frozen, required=_FINAL_HOLDOUT_RESULT_FIELDS, name="final_holdout_result_v1")
        hypothesis = frozen["hypothesis"]
        manifest = frozen["manifest"]
        split = frozen["split"]
        holdout_window = frozen["holdout_window"]
        datasets = frozen["datasets"]
        authorization = frozen["authorization"]
        validation_decision = frozen["validation_decision"]
        evidence = frozen["evidence"]
        result = frozen["result"]
        seal = frozen["seal"]
        friction = frozen["friction"]
        if not all(
            isinstance(x, Mapping)
            for x in (
                hypothesis,
                manifest,
                split,
                holdout_window,
                authorization,
                validation_decision,
                evidence,
                result,
                seal,
                friction,
            )
        ):
            raise FinalHoldoutEvaluationError("final holdout result nested objects are malformed")
        if type(datasets) is not tuple:
            raise FinalHoldoutEvaluationError("final holdout result datasets must be a JSON array")
        built = cls(
            schema_version=frozen["schema_version"],
            result_kind=frozen["result_kind"],
            outcome=ValidationOutcome.from_value(frozen["outcome"]),
            hypothesis_id=hypothesis["hypothesis_id"],
            hypothesis_family_id=hypothesis["hypothesis_family_id"],
            hypothesis_content_hash=hypothesis["content_hash"],
            manifest_semantic_hash=manifest["semantic_hash"],
            manifest_artifact_hash_ref=manifest["artifact_hash_ref"],
            split_plan_semantic_hash=split["semantic_hash"],
            holdout_start=holdout_window["holdout_start"],
            holdout_end=holdout_window["holdout_end"],
            holdout_row_count=holdout_window["holdout_row_count"],
            dataset_artifact_hash_refs=tuple(datasets),
            authorization_artifact_hash_ref=authorization["artifact_hash_ref"],
            authorization_semantic_identity=authorization["semantic_identity"],
            validation_decision_artifact_hash_ref=validation_decision["artifact_hash_ref"],
            validation_decision_semantic_identity=validation_decision["semantic_identity"],
            evidence_artifact_hash_ref=evidence["artifact_hash_ref"],
            evidence_semantic_identity=evidence["semantic_identity"],
            result_artifact_hash_ref=result["artifact_hash_ref"],
            result_semantic_identity=result["semantic_identity"],
            envelope_hash=seal["envelope_hash"],
            holdout_evaluator_id=seal["holdout_evaluator_id"],
            holdout_evaluator_hash=seal["holdout_evaluator_hash"],
            deterministic_seed=frozen["deterministic_seed"],
            code_provenance=CodeProvenance.from_payload(frozen["code_provenance"]),
            friction_absent=friction["absent"],
            friction_model_id=friction["model_id"],
            friction_unit=friction["unit"],
            observed_metrics=ObservedMetricsV1.from_payload(frozen["observed_metrics"]),
            criteria_decision=CriteriaDecisionV1.from_payload(frozen["criteria_decision"]),
            diagnostics=FinalHoldoutResultDiagnostics.from_payload(frozen["diagnostics"]),
        )
        if built.final_holdout_result_semantic_identity != frozen["final_holdout_result_semantic_identity"]:
            raise FinalHoldoutEvaluationError(
                "final holdout result semantic identity does not match its semantic projection"
            )
        return built


def build_final_holdout_result_v1(
    *,
    manifest: ExperimentManifestV2,
    authorization: FinalHoldoutAuthorizationV1,
    authorization_artifact_hash_ref: str,
    envelope_hash: str,
    holdout_evaluator_id: str,
    holdout_evaluator_hash: str,
    observed_metrics: ObservedMetricsV1,
    created_at: str,
    created_by: str,
) -> FinalHoldoutResultV1:
    """Build one immutable terminal result from authoritative holdout aggregates.

    ``observed_metrics`` must already be the trusted, already-decrypted-and-
    aggregated output of ``FinalHoldoutRunner.run_once`` (or its recovered
    ledger record) -- this function never touches plaintext itself. Criteria
    are evaluated ONLY from ``manifest.evaluation_criteria``, the same
    predeclared criteria used throughout discovery and validation; there is
    no parameter through which a caller could supply a different threshold.
    """
    if type(manifest) is not ExperimentManifestV2:
        raise FinalHoldoutEvaluationError("manifest must be ExperimentManifestV2")
    if type(authorization) is not FinalHoldoutAuthorizationV1:
        raise FinalHoldoutEvaluationError("authorization must be FinalHoldoutAuthorizationV1")
    authorization_artifact_hash_ref = _sha256_ref(
        authorization_artifact_hash_ref, field_name="authorization_artifact_hash_ref"
    )
    envelope_hash = _sha256_hex(envelope_hash, field_name="envelope_hash")
    holdout_evaluator_id = _machine_identifier(holdout_evaluator_id, field_name="holdout_evaluator_id")
    holdout_evaluator_hash = _sha256_hex(holdout_evaluator_hash, field_name="holdout_evaluator_hash")
    if type(observed_metrics) is not ObservedMetricsV1:
        raise FinalHoldoutEvaluationError("observed_metrics must be ObservedMetricsV1")

    if authorization.manifest_semantic_hash != manifest.manifest_semantic_hash:
        raise FinalHoldoutEvaluationError(
            "authorization manifest binding does not match the authoritative manifest"
        )
    if (
        authorization.hypothesis_id != manifest.hypothesis_id
        or authorization.hypothesis_family_id != manifest.hypothesis_family_id
        or authorization.hypothesis_content_hash != manifest.bound_hypothesis_content_hash
    ):
        raise FinalHoldoutEvaluationError(
            "authorization hypothesis identity does not match the authoritative manifest"
        )
    if authorization.split_plan_semantic_hash != manifest.split_plan.semantic_hash:
        raise FinalHoldoutEvaluationError(
            "authorization split binding does not match the authoritative manifest"
        )
    expected_datasets = tuple(dataset.artifact_hash_ref for dataset in manifest.datasets)
    if authorization.dataset_artifact_hash_refs != expected_datasets:
        raise FinalHoldoutEvaluationError(
            "authorization dataset bindings do not match the authoritative manifest"
        )
    if observed_metrics.primary_metric != manifest.primary_metric:
        raise FinalHoldoutEvaluationError(
            "observed primary metric does not match the frozen manifest primary metric"
        )

    # The only criteria evaluation in this entire module: the manifest's
    # predeclared EvaluationCriteriaV1, evaluated fresh against the holdout's
    # own aggregates. No ad-hoc threshold, feature, or tuning is possible here.
    decision = evaluate_criteria(manifest.evaluation_criteria, observed_metrics)
    outcome = ValidationOutcome.PASS if decision.passed else ValidationOutcome.FAIL

    return FinalHoldoutResultV1(
        schema_version=FINAL_HOLDOUT_RESULT_V1_SCHEMA_VERSION,
        result_kind=FINAL_HOLDOUT_RESULT_KIND,
        outcome=outcome,
        hypothesis_id=authorization.hypothesis_id,
        hypothesis_family_id=authorization.hypothesis_family_id,
        hypothesis_content_hash=authorization.hypothesis_content_hash,
        manifest_semantic_hash=authorization.manifest_semantic_hash,
        manifest_artifact_hash_ref=authorization.manifest_artifact_hash_ref,
        split_plan_semantic_hash=authorization.split_plan_semantic_hash,
        holdout_start=authorization.holdout_start,
        holdout_end=authorization.holdout_end,
        holdout_row_count=authorization.holdout_row_count,
        dataset_artifact_hash_refs=authorization.dataset_artifact_hash_refs,
        authorization_artifact_hash_ref=authorization_artifact_hash_ref,
        authorization_semantic_identity=authorization.authorization_semantic_identity,
        validation_decision_artifact_hash_ref=authorization.validation_decision_artifact_hash_ref,
        validation_decision_semantic_identity=authorization.validation_decision_semantic_identity,
        evidence_artifact_hash_ref=authorization.evidence_artifact_hash_ref,
        evidence_semantic_identity=authorization.evidence_semantic_identity,
        result_artifact_hash_ref=authorization.result_artifact_hash_ref,
        result_semantic_identity=authorization.result_semantic_identity,
        envelope_hash=envelope_hash,
        holdout_evaluator_id=holdout_evaluator_id,
        holdout_evaluator_hash=holdout_evaluator_hash,
        deterministic_seed=authorization.deterministic_seed,
        code_provenance=authorization.code_provenance,
        friction_absent=authorization.friction_absent,
        friction_model_id=authorization.friction_model_id,
        friction_unit=authorization.friction_unit,
        observed_metrics=observed_metrics,
        criteria_decision=decision,
        diagnostics=FinalHoldoutResultDiagnostics(created_at=created_at, created_by=created_by),
    )


def persist_final_holdout_result_v1(
    result: FinalHoldoutResultV1,
    *,
    artifact_store: ArtifactStore,
) -> ArtifactRef:
    """Persist exact canonical final holdout result bytes under Verified CAS."""
    if type(result) is not FinalHoldoutResultV1:
        raise FinalHoldoutEvaluationError("result must be FinalHoldoutResultV1")
    exact_bytes = result.canonical_bytes()
    artifact = artifact_store.import_snapshot(
        io.BytesIO(exact_bytes),
        media_type=FINAL_HOLDOUT_RESULT_V1_MEDIA_TYPE,
    )
    if artifact.hash_ref != sha256_bytes(exact_bytes):
        raise FinalHoldoutEvaluationPersistenceError(
            "Verified CAS returned an unexpected final holdout result hash"
        )
    return artifact


def verify_final_holdout_result_v1(encoded: str | bytes) -> FinalHoldoutResultV1:
    """Verify strict canonical wire bytes and the recomputed semantic identity."""
    if type(encoded) is bytes:
        exact_bytes = encoded
    elif type(encoded) is str:
        try:
            exact_bytes = encoded.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise FinalHoldoutEvaluationError("final holdout result v1 must be valid UTF-8") from exc
    else:
        raise FinalHoldoutEvaluationError(
            "final holdout result v1 wire payload must be exact str or bytes"
        )
    try:
        parsed = parse_json(exact_bytes)
    except ProvenanceError as exc:
        raise FinalHoldoutEvaluationError(
            f"final holdout result v1 wire payload is invalid: {exc}"
        ) from exc
    if not isinstance(parsed, Mapping):
        raise FinalHoldoutEvaluationError("final holdout result v1 root must be a JSON object")
    if canonical_json_bytes(parsed) != exact_bytes:
        raise FinalHoldoutEvaluationError(
            "final holdout result v1 wire payload must use canonical JSON bytes"
        )
    return FinalHoldoutResultV1.from_payload(parsed)


def load_final_holdout_result_v1(
    result_artifact_hash_ref: str,
    *,
    artifact_store: ArtifactStore,
    manifest: ExperimentManifestV2,
    authorization: FinalHoldoutAuthorizationV1,
    authorization_artifact_hash_ref: str,
) -> FinalHoldoutResultV1:
    """Load and reverify a final holdout result against its authoritative lineage."""
    if type(manifest) is not ExperimentManifestV2:
        raise FinalHoldoutEvaluationError("manifest must be ExperimentManifestV2")
    if type(authorization) is not FinalHoldoutAuthorizationV1:
        raise FinalHoldoutEvaluationError("authorization must be FinalHoldoutAuthorizationV1")
    authorization_artifact_hash_ref = _sha256_ref(
        authorization_artifact_hash_ref, field_name="authorization_artifact_hash_ref"
    )

    artifact = artifact_store.resolve_verified(
        result_artifact_hash_ref,
        expected_media_type=FINAL_HOLDOUT_RESULT_V1_MEDIA_TYPE,
    )
    exact_bytes = artifact_store.read_verified(
        result_artifact_hash_ref,
        expected_media_type=FINAL_HOLDOUT_RESULT_V1_MEDIA_TYPE,
    )
    if artifact.hash_ref != sha256_bytes(exact_bytes):
        raise FinalHoldoutEvaluationPersistenceError("Verified CAS final holdout result identity mismatch")
    result = verify_final_holdout_result_v1(exact_bytes)

    if result.manifest_semantic_hash != manifest.manifest_semantic_hash:
        raise FinalHoldoutEvaluationError(
            "result manifest binding does not match the authoritative manifest"
        )
    if result.split_plan_semantic_hash != manifest.split_plan.semantic_hash:
        raise FinalHoldoutEvaluationError(
            "result split binding does not match the authoritative manifest"
        )
    if (
        result.hypothesis_id != manifest.hypothesis_id
        or result.hypothesis_family_id != manifest.hypothesis_family_id
        or result.hypothesis_content_hash != manifest.bound_hypothesis_content_hash
    ):
        raise FinalHoldoutEvaluationError("result hypothesis identity mismatch")
    expected_datasets = tuple(dataset.artifact_hash_ref for dataset in manifest.datasets)
    if result.dataset_artifact_hash_refs != expected_datasets:
        raise FinalHoldoutEvaluationError("result dataset bindings do not match the authoritative manifest")

    if (
        result.authorization_artifact_hash_ref != authorization_artifact_hash_ref
        or result.authorization_semantic_identity != authorization.authorization_semantic_identity
        or result.validation_decision_artifact_hash_ref
        != authorization.validation_decision_artifact_hash_ref
        or result.validation_decision_semantic_identity != authorization.validation_decision_semantic_identity
        or result.evidence_artifact_hash_ref != authorization.evidence_artifact_hash_ref
        or result.evidence_semantic_identity != authorization.evidence_semantic_identity
        or result.result_artifact_hash_ref != authorization.result_artifact_hash_ref
        or result.result_semantic_identity != authorization.result_semantic_identity
        or result.deterministic_seed != authorization.deterministic_seed
        or result.code_provenance != authorization.code_provenance
        or result.friction_absent != authorization.friction_absent
        or result.friction_model_id != authorization.friction_model_id
        or result.friction_unit != authorization.friction_unit
    ):
        raise FinalHoldoutEvaluationError("result does not match its authoritative source authorization")

    # Never trust the stored decision on reload either.
    recomputed = evaluate_criteria(manifest.evaluation_criteria, result.observed_metrics)
    if recomputed != result.criteria_decision:
        raise FinalHoldoutEvaluationConflictError(
            "criteria decision recomputed from the predeclared manifest criteria conflicts "
            "with the persisted final holdout result"
        )
    expected_outcome = ValidationOutcome.PASS if recomputed.passed else ValidationOutcome.FAIL
    if result.outcome is not expected_outcome:
        raise FinalHoldoutEvaluationConflictError(
            "persisted final holdout result outcome conflicts with the recomputed criteria decision"
        )

    return result


# ---------------------------------------------------------------------------
# End-to-end control: bridges the scientific-integrity chain to the closed
# holdout-secrecy infrastructure and the closed HypothesisRegistry lifecycle.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FinalHoldoutEvaluationOutcome:
    result: FinalHoldoutResultV1
    result_artifact: ArtifactRef


class FinalHoldoutEvaluationControlV1:
    """Trusted, one-shot bridge from Final Holdout Authorization V1 to a terminal verdict.

    Construction binds one already-configured ``FinalHoldoutRunner`` (its
    ``registry``, ``seals`` (``HoldoutSealStore``), and ``ledger``
    (``ResultLedger``) are reused directly, unmodified, from that runner) plus
    the Verified CAS root and the closed declarative ``EvaluatorRegistry``.
    ``evaluate`` accepts only explicit CAS references and a path to the
    already-sealed holdout envelope; it independently reloads and reverifies
    the authoritative manifest, execution result, evidence, validation
    decision, and authorization -- in that order -- before touching the
    registry or the runner at all.
    """

    def __init__(
        self,
        *,
        runner: FinalHoldoutRunner,
        artifact_store: ArtifactStore,
        evaluator_registry: EvaluatorRegistry,
    ) -> None:
        if not isinstance(runner, FinalHoldoutRunner):
            raise TypeError("runner must be FinalHoldoutRunner")
        if not isinstance(artifact_store, ArtifactStore):
            raise TypeError("artifact_store must be ArtifactStore")
        if not isinstance(evaluator_registry, EvaluatorRegistry):
            raise TypeError("evaluator_registry must be EvaluatorRegistry")
        self.runner = runner
        self.registry: HypothesisRegistry = runner.registry
        self.seals = runner.seals
        self.ledger = runner.ledger
        self.artifact_store = artifact_store
        self.evaluator_registry = evaluator_registry

    def _load_manifest_for_hypothesis(
        self, hypothesis_id: str
    ) -> tuple[ExperimentManifestV2, HypothesisRecord]:
        """State-independent manifest reload: see module docstring for why
        ``HypothesisRegistry.load_bound_manifest_v2`` cannot be used here."""
        record = self.registry.get(hypothesis_id)
        if (
            record.manifest_artifact_hash_ref is None
            or record.manifest_schema_version != EXPERIMENT_MANIFEST_V2_SCHEMA_VERSION
            or record.manifest_hash is None
        ):
            raise FinalHoldoutEvaluationError("hypothesis has no complete manifest v2 binding")
        manifest = load_experiment_manifest_v2(
            record.manifest_artifact_hash_ref,
            artifact_store=self.artifact_store,
        )
        if (
            manifest.hypothesis_id != record.hypothesis_id
            or manifest.hypothesis_family_id != record.hypothesis_family_id
            or manifest.bound_hypothesis_content_hash != record.content_hash
            or manifest.manifest_semantic_hash.removeprefix("sha256:") != record.manifest_hash
        ):
            raise FinalHoldoutEvaluationError("bound manifest v2 does not match registry identities")
        return manifest, record

    def _ensure_validation_passed(self, hypothesis_id: str) -> None:
        """Idempotently drive FROZEN -> TRAIN_TESTED -> VALIDATION_PASSED.

        Both transitions are the existing, unmodified
        ``HypothesisRegistry.transition``; both are already guarded by
        ``_require_isolated_holdout`` and fail closed if the holdout was
        never sealed and isolation-attested.
        """
        record = self.registry.get(hypothesis_id)
        if record.state is HypothesisState.FROZEN:
            self.registry.transition(hypothesis_id, HypothesisState.TRAIN_TESTED)
            record = self.registry.get(hypothesis_id)
        if record.state is HypothesisState.TRAIN_TESTED:
            self.registry.transition(hypothesis_id, HypothesisState.VALIDATION_PASSED)

    def _ensure_terminal_transition(self, hypothesis_id: str, target: HypothesisState) -> None:
        record = self.registry.get(hypothesis_id)
        if record.state is target:
            return
        if record.state is HypothesisState.HOLDOUT_CONSUMED:
            self.registry.transition(hypothesis_id, target)
            return
        if record.state in (HypothesisState.ACCEPTED, HypothesisState.REJECTED_FINAL):
            raise FinalHoldoutEvaluationConflictError(
                f"hypothesis is already terminal at {record.state.value}, which conflicts with "
                f"the recomputed final outcome {target.value}"
            )
        raise FinalHoldoutEvaluationError(
            f"unexpected hypothesis state before terminal transition: {record.state.value}"
        )

    def _recover_runner_aggregate_metrics(
        self, *, hypothesis_id: str, hypothesis_family_id: str, envelope_hash: str
    ) -> Mapping[str, object] | None:
        """Read (never re-decrypt) the runner's own already-written ledger record.

        Used only when this hypothesis's holdout was already consumed by a
        prior, possibly crashed, invocation -- the aggregates were already
        durably recorded by ``FinalHoldoutRunner.run_once`` before this
        module ever ran, so recovery never touches plaintext.
        """
        if not self.ledger.verify():
            raise FinalHoldoutEvaluationError("result ledger integrity failed before recovery")
        try:
            with self.ledger.path.open("r", encoding="utf-8") as handle:
                for raw in handle:
                    if not raw.strip():
                        continue
                    record = json.loads(raw)
                    payload = record.get("payload") if isinstance(record, dict) else None
                    if not isinstance(payload, dict):
                        continue
                    if (
                        payload.get("record_type") == _RUNNER_RESULT_LEDGER_RECORD_TYPE
                        and payload.get("hypothesis_id") == hypothesis_id
                        and payload.get("hypothesis_family_id") == hypothesis_family_id
                        and payload.get("envelope_hash") == envelope_hash
                    ):
                        return payload.get("aggregate_metrics")
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise FinalHoldoutEvaluationError("cannot inspect result ledger for recovery") from exc
        return None

    def evaluate(
        self,
        hypothesis_id: str,
        *,
        authorization_artifact_hash_ref: str,
        validation_decision_artifact_hash_ref: str,
        evidence_artifact_hash_ref: str,
        result_artifact_hash_ref: str,
        sealed_holdout_path: str | Path,
        created_at: str,
        created_by: str,
    ) -> FinalHoldoutEvaluationOutcome:
        manifest, record = self._load_manifest_for_hypothesis(hypothesis_id)

        exec_result = load_result_v1(
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
            result=exec_result,
            result_artifact_hash_ref=result_artifact_hash_ref,
        )
        decision = load_validation_decision_v1(
            validation_decision_artifact_hash_ref,
            artifact_store=self.artifact_store,
            manifest=manifest,
            evidence=evidence,
            evidence_artifact_hash_ref=evidence_artifact_hash_ref,
        )
        authorization = load_final_holdout_authorization_v1(
            authorization_artifact_hash_ref,
            artifact_store=self.artifact_store,
            manifest=manifest,
            validation_decision=decision,
            validation_decision_artifact_hash_ref=validation_decision_artifact_hash_ref,
        )

        auth_store = FinalHoldoutAuthorizationStore(self.registry)
        try:
            stored_auth = auth_store.get(hypothesis_id)
        except KeyError as exc:
            raise FinalHoldoutEvaluationError(
                "hypothesis has no authoritative final-holdout authorization on file"
            ) from exc
        if (
            stored_auth.authorization_artifact_hash_ref != authorization_artifact_hash_ref
            or stored_auth.authorization_semantic_identity != authorization.authorization_semantic_identity
        ):
            raise FinalHoldoutEvaluationConflictError(
                "supplied authorization is not the one authoritative grant on file"
            )

        self._ensure_validation_passed(hypothesis_id)
        state = self.registry.get(hypothesis_id).state

        if state is HypothesisState.VALIDATION_PASSED:
            receipt = self.runner.run_once(hypothesis_id=hypothesis_id, sealed_path=sealed_holdout_path)
            aggregate_metrics = receipt.aggregate_metrics
            envelope_hash = receipt.envelope_hash
            holdout_evaluator_id = receipt.evaluator_id
            holdout_evaluator_hash = receipt.evaluator_hash
        elif state in (
            HypothesisState.HOLDOUT_CONSUMED,
            HypothesisState.ACCEPTED,
            HypothesisState.REJECTED_FINAL,
        ):
            seal = self.seals.get(hypothesis_id)
            recovered = self._recover_runner_aggregate_metrics(
                hypothesis_id=hypothesis_id,
                hypothesis_family_id=manifest.hypothesis_family_id,
                envelope_hash=seal.envelope_hash,
            )
            if recovered is None:
                raise FinalHoldoutEvaluationError(
                    "holdout already consumed but no recorded result is recoverable from the "
                    "ledger; this never re-opens the holdout and requires manual recovery"
                )
            aggregate_metrics = recovered
            envelope_hash = seal.envelope_hash
            holdout_evaluator_id = seal.evaluator_id
            holdout_evaluator_hash = seal.evaluator_hash
        else:
            raise FinalHoldoutEvaluationError(
                f"unexpected hypothesis state before holdout consumption: {state.value}"
            )

        numeric_metrics = {
            key: value for key, value in aggregate_metrics.items() if type(value) in (int, float)
        }
        observed = ObservedMetricsV1(primary_metric=manifest.primary_metric, values=numeric_metrics)

        result = build_final_holdout_result_v1(
            manifest=manifest,
            authorization=authorization,
            authorization_artifact_hash_ref=authorization_artifact_hash_ref,
            envelope_hash=envelope_hash,
            holdout_evaluator_id=holdout_evaluator_id,
            holdout_evaluator_hash=holdout_evaluator_hash,
            observed_metrics=observed,
            created_at=created_at,
            created_by=created_by,
        )
        artifact = persist_final_holdout_result_v1(result, artifact_store=self.artifact_store)
        self.ledger.append(
            {
                "record_type": _EVALUATION_LEDGER_RECORD_TYPE,
                "hypothesis_id": hypothesis_id,
                "hypothesis_family_id": manifest.hypothesis_family_id,
                "result_artifact_hash_ref": artifact.hash_ref,
                "final_holdout_result_semantic_identity": result.final_holdout_result_semantic_identity,
                "outcome": result.outcome.value,
            }
        )

        target = (
            HypothesisState.ACCEPTED
            if result.outcome is ValidationOutcome.PASS
            else HypothesisState.REJECTED_FINAL
        )
        self._ensure_terminal_transition(hypothesis_id, target)

        return FinalHoldoutEvaluationOutcome(result=result, result_artifact=artifact)


__all__ = [
    "FINAL_HOLDOUT_RESULT_KIND",
    "FINAL_HOLDOUT_RESULT_V1_MEDIA_TYPE",
    "FINAL_HOLDOUT_RESULT_V1_SCHEMA_VERSION",
    "FinalHoldoutEvaluationConflictError",
    "FinalHoldoutEvaluationControlV1",
    "FinalHoldoutEvaluationError",
    "FinalHoldoutEvaluationOutcome",
    "FinalHoldoutEvaluationPersistenceError",
    "FinalHoldoutResultDiagnostics",
    "FinalHoldoutResultV1",
    "build_final_holdout_result_v1",
    "load_final_holdout_result_v1",
    "persist_final_holdout_result_v1",
    "verify_final_holdout_result_v1",
]
