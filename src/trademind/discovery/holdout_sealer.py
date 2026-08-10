"""Trusted staging sealer for final-holdout plaintext.

The low-level seal operation encrypts and registers one final-holdout artifact.
The production-safe path is ``seal_and_quarantine``: after sealing, it moves the
plaintext out of the declared research root and records a path-free isolation
attestation before research may proceed through the protected bridge.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path

from .data_layer import sha256_file
from .holdout_crypto import seal_bytes, verify_envelope
from .holdout_keys import HoldoutKeyProvider
from .holdout_store import HoldoutSealStore
from .hypothesis_registry import HypothesisRegistry, HypothesisState


class HoldoutSealerError(RuntimeError):
    """Raised when final-holdout sealing cannot complete safely."""


@dataclass(frozen=True, slots=True)
class HoldoutSealReceipt:
    hypothesis_id: str
    hypothesis_family_id: str
    manifest_hash: str
    envelope_hash: str
    key_id: str
    evaluator_id: str
    evaluator_hash: str
    plaintext_sha256: str
    plaintext_size: int
    isolated: bool = False
    isolation_receipt_hash: str | None = None


class FinalHoldoutSealer:
    def __init__(
        self,
        *,
        registry: HypothesisRegistry,
        seals: HoldoutSealStore,
        keys: HoldoutKeyProvider,
    ) -> None:
        self.registry = registry
        self.seals = seals
        self.keys = keys

    @staticmethod
    def _write_new_json(path: Path, document: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            raise FileExistsError(f"sealed holdout already exists: {path}")
        encoded = (
            json.dumps(
                document,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
        fd, temporary_name = tempfile.mkstemp(prefix=".holdout-seal-", dir=path.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
        finally:
            if temporary.exists():
                temporary.unlink()

    def seal_file(
        self,
        *,
        hypothesis_id: str,
        plaintext_path: str | Path,
        destination_path: str | Path,
        key_id: str,
        evaluator_id: str,
        evaluator_hash: str,
    ) -> HoldoutSealReceipt:
        """Low-level seal operation. It does not attest plaintext isolation."""
        source = Path(plaintext_path).expanduser().resolve()
        destination = Path(destination_path).expanduser().resolve()
        if not source.is_file():
            raise FileNotFoundError(source)
        if source == destination:
            raise HoldoutSealerError("plaintext and sealed destination must be different files")

        record = self.registry.get(hypothesis_id)
        if record.state is not HypothesisState.FROZEN:
            raise HoldoutSealerError(
                f"final holdout may be sealed only from FROZEN state, got {record.state.value}"
            )
        if record.manifest_hash is None:
            raise HoldoutSealerError("frozen hypothesis has no manifest_hash")
        family = self.registry.family_status(record.hypothesis_family_id)
        if family["holdout_consumed"] or family["terminal_state"]:
            raise HoldoutSealerError("hypothesis family is not eligible for final-holdout sealing")

        plaintext = source.read_bytes()
        if not plaintext:
            raise HoldoutSealerError("final holdout plaintext is empty")
        key = self.keys.load_key(key_id)
        document = seal_bytes(
            plaintext,
            key=key,
            key_id=key_id,
            hypothesis_family_id=record.hypothesis_family_id,
            manifest_hash=record.manifest_hash,
            evaluator_id=evaluator_id,
            evaluator_hash=evaluator_hash,
        )
        verify_envelope(document)
        self._write_new_json(destination, document)

        try:
            stored = self.seals.register(
                hypothesis_id=hypothesis_id,
                envelope_hash=document["envelope_hash"],
                key_id=key_id,
                evaluator_id=evaluator_id,
                evaluator_hash=evaluator_hash,
            )
        except Exception:
            try:
                destination.unlink(missing_ok=True)
            finally:
                raise

        header = document["header"]
        return HoldoutSealReceipt(
            hypothesis_id=stored.hypothesis_id,
            hypothesis_family_id=stored.hypothesis_family_id,
            manifest_hash=stored.manifest_hash,
            envelope_hash=stored.envelope_hash,
            key_id=stored.key_id,
            evaluator_id=stored.evaluator_id,
            evaluator_hash=stored.evaluator_hash,
            plaintext_sha256=str(header["plaintext_sha256"]),
            plaintext_size=int(header["plaintext_size"]),
        )

    def seal_and_quarantine(
        self,
        *,
        hypothesis_id: str,
        plaintext_path: str | Path,
        destination_path: str | Path,
        research_root: str | Path,
        quarantine_directory: str | Path,
        key_id: str,
        evaluator_id: str,
        evaluator_hash: str,
    ) -> HoldoutSealReceipt:
        """Seal and move plaintext outside the research root before attesting isolation."""
        source = Path(plaintext_path).expanduser().resolve()
        root = Path(research_root).expanduser().resolve()
        quarantine = Path(quarantine_directory).expanduser().resolve()
        if not source.is_file():
            raise FileNotFoundError(source)
        if not root.is_dir():
            raise FileNotFoundError(root)
        if not source.is_relative_to(root):
            raise HoldoutSealerError("final holdout plaintext must begin inside research_root")
        if quarantine.is_relative_to(root) or root.is_relative_to(quarantine):
            raise HoldoutSealerError("quarantine_directory must be disjoint from research_root")

        source_hash = sha256_file(source)
        quarantine.mkdir(parents=True, exist_ok=True)
        quarantine_target = quarantine / f"{source_hash}.holdout-source"
        if quarantine_target.exists():
            raise HoldoutSealerError("quarantine target already exists")

        receipt = self.seal_file(
            hypothesis_id=hypothesis_id,
            plaintext_path=source,
            destination_path=destination_path,
            key_id=key_id,
            evaluator_id=evaluator_id,
            evaluator_hash=evaluator_hash,
        )

        try:
            shutil.move(str(source), str(quarantine_target))
            if source.exists() or not quarantine_target.is_file():
                raise HoldoutSealerError("plaintext quarantine move did not complete")
            if sha256_file(quarantine_target) != receipt.plaintext_sha256:
                raise HoldoutSealerError("quarantined plaintext hash mismatch")
            try:
                os.chmod(quarantine_target, 0o600)
            except OSError:
                pass

            attestation = {
                "schema_version": "final-holdout-isolation-attestation-v1",
                "hypothesis_id": receipt.hypothesis_id,
                "hypothesis_family_id": receipt.hypothesis_family_id,
                "manifest_hash": receipt.manifest_hash,
                "envelope_hash": receipt.envelope_hash,
                "evaluator_id": receipt.evaluator_id,
                "evaluator_hash": receipt.evaluator_hash,
                "plaintext_sha256": receipt.plaintext_sha256,
                "plaintext_size": receipt.plaintext_size,
                "source_absent_from_research_root": True,
            }
            encoded = json.dumps(
                attestation,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")
            isolation_hash = hashlib.sha256(encoded).hexdigest()
            stored = self.seals.mark_isolated(
                hypothesis_id,
                isolation_receipt_hash=isolation_hash,
            )
            if not stored.isolated:
                raise HoldoutSealerError("final holdout isolation attestation was not persisted")
        except Exception as exc:
            raise HoldoutSealerError(
                "final holdout was sealed but plaintext isolation did not complete; "
                "do not advance hypothesis state"
            ) from exc

        return replace(
            receipt,
            isolated=True,
            isolation_receipt_hash=isolation_hash,
        )
