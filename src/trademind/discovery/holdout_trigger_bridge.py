"""Holdout Trigger Bridge V1: the third missing bridge, from a
VALIDATION_PASSED hypothesis to a trusted, authorized invocation of the
already-closed ``FinalHoldoutRunner``.

This module NEVER decrypts, evaluates, or reads final-holdout plaintext, and
NEVER touches decryption key material. Its entire job is authorization:
re-verify every precondition a trusted holdout consumption requires, persist
an immutable, plaintext-free authorization record proving those checks were
made, and then make exactly one call into the CLOSED, already-audited
``FinalHoldoutRunner.run_once()`` -- which owns, unmodified, every piece of
actually sensitive logic: envelope/key verification, in-memory decryption,
the evaluator side-effect guard, the ledger claim, the ``HOLDOUT_CONSUMED``
registry transition, and all of its own crash-recovery handling.

Built strictly additively on top of already-closed contracts, none of which
are modified:

  * ``FinalHoldoutRunner`` (discovery/holdout_runner.py) is invoked, never
    reimplemented. This module does not import ``holdout_crypto`` or
    ``holdout_keys`` at all -- it has no way to decrypt anything or to see a
    decryption key, by construction, not by discipline alone. A fully
    constructed ``FinalHoldoutRunner`` (with its key provider, evaluator,
    and ledger already wired by trusted setup code elsewhere -- outside
    this module's responsibility, exactly as that module's own docstring
    describes: "intended to run in a separate trusted process") is injected
    into this bridge's constructor, the same dependency-injection pattern
    ``DiscoveryOrchestratorBridge`` already uses for ``HoldoutSealStore``.
    Its private ``_ledger_has_claim`` helper is reused directly (a bound
    method on an object this module already holds a trusted reference to)
    rather than re-scanning the ledger with new code -- "do not duplicate
    FinalHoldoutRunner logic" applies to that scan exactly as much as to
    decryption.

  * ``ValidationExecutionControl.get_evidence`` and its private
    ``_load_verified_manifest_past_frozen`` (discovery/validation_execution.py)
    are reused directly, not duplicated, to load and re-verify the prior
    ``ValidationEvidenceV1`` and the V2 manifest binding for a hypothesis
    whose state is now past FROZEN (and past TRAIN_TESTED) -- the identical
    reasoning that module's own docstring already gives for why
    ``HypothesisRegistry.load_bound_manifest_v2`` cannot be called directly
    at this later stage.

  * ``HoldoutSealStore`` and ``HypothesisRegistry`` (discovery/holdout_store.py,
    discovery/hypothesis_registry.py) are read through the SAME instances
    the injected ``FinalHoldoutRunner`` itself already holds
    (``runner.seals``, ``runner.registry``) rather than through independently
    constructed ones, so this bridge's precondition checks and the runner's
    own internal ``_preflight`` checks can never disagree about which
    database they are reading.

``ExperimentManifestV2``, ``HypothesisRegistry``'s state machine, and
``ResultLedger`` are read-only from this module's perspective and are never
modified.

Scope boundary: this module stops at ``HOLDOUT_CONSUMED``. It never
transitions to, or even references, either later terminal-acceptance
outcome state -- that decision is explicitly the next and final
research-layer task.
"""

from __future__ import annotations

import io
import re
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

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

from .holdout_runner import FinalHoldoutRunner, HoldoutRunError, HoldoutRunReceipt
from .holdout_store import HoldoutSealStore
from .hypothesis_registry import HypothesisRegistry, HypothesisState
from .orchestrator_bridge import DiscoveryOrchestratorBridge
from .validation_execution import ValidationExecutionControl, ValidationExecutionError

if TYPE_CHECKING:
    from .manifest import ExperimentManifestV2


class HoldoutTriggerError(RuntimeError):
    """Raised when a bounded holdout-trigger authorization cannot complete
    safely, or when the trusted runner itself refuses the run."""


HOLDOUT_TRIGGER_AUTHORIZATION_SCHEMA_VERSION = "discovery-holdout-trigger-authorization-v1"
HOLDOUT_TRIGGER_AUTHORIZATION_MEDIA_TYPE = (
    "application/vnd.trademind.discovery.holdout-trigger-authorization-v1+json"
)
_AUTHORIZATION_HASH_DOMAIN = b"trademind:discovery:holdout-trigger-authorization:v1"

_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
_TASK_ID_SHAPE = re.compile(r"^discovery-[0-9a-f]{20}$")


def _bare_sha256(value: object, *, field_name: str) -> str:
    if type(value) is not str or _SHA256_HEX.fullmatch(value) is None:
        raise HoldoutTriggerError(f"{field_name} must be 64 lowercase hexadecimal digits")
    return value


def _nonempty_str(value: object, *, field_name: str) -> str:
    if type(value) is not str or not value.strip():
        raise HoldoutTriggerError(f"{field_name} must be a non-empty string")
    return value


@dataclass(frozen=True, slots=True)
class HoldoutTriggerAuthorizationV1:
    """Immutable, plaintext-free record proving every required precondition
    was re-verified before one specific trusted holdout consumption attempt.
    Construct only via :meth:`HoldoutTriggerBridge.trigger` -- never by
    hand. Carries no plaintext, no decryption key, and no raw dataset path;
    only content hashes and stable identifiers."""

    hypothesis_id: str
    hypothesis_family_id: str
    bound_hypothesis_content_hash: str
    manifest_semantic_hash: str
    manifest_artifact_hash_ref: str
    orchestrator_task_id: str
    validation_evidence_hash: str
    envelope_hash: str
    key_id: str
    evaluator_id: str
    evaluator_hash: str
    authorized_at: str
    schema_version: str = HOLDOUT_TRIGGER_AUTHORIZATION_SCHEMA_VERSION
    authorization_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != HOLDOUT_TRIGGER_AUTHORIZATION_SCHEMA_VERSION:
            raise HoldoutTriggerError("unsupported holdout trigger authorization schema_version")
        _nonempty_str(self.hypothesis_id, field_name="hypothesis_id")
        _nonempty_str(self.hypothesis_family_id, field_name="hypothesis_family_id")
        _bare_sha256(self.bound_hypothesis_content_hash, field_name="bound_hypothesis_content_hash")
        for value, field_name in (
            (self.manifest_semantic_hash, "manifest_semantic_hash"),
            (self.manifest_artifact_hash_ref, "manifest_artifact_hash_ref"),
            (self.validation_evidence_hash, "validation_evidence_hash"),
        ):
            try:
                validate_sha256_ref(value)
            except ProvenanceError as exc:
                raise HoldoutTriggerError(f"{field_name} is invalid") from exc
        if (
            type(self.orchestrator_task_id) is not str
            or _TASK_ID_SHAPE.fullmatch(self.orchestrator_task_id) is None
        ):
            raise HoldoutTriggerError("orchestrator_task_id is invalid")
        _bare_sha256(self.envelope_hash, field_name="envelope_hash")
        _nonempty_str(self.key_id, field_name="key_id")
        _nonempty_str(self.evaluator_id, field_name="evaluator_id")
        _bare_sha256(self.evaluator_hash, field_name="evaluator_hash")
        if type(self.authorized_at) is not str:
            raise HoldoutTriggerError("authorized_at must be a string")
        try:
            parsed = datetime.fromisoformat(self.authorized_at)
        except ValueError as exc:
            raise HoldoutTriggerError("authorized_at must be an ISO timestamp") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise HoldoutTriggerError("authorized_at must be timezone-aware")

        object.__setattr__(
            self,
            "authorization_hash",
            sha256_bytes(
                _AUTHORIZATION_HASH_DOMAIN + b"\x00" + canonical_json_bytes(self.semantic_projection())
            ),
        )
        canonical_json_bytes(self.to_payload())

    def semantic_projection(self) -> dict[str, object]:
        """Everything that determines this authorization's identity --
        excludes ``authorized_at`` (wall-clock, non-deterministic) so
        identical inputs always produce an identical ``authorization_hash``."""
        return {
            "schema_version": self.schema_version,
            "hypothesis_id": self.hypothesis_id,
            "hypothesis_family_id": self.hypothesis_family_id,
            "bound_hypothesis_content_hash": self.bound_hypothesis_content_hash,
            "manifest_semantic_hash": self.manifest_semantic_hash,
            "manifest_artifact_hash_ref": self.manifest_artifact_hash_ref,
            "orchestrator_task_id": self.orchestrator_task_id,
            "validation_evidence_hash": self.validation_evidence_hash,
            "envelope_hash": self.envelope_hash,
            "key_id": self.key_id,
            "evaluator_id": self.evaluator_id,
            "evaluator_hash": self.evaluator_hash,
        }

    def to_payload(self) -> dict[str, object]:
        payload = self.semantic_projection()
        payload["authorization_hash"] = self.authorization_hash
        payload["diagnostics"] = {"authorized_at": self.authorized_at}
        return payload

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_payload())

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> HoldoutTriggerAuthorizationV1:
        try:
            frozen = freeze_json_object(payload, field_name="holdout_trigger_authorization")
        except ProvenanceError as exc:
            raise HoldoutTriggerError(
                "holdout_trigger_authorization must be strict bounded JSON"
            ) from exc
        required = frozenset(
            {
                "schema_version",
                "authorization_hash",
                "hypothesis_id",
                "hypothesis_family_id",
                "bound_hypothesis_content_hash",
                "manifest_semantic_hash",
                "manifest_artifact_hash_ref",
                "orchestrator_task_id",
                "validation_evidence_hash",
                "envelope_hash",
                "key_id",
                "evaluator_id",
                "evaluator_hash",
                "diagnostics",
            }
        )
        fields = frozenset(frozen)
        missing = required - fields
        unknown = fields - required
        if missing:
            raise HoldoutTriggerError(
                f"holdout_trigger_authorization is missing fields: {', '.join(sorted(missing))}"
            )
        if unknown:
            raise HoldoutTriggerError(
                f"holdout_trigger_authorization contains unknown fields: {', '.join(sorted(unknown))}"
            )
        diagnostics = frozen["diagnostics"]
        if not isinstance(diagnostics, Mapping) or frozenset(diagnostics) != {"authorized_at"}:
            raise HoldoutTriggerError("holdout_trigger_authorization.diagnostics is invalid")
        built = cls(
            hypothesis_id=frozen["hypothesis_id"],
            hypothesis_family_id=frozen["hypothesis_family_id"],
            bound_hypothesis_content_hash=frozen["bound_hypothesis_content_hash"],
            manifest_semantic_hash=frozen["manifest_semantic_hash"],
            manifest_artifact_hash_ref=frozen["manifest_artifact_hash_ref"],
            orchestrator_task_id=frozen["orchestrator_task_id"],
            validation_evidence_hash=frozen["validation_evidence_hash"],
            envelope_hash=frozen["envelope_hash"],
            key_id=frozen["key_id"],
            evaluator_id=frozen["evaluator_id"],
            evaluator_hash=frozen["evaluator_hash"],
            authorized_at=diagnostics["authorized_at"],
            schema_version=frozen["schema_version"],
        )
        try:
            claimed_hash = validate_sha256_ref(frozen["authorization_hash"])
        except ProvenanceError as exc:
            raise HoldoutTriggerError("holdout_trigger_authorization.authorization_hash is invalid") from exc
        if claimed_hash != built.authorization_hash:
            raise HoldoutTriggerError("holdout_trigger_authorization hash identity mismatch")
        return built


class HoldoutTriggerBridge:
    """VALIDATION_PASSED hypothesis + re-verified V2 manifest + re-verified
    ValidationEvidenceV1 + re-verified isolated holdout seal -> immutable,
    plaintext-free authorization -> exactly one trusted call into
    ``FinalHoldoutRunner.run_once()`` -> ``HOLDOUT_CONSUMED``.

    Owns exactly one new additive SQLite table
    (``holdout_trigger_authorizations``) in the same registry database file.
    ``HypothesisRegistry``'s schema and one-way state machine,
    ``FinalHoldoutRunner``, ``HoldoutSealStore``, and
    ``ValidationExecutionControl`` are never modified.
    """

    def __init__(
        self,
        *,
        registry: HypothesisRegistry,
        control: ControlPlane,
        artifacts: ArtifactStore,
        validator: ValidationExecutionControl,
        runner: FinalHoldoutRunner,
    ) -> None:
        if type(runner) is not FinalHoldoutRunner:
            raise TypeError("runner must be an exact FinalHoldoutRunner")
        if type(runner.seals) is not HoldoutSealStore:
            raise TypeError("runner.seals must be an exact HoldoutSealStore")
        if Path(runner.registry.path).resolve() != Path(registry.path).resolve():
            raise ValueError(
                "runner.registry must point at the same database file as registry"
            )
        self.registry = registry
        self.control = control
        self.artifacts = artifacts
        self.validator = validator
        self.runner = runner
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
                CREATE TABLE IF NOT EXISTS holdout_trigger_authorizations (
                    hypothesis_id TEXT PRIMARY KEY,
                    hypothesis_family_id TEXT NOT NULL,
                    manifest_semantic_hash TEXT NOT NULL,
                    manifest_artifact_hash_ref TEXT NOT NULL,
                    orchestrator_task_id TEXT NOT NULL,
                    validation_evidence_hash TEXT NOT NULL,
                    authorization_hash TEXT NOT NULL,
                    authorization_artifact_hash_ref TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(hypothesis_id) REFERENCES hypotheses(hypothesis_id)
                )
                """
            )
            columns = {
                row["name"]
                for row in db.execute("PRAGMA table_info(holdout_trigger_authorizations)").fetchall()
            }
            required = {
                "hypothesis_id",
                "hypothesis_family_id",
                "manifest_semantic_hash",
                "manifest_artifact_hash_ref",
                "orchestrator_task_id",
                "validation_evidence_hash",
                "authorization_hash",
                "authorization_artifact_hash_ref",
                "created_at",
            }
            if not required.issubset(columns):
                raise HoldoutTriggerError(
                    "legacy holdout_trigger_authorizations schema detected; explicit "
                    "migration is required"
                )

    def _stored_row(self, hypothesis_id: str) -> sqlite3.Row | None:
        with self._connect() as db:
            return db.execute(
                "SELECT * FROM holdout_trigger_authorizations WHERE hypothesis_id=?",
                (hypothesis_id,),
            ).fetchone()

    def get_authorization(self, hypothesis_id: str) -> HoldoutTriggerAuthorizationV1:
        """Re-verify and return the durably recorded authorization for a
        hypothesis this bridge has already attempted to trigger, if any."""
        stored = self._stored_row(hypothesis_id)
        if stored is None:
            raise HoldoutTriggerError(
                "no recorded holdout-trigger authorization exists for this hypothesis"
            )
        try:
            exact_bytes = self.artifacts.read_verified(
                stored["authorization_artifact_hash_ref"],
                expected_media_type=HOLDOUT_TRIGGER_AUTHORIZATION_MEDIA_TYPE,
            )
        except ArtifactStoreError as exc:
            raise HoldoutTriggerError(
                f"recorded holdout-trigger authorization artifact could not be verified: {exc}"
            ) from exc
        authorization = HoldoutTriggerAuthorizationV1.from_payload(parse_json(exact_bytes))
        if authorization.hypothesis_id != hypothesis_id:
            raise HoldoutTriggerError("stored holdout-trigger authorization hypothesis_id mismatch")
        if authorization.authorization_hash != stored["authorization_hash"]:
            raise HoldoutTriggerError("stored holdout-trigger authorization hash mismatch")
        return authorization

    def _insert_row(
        self, authorization: HoldoutTriggerAuthorizationV1, *, authorization_artifact_hash_ref: str
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                db.execute(
                    """
                    INSERT INTO holdout_trigger_authorizations(
                        hypothesis_id, hypothesis_family_id, manifest_semantic_hash,
                        manifest_artifact_hash_ref, orchestrator_task_id, validation_evidence_hash,
                        authorization_hash, authorization_artifact_hash_ref, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        authorization.hypothesis_id,
                        authorization.hypothesis_family_id,
                        authorization.manifest_semantic_hash,
                        authorization.manifest_artifact_hash_ref,
                        authorization.orchestrator_task_id,
                        authorization.validation_evidence_hash,
                        authorization.authorization_hash,
                        authorization_artifact_hash_ref,
                        now,
                    ),
                )
            except sqlite3.IntegrityError:
                existing = db.execute(
                    "SELECT authorization_hash FROM holdout_trigger_authorizations WHERE hypothesis_id=?",
                    (authorization.hypothesis_id,),
                ).fetchone()
                if existing is None or existing["authorization_hash"] != authorization.authorization_hash:
                    raise HoldoutTriggerError(
                        "conflicting holdout-trigger authorization already recorded for this hypothesis"
                    ) from None

    def trigger(
        self,
        hypothesis_id: str,
        *,
        sealed_path: str | Path,
    ) -> HoldoutRunReceipt:
        """Re-verify every required precondition, durably persist a
        plaintext-free authorization record, and then make exactly one call
        into the trusted, unmodified ``FinalHoldoutRunner.run_once()``.

        ``sealed_path`` is passed straight through, unread and
        uninterpreted, to the trusted runner -- this bridge never opens it.
        The returned ``HoldoutRunReceipt`` carries only bounded aggregate
        metrics, exactly what ``FinalHoldoutRunner`` itself already
        considers safe to return to its caller.

        Idempotent for a retry that has not yet reached consumption
        (identical authorization inputs recompute the identical hash and
        are not re-inserted or re-authorized); fails closed, without a
        second consumption attempt, for a retry after the hypothesis has
        already reached ``HOLDOUT_CONSUMED`` -- ``FinalHoldoutRunner`` is a
        one-shot contract with no persisted receipt to recover, so this
        bridge does not pretend one exists.
        """
        try:
            record = self.registry.get(hypothesis_id)
        except KeyError as exc:
            raise HoldoutTriggerError(f"hypothesis does not exist: {exc}") from exc

        if record.state is HypothesisState.HOLDOUT_CONSUMED:
            raise HoldoutTriggerError(
                "final holdout has already been consumed for this hypothesis; "
                "FinalHoldoutRunner is a one-shot contract with no receipt to recover -- "
                "retry after consumption fails closed rather than attempting a second run"
            )
        if record.state is not HypothesisState.VALIDATION_PASSED:
            raise HoldoutTriggerError(
                f"hypothesis must be VALIDATION_PASSED to trigger final holdout, "
                f"got {record.state.value}"
            )

        try:
            manifest: ExperimentManifestV2 = self.validator._load_verified_manifest_past_frozen(
                hypothesis_id
            )
        except ValidationExecutionError as exc:
            raise HoldoutTriggerError(f"bound manifest v2 could not be verified: {exc}") from exc

        family = self.registry.family_status(manifest.hypothesis_family_id)
        if family["holdout_consumed"]:
            raise HoldoutTriggerError("final holdout has already been consumed for this family")
        if family["terminal_state"]:
            raise HoldoutTriggerError(f"hypothesis family is terminal: {family['terminal_state']}")

        task_id = DiscoveryOrchestratorBridge._task_id(hypothesis_id)
        if self.control.task_store.get(task_id) is None:
            raise HoldoutTriggerError("no matching Orchestrator task exists for this hypothesis")

        try:
            validation_evidence = self.validator.get_evidence(hypothesis_id)
        except ValidationExecutionError as exc:
            raise HoldoutTriggerError(f"validation evidence could not be verified: {exc}") from exc

        if validation_evidence.verdict != HypothesisState.VALIDATION_PASSED.value:
            raise HoldoutTriggerError("validation evidence verdict is not VALIDATION_PASSED")
        if (
            validation_evidence.hypothesis_id != manifest.hypothesis_id
            or validation_evidence.hypothesis_family_id != manifest.hypothesis_family_id
            or validation_evidence.bound_hypothesis_content_hash != manifest.bound_hypothesis_content_hash
            or validation_evidence.manifest_semantic_hash != manifest.manifest_semantic_hash
            or validation_evidence.manifest_artifact_hash_ref != record.manifest_artifact_hash_ref
            or validation_evidence.orchestrator_task_id != task_id
        ):
            raise HoldoutTriggerError(
                "validation evidence identity does not match this hypothesis's current binding"
            )

        try:
            seal = self.runner.seals.get(hypothesis_id)
        except KeyError as exc:
            raise HoldoutTriggerError(
                "no registered final-holdout seal for this hypothesis"
            ) from exc
        if not seal.isolated:
            raise HoldoutTriggerError(
                "final-holdout plaintext isolation must be attested before trigger"
            )
        if seal.hypothesis_family_id != manifest.hypothesis_family_id:
            raise HoldoutTriggerError("sealed holdout family does not match hypothesis")
        if seal.manifest_hash != record.manifest_hash:
            raise HoldoutTriggerError("sealed holdout manifest does not match hypothesis")
        if seal.evaluator_id != self.runner.evaluator.evaluator_id:
            raise HoldoutTriggerError(
                "sealed holdout evaluator_id does not match the configured trusted runner"
            )
        if seal.evaluator_hash != self.runner.evaluator_hash:
            raise HoldoutTriggerError(
                "sealed holdout evaluator_hash does not match the configured trusted runner"
            )

        # Reuses FinalHoldoutRunner's own private ledger-claim scan rather
        # than re-implementing it -- the exact same check run_once() itself
        # performs inside _preflight(), re-verified here BEFORE the
        # authorization record is written, not merely relied upon later.
        if self.runner._ledger_has_claim(manifest.hypothesis_family_id):
            raise HoldoutTriggerError(
                "result ledger already contains a final-holdout claim for this family"
            )

        authorized_at = datetime.now(timezone.utc).isoformat()
        authorization = HoldoutTriggerAuthorizationV1(
            hypothesis_id=manifest.hypothesis_id,
            hypothesis_family_id=manifest.hypothesis_family_id,
            bound_hypothesis_content_hash=manifest.bound_hypothesis_content_hash,
            manifest_semantic_hash=manifest.manifest_semantic_hash,
            manifest_artifact_hash_ref=record.manifest_artifact_hash_ref,
            orchestrator_task_id=task_id,
            validation_evidence_hash=validation_evidence.evidence_hash,
            envelope_hash=seal.envelope_hash,
            key_id=seal.key_id,
            evaluator_id=seal.evaluator_id,
            evaluator_hash=seal.evaluator_hash,
            authorized_at=authorized_at,
        )

        exact_bytes = authorization.canonical_bytes()
        artifact = self.artifacts.import_snapshot(
            io.BytesIO(exact_bytes),
            media_type=HOLDOUT_TRIGGER_AUTHORIZATION_MEDIA_TYPE,
        )
        if artifact.hash_ref != sha256_bytes(exact_bytes):
            raise HoldoutTriggerError("Verified CAS returned an unexpected authorization hash")

        # Authorization is durable BEFORE the trusted runner is ever
        # invoked -- required ordering, and what makes a crash before this
        # point (or a lock-contention failure inside run_once() itself)
        # safely retryable without re-authorizing or double-consuming.
        self._insert_row(authorization, authorization_artifact_hash_ref=artifact.hash_ref)

        try:
            receipt = self.runner.run_once(hypothesis_id=hypothesis_id, sealed_path=sealed_path)
        except HoldoutRunError as exc:
            raise HoldoutTriggerError(
                f"trusted final-holdout runner refused or failed the run: {exc}"
            ) from exc
        return receipt


__all__ = [
    "HOLDOUT_TRIGGER_AUTHORIZATION_MEDIA_TYPE",
    "HOLDOUT_TRIGGER_AUTHORIZATION_SCHEMA_VERSION",
    "HoldoutTriggerAuthorizationV1",
    "HoldoutTriggerBridge",
    "HoldoutTriggerError",
]
