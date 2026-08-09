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
from typing import Any


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


@dataclass(frozen=True, slots=True)
class HypothesisRecord:
    hypothesis_id: str
    hypothesis_family_id: str
    content_hash: str
    manifest_hash: str | None
    state: HypothesisState
    created_at: str
    updated_at: str


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

    def register(
        self,
        *,
        hypothesis_id: str,
        family_definition: Mapping[str, Any],
        content_definition: Mapping[str, Any],
    ) -> HypothesisRecord:
        hypothesis_id = self._validate_nonempty(hypothesis_id, "hypothesis_id")
        family_json = _canonical(family_definition)
        content_json = _canonical(content_definition)
        family_id = derive_hypothesis_family_id(family_definition)
        content_hash = derive_content_hash(content_definition)
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
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
                    raise RegistryError(
                        f"hypothesis family is terminal: {family['terminal_state']}"
                    )
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
        return self.get(hypothesis_id)

    @staticmethod
    def _row(row: sqlite3.Row) -> HypothesisRecord:
        return HypothesisRecord(
            hypothesis_id=row["hypothesis_id"],
            hypothesis_family_id=row["family_id"],
            content_hash=row["content_hash"],
            manifest_hash=row["manifest_hash"],
            state=HypothesisState(row["state"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
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
