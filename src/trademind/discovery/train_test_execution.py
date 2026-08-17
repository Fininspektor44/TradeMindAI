"""Train/Test Execution Control V1: the first missing bridge between a
FROZEN, V2-bound Discovery hypothesis and ``TRAIN_TESTED``.

Closes the gap identified by the Research Downstream Lifecycle audit: no
production module previously consumed a ``DiscoveryOrchestratorBridge`` V2
task and advanced ``HypothesisRegistry`` past ``FROZEN`` with real evidence --
the only callers of ``registry.transition(..., TRAIN_TESTED)`` anywhere in
the repository were raw test fixtures. This module is that missing layer,
built strictly on top of already-closed contracts without modifying any of
them:

  * ``HypothesisRegistry`` -- state machine and ``load_bound_manifest_v2``
    (discovery/hypothesis_registry.py) are reused exactly as-is. This module
    owns exactly one new additive SQLite table, ``train_test_evidence``, in
    the SAME registry database file -- the identical pattern
    ``HoldoutSealStore`` already established for ``final_holdout_seals``
    (discovery/holdout_store.py). The one-way ``_ALLOWED`` transition map is
    never touched.
  * ``DiscoveryOrchestratorBridge`` (discovery/orchestrator_bridge.py) is
    reused, not duplicated: ``_task_id()`` is called directly (it is a pure,
    deterministic function of ``hypothesis_id`` alone) to require that the
    exact Orchestrator task the Bridge would have created for this
    hypothesis already exists. Whether a Task can be created outside the
    Bridge with a colliding id is the SAME trust boundary the Bridge's own
    ``submit_frozen_hypothesis_v2`` duplicate-detection already accepts
    (existence-by-deterministic-id); this module does not narrow or widen
    that boundary.
  * ``dataset_split_provenance.verify_dataset_split_membership`` is reused,
    not duplicated: this module never trusts a caller's claim that a dataset
    is genuinely DISCOVERY content -- it always re-executes the same
    content-based CAS verification the Bridge uses, hardcoded to role
    ``"DISCOVERY"`` (never caller-selectable), which structurally forbids
    ever running against VALIDATION or FINAL_HOLDOUT content.
  * ``ArtifactStore``'s Verified CAS v1 (``import_snapshot`` /
    ``resolve_verified`` / ``read_verified``) is reused for evidence
    persistence, exactly like ``ExperimentManifestV2`` itself
    (``persist_experiment_manifest_v2`` / ``load_experiment_manifest_v2``) --
    content-addressed, tamper-evident, naturally idempotent for identical
    bytes.

``ExperimentManifestV2.test_family`` is, by that dataclass's own docstring,
"a predeclared name, not proof that a future runner supports them; the
trusted creation/execution boundary must bind them to its explicit supported
vocabulary before running anything." ``SUPPORTED_TEST_FAMILIES`` below is
exactly that vocabulary: a small, explicit, closed mapping from a
``test_family`` name to a deterministic, bounded, side-effect-free Python
callable. An unrecognized name fails closed rather than running anything.

Scope boundary: this module stops at ``TRAIN_TESTED``. It never references
either later train-tested-outcome state, and it never imports anything from
the final-holdout subsystem (holdout_crypto.py/holdout_keys.py/
holdout_runner.py/holdout_sealer.py) -- evaluating those later outcomes and
any subsequent final-holdout consumption remain the separately closed
responsibility of their own modules, most notably
``discovery/holdout_runner.py``.
"""

from __future__ import annotations

import csv
import io
import math
import re
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any

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
from .orchestrator_bridge import DiscoveryOrchestratorBridge


class TrainTestExecutionError(RuntimeError):
    """Raised when a bounded train/test execution cannot complete safely."""


TRAIN_TEST_EVIDENCE_SCHEMA_VERSION = "discovery-train-test-evidence-v1"
TRAIN_TEST_EVIDENCE_MEDIA_TYPE = "application/vnd.trademind.discovery.train-test-evidence-v1+json"
_EVIDENCE_HASH_DOMAIN = b"trademind:discovery:train-test-evidence:v1"

_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
_TASK_ID_SHAPE = re.compile(r"^discovery-[0-9a-f]{20}$")
_METRIC_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_MAX_METRICS = 64

_EVIDENCE_REQUIRED_FIELDS = frozenset(
    {
        "schema_version",
        "evidence_hash",
        "hypothesis_id",
        "hypothesis_family_id",
        "bound_hypothesis_content_hash",
        "manifest_semantic_hash",
        "manifest_artifact_hash_ref",
        "orchestrator_task_id",
        "dataset_split_membership",
        "test_family",
        "primary_metric",
        "metrics",
        "deterministic_seed",
        "diagnostics",
    }
)


def _bare_sha256(value: object, *, field_name: str) -> str:
    if type(value) is not str or _SHA256_HEX.fullmatch(value) is None:
        raise TrainTestExecutionError(f"{field_name} must be 64 lowercase hexadecimal digits")
    return value


def _nonempty_str(value: object, *, field_name: str) -> str:
    if type(value) is not str or not value.strip():
        raise TrainTestExecutionError(f"{field_name} must be a non-empty string")
    return value


def _validated_metrics(values: object) -> dict[str, float | int | bool | None]:
    """Bounded, deterministic, scalar-only metric validation.

    Deliberately mirrors ``discovery/holdout_runner.py``'s own
    ``_validated_metrics`` scalar/bounded contract (identical metric-name
    shape, identical finite-float requirement, identical entry ceiling) so
    evidence recorded here and at final-holdout evaluation share one
    disciplined shape -- without importing across the closed final-holdout
    boundary this module must never touch.
    """
    if not isinstance(values, Mapping) or not values:
        raise TrainTestExecutionError("test family must return non-empty aggregate metrics")
    if len(values) > _MAX_METRICS:
        raise TrainTestExecutionError("test family returned too many aggregate metrics")
    cleaned: dict[str, float | int | bool | None] = {}
    for key, value in values.items():
        if not isinstance(key, str) or not _METRIC_NAME.fullmatch(key):
            raise TrainTestExecutionError("test family metric name is invalid")
        if isinstance(value, bool) or value is None or isinstance(value, int):
            cleaned[key] = value
            continue
        if isinstance(value, float):
            if not math.isfinite(value):
                raise TrainTestExecutionError("test family metrics must be finite")
            cleaned[key] = value
            continue
        raise TrainTestExecutionError("test family may return only scalar numeric/bool metrics")
    return cleaned


TestFamilyRunner = Callable[..., Mapping[str, "float | int | bool | None"]]


def _deterministic_aggregate_v1(
    rows: Sequence[Mapping[str, str]],
    *,
    primary_metric: str,
    parameters: Mapping[str, Any],
) -> dict[str, float | int | bool | None]:
    """Built-in reference test family: deterministic, bounded, pure.

    Computes ``row_count`` over exactly the verified DISCOVERY rows and,
    when ``primary_metric`` names a column present in those rows, its
    arithmetic mean. No randomness, no I/O, no external calls -- a pure
    function of its inputs, which is what makes this stage's determinism
    and idempotency guarantee possible. ``parameters`` (the manifest's own
    ``semantic_parameters``) is accepted for forward compatibility with
    future test families and is unused by this one.
    """
    del parameters
    if not rows:
        raise TrainTestExecutionError("verified DISCOVERY dataset has no rows to evaluate")
    metrics: dict[str, float | int | bool | None] = {"row_count": len(rows)}
    if primary_metric in rows[0]:
        values: list[float] = []
        for row in rows:
            raw = (row.get(primary_metric) or "").strip()
            if not raw:
                continue
            try:
                values.append(float(raw))
            except ValueError as exc:
                raise TrainTestExecutionError(
                    f"primary_metric column {primary_metric!r} contains a non-numeric value"
                ) from exc
        if values:
            metrics[primary_metric] = sum(values) / len(values)
    return metrics


# Explicit, closed vocabulary binding ExperimentManifestV2.test_family (a
# predeclared name only -- see that dataclass's own docstring) to real,
# vetted code, exactly once, exactly here. An unrecognized name fails closed
# rather than running an unvetted computation.
SUPPORTED_TEST_FAMILIES: Mapping[str, TestFamilyRunner] = MappingProxyType(
    {
        "deterministic_aggregate_v1": _deterministic_aggregate_v1,
    }
)


def _read_csv_rows(exact_bytes: bytes) -> list[dict[str, str]]:
    try:
        text = exact_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise TrainTestExecutionError("DISCOVERY dataset artifact must be valid UTF-8 CSV") from exc
    reader = csv.DictReader(io.StringIO(text))
    rows = [dict(row) for row in reader]
    if not rows:
        raise TrainTestExecutionError("DISCOVERY dataset artifact contains no rows")
    return rows


@dataclass(frozen=True, slots=True)
class TrainTestEvidenceV1:
    """Provenance-bound evidence produced by exactly one bounded train/test
    execution for exactly one FROZEN hypothesis. Construct only via
    :meth:`TrainTestExecutionControl.execute` -- never by hand -- since the
    proof is only meaningful if it was produced by actually re-verifying the
    manifest binding, the matching Orchestrator task, and the DISCOVERY
    dataset content, and then actually running the manifest-declared
    ``test_family``."""

    hypothesis_id: str
    hypothesis_family_id: str
    bound_hypothesis_content_hash: str
    manifest_semantic_hash: str
    manifest_artifact_hash_ref: str
    orchestrator_task_id: str
    dataset_split_membership: DatasetSplitMembershipV1
    test_family: str
    primary_metric: str
    metrics: Mapping[str, float | int | bool | None]
    deterministic_seed: int | None
    executed_at: str
    schema_version: str = TRAIN_TEST_EVIDENCE_SCHEMA_VERSION
    evidence_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != TRAIN_TEST_EVIDENCE_SCHEMA_VERSION:
            raise TrainTestExecutionError("unsupported train/test evidence schema_version")
        _nonempty_str(self.hypothesis_id, field_name="hypothesis_id")
        _nonempty_str(self.hypothesis_family_id, field_name="hypothesis_family_id")
        _bare_sha256(self.bound_hypothesis_content_hash, field_name="bound_hypothesis_content_hash")
        try:
            validate_sha256_ref(self.manifest_semantic_hash)
        except ProvenanceError as exc:
            raise TrainTestExecutionError("manifest_semantic_hash is invalid") from exc
        try:
            validate_sha256_ref(self.manifest_artifact_hash_ref)
        except ProvenanceError as exc:
            raise TrainTestExecutionError("manifest_artifact_hash_ref is invalid") from exc
        if (
            type(self.orchestrator_task_id) is not str
            or _TASK_ID_SHAPE.fullmatch(self.orchestrator_task_id) is None
        ):
            raise TrainTestExecutionError("orchestrator_task_id is invalid")
        if type(self.dataset_split_membership) is not DatasetSplitMembershipV1:
            raise TrainTestExecutionError(
                "dataset_split_membership must be an exact DatasetSplitMembershipV1"
            )
        if self.dataset_split_membership.role != "DISCOVERY":
            raise TrainTestExecutionError(
                "train/test evidence must be bound to DISCOVERY dataset content only"
            )
        _nonempty_str(self.test_family, field_name="test_family")
        _nonempty_str(self.primary_metric, field_name="primary_metric")
        object.__setattr__(self, "metrics", MappingProxyType(_validated_metrics(self.metrics)))
        if self.deterministic_seed is not None and (
            type(self.deterministic_seed) is not int or self.deterministic_seed < 0
        ):
            raise TrainTestExecutionError(
                "deterministic_seed must be null or an exact non-negative integer"
            )
        if type(self.executed_at) is not str:
            raise TrainTestExecutionError("executed_at must be a string")
        try:
            parsed = datetime.fromisoformat(self.executed_at)
        except ValueError as exc:
            raise TrainTestExecutionError("executed_at must be an ISO timestamp") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise TrainTestExecutionError("executed_at must be timezone-aware")

        object.__setattr__(
            self,
            "evidence_hash",
            sha256_bytes(_EVIDENCE_HASH_DOMAIN + b"\x00" + canonical_json_bytes(self.semantic_projection())),
        )
        canonical_json_bytes(self.to_payload())

    def semantic_projection(self) -> dict[str, object]:
        """Everything that determines this evidence's identity -- excludes
        ``executed_at`` (wall-clock, non-deterministic) so identical inputs
        always produce the identical ``evidence_hash``, which is exactly
        what makes retrying an interrupted execution idempotent."""
        return {
            "schema_version": self.schema_version,
            "hypothesis_id": self.hypothesis_id,
            "hypothesis_family_id": self.hypothesis_family_id,
            "bound_hypothesis_content_hash": self.bound_hypothesis_content_hash,
            "manifest_semantic_hash": self.manifest_semantic_hash,
            "manifest_artifact_hash_ref": self.manifest_artifact_hash_ref,
            "orchestrator_task_id": self.orchestrator_task_id,
            "dataset_split_membership": self.dataset_split_membership.to_payload(),
            "test_family": self.test_family,
            "primary_metric": self.primary_metric,
            "metrics": dict(self.metrics),
            "deterministic_seed": self.deterministic_seed,
        }

    def to_payload(self) -> dict[str, object]:
        payload = self.semantic_projection()
        payload["evidence_hash"] = self.evidence_hash
        payload["diagnostics"] = {"executed_at": self.executed_at}
        return payload

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_payload())

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> TrainTestEvidenceV1:
        try:
            frozen = freeze_json_object(payload, field_name="train_test_evidence")
        except ProvenanceError as exc:
            raise TrainTestExecutionError("train_test_evidence must be strict bounded JSON") from exc
        fields = frozenset(frozen)
        missing = _EVIDENCE_REQUIRED_FIELDS - fields
        unknown = fields - _EVIDENCE_REQUIRED_FIELDS
        if missing:
            raise TrainTestExecutionError(
                f"train_test_evidence is missing fields: {', '.join(sorted(missing))}"
            )
        if unknown:
            raise TrainTestExecutionError(
                f"train_test_evidence contains unknown fields: {', '.join(sorted(unknown))}"
            )
        diagnostics = frozen["diagnostics"]
        if not isinstance(diagnostics, Mapping) or frozenset(diagnostics) != {"executed_at"}:
            raise TrainTestExecutionError("train_test_evidence.diagnostics is invalid")
        membership_payload = frozen["dataset_split_membership"]
        if not isinstance(membership_payload, Mapping):
            raise TrainTestExecutionError(
                "train_test_evidence.dataset_split_membership must be a JSON object"
            )
        try:
            membership = DatasetSplitMembershipV1.from_payload(membership_payload)
        except DatasetProvenanceError as exc:
            raise TrainTestExecutionError(
                f"train_test_evidence dataset_split_membership is invalid: {exc}"
            ) from exc
        metrics_payload = frozen["metrics"]
        if not isinstance(metrics_payload, Mapping):
            raise TrainTestExecutionError("train_test_evidence.metrics must be a JSON object")
        built = cls(
            hypothesis_id=frozen["hypothesis_id"],
            hypothesis_family_id=frozen["hypothesis_family_id"],
            bound_hypothesis_content_hash=frozen["bound_hypothesis_content_hash"],
            manifest_semantic_hash=frozen["manifest_semantic_hash"],
            manifest_artifact_hash_ref=frozen["manifest_artifact_hash_ref"],
            orchestrator_task_id=frozen["orchestrator_task_id"],
            dataset_split_membership=membership,
            test_family=frozen["test_family"],
            primary_metric=frozen["primary_metric"],
            metrics=dict(metrics_payload),
            deterministic_seed=frozen["deterministic_seed"],
            executed_at=diagnostics["executed_at"],
            schema_version=frozen["schema_version"],
        )
        try:
            claimed_hash = validate_sha256_ref(frozen["evidence_hash"])
        except ProvenanceError as exc:
            raise TrainTestExecutionError("train_test_evidence.evidence_hash is invalid") from exc
        if claimed_hash != built.evidence_hash:
            raise TrainTestExecutionError("train_test_evidence hash identity mismatch")
        return built


class TrainTestExecutionControl:
    """FROZEN hypothesis + verified V2 manifest + matching Orchestrator task +
    verified DISCOVERY dataset -> real bounded train/test evidence ->
    ``TRAIN_TESTED``.

    Owns exactly one new additive SQLite table (``train_test_evidence``) in
    the same registry database file, mirroring ``HoldoutSealStore``'s own
    pattern. ``HypothesisRegistry``'s schema, one-way state machine, and
    ``DiscoveryOrchestratorBridge`` are never modified.
    """

    def __init__(
        self,
        *,
        registry: HypothesisRegistry,
        control: ControlPlane,
        artifacts: ArtifactStore,
    ) -> None:
        self.registry = registry
        self.control = control
        self.artifacts = artifacts
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
                CREATE TABLE IF NOT EXISTS train_test_evidence (
                    hypothesis_id TEXT PRIMARY KEY,
                    hypothesis_family_id TEXT NOT NULL,
                    manifest_semantic_hash TEXT NOT NULL,
                    manifest_artifact_hash_ref TEXT NOT NULL,
                    orchestrator_task_id TEXT NOT NULL,
                    evidence_hash TEXT NOT NULL,
                    evidence_artifact_hash_ref TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(hypothesis_id) REFERENCES hypotheses(hypothesis_id)
                )
                """
            )
            columns = {
                row["name"] for row in db.execute("PRAGMA table_info(train_test_evidence)").fetchall()
            }
            required = {
                "hypothesis_id",
                "hypothesis_family_id",
                "manifest_semantic_hash",
                "manifest_artifact_hash_ref",
                "orchestrator_task_id",
                "evidence_hash",
                "evidence_artifact_hash_ref",
                "created_at",
            }
            if not required.issubset(columns):
                raise TrainTestExecutionError(
                    "legacy train_test_evidence schema detected; explicit migration is required"
                )

    def _stored_row(self, hypothesis_id: str) -> sqlite3.Row | None:
        with self._connect() as db:
            return db.execute(
                "SELECT * FROM train_test_evidence WHERE hypothesis_id=?",
                (hypothesis_id,),
            ).fetchone()

    def _insert_row(self, evidence: TrainTestEvidenceV1, *, evidence_artifact_hash_ref: str) -> None:
        """Durably record the evidence binding. Fails closed (raises
        ``TrainTestExecutionError``) if a DIFFERENT evidence binding already
        exists for this hypothesis; silently returns if an IDENTICAL one
        already exists (a benign concurrent-writer race, not a conflict)."""
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                db.execute(
                    """
                    INSERT INTO train_test_evidence(
                        hypothesis_id, hypothesis_family_id, manifest_semantic_hash,
                        manifest_artifact_hash_ref, orchestrator_task_id, evidence_hash,
                        evidence_artifact_hash_ref, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        evidence.hypothesis_id,
                        evidence.hypothesis_family_id,
                        evidence.manifest_semantic_hash,
                        evidence.manifest_artifact_hash_ref,
                        evidence.orchestrator_task_id,
                        evidence.evidence_hash,
                        evidence_artifact_hash_ref,
                        now,
                    ),
                )
            except sqlite3.IntegrityError:
                existing = db.execute(
                    "SELECT evidence_hash FROM train_test_evidence WHERE hypothesis_id=?",
                    (evidence.hypothesis_id,),
                ).fetchone()
                if existing is None or existing["evidence_hash"] != evidence.evidence_hash:
                    raise TrainTestExecutionError(
                        "conflicting train/test evidence already recorded for this hypothesis"
                    ) from None

    def execute(
        self,
        hypothesis_id: str,
        *,
        bound_split_plan: BoundSplitPlanV1,
    ) -> TrainTestEvidenceV1:
        """Run the manifest-declared ``test_family`` against the verified
        DISCOVERY dataset for a FROZEN, V2-bound hypothesis, durably persist
        provenance-bound evidence, and only then advance the hypothesis to
        ``TRAIN_TESTED``. Idempotent: an identical repeat (or a retry after a
        prior call recorded evidence but failed before completing the state
        transition) returns the same evidence without re-executing the test
        family or duplicating any row. A conflicting repeat -- different
        inputs producing different evidence for the same hypothesis -- fails
        closed."""
        if type(bound_split_plan) is not BoundSplitPlanV1:
            raise TrainTestExecutionError("bound_split_plan must be an exact BoundSplitPlanV1")

        record = self.registry.get(hypothesis_id)

        if record.state is HypothesisState.TRAIN_TESTED:
            return self._reload_existing_evidence(hypothesis_id)

        if record.state is not HypothesisState.FROZEN:
            raise TrainTestExecutionError(
                f"hypothesis must be FROZEN to execute train/test evidence, got {record.state.value}"
            )

        try:
            manifest = self.registry.load_bound_manifest_v2(hypothesis_id, artifact_store=self.artifacts)
        except KeyError as exc:
            raise TrainTestExecutionError(f"hypothesis does not exist: {exc}") from exc
        except RegistryError as exc:
            raise TrainTestExecutionError(
                f"hypothesis is not safely bound to a verified V2 manifest: {exc}"
            ) from exc
        except ArtifactStoreError as exc:
            raise TrainTestExecutionError(
                f"bound manifest v2 dataset artifacts could not be verified: {exc}"
            ) from exc

        family = self.registry.family_status(manifest.hypothesis_family_id)
        if family["holdout_consumed"]:
            raise TrainTestExecutionError("final holdout has already been consumed for this family")
        if family["terminal_state"]:
            raise TrainTestExecutionError(f"hypothesis family is terminal: {family['terminal_state']}")

        task_id = DiscoveryOrchestratorBridge._task_id(hypothesis_id)
        task = self.control.task_store.get(task_id)
        if task is None:
            raise TrainTestExecutionError(
                "no matching Orchestrator task exists for this frozen hypothesis; submit it "
                "through DiscoveryOrchestratorBridge.submit_frozen_hypothesis_v2 first"
            )

        if manifest.test_family not in SUPPORTED_TEST_FAMILIES:
            raise TrainTestExecutionError(
                f"test_family {manifest.test_family!r} is not in the supported execution vocabulary"
            )
        runner = SUPPORTED_TEST_FAMILIES[manifest.test_family]

        discovery_dataset = next(
            (item for item in manifest.datasets if item.role == manifest.split_dataset_role),
            None,
        )
        if discovery_dataset is None:
            raise TrainTestExecutionError("manifest split_dataset_role has no matching dataset binding")

        try:
            membership = verify_dataset_split_membership(
                artifact_store=self.artifacts,
                bound_split_plan=bound_split_plan,
                role="DISCOVERY",
                artifact_hash_ref=discovery_dataset.artifact_hash_ref,
                media_type=discovery_dataset.media_type,
            )
        except DatasetProvenanceError as exc:
            raise TrainTestExecutionError(
                f"DISCOVERY dataset split-membership verification failed: {exc}"
            ) from exc
        except ArtifactStoreError as exc:
            raise TrainTestExecutionError(
                f"DISCOVERY dataset artifact could not be verified: {exc}"
            ) from exc

        try:
            exact_bytes = self.artifacts.read_verified(
                discovery_dataset.artifact_hash_ref,
                expected_media_type=discovery_dataset.media_type,
            )
        except ArtifactStoreError as exc:
            raise TrainTestExecutionError(
                f"DISCOVERY dataset artifact could not be read: {exc}"
            ) from exc
        rows = _read_csv_rows(exact_bytes)

        try:
            raw_metrics = runner(rows, primary_metric=manifest.primary_metric, parameters=manifest.semantic_parameters)
        except TrainTestExecutionError:
            raise
        except Exception as exc:  # noqa: BLE001 -- an injected test family is untrusted input.
            raise TrainTestExecutionError(f"test family execution failed: {exc}") from exc
        metrics = _validated_metrics(raw_metrics)

        executed_at = datetime.now(timezone.utc).isoformat()
        evidence = TrainTestEvidenceV1(
            hypothesis_id=manifest.hypothesis_id,
            hypothesis_family_id=manifest.hypothesis_family_id,
            bound_hypothesis_content_hash=manifest.bound_hypothesis_content_hash,
            manifest_semantic_hash=manifest.manifest_semantic_hash,
            manifest_artifact_hash_ref=record.manifest_artifact_hash_ref,
            orchestrator_task_id=task_id,
            dataset_split_membership=membership,
            test_family=manifest.test_family,
            primary_metric=manifest.primary_metric,
            metrics=metrics,
            deterministic_seed=manifest.deterministic_seed,
            executed_at=executed_at,
        )

        exact_evidence_bytes = evidence.canonical_bytes()
        artifact = self.artifacts.import_snapshot(
            io.BytesIO(exact_evidence_bytes),
            media_type=TRAIN_TEST_EVIDENCE_MEDIA_TYPE,
        )
        if artifact.hash_ref != sha256_bytes(exact_evidence_bytes):
            raise TrainTestExecutionError("Verified CAS returned an unexpected evidence hash")

        # Evidence is durable BEFORE the registry transition is ever
        # attempted -- required ordering, and what makes a crash between
        # these two steps safely retryable (the retry finds this exact row
        # and skips straight to (re)attempting the transition).
        self._insert_row(evidence, evidence_artifact_hash_ref=artifact.hash_ref)

        try:
            self.registry.transition(hypothesis_id, HypothesisState.TRAIN_TESTED)
        except RegistryError as exc:
            current = self.registry.get(hypothesis_id)
            if current.state is HypothesisState.TRAIN_TESTED:
                stored = self._stored_row(hypothesis_id)
                if stored is not None and stored["evidence_hash"] == evidence.evidence_hash:
                    # A concurrent caller already completed the transition
                    # with the SAME evidence -- benign race, not a failure.
                    return evidence
            raise TrainTestExecutionError(
                "registry transition to TRAIN_TESTED failed after evidence was durably "
                f"persisted; hypothesis remains safely retryable: {exc}"
            ) from exc

        return evidence

    def _reload_existing_evidence(self, hypothesis_id: str) -> TrainTestEvidenceV1:
        stored = self._stored_row(hypothesis_id)
        if stored is None:
            raise TrainTestExecutionError(
                "hypothesis is TRAIN_TESTED but this control has no recorded train/test "
                "evidence binding for it (advanced through an external or legacy path)"
            )
        try:
            exact_bytes = self.artifacts.read_verified(
                stored["evidence_artifact_hash_ref"],
                expected_media_type=TRAIN_TEST_EVIDENCE_MEDIA_TYPE,
            )
        except ArtifactStoreError as exc:
            raise TrainTestExecutionError(
                f"recorded train/test evidence artifact could not be verified: {exc}"
            ) from exc
        evidence = TrainTestEvidenceV1.from_payload(parse_json(exact_bytes))
        if evidence.hypothesis_id != hypothesis_id:
            raise TrainTestExecutionError("stored train/test evidence hypothesis_id mismatch")
        if evidence.evidence_hash != stored["evidence_hash"]:
            raise TrainTestExecutionError("stored train/test evidence hash mismatch")
        return evidence

    def get_evidence(self, hypothesis_id: str) -> TrainTestEvidenceV1:
        """Re-verify and return the durably recorded train/test evidence for
        an already-``TRAIN_TESTED`` hypothesis."""
        record = self.registry.get(hypothesis_id)
        if record.state is not HypothesisState.TRAIN_TESTED:
            raise TrainTestExecutionError(
                f"hypothesis has no train/test evidence to read, state={record.state.value}"
            )
        return self._reload_existing_evidence(hypothesis_id)


__all__ = [
    "SUPPORTED_TEST_FAMILIES",
    "TRAIN_TEST_EVIDENCE_MEDIA_TYPE",
    "TRAIN_TEST_EVIDENCE_SCHEMA_VERSION",
    "TrainTestEvidenceV1",
    "TrainTestExecutionControl",
    "TrainTestExecutionError",
]
