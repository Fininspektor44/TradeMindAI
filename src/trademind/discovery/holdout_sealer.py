"""Trusted staging sealer for final-holdout plaintext.

The sealer is the only component in this slice that needs plaintext-file access.
It writes an authenticated encrypted envelope and registers that exact envelope
against the already-frozen hypothesis manifest. It never deletes the plaintext
automatically; removal from the research environment is an explicit operational
step so source evidence cannot be destroyed by library code.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

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
    plaintext_sha256: str
    plaintext_size: int


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
    ) -> HoldoutSealReceipt:
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
        )
        verify_envelope(document)
        self._write_new_json(destination, document)

        try:
            stored = self.seals.register(
                hypothesis_id=hypothesis_id,
                envelope_hash=document["envelope_hash"],
                key_id=key_id,
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
            plaintext_sha256=str(header["plaintext_sha256"]),
            plaintext_size=int(header["plaintext_size"]),
        )
