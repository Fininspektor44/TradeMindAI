"""Final Verdict / Acceptance Control V1: the last research lifecycle layer,
from a HOLDOUT_CONSUMED hypothesis to ``ACCEPTED`` or ``REJECTED_FINAL``.

This module NEVER decrypts, evaluates, or reads final-holdout plaintext,
NEVER touches decryption key material, NEVER calls ``FinalHoldoutRunner.
run_once()``, and NEVER re-runs the evaluator. It consumes only what the
already-closed, trusted holdout consumption durably left behind:

  * the tamper-evident ``ResultLedger`` (discovery/result_ledger.py) --
    specifically the ``FINAL_HOLDOUT_CLAIM``/``FINAL_HOLDOUT_RESULT`` records
    ``FinalHoldoutRunner.run_once()`` (discovery/holdout_runner.py) already
    wrote at consumption time. This module reads that ledger read-only,
    reusing its own closed ``verify()`` method for hash-chain tamper
    evidence, and never appends to it -- appending, claiming, and the
    ``HOLDOUT_CONSUMED`` transition itself remain exclusively
    ``FinalHoldoutRunner``'s job, already done, before this module ever
    runs.

  * ``HoldoutTriggerBridge.get_authorization`` (discovery/
    holdout_trigger_bridge.py) is reused directly to load and re-verify the
    plaintext-free authorization record that was durably persisted before
    the trusted run.

  * ``ValidationExecutionControl``'s private ``_load_verified_manifest_past_frozen``
    and ``_reload_existing_evidence`` (discovery/validation_execution.py) are
    reused directly, exactly as ``HoldoutTriggerBridge`` itself already
    reuses ``_load_verified_manifest_past_frozen`` -- the identical
    reasoning applies at this even-later pipeline stage. The PUBLIC
    ``get_evidence`` wrapper cannot be used here: it requires the
    hypothesis's CURRENT state to still be VALIDATION_PASSED/
    VALIDATION_REJECTED, which is never true once the hypothesis has
    legitimately advanced to HOLDOUT_CONSUMED. ``_reload_existing_evidence``
    performs the identical CAS re-verification without that state gate,
    taking the expected verdict (``VALIDATION_PASSED``) as an explicit
    parameter instead of deriving it from current registry state -- the same
    pattern ``get_authorization`` on ``HoldoutTriggerBridge`` already uses by
    never gating on hypothesis state at all.

  * ``validation_execution._evaluate_criteria`` (the exact function
    ``ValidationExecutionControl`` uses for its own VALIDATION-stage
    decision) is reused directly for the FINAL-HOLDOUT-stage decision too --
    not because the manifest's VALIDATION-stage criteria field itself is
    reused (it is never read by this module at all), but because the
    criterion-evaluation ALGORITHM
    (missing-metric/NaN-Inf/malformed-threshold/unsupported-operator-or-mode
    fail-closed dispatch, then ALL/ANY combination) is stage-neutral code
    that both ``EvaluationCriteriaV1`` and ``FinalHoldoutCriteriaV1``
    (discovery/manifest.py) satisfy by construction -- they share the same
    ``mode``/``criteria`` attribute shape. This is "reuse existing criteria/
    operator evaluation logic where safe," applied to the algorithm, never
    to the VALIDATION-stage data.

  * ``manifest.verify_final_holdout_metric_vocabulary`` (discovery/
    manifest.py) is reused directly to prove the ledger-recovered
    ``aggregate_metrics`` satisfy ``ExperimentManifestV2.
    final_holdout_criteria``'s declared metric-name vocabulary before any
    threshold is ever evaluated.

``ExperimentManifestV2.alpha``/``q``/``minimum_effect_size``/
``max_hypotheses_tests`` and the manifest's VALIDATION-stage criteria field
are never read by this module -- see the Final Holdout Acceptance
Contract's own documentation for why reapplying them here would silently
assume a correspondence neither contract promises.

Scope: this module is the terminal research-lifecycle layer. It transitions
a hypothesis to exactly one of the two terminal outcome states and then
stops -- there is nothing further downstream.
"""

from __future__ import annotations

import io
import json
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

from .holdout_trigger_bridge import HoldoutTriggerBridge, HoldoutTriggerError
from .hypothesis_registry import HypothesisRegistry, HypothesisState, RegistryError
from .manifest import (
    CriteriaMode,
    CriterionOperator,
    ManifestV2ValidationError,
    verify_final_holdout_metric_vocabulary,
)
from .orchestrator_bridge import DiscoveryOrchestratorBridge
from .train_test_execution import _validated_metrics
from .validation_execution import (
    ValidationExecutionControl,
    ValidationExecutionError,
    _evaluate_criteria,
)


class FinalVerdictError(RuntimeError):
    """Raised when a bounded final-verdict finalization cannot complete
    safely."""


FINAL_VERDICT_EVIDENCE_SCHEMA_VERSION = "discovery-final-verdict-evidence-v1"
FINAL_VERDICT_EVIDENCE_MEDIA_TYPE = "application/vnd.trademind.discovery.final-verdict-evidence-v1+json"
_EVIDENCE_HASH_DOMAIN = b"trademind:discovery:final-verdict-evidence:v1"

_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
_TASK_ID_SHAPE = re.compile(r"^discovery-[0-9a-f]{20}$")

_VERDICT_VALUES = frozenset(
    {HypothesisState.ACCEPTED.value, HypothesisState.REJECTED_FINAL.value}
)
_CRITERIA_MODE_VALUES = frozenset({CriteriaMode.ALL.value, CriteriaMode.ANY.value})
_OPERATOR_VALUES = frozenset(item.value for item in CriterionOperator)
_CRITERION_RESULT_FIELDS = frozenset({"metric", "operator", "threshold", "observed_value", "passed"})


def _bare_sha256(value: object, *, field_name: str) -> str:
    if type(value) is not str or _SHA256_HEX.fullmatch(value) is None:
        raise FinalVerdictError(f"{field_name} must be 64 lowercase hexadecimal digits")
    return value


def _nonempty_str(value: object, *, field_name: str) -> str:
    if type(value) is not str or not value.strip():
        raise FinalVerdictError(f"{field_name} must be a non-empty string")
    return value


def _read_verified_ledger_records(ledger) -> list[dict[str, object]]:
    """Read every record from a closed ``ResultLedger``, after re-verifying
    its own tamper-evident hash chain. Read-only: never appends, never
    claims, never mutates the ledger."""
    if not ledger.verify():
        raise FinalVerdictError("result ledger integrity verification failed")
    try:
        with ledger.path.open("r", encoding="utf-8") as handle:
            return [json.loads(raw) for raw in handle if raw.strip()]
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FinalVerdictError(f"cannot read result ledger: {exc}") from exc


def _find_holdout_receipt_records(
    records: list[dict[str, object]],
    *,
    hypothesis_id: str,
    hypothesis_family_id: str,
) -> tuple[dict[str, object], str, dict[str, object], str]:
    """Recover, from the already-persisted ledger alone, the exact
    ``FINAL_HOLDOUT_CLAIM``/``FINAL_HOLDOUT_RESULT`` record pair
    ``FinalHoldoutRunner.run_once()`` wrote for this hypothesis, requiring
    exactly one of each and a matching cross-reference between them.

    Fails closed if the holdout was consumed but never reached a result
    (e.g. the evaluator failed after one-shot consumption, recorded only as
    ``FINAL_HOLDOUT_RUN_FAILED``) -- there is no ``aggregate_metrics`` to
    finalize in that case, and this function never invents one.
    """
    claims: list[tuple[dict[str, object], str]] = []
    results: list[tuple[dict[str, object], str]] = []
    for record in records:
        payload = record.get("payload")
        record_hash = record.get("record_hash")
        if not isinstance(payload, dict) or not isinstance(record_hash, str):
            continue
        if payload.get("hypothesis_id") != hypothesis_id:
            continue
        record_type = payload.get("record_type")
        if record_type == "FINAL_HOLDOUT_CLAIM":
            claims.append((payload, record_hash))
        elif record_type == "FINAL_HOLDOUT_RESULT":
            results.append((payload, record_hash))

    if not results:
        raise FinalVerdictError(
            "no FINAL_HOLDOUT_RESULT record exists for this hypothesis in the result "
            "ledger; the holdout run may have failed after consumption or never completed"
        )
    if len(results) > 1:
        raise FinalVerdictError(
            "more than one FINAL_HOLDOUT_RESULT record exists for this hypothesis; "
            "conflicting ledger state"
        )
    if not claims:
        raise FinalVerdictError("no FINAL_HOLDOUT_CLAIM record exists for this hypothesis")
    if len(claims) > 1:
        raise FinalVerdictError(
            "more than one FINAL_HOLDOUT_CLAIM record exists for this hypothesis; "
            "conflicting ledger state"
        )

    result_payload, result_record_hash = results[0]
    claim_payload, claim_record_hash = claims[0]
    if result_payload.get("claim_record_hash") != claim_record_hash:
        raise FinalVerdictError(
            "FINAL_HOLDOUT_RESULT does not reference the matching FINAL_HOLDOUT_CLAIM"
        )
    if (
        claim_payload.get("hypothesis_family_id") != hypothesis_family_id
        or result_payload.get("hypothesis_family_id") != hypothesis_family_id
    ):
        raise FinalVerdictError(
            "ledger holdout records do not match the expected hypothesis family"
        )
    return claim_payload, claim_record_hash, result_payload, result_record_hash


@dataclass(frozen=True, slots=True)
class FinalVerdictEvidenceV1:
    """Provenance-bound evidence produced by exactly one bounded
    finalization for exactly one HOLDOUT_CONSUMED hypothesis. Construct
    only via :meth:`FinalVerdictAcceptanceControl.finalize` -- never by
    hand. Carries no plaintext, no decryption key, and no raw path; only
    content hashes, stable identifiers, and the bounded aggregate metrics
    already safely returned by the trusted holdout run."""

    hypothesis_id: str
    hypothesis_family_id: str
    bound_hypothesis_content_hash: str
    manifest_semantic_hash: str
    manifest_artifact_hash_ref: str
    orchestrator_task_id: str
    validation_evidence_hash: str
    holdout_authorization_hash: str
    final_holdout_claim_record_hash: str
    final_holdout_result_record_hash: str
    envelope_hash: str
    evaluator_id: str
    evaluator_hash: str
    aggregate_metrics: Mapping[str, float | int | bool | None]
    final_holdout_criteria_semantic_hash: str
    criteria_mode: str
    criteria_results: tuple[Mapping[str, object], ...]
    verdict: str
    finalized_at: str
    schema_version: str = FINAL_VERDICT_EVIDENCE_SCHEMA_VERSION
    evidence_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != FINAL_VERDICT_EVIDENCE_SCHEMA_VERSION:
            raise FinalVerdictError("unsupported final verdict evidence schema_version")
        _nonempty_str(self.hypothesis_id, field_name="hypothesis_id")
        _nonempty_str(self.hypothesis_family_id, field_name="hypothesis_family_id")
        _bare_sha256(self.bound_hypothesis_content_hash, field_name="bound_hypothesis_content_hash")
        for value, field_name in (
            (self.manifest_semantic_hash, "manifest_semantic_hash"),
            (self.manifest_artifact_hash_ref, "manifest_artifact_hash_ref"),
            (self.validation_evidence_hash, "validation_evidence_hash"),
            (self.holdout_authorization_hash, "holdout_authorization_hash"),
            (self.final_holdout_criteria_semantic_hash, "final_holdout_criteria_semantic_hash"),
        ):
            try:
                validate_sha256_ref(value)
            except ProvenanceError as exc:
                raise FinalVerdictError(f"{field_name} is invalid") from exc
        for value, field_name in (
            (self.final_holdout_claim_record_hash, "final_holdout_claim_record_hash"),
            (self.final_holdout_result_record_hash, "final_holdout_result_record_hash"),
            (self.envelope_hash, "envelope_hash"),
            (self.evaluator_hash, "evaluator_hash"),
        ):
            _bare_sha256(value, field_name=field_name)
        if (
            type(self.orchestrator_task_id) is not str
            or _TASK_ID_SHAPE.fullmatch(self.orchestrator_task_id) is None
        ):
            raise FinalVerdictError("orchestrator_task_id is invalid")
        _nonempty_str(self.evaluator_id, field_name="evaluator_id")
        object.__setattr__(self, "aggregate_metrics", MappingProxyType(_validated_metrics(self.aggregate_metrics)))

        if self.criteria_mode not in _CRITERIA_MODE_VALUES:
            raise FinalVerdictError("criteria_mode is invalid")
        if type(self.criteria_results) is not tuple or not self.criteria_results:
            raise FinalVerdictError("criteria_results must be a non-empty tuple")
        cleaned_results: list[Mapping[str, object]] = []
        for entry in self.criteria_results:
            if not isinstance(entry, Mapping) or frozenset(entry) != _CRITERION_RESULT_FIELDS:
                raise FinalVerdictError("criteria_results entry is malformed")
            _nonempty_str(entry["metric"], field_name="criteria_results.metric")
            if entry["operator"] not in _OPERATOR_VALUES:
                raise FinalVerdictError("criteria_results.operator is invalid")
            if type(entry["passed"]) is not bool:
                raise FinalVerdictError("criteria_results.passed must be an exact bool")
            cleaned_results.append(MappingProxyType(dict(entry)))
        object.__setattr__(self, "criteria_results", tuple(cleaned_results))

        if self.criteria_mode == CriteriaMode.ALL.value:
            expected_overall = all(item["passed"] for item in self.criteria_results)
        else:
            expected_overall = any(item["passed"] for item in self.criteria_results)
        expected_verdict = (
            HypothesisState.ACCEPTED.value if expected_overall else HypothesisState.REJECTED_FINAL.value
        )
        if self.verdict not in _VERDICT_VALUES:
            raise FinalVerdictError("verdict is invalid")
        if self.verdict != expected_verdict:
            raise FinalVerdictError(
                "verdict is inconsistent with its own criteria_results/criteria_mode"
            )

        if type(self.finalized_at) is not str:
            raise FinalVerdictError("finalized_at must be a string")
        try:
            parsed = datetime.fromisoformat(self.finalized_at)
        except ValueError as exc:
            raise FinalVerdictError("finalized_at must be an ISO timestamp") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise FinalVerdictError("finalized_at must be timezone-aware")

        object.__setattr__(
            self,
            "evidence_hash",
            sha256_bytes(_EVIDENCE_HASH_DOMAIN + b"\x00" + canonical_json_bytes(self.semantic_projection())),
        )
        canonical_json_bytes(self.to_payload())

    def semantic_projection(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "hypothesis_id": self.hypothesis_id,
            "hypothesis_family_id": self.hypothesis_family_id,
            "bound_hypothesis_content_hash": self.bound_hypothesis_content_hash,
            "manifest_semantic_hash": self.manifest_semantic_hash,
            "manifest_artifact_hash_ref": self.manifest_artifact_hash_ref,
            "orchestrator_task_id": self.orchestrator_task_id,
            "validation_evidence_hash": self.validation_evidence_hash,
            "holdout_authorization_hash": self.holdout_authorization_hash,
            "final_holdout_claim_record_hash": self.final_holdout_claim_record_hash,
            "final_holdout_result_record_hash": self.final_holdout_result_record_hash,
            "envelope_hash": self.envelope_hash,
            "evaluator_id": self.evaluator_id,
            "evaluator_hash": self.evaluator_hash,
            "aggregate_metrics": dict(self.aggregate_metrics),
            "final_holdout_criteria_semantic_hash": self.final_holdout_criteria_semantic_hash,
            "criteria_mode": self.criteria_mode,
            "criteria_results": [dict(item) for item in self.criteria_results],
            "verdict": self.verdict,
        }

    def to_payload(self) -> dict[str, object]:
        payload = self.semantic_projection()
        payload["evidence_hash"] = self.evidence_hash
        payload["diagnostics"] = {"finalized_at": self.finalized_at}
        return payload

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_payload())

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> FinalVerdictEvidenceV1:
        try:
            frozen = freeze_json_object(payload, field_name="final_verdict_evidence")
        except ProvenanceError as exc:
            raise FinalVerdictError("final_verdict_evidence must be strict bounded JSON") from exc
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
                "validation_evidence_hash",
                "holdout_authorization_hash",
                "final_holdout_claim_record_hash",
                "final_holdout_result_record_hash",
                "envelope_hash",
                "evaluator_id",
                "evaluator_hash",
                "aggregate_metrics",
                "final_holdout_criteria_semantic_hash",
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
            raise FinalVerdictError(
                f"final_verdict_evidence is missing fields: {', '.join(sorted(missing))}"
            )
        if unknown:
            raise FinalVerdictError(
                f"final_verdict_evidence contains unknown fields: {', '.join(sorted(unknown))}"
            )
        diagnostics = frozen["diagnostics"]
        if not isinstance(diagnostics, Mapping) or frozenset(diagnostics) != {"finalized_at"}:
            raise FinalVerdictError("final_verdict_evidence.diagnostics is invalid")
        metrics_payload = frozen["aggregate_metrics"]
        if not isinstance(metrics_payload, Mapping):
            raise FinalVerdictError("final_verdict_evidence.aggregate_metrics must be a JSON object")
        raw_results = frozen["criteria_results"]
        if type(raw_results) is not tuple:
            raise FinalVerdictError("final_verdict_evidence.criteria_results must be a JSON array")
        criteria_results = tuple(dict(item) if isinstance(item, Mapping) else item for item in raw_results)

        built = cls(
            hypothesis_id=frozen["hypothesis_id"],
            hypothesis_family_id=frozen["hypothesis_family_id"],
            bound_hypothesis_content_hash=frozen["bound_hypothesis_content_hash"],
            manifest_semantic_hash=frozen["manifest_semantic_hash"],
            manifest_artifact_hash_ref=frozen["manifest_artifact_hash_ref"],
            orchestrator_task_id=frozen["orchestrator_task_id"],
            validation_evidence_hash=frozen["validation_evidence_hash"],
            holdout_authorization_hash=frozen["holdout_authorization_hash"],
            final_holdout_claim_record_hash=frozen["final_holdout_claim_record_hash"],
            final_holdout_result_record_hash=frozen["final_holdout_result_record_hash"],
            envelope_hash=frozen["envelope_hash"],
            evaluator_id=frozen["evaluator_id"],
            evaluator_hash=frozen["evaluator_hash"],
            aggregate_metrics=dict(metrics_payload),
            final_holdout_criteria_semantic_hash=frozen["final_holdout_criteria_semantic_hash"],
            criteria_mode=frozen["criteria_mode"],
            criteria_results=criteria_results,
            verdict=frozen["verdict"],
            finalized_at=diagnostics["finalized_at"],
            schema_version=frozen["schema_version"],
        )
        try:
            claimed_hash = validate_sha256_ref(frozen["evidence_hash"])
        except ProvenanceError as exc:
            raise FinalVerdictError("final_verdict_evidence.evidence_hash is invalid") from exc
        if claimed_hash != built.evidence_hash:
            raise FinalVerdictError("final_verdict_evidence hash identity mismatch")
        return built


class FinalVerdictAcceptanceControl:
    """HOLDOUT_CONSUMED hypothesis + re-verified V2 manifest with declared
    ``final_holdout_criteria`` + re-verified ValidationEvidenceV1 +
    re-verified HoldoutTriggerAuthorizationV1 + ledger-recovered
    FINAL_HOLDOUT_CLAIM/RESULT provenance -> deterministic verdict ->
    ``ACCEPTED`` / ``REJECTED_FINAL``.

    Owns exactly one new additive SQLite table (``final_verdict_evidence``)
    in the same registry database file. ``HypothesisRegistry``'s schema and
    one-way state machine, ``FinalHoldoutRunner``, ``HoldoutTriggerBridge``,
    ``ValidationExecutionControl``, ``ExperimentManifestV2``, and
    ``FinalHoldoutCriteriaV1`` are never modified.
    """

    def __init__(
        self,
        *,
        registry: HypothesisRegistry,
        control: ControlPlane,
        artifacts: ArtifactStore,
        validator: ValidationExecutionControl,
        trigger_bridge: HoldoutTriggerBridge,
    ) -> None:
        if Path(trigger_bridge.registry.path).resolve() != Path(registry.path).resolve():
            raise ValueError(
                "trigger_bridge.registry must point at the same database file as registry"
            )
        self.registry = registry
        self.control = control
        self.artifacts = artifacts
        self.validator = validator
        self.trigger_bridge = trigger_bridge
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
                CREATE TABLE IF NOT EXISTS final_verdict_evidence (
                    hypothesis_id TEXT PRIMARY KEY,
                    hypothesis_family_id TEXT NOT NULL,
                    manifest_semantic_hash TEXT NOT NULL,
                    manifest_artifact_hash_ref TEXT NOT NULL,
                    orchestrator_task_id TEXT NOT NULL,
                    validation_evidence_hash TEXT NOT NULL,
                    holdout_authorization_hash TEXT NOT NULL,
                    evidence_hash TEXT NOT NULL,
                    evidence_artifact_hash_ref TEXT NOT NULL,
                    verdict TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(hypothesis_id) REFERENCES hypotheses(hypothesis_id)
                )
                """
            )
            columns = {
                row["name"] for row in db.execute("PRAGMA table_info(final_verdict_evidence)").fetchall()
            }
            required = {
                "hypothesis_id",
                "hypothesis_family_id",
                "manifest_semantic_hash",
                "manifest_artifact_hash_ref",
                "orchestrator_task_id",
                "validation_evidence_hash",
                "holdout_authorization_hash",
                "evidence_hash",
                "evidence_artifact_hash_ref",
                "verdict",
                "created_at",
            }
            if not required.issubset(columns):
                raise FinalVerdictError(
                    "legacy final_verdict_evidence schema detected; explicit migration is required"
                )

    def _stored_row(self, hypothesis_id: str) -> sqlite3.Row | None:
        with self._connect() as db:
            return db.execute(
                "SELECT * FROM final_verdict_evidence WHERE hypothesis_id=?",
                (hypothesis_id,),
            ).fetchone()

    def _insert_row(self, evidence: FinalVerdictEvidenceV1, *, evidence_artifact_hash_ref: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                db.execute(
                    """
                    INSERT INTO final_verdict_evidence(
                        hypothesis_id, hypothesis_family_id, manifest_semantic_hash,
                        manifest_artifact_hash_ref, orchestrator_task_id, validation_evidence_hash,
                        holdout_authorization_hash, evidence_hash, evidence_artifact_hash_ref,
                        verdict, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        evidence.hypothesis_id,
                        evidence.hypothesis_family_id,
                        evidence.manifest_semantic_hash,
                        evidence.manifest_artifact_hash_ref,
                        evidence.orchestrator_task_id,
                        evidence.validation_evidence_hash,
                        evidence.holdout_authorization_hash,
                        evidence.evidence_hash,
                        evidence_artifact_hash_ref,
                        evidence.verdict,
                        now,
                    ),
                )
            except sqlite3.IntegrityError:
                existing = db.execute(
                    "SELECT evidence_hash FROM final_verdict_evidence WHERE hypothesis_id=?",
                    (evidence.hypothesis_id,),
                ).fetchone()
                if existing is None or existing["evidence_hash"] != evidence.evidence_hash:
                    raise FinalVerdictError(
                        "conflicting final verdict evidence already recorded for this hypothesis"
                    ) from None

    def finalize(self, hypothesis_id: str) -> FinalVerdictEvidenceV1:
        """Re-verify every required precondition, recover the trusted
        holdout receipt from the closed result ledger alone, apply
        ``ExperimentManifestV2.final_holdout_criteria`` to its
        ``aggregate_metrics``, durably persist provenance-bound evidence,
        and only then advance the hypothesis to its terminal outcome.

        Idempotent for identical inputs; fails closed on conflicting ones.
        Never decrypts, evaluates, or reads holdout plaintext, never touches
        a decryption key, and never calls
        ``FinalHoldoutRunner.run_once()`` -- the holdout was already
        consumed, entirely by prior, separate, closed code, before this
        method is ever called.
        """
        try:
            record = self.registry.get(hypothesis_id)
        except KeyError as exc:
            raise FinalVerdictError(f"hypothesis does not exist: {exc}") from exc

        if record.state in (HypothesisState.ACCEPTED, HypothesisState.REJECTED_FINAL):
            return self._reload_existing_evidence(hypothesis_id, expected_state=record.state)

        if record.state is not HypothesisState.HOLDOUT_CONSUMED:
            raise FinalVerdictError(
                f"hypothesis must be HOLDOUT_CONSUMED to finalize, got {record.state.value}"
            )

        try:
            manifest = self.validator._load_verified_manifest_past_frozen(hypothesis_id)
        except ValidationExecutionError as exc:
            raise FinalVerdictError(f"bound manifest v2 could not be verified: {exc}") from exc

        if manifest.final_holdout_criteria is None:
            raise FinalVerdictError(
                "manifest declares no final_holdout_criteria; a final verdict cannot be computed"
            )

        family = self.registry.family_status(manifest.hypothesis_family_id)
        if family["terminal_state"]:
            raise FinalVerdictError(f"hypothesis family is terminal: {family['terminal_state']}")
        if not family["holdout_consumed"]:
            raise FinalVerdictError(
                "hypothesis family has not consumed final holdout; nothing to finalize"
            )

        task_id = DiscoveryOrchestratorBridge._task_id(hypothesis_id)
        if self.control.task_store.get(task_id) is None:
            raise FinalVerdictError("no matching Orchestrator task exists for this hypothesis")

        # ValidationExecutionControl.get_evidence() cannot be used here: it
        # requires the hypothesis's CURRENT state to still be
        # VALIDATION_PASSED/VALIDATION_REJECTED, which is never true once
        # HOLDOUT_CONSUMED has legitimately been reached. Its private
        # _reload_existing_evidence re-verifies the identical CAS content
        # without that state gate, taking the expected verdict explicitly.
        try:
            validation_evidence = self.validator._reload_existing_evidence(
                hypothesis_id, expected_state=HypothesisState.VALIDATION_PASSED
            )
        except ValidationExecutionError as exc:
            raise FinalVerdictError(f"validation evidence could not be verified: {exc}") from exc
        if (
            validation_evidence.hypothesis_id != manifest.hypothesis_id
            or validation_evidence.hypothesis_family_id != manifest.hypothesis_family_id
            or validation_evidence.bound_hypothesis_content_hash != manifest.bound_hypothesis_content_hash
            or validation_evidence.manifest_semantic_hash != manifest.manifest_semantic_hash
            or validation_evidence.manifest_artifact_hash_ref != record.manifest_artifact_hash_ref
            or validation_evidence.orchestrator_task_id != task_id
        ):
            raise FinalVerdictError(
                "validation evidence identity does not match this hypothesis's current binding"
            )

        try:
            authorization = self.trigger_bridge.get_authorization(hypothesis_id)
        except HoldoutTriggerError as exc:
            raise FinalVerdictError(f"holdout authorization could not be verified: {exc}") from exc
        if (
            authorization.hypothesis_id != manifest.hypothesis_id
            or authorization.hypothesis_family_id != manifest.hypothesis_family_id
            or authorization.bound_hypothesis_content_hash != manifest.bound_hypothesis_content_hash
            or authorization.manifest_semantic_hash != manifest.manifest_semantic_hash
            or authorization.manifest_artifact_hash_ref != record.manifest_artifact_hash_ref
            or authorization.orchestrator_task_id != task_id
            or authorization.validation_evidence_hash != validation_evidence.evidence_hash
        ):
            raise FinalVerdictError(
                "holdout authorization identity does not match this hypothesis's current binding"
            )

        records = _read_verified_ledger_records(self.trigger_bridge.runner.ledger)
        claim_payload, claim_record_hash, result_payload, result_record_hash = (
            _find_holdout_receipt_records(
                records,
                hypothesis_id=hypothesis_id,
                hypothesis_family_id=manifest.hypothesis_family_id,
            )
        )
        for payload, label in ((claim_payload, "claim"), (result_payload, "result")):
            if (
                payload.get("envelope_hash") != authorization.envelope_hash
                or payload.get("evaluator_id") != authorization.evaluator_id
                or payload.get("evaluator_hash") != authorization.evaluator_hash
            ):
                raise FinalVerdictError(
                    f"final holdout {label} record does not match the authorized "
                    "envelope/evaluator lineage"
                )
        if result_payload.get("holdout_consumed") is not True:
            raise FinalVerdictError("final holdout result record does not attest consumption")

        raw_metrics = result_payload.get("aggregate_metrics")
        if not isinstance(raw_metrics, Mapping):
            raise FinalVerdictError("final holdout result record has no aggregate_metrics")
        aggregate_metrics = _validated_metrics(raw_metrics)

        try:
            verify_final_holdout_metric_vocabulary(manifest.final_holdout_criteria, aggregate_metrics)
        except ManifestV2ValidationError as exc:
            raise FinalVerdictError(f"final holdout metrics failed vocabulary check: {exc}") from exc

        try:
            criteria_results, overall_passed = _evaluate_criteria(
                manifest.final_holdout_criteria, aggregate_metrics
            )
        except ValidationExecutionError as exc:
            raise FinalVerdictError(f"final holdout criteria evaluation failed: {exc}") from exc

        verdict = HypothesisState.ACCEPTED if overall_passed else HypothesisState.REJECTED_FINAL

        finalized_at = datetime.now(timezone.utc).isoformat()
        evidence = FinalVerdictEvidenceV1(
            hypothesis_id=manifest.hypothesis_id,
            hypothesis_family_id=manifest.hypothesis_family_id,
            bound_hypothesis_content_hash=manifest.bound_hypothesis_content_hash,
            manifest_semantic_hash=manifest.manifest_semantic_hash,
            manifest_artifact_hash_ref=record.manifest_artifact_hash_ref,
            orchestrator_task_id=task_id,
            validation_evidence_hash=validation_evidence.evidence_hash,
            holdout_authorization_hash=authorization.authorization_hash,
            final_holdout_claim_record_hash=claim_record_hash,
            final_holdout_result_record_hash=result_record_hash,
            envelope_hash=authorization.envelope_hash,
            evaluator_id=authorization.evaluator_id,
            evaluator_hash=authorization.evaluator_hash,
            aggregate_metrics=aggregate_metrics,
            final_holdout_criteria_semantic_hash=manifest.final_holdout_criteria.semantic_hash,
            criteria_mode=manifest.final_holdout_criteria.mode.value,
            criteria_results=tuple(criteria_results),
            verdict=verdict.value,
            finalized_at=finalized_at,
        )

        exact_bytes = evidence.canonical_bytes()
        artifact = self.artifacts.import_snapshot(
            io.BytesIO(exact_bytes),
            media_type=FINAL_VERDICT_EVIDENCE_MEDIA_TYPE,
        )
        if artifact.hash_ref != sha256_bytes(exact_bytes):
            raise FinalVerdictError("Verified CAS returned an unexpected evidence hash")

        # Evidence is durable BEFORE the terminal registry transition is
        # ever attempted -- required ordering, and what makes a crash
        # between these two steps safely retryable.
        self._insert_row(evidence, evidence_artifact_hash_ref=artifact.hash_ref)

        try:
            self.registry.transition(hypothesis_id, verdict)
        except RegistryError as exc:
            current = self.registry.get(hypothesis_id)
            if current.state in (HypothesisState.ACCEPTED, HypothesisState.REJECTED_FINAL):
                stored = self._stored_row(hypothesis_id)
                if (
                    stored is not None
                    and stored["evidence_hash"] == evidence.evidence_hash
                    and stored["verdict"] == evidence.verdict
                ):
                    # A concurrent caller already completed the transition
                    # with the SAME evidence -- benign race, not a failure.
                    return evidence
            raise FinalVerdictError(
                "registry transition failed after evidence was durably persisted; hypothesis "
                f"remains safely retryable: {exc}"
            ) from exc

        return evidence

    def _reload_existing_evidence(
        self, hypothesis_id: str, *, expected_state: HypothesisState
    ) -> FinalVerdictEvidenceV1:
        stored = self._stored_row(hypothesis_id)
        if stored is None:
            raise FinalVerdictError(
                "hypothesis has a final verdict but this control has no recorded final verdict "
                "evidence binding for it (advanced through an external or legacy path)"
            )
        try:
            exact_bytes = self.artifacts.read_verified(
                stored["evidence_artifact_hash_ref"],
                expected_media_type=FINAL_VERDICT_EVIDENCE_MEDIA_TYPE,
            )
        except ArtifactStoreError as exc:
            raise FinalVerdictError(
                f"recorded final verdict evidence artifact could not be verified: {exc}"
            ) from exc
        evidence = FinalVerdictEvidenceV1.from_payload(parse_json(exact_bytes))
        if evidence.hypothesis_id != hypothesis_id:
            raise FinalVerdictError("stored final verdict evidence hypothesis_id mismatch")
        if evidence.evidence_hash != stored["evidence_hash"]:
            raise FinalVerdictError("stored final verdict evidence hash mismatch")
        if evidence.verdict != expected_state.value:
            raise FinalVerdictError(
                "stored final verdict evidence verdict does not match the hypothesis's current state"
            )
        return evidence

    def get_evidence(self, hypothesis_id: str) -> FinalVerdictEvidenceV1:
        """Re-verify and return the durably recorded final verdict evidence
        for an already-finalized hypothesis."""
        record = self.registry.get(hypothesis_id)
        if record.state not in (HypothesisState.ACCEPTED, HypothesisState.REJECTED_FINAL):
            raise FinalVerdictError(
                f"hypothesis has no final verdict to read, state={record.state.value}"
            )
        return self._reload_existing_evidence(hypothesis_id, expected_state=record.state)


__all__ = [
    "FINAL_VERDICT_EVIDENCE_MEDIA_TYPE",
    "FINAL_VERDICT_EVIDENCE_SCHEMA_VERSION",
    "FinalVerdictAcceptanceControl",
    "FinalVerdictError",
    "FinalVerdictEvidenceV1",
]
