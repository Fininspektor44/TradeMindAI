"""Persistent binding between a frozen hypothesis and one sealed final holdout."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .hypothesis_registry import HypothesisRegistry, HypothesisState


class HoldoutSealError(RuntimeError):
    """Raised when a final-holdout seal cannot be registered safely."""


@dataclass(frozen=True, slots=True)
class HoldoutSealRecord:
    hypothesis_id: str
    hypothesis_family_id: str
    manifest_hash: str
    envelope_hash: str
    key_id: str
    evaluator_id: str
    evaluator_hash: str
    created_at: str
    isolated_at: str | None
    isolation_receipt_hash: str | None

    @property
    def isolated(self) -> bool:
        return self.isolated_at is not None and self.isolation_receipt_hash is not None


class HoldoutSealStore:
    """Store one immutable sealed-holdout entitlement per hypothesis."""

    def __init__(self, registry: HypothesisRegistry) -> None:
        self.registry = registry
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
                CREATE TABLE IF NOT EXISTS final_holdout_seals (
                    hypothesis_id TEXT PRIMARY KEY,
                    family_id TEXT NOT NULL,
                    manifest_hash TEXT NOT NULL,
                    envelope_hash TEXT NOT NULL UNIQUE,
                    key_id TEXT NOT NULL,
                    evaluator_id TEXT NOT NULL,
                    evaluator_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    isolated_at TEXT,
                    isolation_receipt_hash TEXT,
                    FOREIGN KEY(hypothesis_id) REFERENCES hypotheses(hypothesis_id),
                    FOREIGN KEY(family_id) REFERENCES hypothesis_families(family_id)
                )
                """
            )
            columns = {
                row["name"]
                for row in db.execute("PRAGMA table_info(final_holdout_seals)").fetchall()
            }
            required = {
                "hypothesis_id",
                "family_id",
                "manifest_hash",
                "envelope_hash",
                "key_id",
                "evaluator_id",
                "evaluator_hash",
                "created_at",
                "isolated_at",
                "isolation_receipt_hash",
            }
            if not required.issubset(columns):
                raise HoldoutSealError(
                    "legacy final_holdout_seals schema detected; explicit migration is required"
                )

    @staticmethod
    def _sha256(value: str, label: str) -> str:
        cleaned = value.strip().lower()
        if len(cleaned) != 64 or any(char not in "0123456789abcdef" for char in cleaned):
            raise ValueError(f"{label} must be a SHA-256 hex digest")
        return cleaned

    @staticmethod
    def _nonempty(value: str, label: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError(f"{label} must not be empty")
        return cleaned

    def register(
        self,
        *,
        hypothesis_id: str,
        envelope_hash: str,
        key_id: str,
        evaluator_id: str,
        evaluator_hash: str,
    ) -> HoldoutSealRecord:
        hypothesis_id = self._nonempty(hypothesis_id, "hypothesis_id")
        envelope_hash = self._sha256(envelope_hash, "envelope_hash")
        key_id = self._nonempty(key_id, "key_id")
        evaluator_id = self._nonempty(evaluator_id, "evaluator_id")
        evaluator_hash = self._sha256(evaluator_hash, "evaluator_hash")
        now = datetime.now(timezone.utc).isoformat()

        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM hypotheses WHERE hypothesis_id=?",
                (hypothesis_id,),
            ).fetchone()
            if row is None:
                raise KeyError(hypothesis_id)
            state = HypothesisState(row["state"])
            if state is not HypothesisState.FROZEN:
                raise HoldoutSealError(
                    f"final holdout may be sealed only while hypothesis is FROZEN, got {state.value}"
                )
            manifest_hash = row["manifest_hash"]
            if not manifest_hash:
                raise HoldoutSealError("frozen hypothesis has no manifest_hash")

            family = db.execute(
                "SELECT * FROM hypothesis_families WHERE family_id=?",
                (row["family_id"],),
            ).fetchone()
            if family is None:
                raise HoldoutSealError("hypothesis family registry row is missing")
            if int(family["holdout_consumed"]):
                raise HoldoutSealError("hypothesis family already consumed final holdout")
            if family["terminal_state"]:
                raise HoldoutSealError(f"hypothesis family is terminal: {family['terminal_state']}")

            try:
                db.execute(
                    """
                    INSERT INTO final_holdout_seals(
                        hypothesis_id, family_id, manifest_hash, envelope_hash, key_id,
                        evaluator_id, evaluator_hash, created_at, isolated_at,
                        isolation_receipt_hash
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL)
                    """,
                    (
                        hypothesis_id,
                        row["family_id"],
                        manifest_hash,
                        envelope_hash,
                        key_id,
                        evaluator_id,
                        evaluator_hash,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise HoldoutSealError(
                    "hypothesis already has a final-holdout seal or envelope was reused"
                ) from exc
        return self.get(hypothesis_id)

    def mark_isolated(
        self,
        hypothesis_id: str,
        *,
        isolation_receipt_hash: str,
    ) -> HoldoutSealRecord:
        hypothesis_id = self._nonempty(hypothesis_id, "hypothesis_id")
        receipt_hash = self._sha256(isolation_receipt_hash, "isolation_receipt_hash")
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                """
                SELECT s.*, h.state AS hypothesis_state
                FROM final_holdout_seals s
                JOIN hypotheses h ON h.hypothesis_id=s.hypothesis_id
                WHERE s.hypothesis_id=?
                """,
                (hypothesis_id,),
            ).fetchone()
            if row is None:
                raise KeyError(hypothesis_id)
            if HypothesisState(row["hypothesis_state"]) is not HypothesisState.FROZEN:
                raise HoldoutSealError(
                    "final-holdout isolation must be attested before leaving FROZEN state"
                )
            if row["isolated_at"] is not None or row["isolation_receipt_hash"] is not None:
                raise HoldoutSealError("final holdout isolation is already attested")
            cursor = db.execute(
                """
                UPDATE final_holdout_seals
                SET isolated_at=?, isolation_receipt_hash=?
                WHERE hypothesis_id=? AND isolated_at IS NULL AND isolation_receipt_hash IS NULL
                """,
                (now, receipt_hash, hypothesis_id),
            )
            if cursor.rowcount != 1:
                raise HoldoutSealError("final holdout isolation attestation changed concurrently")
        return self.get(hypothesis_id)

    @staticmethod
    def _record(row: sqlite3.Row) -> HoldoutSealRecord:
        return HoldoutSealRecord(
            hypothesis_id=row["hypothesis_id"],
            hypothesis_family_id=row["family_id"],
            manifest_hash=row["manifest_hash"],
            envelope_hash=row["envelope_hash"],
            key_id=row["key_id"],
            evaluator_id=row["evaluator_id"],
            evaluator_hash=row["evaluator_hash"],
            created_at=row["created_at"],
            isolated_at=row["isolated_at"],
            isolation_receipt_hash=row["isolation_receipt_hash"],
        )

    def get(self, hypothesis_id: str) -> HoldoutSealRecord:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM final_holdout_seals WHERE hypothesis_id=?",
                (hypothesis_id,),
            ).fetchone()
        if row is None:
            raise KeyError(hypothesis_id)
        return self._record(row)
