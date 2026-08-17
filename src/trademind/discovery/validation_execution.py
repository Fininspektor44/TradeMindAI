"""Validation Execution Control V1: the second missing bridge, from a
TRAIN_TESTED hypothesis to ``VALIDATION_PASSED`` or ``VALIDATION_REJECTED``.

Built strictly additively on top of already-closed contracts, none of which
are modified:

  * ``HypothesisRegistry`` -- state machine, ``family_status``, and
    ``transition`` (discovery/hypothesis_registry.py) are reused exactly
    as-is. This module owns exactly one new additive SQLite table,
    ``validation_evidence``, in the SAME registry database file -- the
    identical pattern ``HoldoutSealStore`` and ``train_test_evidence``
    (discovery/train_test_execution.py) already established.

  * ``HypothesisRegistry.load_bound_manifest_v2`` cannot be reused here: it
    hardcodes a ``state is FROZEN`` precondition (discovery/
    hypothesis_registry.py), and a hypothesis this module operates on is, by
    definition, already past FROZEN. Rather than weaken that CLOSED
    function's precondition -- which would let every OTHER caller
    (``DiscoveryOrchestratorBridge``, ``TrainTestExecutionControl``) load a
    manifest for a non-FROZEN hypothesis too, silently widening their own
    trust boundary -- this module calls the lower-level, state-agnostic
    ``manifest.load_experiment_manifest_v2`` (the same CAS-verification
    primitive ``load_bound_manifest_v2`` itself is built on) directly, and
    re-implements the identical four identity checks
    ``load_bound_manifest_v2`` already performs (hypothesis_id, family_id,
    bound_hypothesis_content_hash, manifest_semantic_hash all cross-checked
    against the live registry row). No closed invariant is weakened: the
    checks are the same, only the state gate differs, and it differs
    correctly for this later, still-legitimate pipeline stage.

  * ``TrainTestExecutionControl.get_evidence`` (discovery/
    train_test_execution.py) is reused, not duplicated, to load and
    re-verify the prior ``TrainTestEvidenceV1`` this validation is built on.
    ``SUPPORTED_TEST_FAMILIES``, the internal metric validator, and the CSV
    row reader from that same module are reused directly rather than
    reimplemented, so both pipeline stages execute the identical, singly
    vetted test-family vocabulary.

  * ``dataset_split_provenance.verify_dataset_split_membership`` is reused,
    not duplicated, hardcoded to role ``"VALIDATION"`` (never
    caller-selectable) -- structurally forbidding DISCOVERY or
    FINAL_HOLDOUT content from ever being accepted as validation evidence.
    Unlike the DISCOVERY dataset (which ``TrainTestExecutionControl`` locates
    via the manifest's singular ``split_dataset_role`` label), the
    VALIDATION dataset is located by CONTENT alone: every dataset entry in
    the manifest is tried against the VALIDATION boundary, and exactly one
    must verify. This mirrors this lineage's core security principle
    (content-based verification, never label-based) and requires no new
    manifest field or naming convention.

``ExperimentManifestV2.evaluation_criteria`` (``EvaluationCriteriaV1`` /
``EvaluationCriterionV1``) was, until this task, a fully defined and
persisted schema with no consumer anywhere in the repository. This module is
that consumer: every criterion is evaluated deterministically against the
freshly computed VALIDATION metrics (never the prior DISCOVERY metrics,
which are used only for identity/provenance cross-checking), and fails
closed on a missing metric, a non-numeric or non-finite observed value or
threshold, or an unrecognized operator/mode.

Scope boundary: this module stops at ``VALIDATION_PASSED`` /
``VALIDATION_REJECTED``. It never imports anything from the final-holdout
subsystem (holdout_crypto.py/holdout_keys.py/holdout_runner.py/
holdout_sealer.py) and never references the holdout-consumption or
final-decision state names -- final-holdout evaluation remains the
separately closed responsibility of ``discovery/holdout_runner.py``.
"""

from __future__ import annotations

import io
import math
import re
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType

from trademind.orchestrator.artifact_store import ArtifactStore, ArtifactStoreError
from trademind.orchestrator.control_plane import ControlPlane
from trademind.signal_statistics_provenance import (
    ProvenanceError,
    canonical_json_bytes,
    freeze_json_object,
    parse_json,
    sha256_bytes,
    validate_sha256_ref,
)

from .dataset_split_provenance import (
    BoundSplitPlanV1,
    DatasetProvenanceError,
    DatasetSplitMembershipV1,
    verify_dataset_split_membership,
)
from .hypothesis_registry import HypothesisRegistry, HypothesisState, RegistryError
from .manifest import (
    EXPERIMENT_MANIFEST_V2_SCHEMA_VERSION,
    CriteriaMode,
    CriterionOperator,
    EvaluationCriteriaV1,
    ExperimentManifestV2,
    load_experiment_manifest_v2,
)
from .orchestrator_bridge import DiscoveryOrchestratorBridge
from .train_test_execution import (
    SUPPORTED_TEST_FAMILIES,
    TrainTestExecutionControl,
    TrainTestExecutionError,
    _read_csv_rows,
    _validated_metrics,
)


class ValidationExecutionError(RuntimeError):
    """Raised when a bounded validation execution cannot complete safely."""


VALIDATION_EVIDENCE_SCHEMA_VERSION = "discovery-validation-evidence-v1"
VALIDATION_EVIDENCE_MEDIA_TYPE = "application/vnd.trademind.discovery.validation-evidence-v1+json"
_EVIDENCE_HASH_DOMAIN = b"trademind:discovery:validation-evidence:v1"

_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
_TASK_ID_SHAPE = re.compile(r"^discovery-[0-9a-f]{20}$")

_VERDICT_VALUES = frozenset(
    {HypothesisState.VALIDATION_PASSED.value, HypothesisState.VALIDATION_REJECTED.value}
)
_CRITERIA_MODE_VALUES = frozenset({CriteriaMode.ALL.value, CriteriaMode.ANY.value})
_OPERATOR_VALUES = frozenset(item.value for item in CriterionOperator)

_CRITERION_RESULT_FIELDS = frozenset({"metric", "operator", "threshold", "observed_value", "passed"})


def _bare_sha256(value: object, *, field_name: str) -> str:
    if type(value) is not str or _SHA256_HEX.fullmatch(value) is None:
        raise ValidationExecutionError(f"{field_name} must be 64 lowercase hexadecimal digits")
    return value


def _nonempty_str(value: object, *, field_name: str) -> str:
    if type(value) is not str or not value.strip():
        raise ValidationExecutionError(f"{field_name} must be a non-empty string")
    return value


def _finite_number(value: object, *, field_name: str) -> int | float:
    """Deterministic, fail-closed numeric check shared by both the observed
    metric value and the criterion threshold: an exact ``bool`` is rejected
    (bools are not comparable magnitudes here), a non-finite float (NaN/Inf)
    is rejected, and anything else that is not an ``int``/``float`` is
    rejected."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationExecutionError(f"{field_name} must be an exact int or float")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValidationExecutionError(f"{field_name} must be finite (no NaN/Inf)")
    return value


def _evaluate_criterion(criterion, metrics: Mapping[str, object]) -> dict[str, object]:
    """Evaluate exactly one criterion against the freshly computed VALIDATION
    metrics. Fails closed -- raises, never silently treats an ambiguity as a
    pass or a fail -- on a missing metric, a non-numeric/non-finite observed
    value, a malformed threshold, or an operator this function does not
    explicitly recognize."""
    if criterion.metric not in metrics:
        raise ValidationExecutionError(
            f"validation metrics are missing required metric {criterion.metric!r}"
        )
    observed = _finite_number(metrics[criterion.metric], field_name=f"metric {criterion.metric!r}")
    threshold = _finite_number(criterion.threshold, field_name=f"criterion threshold for {criterion.metric!r}")

    if criterion.operator is CriterionOperator.GREATER_THAN_OR_EQUAL:
        passed = observed >= threshold
    elif criterion.operator is CriterionOperator.GREATER_THAN:
        passed = observed > threshold
    elif criterion.operator is CriterionOperator.LESS_THAN_OR_EQUAL:
        passed = observed <= threshold
    elif criterion.operator is CriterionOperator.LESS_THAN:
        passed = observed < threshold
    else:  # pragma: no cover -- ExperimentManifestV2 already only accepts a
        # CriterionOperator enum member at construction time; this branch is
        # unreachable today and kept as documented defense-in-depth, exactly
        # like dataset_split_provenance.py's own redundant holdout check.
        raise ValidationExecutionError(f"unsupported criterion operator: {criterion.operator!r}")

    return {
        "metric": criterion.metric,
        "operator": criterion.operator.value,
        "threshold": threshold,
        "observed_value": observed,
        "passed": bool(passed),
    }


def _evaluate_criteria(
    criteria: EvaluationCriteriaV1, metrics: Mapping[str, object]
) -> tuple[list[dict[str, object]], bool]:
    results = [_evaluate_criterion(criterion, metrics) for criterion in criteria.criteria]
    if criteria.mode is CriteriaMode.ALL:
        overall = all(item["passed"] for item in results)
    elif criteria.mode is CriteriaMode.ANY:
        overall = any(item["passed"] for item in results)
    else:  # pragma: no cover -- EvaluationCriteriaV1 already only accepts a
        # CriteriaMode enum member at construction time; kept as documented
        # defense-in-depth, unreachable today.
        raise ValidationExecutionError(f"unsupported evaluation criteria mode: {criteria.mode!r}")
    return results, overall


@dataclass(frozen=True, slots=True)
class ValidationEvidenceV1:
    """Provenance-bound evidence produced by exactly one bounded validation
    execution for exactly one TRAIN_TESTED hypothesis. Construct only via
    :meth:`ValidationExecutionControl.execute` -- never by hand."""

    hypothesis_id: str
    hypothesis_family_id: str
    bound_hypothesis_content_hash: str
    manifest_semantic_hash: str
    manifest_artifact_hash_ref: str
    orchestrator_task_id: str
    train_test_evidence_hash: str
    dataset_split_membership: DatasetSplitMembershipV1
    test_family: str
    metrics: Mapping[str, float | int | bool | None]
    criteria_mode: str
    criteria_results: tuple[Mapping[str, object], ...]
    verdict: str
    executed_at: str
    schema_version: str = VALIDATION_EVIDENCE_SCHEMA_VERSION
    evidence_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != VALIDATION_EVIDENCE_SCHEMA_VERSION:
            raise ValidationExecutionError("unsupported validation evidence schema_version")
        _nonempty_str(self.hypothesis_id, field_name="hypothesis_id")
        _nonempty_str(self.hypothesis_family_id, field_name="hypothesis_family_id")
        _bare_sha256(self.bound_hypothesis_content_hash, field_name="bound_hypothesis_content_hash")
        for value, field_name in (
            (self.manifest_semantic_hash, "manifest_semantic_hash"),
            (self.manifest_artifact_hash_ref, "manifest_artifact_hash_ref"),
            (self.train_test_evidence_hash, "train_test_evidence_hash"),
        ):
            try:
                validate_sha256_ref(value)
            except ProvenanceError as exc:
                raise ValidationExecutionError(f"{field_name} is invalid") from exc
        if (
            type(self.orchestrator_task_id) is not str
            or _TASK_ID_SHAPE.fullmatch(self.orchestrator_task_id) is None
        ):
            raise ValidationExecutionError("orchestrator_task_id is invalid")
        if type(self.dataset_split_membership) is not DatasetSplitMembershipV1:
            raise ValidationExecutionError(
                "dataset_split_membership must be an exact DatasetSplitMembershipV1"
            )
        if self.dataset_split_membership.role != "VALIDATION":
            raise ValidationExecutionError(
                "validation evidence must be bound to VALIDATION dataset content only"
            )
        _nonempty_str(self.test_family, field_name="test_family")
        object.__setattr__(self, "metrics", MappingProxyType(_validated_metrics(self.metrics)))

        if self.criteria_mode not in _CRITERIA_MODE_VALUES:
            raise ValidationExecutionError("criteria_mode is invalid")
        if type(self.criteria_results) is not tuple or not self.criteria_results:
            raise ValidationExecutionError("criteria_results must be a non-empty tuple")
        cleaned_results: list[Mapping[str, object]] = []
        for entry in self.criteria_results:
            if not isinstance(entry, Mapping) or frozenset(entry) != _CRITERION_RESULT_FIELDS:
                raise ValidationExecutionError("criteria_results entry is malformed")
            _nonempty_str(entry["metric"], field_name="criteria_results.metric")
            if entry["operator"] not in _OPERATOR_VALUES:
                raise ValidationExecutionError("criteria_results.operator is invalid")
            _finite_number(entry["threshold"], field_name="criteria_results.threshold")
            _finite_number(entry["observed_value"], field_name="criteria_results.observed_value")
            if type(entry["passed"]) is not bool:
                raise ValidationExecutionError("criteria_results.passed must be an exact bool")
            cleaned_results.append(MappingProxyType(dict(entry)))
        object.__setattr__(self, "criteria_results", tuple(cleaned_results))

        if self.criteria_mode == CriteriaMode.ALL.value:
            expected_overall = all(item["passed"] for item in self.criteria_results)
        else:
            expected_overall = any(item["passed"] for item in self.criteria_results)
        expected_verdict = (
            HypothesisState.VALIDATION_PASSED.value
            if expected_overall
            else HypothesisState.VALIDATION_REJECTED.value
        )
        if self.verdict not in _VERDICT_VALUES:
            raise ValidationExecutionError("verdict is invalid")
        if self.verdict != expected_verdict:
            raise ValidationExecutionError(
                "verdict is inconsistent with its own criteria_results/criteria_mode"
            )

        if type(self.executed_at) is not str:
            raise ValidationExecutionError("executed_at must be a string")
        try:
            parsed = datetime.fromisoformat(self.executed_at)
        except ValueError as exc:
            raise ValidationExecutionError("executed_at must be an ISO timestamp") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValidationExecutionError("executed_at must be timezone-aware")

        object.__setattr__(
            self,
            "evidence_hash",
            sha256_bytes(_EVIDENCE_HASH_DOMAIN + b"\x00" + canonical_json_bytes(self.semantic_projection())),
        )
        canonical_json_bytes(self.to_payload())

    def semantic_projection(self) -> dict[str, object]:
        """Everything that determines this evidence's identity -- excludes
        ``executed_at`` so identical inputs always produce an identical
        ``evidence_hash``."""
        return {
            "schema_version": self.schema_version,
            "hypothesis_id": self.hypothesis_id,
            "hypothesis_family_id": self.hypothesis_family_id,
            "bound_hypothesis_content_hash": self.bound_hypothesis_content_hash,
            "manifest_semantic_hash": self.manifest_semantic_hash,
            "manifest_artifact_hash_ref": self.manifest_artifact_hash_ref,
            "orchestrator_task_id": self.orchestrator_task_id,
            "train_test_evidence_hash": self.train_test_evidence_hash,
            "dataset_split_membership": self.dataset_split_membership.to_payload(),
            "test_family": self.test_family,
            "metrics": dict(self.metrics),
            "criteria_mode": self.criteria_mode,
            "criteria_results": [dict(item) for item in self.criteria_results],
            "verdict": self.verdict,
        }

    def to_payload(self) -> dict[str, object]:
        payload = self.semantic_projection()
        payload["evidence_hash"] = self.evidence_hash
        payload["diagnostics"] = {"executed_at": self.executed_at}
        return payload

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_payload())

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> ValidationEvidenceV1:
        try:
            frozen = freeze_json_object(payload, field_name="validation_evidence")
        except ProvenanceError as exc:
            raise ValidationExecutionError("validation_evidence must be strict bounded JSON") from exc
        required = frozenset(
            {
                "schema_version",
                "evidence_hash",
                "hypothesis_id",
                "hypothesis_family_id",
                "bound_hypothesis_content_hash",
                "manifest_semantic_hash",
                "manifest_artifact_hash_ref",
                "orchestrator_task_id",
                "train_test_evidence_hash",
                "dataset_split_membership",
                "test_family",
                "metrics",
                "criteria_mode",
                "criteria_results",
                "verdict",
                "diagnostics",
            }
        )
        fields = frozenset(frozen)
        missing = required - fields
        unknown = fields - required
        if missing:
            raise ValidationExecutionError(
                f"validation_evidence is missing fields: {', '.join(sorted(missing))}"
            )
        if unknown:
            raise ValidationExecutionError(
                f"validation_evidence contains unknown fields: {', '.join(sorted(unknown))}"
            )
        diagnostics = frozen["diagnostics"]
        if not isinstance(diagnostics, Mapping) or frozenset(diagnostics) != {"executed_at"}:
            raise ValidationExecutionError("validation_evidence.diagnostics is invalid")
        membership_payload = frozen["dataset_split_membership"]
        if not isinstance(membership_payload, Mapping):
            raise ValidationExecutionError(
                "validation_evidence.dataset_split_membership must be a JSON object"
            )
        try:
            membership = DatasetSplitMembershipV1.from_payload(membership_payload)
        except DatasetProvenanceError as exc:
            raise ValidationExecutionError(
                f"validation_evidence dataset_split_membership is invalid: {exc}"
            ) from exc
        metrics_payload = frozen["metrics"]
        if not isinstance(metrics_payload, Mapping):
            raise ValidationExecutionError("validation_evidence.metrics must be a JSON object")
        raw_results = frozen["criteria_results"]
        if type(raw_results) is not tuple:
            raise ValidationExecutionError("validation_evidence.criteria_results must be a JSON array")
        criteria_results = tuple(dict(item) if isinstance(item, Mapping) else item for item in raw_results)

        built = cls(
            hypothesis_id=frozen["hypothesis_id"],
            hypothesis_family_id=frozen["hypothesis_family_id"],
            bound_hypothesis_content_hash=frozen["bound_hypothesis_content_hash"],
            manifest_semantic_hash=frozen["manifest_semantic_hash"],
            manifest_artifact_hash_ref=frozen["manifest_artifact_hash_ref"],
            orchestrator_task_id=frozen["orchestrator_task_id"],
            train_test_evidence_hash=frozen["train_test_evidence_hash"],
            dataset_split_membership=membership,
            test_family=frozen["test_family"],
            metrics=dict(metrics_payload),
            criteria_mode=frozen["criteria_mode"],
            criteria_results=criteria_results,
            verdict=frozen["verdict"],
            executed_at=diagnostics["executed_at"],
            schema_version=frozen["schema_version"],
        )
        try:
            claimed_hash = validate_sha256_ref(frozen["evidence_hash"])
        except ProvenanceError as exc:
            raise ValidationExecutionError("validation_evidence.evidence_hash is invalid") from exc
        if claimed_hash != built.evidence_hash:
            raise ValidationExecutionError("validation_evidence hash identity mismatch")
        return built


class ValidationExecutionControl:
    """TRAIN_TESTED hypothesis + re-verified V2 manifest + matching
    Orchestrator task + verified prior TrainTestEvidenceV1 + verified
    VALIDATION dataset -> real bounded validation evidence ->
    ``VALIDATION_PASSED`` / ``VALIDATION_REJECTED``.

    Owns exactly one new additive SQLite table (``validation_evidence``) in
    the same registry database file. ``HypothesisRegistry``'s schema and
    one-way state machine, ``DiscoveryOrchestratorBridge``, and
    ``TrainTestExecutionControl`` are never modified.
    """

    def __init__(
        self,
        *,
        registry: HypothesisRegistry,
        control: ControlPlane,
        artifacts: ArtifactStore,
        train_test: TrainTestExecutionControl,
    ) -> None:
        self.registry = registry
        self.control = control
        self.artifacts = artifacts
        self.train_test = train_test
        self.path = Path(registry.path)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        return db

    def _init_schema(self) -> None:
        with self._connect() as db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS validation_evidence (
                    hypothesis_id TEXT PRIMARY KEY,
                    hypothesis_family_id TEXT NOT NULL,
                    manifest_semantic_hash TEXT NOT NULL,
                    manifest_artifact_hash_ref TEXT NOT NULL,
                    orchestrator_task_id TEXT NOT NULL,
                    train_test_evidence_hash TEXT NOT NULL,
                    evidence_hash TEXT NOT NULL,
                    evidence_artifact_hash_ref TEXT NOT NULL,
                    verdict TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(hypothesis_id) REFERENCES hypotheses(hypothesis_id)
                )
                """
            )
            columns = {
                row["name"] for row in db.execute("PRAGMA table_info(validation_evidence)").fetchall()
            }
            required = {
                "hypothesis_id",
                "hypothesis_family_id",
                "manifest_semantic_hash",
                "manifest_artifact_hash_ref",
                "orchestrator_task_id",
                "train_test_evidence_hash",
                "evidence_hash",
                "evidence_artifact_hash_ref",
                "verdict",
                "created_at",
            }
            if not required.issubset(columns):
                raise ValidationExecutionError(
                    "legacy validation_evidence schema detected; explicit migration is required"
                )

    def _stored_row(self, hypothesis_id: str) -> sqlite3.Row | None:
        with self._connect() as db:
            return db.execute(
                "SELECT * FROM validation_evidence WHERE hypothesis_id=?",
                (hypothesis_id,),
            ).fetchone()

    def _insert_row(self, evidence: ValidationEvidenceV1, *, evidence_artifact_hash_ref: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                db.execute(
                    """
                    INSERT INTO validation_evidence(
                        hypothesis_id, hypothesis_family_id, manifest_semantic_hash,
                        manifest_artifact_hash_ref, orchestrator_task_id, train_test_evidence_hash,
                        evidence_hash, evidence_artifact_hash_ref, verdict, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        evidence.hypothesis_id,
                        evidence.hypothesis_family_id,
                        evidence.manifest_semantic_hash,
                        evidence.manifest_artifact_hash_ref,
                        evidence.orchestrator_task_id,
                        evidence.train_test_evidence_hash,
                        evidence.evidence_hash,
                        evidence_artifact_hash_ref,
                        evidence.verdict,
                        now,
                    ),
                )
            except sqlite3.IntegrityError:
                existing = db.execute(
                    "SELECT evidence_hash FROM validation_evidence WHERE hypothesis_id=?",
                    (evidence.hypothesis_id,),
                ).fetchone()
                if existing is None or existing["evidence_hash"] != evidence.evidence_hash:
                    raise ValidationExecutionError(
                        "conflicting validation evidence already recorded for this hypothesis"
                    ) from None

    def _load_verified_manifest_past_frozen(self, hypothesis_id: str) -> ExperimentManifestV2:
        """Load and re-verify the V2 manifest bound to a hypothesis whose
        state has already legitimately advanced past FROZEN.

        Cannot use ``HypothesisRegistry.load_bound_manifest_v2`` (hardcodes
        a FROZEN-only precondition -- see module docstring). Calls the same
        underlying CAS-verification primitive that function itself uses
        (``load_experiment_manifest_v2``) and re-implements the identical
        identity cross-checks."""
        record = self.registry.get(hypothesis_id)
        if (
            record.manifest_hash is None
            or record.manifest_artifact_hash_ref is None
            or record.manifest_schema_version != EXPERIMENT_MANIFEST_V2_SCHEMA_VERSION
        ):
            raise ValidationExecutionError("hypothesis has no complete manifest v2 binding")
        try:
            manifest = load_experiment_manifest_v2(
                record.manifest_artifact_hash_ref, artifact_store=self.artifacts
            )
        except ArtifactStoreError as exc:
            raise ValidationExecutionError(
                f"bound manifest v2 dataset artifacts could not be verified: {exc}"
            ) from exc
        except Exception as exc:  # noqa: BLE001 -- CAS content is untrusted input.
            raise ValidationExecutionError(f"bound manifest v2 could not be verified: {exc}") from exc
        if (
            manifest.hypothesis_id != record.hypothesis_id
            or manifest.hypothesis_family_id != record.hypothesis_family_id
            or manifest.bound_hypothesis_content_hash != record.content_hash
            or manifest.manifest_semantic_hash.removeprefix("sha256:") != record.manifest_hash
        ):
            raise ValidationExecutionError("bound manifest v2 does not match registry identities")
        return manifest

    def _find_validation_dataset(
        self, manifest: ExperimentManifestV2, bound_split_plan: BoundSplitPlanV1
    ) -> DatasetSplitMembershipV1:
        """Content-based discovery: try every dataset the manifest declares
        against the VALIDATION boundary and require exactly one to verify.
        Never trusts any dataset's ``role`` label to mean VALIDATION -- only
        actual row content, re-read fresh from Verified CAS, decides."""
        matches: list[DatasetSplitMembershipV1] = []
        last_error: Exception | None = None
        for dataset in manifest.datasets:
            try:
                membership = verify_dataset_split_membership(
                    artifact_store=self.artifacts,
                    bound_split_plan=bound_split_plan,
                    role="VALIDATION",
                    artifact_hash_ref=dataset.artifact_hash_ref,
                    media_type=dataset.media_type,
                )
            except DatasetProvenanceError as exc:
                last_error = exc
                continue
            except ArtifactStoreError as exc:
                raise ValidationExecutionError(
                    f"VALIDATION dataset artifact could not be verified: {exc}"
                ) from exc
            matches.append(membership)
        if not matches:
            raise ValidationExecutionError(
                f"no dataset declared by the manifest verifies as genuine VALIDATION content: {last_error}"
            )
        if len(matches) > 1:
            raise ValidationExecutionError(
                "more than one manifest dataset verifies as VALIDATION content; ambiguous"
            )
        return matches[0]

    def execute(
        self,
        hypothesis_id: str,
        *,
        bound_split_plan: BoundSplitPlanV1,
    ) -> ValidationEvidenceV1:
        """Run the manifest-declared ``test_family`` against the verified
        VALIDATION dataset for a TRAIN_TESTED hypothesis, apply
        ``evaluation_criteria`` to the freshly computed metrics, durably
        persist provenance-bound evidence, and only then advance the
        hypothesis to ``VALIDATION_PASSED`` or ``VALIDATION_REJECTED``.
        Idempotent for identical inputs; fails closed on conflicting ones."""
        if type(bound_split_plan) is not BoundSplitPlanV1:
            raise ValidationExecutionError("bound_split_plan must be an exact BoundSplitPlanV1")

        record = self.registry.get(hypothesis_id)

        if record.state in (HypothesisState.VALIDATION_PASSED, HypothesisState.VALIDATION_REJECTED):
            return self._reload_existing_evidence(hypothesis_id, expected_state=record.state)

        if record.state is not HypothesisState.TRAIN_TESTED:
            raise ValidationExecutionError(
                f"hypothesis must be TRAIN_TESTED to execute validation, got {record.state.value}"
            )

        manifest = self._load_verified_manifest_past_frozen(hypothesis_id)

        family = self.registry.family_status(manifest.hypothesis_family_id)
        if family["holdout_consumed"]:
            raise ValidationExecutionError("final holdout has already been consumed for this family")
        if family["terminal_state"]:
            raise ValidationExecutionError(f"hypothesis family is terminal: {family['terminal_state']}")

        task_id = DiscoveryOrchestratorBridge._task_id(hypothesis_id)
        task = self.control.task_store.get(task_id)
        if task is None:
            raise ValidationExecutionError(
                "no matching Orchestrator task exists for this hypothesis; submit it through "
                "DiscoveryOrchestratorBridge.submit_frozen_hypothesis_v2 first"
            )

        try:
            train_test_evidence = self.train_test.get_evidence(hypothesis_id)
        except TrainTestExecutionError as exc:
            raise ValidationExecutionError(f"train/test evidence could not be verified: {exc}") from exc

        if (
            train_test_evidence.hypothesis_id != manifest.hypothesis_id
            or train_test_evidence.hypothesis_family_id != manifest.hypothesis_family_id
            or train_test_evidence.bound_hypothesis_content_hash != manifest.bound_hypothesis_content_hash
            or train_test_evidence.manifest_semantic_hash != manifest.manifest_semantic_hash
            or train_test_evidence.manifest_artifact_hash_ref != record.manifest_artifact_hash_ref
            or train_test_evidence.orchestrator_task_id != task_id
        ):
            raise ValidationExecutionError(
                "train/test evidence identity does not match this hypothesis's current binding"
            )
        if bound_split_plan.bound_split_plan_hash != train_test_evidence.dataset_split_membership.bound_split_plan_hash:
            raise ValidationExecutionError(
                "bound_split_plan does not match the split plan used to produce the prior "
                "train/test evidence"
            )

        if manifest.test_family not in SUPPORTED_TEST_FAMILIES:
            raise ValidationExecutionError(
                f"test_family {manifest.test_family!r} is not in the supported execution vocabulary"
            )
        runner = SUPPORTED_TEST_FAMILIES[manifest.test_family]

        membership = self._find_validation_dataset(manifest, bound_split_plan)

        try:
            exact_bytes = self.artifacts.read_verified(
                membership.artifact_hash_ref,
                expected_media_type=membership.artifact_media_type,
            )
        except ArtifactStoreError as exc:
            raise ValidationExecutionError(
                f"VALIDATION dataset artifact could not be read: {exc}"
            ) from exc
        rows = _read_csv_rows(exact_bytes)

        try:
            raw_metrics = runner(rows, primary_metric=manifest.primary_metric, parameters=manifest.semantic_parameters)
        except TrainTestExecutionError as exc:
            raise ValidationExecutionError(f"test family execution failed: {exc}") from exc
        except Exception as exc:  # noqa: BLE001 -- an injected test family is untrusted input.
            raise ValidationExecutionError(f"test family execution failed: {exc}") from exc
        metrics = _validated_metrics(raw_metrics)

        criteria_results, overall_passed = _evaluate_criteria(manifest.evaluation_criteria, metrics)
        verdict = (
            HypothesisState.VALIDATION_PASSED if overall_passed else HypothesisState.VALIDATION_REJECTED
        )

        executed_at = datetime.now(timezone.utc).isoformat()
        evidence = ValidationEvidenceV1(
            hypothesis_id=manifest.hypothesis_id,
            hypothesis_family_id=manifest.hypothesis_family_id,
            bound_hypothesis_content_hash=manifest.bound_hypothesis_content_hash,
            manifest_semantic_hash=manifest.manifest_semantic_hash,
            manifest_artifact_hash_ref=record.manifest_artifact_hash_ref,
            orchestrator_task_id=task_id,
            train_test_evidence_hash=train_test_evidence.evidence_hash,
            dataset_split_membership=membership,
            test_family=manifest.test_family,
            metrics=metrics,
            criteria_mode=manifest.evaluation_criteria.mode.value,
            criteria_results=tuple(criteria_results),
            verdict=verdict.value,
            executed_at=executed_at,
        )

        exact_evidence_bytes = evidence.canonical_bytes()
        artifact = self.artifacts.import_snapshot(
            io.BytesIO(exact_evidence_bytes),
            media_type=VALIDATION_EVIDENCE_MEDIA_TYPE,
        )
        if artifact.hash_ref != sha256_bytes(exact_evidence_bytes):
            raise ValidationExecutionError("Verified CAS returned an unexpected evidence hash")

        # Evidence is durable BEFORE the registry transition is ever
        # attempted, exactly like TrainTestExecutionControl -- required
        # ordering, and what makes a crash between these two steps safely
        # retryable.
        self._insert_row(evidence, evidence_artifact_hash_ref=artifact.hash_ref)

        try:
            self.registry.transition(hypothesis_id, verdict)
        except RegistryError as exc:
            current = self.registry.get(hypothesis_id)
            if current.state in (HypothesisState.VALIDATION_PASSED, HypothesisState.VALIDATION_REJECTED):
                stored = self._stored_row(hypothesis_id)
                if (
                    stored is not None
                    and stored["evidence_hash"] == evidence.evidence_hash
                    and stored["verdict"] == evidence.verdict
                ):
                    # A concurrent caller already completed the transition
                    # with the SAME evidence -- benign race, not a failure.
                    return evidence
            raise ValidationExecutionError(
                "registry transition failed after evidence was durably persisted; hypothesis "
                f"remains safely retryable: {exc}"
            ) from exc

        return evidence

    def _reload_existing_evidence(
        self, hypothesis_id: str, *, expected_state: HypothesisState
    ) -> ValidationEvidenceV1:
        stored = self._stored_row(hypothesis_id)
        if stored is None:
            raise ValidationExecutionError(
                "hypothesis has a validation verdict but this control has no recorded "
                "validation evidence binding for it (advanced through an external or legacy path)"
            )
        try:
            exact_bytes = self.artifacts.read_verified(
                stored["evidence_artifact_hash_ref"],
                expected_media_type=VALIDATION_EVIDENCE_MEDIA_TYPE,
            )
        except ArtifactStoreError as exc:
            raise ValidationExecutionError(
                f"recorded validation evidence artifact could not be verified: {exc}"
            ) from exc
        evidence = ValidationEvidenceV1.from_payload(parse_json(exact_bytes))
        if evidence.hypothesis_id != hypothesis_id:
            raise ValidationExecutionError("stored validation evidence hypothesis_id mismatch")
        if evidence.evidence_hash != stored["evidence_hash"]:
            raise ValidationExecutionError("stored validation evidence hash mismatch")
        if evidence.verdict != expected_state.value:
            raise ValidationExecutionError(
                "stored validation evidence verdict does not match the hypothesis's current state"
            )
        return evidence

    def get_evidence(self, hypothesis_id: str) -> ValidationEvidenceV1:
        """Re-verify and return the durably recorded validation evidence for
        an already-validated hypothesis."""
        record = self.registry.get(hypothesis_id)
        if record.state not in (HypothesisState.VALIDATION_PASSED, HypothesisState.VALIDATION_REJECTED):
            raise ValidationExecutionError(
                f"hypothesis has no validation evidence to read, state={record.state.value}"
            )
        return self._reload_existing_evidence(hypothesis_id, expected_state=record.state)


__all__ = [
    "VALIDATION_EVIDENCE_MEDIA_TYPE",
    "VALIDATION_EVIDENCE_SCHEMA_VERSION",
    "ValidationEvidenceV1",
    "ValidationExecutionControl",
    "ValidationExecutionError",
]
