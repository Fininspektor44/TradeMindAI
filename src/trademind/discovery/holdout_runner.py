"""One-shot final-holdout runner with bounded aggregate output.

This module is intended to run in a separate trusted process. The evaluator is
preconfigured there and is bound by immutable evaluator id/hash recorded when the
holdout was sealed. Research agents never receive plaintext rows or the key.
"""

from __future__ import annotations

import inspect
import json
import math
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Protocol

from .data_layer import sha256_file
from .holdout_crypto import HoldoutCryptoError, decrypt_bytes, verify_envelope, verify_key
from .holdout_keys import HoldoutKeyProvider
from .holdout_store import HoldoutSealStore
from .hypothesis_registry import HypothesisRegistry, HypothesisState
from .result_ledger import ResultLedger


class HoldoutRunError(RuntimeError):
    """Raised when a protected final-holdout run cannot complete safely."""


class HoldoutEvaluator(Protocol):
    """Trusted evaluator contract installed inside the isolated runner."""

    @property
    def evaluator_id(self) -> str: ...

    def evaluate(self, plaintext: bytes) -> Mapping[str, int | float | bool | None]: ...


@dataclass(frozen=True, slots=True)
class HoldoutRunReceipt:
    hypothesis_id: str
    hypothesis_family_id: str
    envelope_hash: str
    evaluator_id: str
    evaluator_hash: str
    aggregate_metrics: dict[str, int | float | bool | None]
    claim_record_hash: str
    ledger_record_hash: str


_METRIC_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


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


class _RunLock:
    """Cross-process fail-closed lock for one result ledger."""

    def __init__(self, ledger_path: Path) -> None:
        self.path = ledger_path.with_suffix(ledger_path.suffix + ".holdout-run.lock")
        self.fd: int | None = None

    def __enter__(self) -> "_RunLock":
        try:
            self.fd = os.open(
                self.path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o600,
            )
        except FileExistsError as exc:
            raise HoldoutRunError(
                "final-holdout runner lock already exists; refuse concurrent or stale run"
            ) from exc
        os.write(self.fd, f"pid={os.getpid()}\n".encode("ascii"))
        os.fsync(self.fd)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            pass


class FinalHoldoutRunner:
    def __init__(
        self,
        *,
        registry: HypothesisRegistry,
        seals: HoldoutSealStore,
        keys: HoldoutKeyProvider,
        ledger: ResultLedger,
        evaluator: HoldoutEvaluator,
        evaluator_artifact_path: str | Path,
    ) -> None:
        if not evaluator.evaluator_id.strip():
            raise ValueError("evaluator_id must not be empty")
        artifact = Path(evaluator_artifact_path).expanduser().resolve()
        if not artifact.is_file():
            raise FileNotFoundError(artifact)
        source_file = inspect.getsourcefile(type(evaluator))
        if source_file is None or Path(source_file).resolve() != artifact:
            raise HoldoutRunError(
                "evaluator artifact must be the actual source file defining evaluator class"
            )
        self.registry = registry
        self.seals = seals
        self.keys = keys
        self.ledger = ledger
        self.evaluator = evaluator
        self.evaluator_artifact_path = artifact
        self.evaluator_hash = sha256_file(artifact)

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

    def _ledger_has_claim(self, family_id: str) -> bool:
        if not self.ledger.verify():
            raise HoldoutRunError("result ledger integrity failed before final-holdout claim")
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
                        payload.get("record_type") == "FINAL_HOLDOUT_CLAIM"
                        and payload.get("hypothesis_family_id") == family_id
                    ):
                        return True
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HoldoutRunError("cannot inspect result ledger for holdout claim") from exc
        return False

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
        if self._ledger_has_claim(record.hypothesis_family_id):
            raise HoldoutRunError("result ledger already contains final-holdout claim for family")

        seal = self.seals.get(hypothesis_id)
        if not seal.isolated:
            raise HoldoutRunError("final holdout has no persisted plaintext-isolation attestation")
        if seal.hypothesis_family_id != record.hypothesis_family_id:
            raise HoldoutRunError("sealed holdout family does not match hypothesis registry")
        if seal.manifest_hash != record.manifest_hash:
            raise HoldoutRunError("sealed holdout manifest does not match hypothesis registry")
        if seal.evaluator_id != self.evaluator.evaluator_id:
            raise HoldoutRunError("configured evaluator_id does not match frozen holdout seal")
        if seal.evaluator_hash != self.evaluator_hash:
            raise HoldoutRunError("computed evaluator source hash does not match frozen holdout seal")

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

    def _append_claim(
        self,
        *,
        hypothesis_id: str,
        family_id: str,
        envelope_hash: str,
        intent_hash: str,
    ) -> str:
        return self.ledger.append(
            {
                "record_type": "FINAL_HOLDOUT_CLAIM",
                "hypothesis_id": hypothesis_id,
                "hypothesis_family_id": family_id,
                "envelope_hash": envelope_hash,
                "evaluator_id": self.evaluator.evaluator_id,
                "evaluator_hash": self.evaluator_hash,
                "intent_record_hash": intent_hash,
            }
        )

    def run_once(
        self,
        *,
        hypothesis_id: str,
        sealed_path: str | Path,
    ) -> HoldoutRunReceipt:
        """Claim one family entitlement, decrypt in memory, and emit aggregates only.

        Key/evaluator/envelope preflight occurs before any durable claim. The runner
        first writes a non-consuming intent record, then commits HOLDOUT_CONSUMED in
        SQLite, then writes the independent FINAL_HOLDOUT_CLAIM ledger anchor. Only
        after both durable consumption anchors exist may plaintext be decrypted.

        A transient registry failure before HOLDOUT_CONSUMED leaves only an intent
        record and is retryable after infrastructure recovery. Any failure after
        registry consumption remains fail-closed and never decrypts plaintext.
        """
        with _RunLock(self.ledger.path):
            document = self._load_envelope(sealed_path)
            key, family_id, envelope_hash = self._preflight(hypothesis_id, document)

            intent_hash = self.ledger.append(
                {
                    "record_type": "FINAL_HOLDOUT_INTENT",
                    "hypothesis_id": hypothesis_id,
                    "hypothesis_family_id": family_id,
                    "envelope_hash": envelope_hash,
                    "evaluator_id": self.evaluator.evaluator_id,
                    "evaluator_hash": self.evaluator_hash,
                }
            )

            try:
                self.registry.transition(hypothesis_id, HypothesisState.HOLDOUT_CONSUMED)
            except Exception as exc:
                try:
                    state_after = self.registry.get(hypothesis_id).state
                except Exception as state_exc:
                    raise HoldoutRunError(
                        "registry transition outcome is unknown after holdout intent; "
                        "manual recovery is required and plaintext was not decrypted"
                    ) from state_exc

                if state_after is HypothesisState.HOLDOUT_CONSUMED:
                    try:
                        self._append_claim(
                            hypothesis_id=hypothesis_id,
                            family_id=family_id,
                            envelope_hash=envelope_hash,
                            intent_hash=intent_hash,
                        )
                    except Exception as claim_exc:
                        raise HoldoutRunError(
                            "registry consumed final holdout but independent ledger claim could "
                            "not be recorded; family remains burned and plaintext was not decrypted"
                        ) from claim_exc
                    raise HoldoutRunError(
                        "registry reported a transition failure after durable holdout consumption; "
                        "family remains burned and plaintext was not decrypted"
                    ) from exc

                if state_after is not HypothesisState.VALIDATION_PASSED:
                    raise HoldoutRunError(
                        "registry state changed unexpectedly after holdout intent; "
                        "manual recovery is required and plaintext was not decrypted"
                    ) from exc

                try:
                    self.ledger.append(
                        {
                            "record_type": "FINAL_HOLDOUT_INTENT_ABORTED",
                            "hypothesis_id": hypothesis_id,
                            "hypothesis_family_id": family_id,
                            "envelope_hash": envelope_hash,
                            "intent_record_hash": intent_hash,
                            "error_type": type(exc).__name__,
                            "holdout_consumed": False,
                        }
                    )
                except Exception:
                    pass
                raise HoldoutRunError(
                    "registry claim failed before holdout consumption; retry is allowed after "
                    "infrastructure recovery and plaintext was not decrypted"
                ) from exc

            try:
                claim_hash = self._append_claim(
                    hypothesis_id=hypothesis_id,
                    family_id=family_id,
                    envelope_hash=envelope_hash,
                    intent_hash=intent_hash,
                )
            except Exception as exc:
                raise HoldoutRunError(
                    "registry holdout consumption committed but independent ledger claim failed; "
                    "family remains burned and plaintext was not decrypted"
                ) from exc

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
                        "evaluator_hash": self.evaluator_hash,
                        "claim_record_hash": claim_hash,
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
                    "evaluator_hash": self.evaluator_hash,
                    "claim_record_hash": claim_hash,
                    "aggregate_metrics": metrics,
                    "holdout_consumed": True,
                }
            )
            return HoldoutRunReceipt(
                hypothesis_id=hypothesis_id,
                hypothesis_family_id=family_id,
                envelope_hash=envelope_hash,
                evaluator_id=self.evaluator.evaluator_id,
                evaluator_hash=self.evaluator_hash,
                aggregate_metrics=metrics,
                claim_record_hash=claim_hash,
                ledger_record_hash=ledger_hash,
            )