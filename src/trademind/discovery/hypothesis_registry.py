"""Persistent hypothesis-family registry with one-way anti-overfitting state."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from trademind.orchestrator.artifact_store import ArtifactStore

    from .manifest import ExperimentManifestV2


class RegistryError(RuntimeError):
    pass


class DuplicateHypothesis(RegistryError):
    pass


def _canonical(payload: Mapping[str, Any]) -> str:
    if not isinstance(payload, Mapping) or not payload:
        raise ValueError("hypothesis definitions must be non-empty mappings")
    try:
        return json.dumps(
            dict(payload),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("hypothesis definitions must be canonical JSON data") from exc


def derive_hypothesis_family_id(family_definition: Mapping[str, Any]) -> str:
    """Derive the family identity from immutable semantic family content.

    Tunable thresholds belong in the full content definition, not in this family
    definition. That way a threshold/window tweak cannot create a fresh final
    holdout entitlement for the same underlying hypothesis family.
    """
    digest = hashlib.sha256(_canonical(family_definition).encode("utf-8")).hexdigest()
    return f"hf_{digest}"


def derive_content_hash(content_definition: Mapping[str, Any]) -> str:
    """Fingerprint the exact registered hypothesis content."""
    return hashlib.sha256(_canonical(content_definition).encode("utf-8")).hexdigest()


class HypothesisState(StrEnum):
    PROPOSED = "PROPOSED"
    FROZEN = "FROZEN"
    TRAIN_TESTED = "TRAIN_TESTED"
    VALIDATION_PASSED = "VALIDATION_PASSED"
    VALIDATION_REJECTED = "VALIDATION_REJECTED"
    HOLDOUT_CONSUMED = "HOLDOUT_CONSUMED"
    ACCEPTED = "ACCEPTED"
    REJECTED_FINAL = "REJECTED_FINAL"


_ALLOWED = {
    HypothesisState.PROPOSED: {HypothesisState.FROZEN},
    HypothesisState.FROZEN: {HypothesisState.TRAIN_TESTED},
    HypothesisState.TRAIN_TESTED: {
        HypothesisState.VALIDATION_PASSED,
        HypothesisState.VALIDATION_REJECTED,
    },
    HypothesisState.VALIDATION_PASSED: {HypothesisState.HOLDOUT_CONSUMED},
    HypothesisState.HOLDOUT_CONSUMED: {
        HypothesisState.ACCEPTED,
        HypothesisState.REJECTED_FINAL,
    },
}

_TERMINAL_FAMILY_STATES = {
    HypothesisState.VALIDATION_REJECTED,
    HypothesisState.ACCEPTED,
    HypothesisState.REJECTED_FINAL,
}

_HOLDOUT_GUARDED_TRANSITIONS = {
    (HypothesisState.FROZEN, HypothesisState.TRAIN_TESTED),
    (HypothesisState.TRAIN_TESTED, HypothesisState.VALIDATION_PASSED),
    (HypothesisState.VALIDATION_PASSED, HypothesisState.HOLDOUT_CONSUMED),
}


@dataclass(frozen=True, slots=True)
class HypothesisRecord:
    hypothesis_id: str
    hypothesis_family_id: str
    content_hash: str
    manifest_hash: str | None
    state: HypothesisState
    created_at: str
    updated_at: str
    manifest_artifact_hash_ref: str | None = None
    manifest_schema_version: str | None = None


@dataclass(frozen=True, slots=True)
class ManifestV2FreezeResult:
    record: HypothesisRecord
    created: bool


class HypothesisRegistry:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        return db

    def _validate_transaction_connection(self, db: sqlite3.Connection) -> None:
        """Require a caller-owned transaction on this registry's database."""
        if not isinstance(db, sqlite3.Connection) or not db.in_transaction:
            raise RegistryError("hypothesis registry requires an active caller transaction")
        if db.row_factory is not sqlite3.Row:
            raise RegistryError("hypothesis registry transaction requires sqlite3.Row row_factory")
        if db.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
            raise RegistryError("hypothesis registry transaction requires foreign keys")
        rows = db.execute("PRAGMA database_list").fetchall()
        main_path = next((row[2] for row in rows if row[1] == "main"), "")
        if not main_path or Path(main_path).resolve() != self.path.resolve():
            raise RegistryError("hypothesis registry transaction uses a different database")

    def _init_schema(self) -> None:
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS hypothesis_families (
                    family_id TEXT PRIMARY KEY,
                    definition_json TEXT NOT NULL,
                    holdout_consumed INTEGER NOT NULL DEFAULT 0,
                    terminal_state TEXT,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS hypotheses (
                    hypothesis_id TEXT PRIMARY KEY,
                    family_id TEXT NOT NULL,
                    content_hash TEXT NOT NULL UNIQUE,
                    content_definition_json TEXT NOT NULL,
                    manifest_hash TEXT,
                    manifest_artifact_hash_ref TEXT,
                    manifest_schema_version TEXT,
                    state TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(family_id) REFERENCES hypothesis_families(family_id)
                );
                """
            )
            family_columns = {
                row["name"]
                for row in db.execute("PRAGMA table_info(hypothesis_families)").fetchall()
            }
            if "definition_json" not in family_columns:
                db.execute("ALTER TABLE hypothesis_families ADD COLUMN definition_json TEXT")
            hypothesis_columns = {
                row["name"] for row in db.execute("PRAGMA table_info(hypotheses)").fetchall()
            }
            if "content_definition_json" not in hypothesis_columns:
                db.execute("ALTER TABLE hypotheses ADD COLUMN content_definition_json TEXT")
            if "manifest_artifact_hash_ref" not in hypothesis_columns:
                db.execute("ALTER TABLE hypotheses ADD COLUMN manifest_artifact_hash_ref TEXT")
            if "manifest_schema_version" not in hypothesis_columns:
                db.execute("ALTER TABLE hypotheses ADD COLUMN manifest_schema_version TEXT")

    @staticmethod
    def _validate_nonempty(value: str, label: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError(f"{label} must not be empty")
        return cleaned

    @staticmethod
    def _validate_sha256(value: str, label: str) -> str:
        cleaned = value.strip().lower()
        if len(cleaned) != 64 or any(char not in "0123456789abcdef" for char in cleaned):
            raise ValueError(f"{label} must be a SHA-256 hex digest")
        return cleaned

    @staticmethod
    def _require_isolated_holdout(db: sqlite3.Connection, hypothesis_id: str) -> None:
        table = db.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type='table' AND name='final_holdout_seals'
            """
        ).fetchone()
        if table is None:
            raise RegistryError(
                "protected final-holdout seal is required before research state may advance"
            )
        seal = db.execute(
            """
            SELECT isolated_at, isolation_receipt_hash
            FROM final_holdout_seals
            WHERE hypothesis_id=?
            """,
            (hypothesis_id,),
        ).fetchone()
        if seal is None or seal["isolated_at"] is None or seal["isolation_receipt_hash"] is None:
            raise RegistryError(
                "final-holdout plaintext isolation must be attested before research state advances"
            )

    def register(
        self,
        *,
        hypothesis_id: str,
        family_definition: Mapping[str, Any],
        content_definition: Mapping[str, Any],
    ) -> HypothesisRecord:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            return self.register_in_transaction(
                db,
                hypothesis_id=hypothesis_id,
                family_definition=family_definition,
                content_definition=content_definition,
            )

    def register_in_transaction(
        self,
        db: sqlite3.Connection,
        *,
        hypothesis_id: str,
        family_definition: Mapping[str, Any],
        content_definition: Mapping[str, Any],
    ) -> HypothesisRecord:
        """Register through an existing transaction without committing it.

        This narrow adapter lets a trusted control-plane layer bind its own state,
        audit evidence, and the existing registry entry atomically. It does not
        change registry identity or lifecycle semantics.
        """
        self._validate_transaction_connection(db)
        hypothesis_id = self._validate_nonempty(hypothesis_id, "hypothesis_id")
        family_json = _canonical(family_definition)
        content_json = _canonical(content_definition)
        family_id = derive_hypothesis_family_id(family_definition)
        content_hash = derive_content_hash(content_definition)
        now = datetime.now(timezone.utc).isoformat()
        duplicate = db.execute(
            "SELECT hypothesis_id FROM hypotheses WHERE content_hash=?",
            (content_hash,),
        ).fetchone()
        if duplicate is not None:
            raise DuplicateHypothesis(f"duplicate_of={duplicate['hypothesis_id']}")

        family = db.execute(
            "SELECT * FROM hypothesis_families WHERE family_id=?",
            (family_id,),
        ).fetchone()
        if family is not None:
            stored_definition = family["definition_json"]
            if stored_definition is None:
                raise RegistryError(
                    "legacy family row has no immutable definition fingerprint; migrate explicitly"
                )
            if stored_definition != family_json:
                raise RegistryError("family identity collision or definition mismatch")
            if int(family["holdout_consumed"]):
                raise RegistryError("hypothesis family has already consumed final holdout")
            if family["terminal_state"]:
                raise RegistryError(f"hypothesis family is terminal: {family['terminal_state']}")
        else:
            db.execute(
                """
                INSERT INTO hypothesis_families(
                    family_id, definition_json, holdout_consumed, terminal_state, updated_at
                ) VALUES (?, ?, 0, NULL, ?)
                """,
                (family_id, family_json, now),
            )

        try:
            db.execute(
                """
                INSERT INTO hypotheses(
                    hypothesis_id, family_id, content_hash, content_definition_json,
                    manifest_hash, state, created_at, updated_at
                ) VALUES (?, ?, ?, ?, NULL, ?, ?, ?)
                """,
                (
                    hypothesis_id,
                    family_id,
                    content_hash,
                    content_json,
                    HypothesisState.PROPOSED.value,
                    now,
                    now,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise RegistryError(f"hypothesis already exists: {hypothesis_id}") from exc
        row = db.execute(
            "SELECT * FROM hypotheses WHERE hypothesis_id=?",
            (hypothesis_id,),
        ).fetchone()
        if row is None:
            raise RegistryError("hypothesis insert was not visible")
        return self._row(row)

    @staticmethod
    def _row(row: sqlite3.Row) -> HypothesisRecord:
        from .manifest import EXPERIMENT_MANIFEST_V2_SCHEMA_VERSION

        artifact_hash_ref = row["manifest_artifact_hash_ref"]
        manifest_schema_version = row["manifest_schema_version"]
        if (artifact_hash_ref is None) != (manifest_schema_version is None):
            raise RegistryError("persisted manifest v2 binding is incomplete")
        if artifact_hash_ref is not None:
            if (
                type(artifact_hash_ref) is not str
                or len(artifact_hash_ref) != 71
                or not artifact_hash_ref.startswith("sha256:")
                or any(character not in "0123456789abcdef" for character in artifact_hash_ref[7:])
            ):
                raise RegistryError("persisted manifest artifact hash ref is invalid")
            if manifest_schema_version != EXPERIMENT_MANIFEST_V2_SCHEMA_VERSION:
                raise RegistryError("persisted manifest schema version is unsupported")
            manifest_hash = row["manifest_hash"]
            if (
                type(manifest_hash) is not str
                or len(manifest_hash) != 64
                or any(character not in "0123456789abcdef" for character in manifest_hash)
            ):
                raise RegistryError("persisted manifest semantic hash is invalid")
        return HypothesisRecord(
            hypothesis_id=row["hypothesis_id"],
            hypothesis_family_id=row["family_id"],
            content_hash=row["content_hash"],
            manifest_hash=row["manifest_hash"],
            state=HypothesisState(row["state"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            manifest_artifact_hash_ref=artifact_hash_ref,
            manifest_schema_version=manifest_schema_version,
        )

    def get(self, hypothesis_id: str) -> HypothesisRecord:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM hypotheses WHERE hypothesis_id=?",
                (hypothesis_id,),
            ).fetchone()
        if row is None:
            raise KeyError(hypothesis_id)
        return self._row(row)

    def freeze(self, hypothesis_id: str, *, manifest_hash: str) -> HypothesisRecord:
        manifest_hash = self._validate_sha256(manifest_hash, "manifest_hash")
        return self.transition(
            hypothesis_id,
            HypothesisState.FROZEN,
            manifest_hash=manifest_hash,
        )

    def freeze_manifest_v2_in_transaction(
        self,
        db: sqlite3.Connection,
        *,
        manifest_artifact_hash_ref: str,
        artifact_store: ArtifactStore,
    ) -> ManifestV2FreezeResult:
        """Atomically bind one verified V2 CAS manifest and freeze its hypothesis.

        The caller owns and commits or rolls back ``db`` and may append audit rows
        in the same transaction. This primitive performs no audit by itself. A
        retry with the same scientific identity is inert and reports
        ``created=False`` so the future trusted creation layer can avoid duplicate
        audit. The first exact CAS artifact wins when diagnostic-only bytes differ.
        """
        from .manifest import (
            EXPERIMENT_MANIFEST_V2_SCHEMA_VERSION,
            load_experiment_manifest_v2,
        )

        self._validate_transaction_connection(db)
        manifest = load_experiment_manifest_v2(
            manifest_artifact_hash_ref,
            artifact_store=artifact_store,
        )
        row = db.execute(
            "SELECT * FROM hypotheses WHERE hypothesis_id=?",
            (manifest.hypothesis_id,),
        ).fetchone()
        if row is None:
            raise KeyError(manifest.hypothesis_id)
        if row["family_id"] != manifest.hypothesis_family_id:
            raise RegistryError("manifest v2 family identity does not match registry")
        if row["content_hash"] != manifest.bound_hypothesis_content_hash:
            raise RegistryError("manifest v2 content identity does not match registry")

        semantic_digest = manifest.manifest_semantic_hash.removeprefix("sha256:")
        current = HypothesisState(row["state"])
        if current is HypothesisState.FROZEN:
            existing = self._row(row)
            if (
                existing.manifest_hash != semantic_digest
                or existing.manifest_schema_version != EXPERIMENT_MANIFEST_V2_SCHEMA_VERSION
                or existing.manifest_artifact_hash_ref is None
            ):
                raise RegistryError("hypothesis is frozen to a conflicting manifest")
            existing_manifest = load_experiment_manifest_v2(
                existing.manifest_artifact_hash_ref,
                artifact_store=artifact_store,
            )
            if (
                existing_manifest.manifest_semantic_hash != manifest.manifest_semantic_hash
                or existing_manifest.hypothesis_id != manifest.hypothesis_id
                or existing_manifest.hypothesis_family_id != manifest.hypothesis_family_id
                or existing_manifest.bound_hypothesis_content_hash
                != manifest.bound_hypothesis_content_hash
            ):
                raise RegistryError("persisted manifest v2 binding is inconsistent")
            return ManifestV2FreezeResult(record=existing, created=False)
        if current is not HypothesisState.PROPOSED:
            raise RegistryError(f"illegal transition {current.value} -> FROZEN")
        if (
            row["manifest_hash"] is not None
            or row["manifest_artifact_hash_ref"] is not None
            or row["manifest_schema_version"] is not None
        ):
            raise RegistryError("PROPOSED hypothesis contains a stale manifest binding")

        family = db.execute(
            "SELECT * FROM hypothesis_families WHERE family_id=?",
            (row["family_id"],),
        ).fetchone()
        if family is None:
            raise RegistryError("family registry row is missing")
        if int(family["holdout_consumed"]):
            raise RegistryError("hypothesis family has already consumed final holdout")
        if family["terminal_state"]:
            raise RegistryError(f"hypothesis family is terminal: {family['terminal_state']}")

        now = datetime.now(timezone.utc).isoformat()
        cursor = db.execute(
            """
            UPDATE hypotheses
            SET state=?, manifest_hash=?, manifest_artifact_hash_ref=?,
                manifest_schema_version=?, updated_at=?
            WHERE hypothesis_id=? AND state=? AND manifest_hash IS NULL
                AND manifest_artifact_hash_ref IS NULL AND manifest_schema_version IS NULL
            """,
            (
                HypothesisState.FROZEN.value,
                semantic_digest,
                manifest_artifact_hash_ref,
                EXPERIMENT_MANIFEST_V2_SCHEMA_VERSION,
                now,
                manifest.hypothesis_id,
                HypothesisState.PROPOSED.value,
            ),
        )
        if cursor.rowcount != 1:
            raise RegistryError("hypothesis manifest binding changed concurrently")
        family_cursor = db.execute(
            "UPDATE hypothesis_families SET updated_at=? WHERE family_id=?",
            (now, row["family_id"]),
        )
        if family_cursor.rowcount != 1:
            raise RegistryError("hypothesis family disappeared during manifest binding")
        updated = db.execute(
            "SELECT * FROM hypotheses WHERE hypothesis_id=?",
            (manifest.hypothesis_id,),
        ).fetchone()
        if updated is None:
            raise RegistryError("frozen hypothesis disappeared")
        return ManifestV2FreezeResult(record=self._row(updated), created=True)

    def load_bound_manifest_v2(
        self,
        hypothesis_id: str,
        *,
        artifact_store: ArtifactStore,
    ) -> ExperimentManifestV2:
        """Load and reverify the exact V2 CAS object bound to a frozen hypothesis."""
        from .manifest import (
            EXPERIMENT_MANIFEST_V2_SCHEMA_VERSION,
            load_experiment_manifest_v2,
        )

        record = self.get(hypothesis_id)
        if record.state is not HypothesisState.FROZEN:
            raise RegistryError("hypothesis must be FROZEN before loading manifest v2")
        if (
            record.manifest_hash is None
            or record.manifest_artifact_hash_ref is None
            or record.manifest_schema_version != EXPERIMENT_MANIFEST_V2_SCHEMA_VERSION
        ):
            raise RegistryError("hypothesis has no complete manifest v2 binding")
        manifest = load_experiment_manifest_v2(
            record.manifest_artifact_hash_ref,
            artifact_store=artifact_store,
        )
        if (
            manifest.hypothesis_id != record.hypothesis_id
            or manifest.hypothesis_family_id != record.hypothesis_family_id
            or manifest.bound_hypothesis_content_hash != record.content_hash
            or manifest.manifest_semantic_hash.removeprefix("sha256:") != record.manifest_hash
        ):
            raise RegistryError("bound manifest v2 does not match registry identities")
        return manifest

    def transition(
        self,
        hypothesis_id: str,
        target: HypothesisState,
        *,
        manifest_hash: str | None = None,
    ) -> HypothesisRecord:
        if manifest_hash is not None:
            manifest_hash = self._validate_sha256(manifest_hash, "manifest_hash")
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM hypotheses WHERE hypothesis_id=?",
                (hypothesis_id,),
            ).fetchone()
            if row is None:
                raise KeyError(hypothesis_id)
            current = HypothesisState(row["state"])
            if target not in _ALLOWED.get(current, set()):
                raise RegistryError(f"illegal transition {current.value} -> {target.value}")
            if current is HypothesisState.PROPOSED and target is HypothesisState.FROZEN:
                if not manifest_hash:
                    raise RegistryError("freezing requires manifest_hash")
            elif manifest_hash is not None and manifest_hash != row["manifest_hash"]:
                raise RegistryError("manifest_hash is immutable after freeze")

            if (current, target) in _HOLDOUT_GUARDED_TRANSITIONS:
                self._require_isolated_holdout(db, hypothesis_id)

            family = db.execute(
                "SELECT * FROM hypothesis_families WHERE family_id=?",
                (row["family_id"],),
            ).fetchone()
            if family is None:
                raise RegistryError("family registry row is missing")
            if int(family["holdout_consumed"]) and target is HypothesisState.HOLDOUT_CONSUMED:
                raise RegistryError("final holdout already consumed for this family")
            if family["terminal_state"]:
                raise RegistryError(f"family is terminal: {family['terminal_state']}")

            stored_manifest = manifest_hash if manifest_hash is not None else row["manifest_hash"]
            cursor = db.execute(
                """
                UPDATE hypotheses SET state=?, manifest_hash=?, updated_at=?
                WHERE hypothesis_id=? AND state=?
                """,
                (target.value, stored_manifest, now, hypothesis_id, current.value),
            )
            if cursor.rowcount != 1:
                raise RegistryError("hypothesis state changed concurrently")

            terminal_state = None
            holdout_consumed = int(family["holdout_consumed"])
            if target is HypothesisState.HOLDOUT_CONSUMED:
                holdout_consumed = 1
            if target in _TERMINAL_FAMILY_STATES:
                terminal_state = target.value
            db.execute(
                """
                UPDATE hypothesis_families
                SET holdout_consumed=?, terminal_state=COALESCE(?, terminal_state), updated_at=?
                WHERE family_id=?
                """,
                (holdout_consumed, terminal_state, now, row["family_id"]),
            )
        return self.get(hypothesis_id)

    def family_status(self, family_id: str) -> dict[str, object]:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM hypothesis_families WHERE family_id=?",
                (family_id,),
            ).fetchone()
        if row is None:
            raise KeyError(family_id)
        return {
            "family_id": row["family_id"],
            "holdout_consumed": bool(row["holdout_consumed"]),
            "terminal_state": row["terminal_state"],
            "updated_at": row["updated_at"],
        }
