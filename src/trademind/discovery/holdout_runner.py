"""One-shot final-holdout runner with bounded aggregate output.

This module is intended to run in a separate trusted process. The evaluator is
preconfigured there and is bound by immutable evaluator id/hash recorded when the
holdout was sealed. Research agents never receive plaintext rows or the key.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Protocol

from .holdout_crypto import HoldoutCryptoError, decrypt_bytes, verify_envelope, verify_key
from .holdout_keys import HoldoutKeyProvider
from .holdout_store import HoldoutSealStore
from .hypothesis_registry import HypothesisRegistry, HypothesisState, RegistryError
from .result_ledger import ResultLedger


class HoldoutRunError(RuntimeError):
    """Raised when a protected final-holdout run cannot complete safely."""


class HoldoutEvaluator(Protocol):
    """Trusted evaluator contract installed inside the isolated runner."""

    @property
    def evaluator_id(self) -> str: ...

    @property
    def evaluator_hash(self) -> str: ...

    def evaluate(self, plaintext: bytes) -> Mapping[str, int | float | bool | None]: ...


@dataclass(frozen=True, slots=True)
class HoldoutRunReceipt:
    hypothesis_id: str
    hypothesis_family_id: str
    envelope_hash: str
    evaluator_id: str
    evaluator_hash: str
    aggregate_metrics: dict[str, int | float | bool | None]
    ledger_record_hash: str


_METRIC_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


def _sha256(value: str, label: str) -> str:
    cleaned = value.strip().lower()
    if len(cleaned) != 64 or any(char not in "0123456789abcdef" for char in cleaned):
        raise HoldoutRunError(f"{label} must be a SHA-256 hex digest")
    return cleaned


def _validated_metrics(
    values: Mapping[str, int | float | bool | None],
) -> dict[str, int | float | bool | None]:
    if not isinstance(values, Mapping) or not values:
        raise HoldoutRunError("holdout evaluator must return non-empty aggregate metrics")
    if len(values) > 64:
        raise HoldoutRunError("holdout evaluator returned too many aggregate metrics")
    cleaned: dict[str, int | float | bool | None] = {}
    for key, value in values.items():
        if not isinstance(key, str) or not _METRIC_NAME.fullmatch(key):
            raise HoldoutRunError("holdout metric name is invalid")
        if isinstance(value, bool) or value is None or isinstance(value, int):
            cleaned[key] = value
            continue
        if isinstance(value, float):
            if not math.isfinite(value):
                raise HoldoutRunError("holdout aggregate metrics must be finite")
            cleaned[key] = value
            continue
        raise HoldoutRunError("holdout evaluator may return only scalar numeric/bool metrics")
    return cleaned


class FinalHoldoutRunner:
    def __init__(
        self,
        *,
        registry: HypothesisRegistry,
        seals: HoldoutSealStore,
        keys: HoldoutKeyProvider,
        ledger: ResultLedger,
        evaluator: HoldoutEvaluator,
    ) -> None:
        if not evaluator.evaluator_id.strip():
            raise ValueError("evaluator_id must not be empty")
        _sha256(evaluator.evaluator_hash, "evaluator_hash")
        self.registry = registry
        self.seals = seals
        self.keys = keys
        self.ledger = ledger
        self.evaluator = evaluator

    @staticmethod
    def _load_envelope(path: str | Path) -> dict:
        source = Path(path)
        try:
            document = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HoldoutRunError("cannot read protected final-holdout envelope") from exc
        if not isinstance(document, dict):
            raise HoldoutRunError("protected final-holdout envelope is not a JSON object")
        return document

    def _preflight(self, hypothesis_id: str, document: dict) -> tuple[bytes, str, str]:
        record = self.registry.get(hypothesis_id)
        if record.state is not HypothesisState.VALIDATION_PASSED:
            raise HoldoutRunError(
                "final holdout may run only after frozen validation has passed"
            )
        if record.manifest_hash is None:
            raise HoldoutRunError("validated hypothesis has no frozen manifest_hash")
        family = self.registry.family_status(record.hypothesis_family_id)
        if family["holdout_consumed"]:
            raise HoldoutRunError("final holdout already consumed for this hypothesis family")
        if family["terminal_state"]:
            raise HoldoutRunError("hypothesis family is already terminal")

        seal = self.seals.get(hypothesis_id)
        if seal.hypothesis_family_id != record.hypothesis_family_id:
            raise HoldoutRunError("sealed holdout family does not match hypothesis registry")
        if seal.manifest_hash != record.manifest_hash:
            raise HoldoutRunError("sealed holdout manifest does not match hypothesis registry")
        if seal.evaluator_id != self.evaluator.evaluator_id:
            raise HoldoutRunError("configured evaluator_id does not match frozen holdout seal")
        evaluator_hash = _sha256(self.evaluator.evaluator_hash, "evaluator_hash")
        if seal.evaluator_hash != evaluator_hash:
            raise HoldoutRunError("configured evaluator_hash does not match frozen holdout seal")

        try:
            verify_envelope(document)
        except HoldoutCryptoError as exc:
            raise HoldoutRunError("protected final-holdout envelope verification failed") from exc
        header = document["header"]
        if document.get("envelope_hash") != seal.envelope_hash:
            raise HoldoutRunError("protected envelope does not match registered envelope_hash")
        expected_header = {
            "hypothesis_family_id": record.hypothesis_family_id,
            "manifest_hash": record.manifest_hash,
            "key_id": seal.key_id,
            "evaluator_id": seal.evaluator_id,
            "evaluator_hash": seal.evaluator_hash,
        }
        for key_name, expected in expected_header.items():
            if header.get(key_name) != expected:
                raise HoldoutRunError(f"protected envelope {key_name} does not match frozen seal")

        key = self.keys.load_key(seal.key_id)
        try:
            verify_key(document, key)
        except HoldoutCryptoError as exc:
            raise HoldoutRunError("external holdout key does not match frozen seal") from exc
        return key, record.hypothesis_family_id, seal.envelope_hash

    def run_once(
        self,
        *,
        hypothesis_id: str,
        sealed_path: str | Path,
    ) -> HoldoutRunReceipt:
        """Consume one family entitlement, decrypt in memory, and emit aggregates only.

        Key/evaluator/envelope preflight occurs before consumption so deployment
        mistakes can be corrected without burning the holdout. The irreversible
        HOLDOUT_CONSUMED transition occurs before plaintext decryption. Any failure
        after that point is terminal for this family and cannot be retried.
        """
        document = self._load_envelope(sealed_path)
        key, family_id, envelope_hash = self._preflight(hypothesis_id, document)

        try:
            self.registry.transition(hypothesis_id, HypothesisState.HOLDOUT_CONSUMED)
        except RegistryError as exc:
            raise HoldoutRunError("failed to claim one-shot final-holdout entitlement") from exc

        try:
            plaintext = decrypt_bytes(document, key)
            metrics = _validated_metrics(self.evaluator.evaluate(plaintext))
        except Exception as exc:
            failure_hash = self.ledger.append(
                {
                    "record_type": "FINAL_HOLDOUT_RUN_FAILED",
                    "hypothesis_id": hypothesis_id,
                    "hypothesis_family_id": family_id,
                    "envelope_hash": envelope_hash,
                    "evaluator_id": self.evaluator.evaluator_id,
                    "evaluator_hash": self.evaluator.evaluator_hash,
                    "error_type": type(exc).__name__,
                    "holdout_consumed": True,
                }
            )
            raise HoldoutRunError(
                f"final holdout failed after one-shot consumption; ledger={failure_hash}"
            ) from exc

        ledger_hash = self.ledger.append(
            {
                "record_type": "FINAL_HOLDOUT_RESULT",
                "hypothesis_id": hypothesis_id,
                "hypothesis_family_id": family_id,
                "envelope_hash": envelope_hash,
                "evaluator_id": self.evaluator.evaluator_id,
                "evaluator_hash": self.evaluator.evaluator_hash,
                "aggregate_metrics": metrics,
                "holdout_consumed": True,
            }
        )
        return HoldoutRunReceipt(
            hypothesis_id=hypothesis_id,
            hypothesis_family_id=family_id,
            envelope_hash=envelope_hash,
            evaluator_id=self.evaluator.evaluator_id,
            evaluator_hash=self.evaluator.evaluator_hash,
            aggregate_metrics=metrics,
            ledger_record_hash=ledger_hash,
        )
