"""Trusted staging sealer for final-holdout plaintext.

The low-level seal operation encrypts and registers one final-holdout artifact.
The production-safe path is ``seal_and_quarantine``: it verifies that every frozen
manifest dataset ends before the final holdout begins, seals the holdout, moves its
plaintext out of the declared research root, and records a path-free isolation
attestation before research may proceed.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import shutil
import tempfile
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from .data_layer import sha256_file
from .holdout_crypto import seal_bytes, verify_envelope
from .holdout_keys import HoldoutKeyProvider
from .holdout_store import HoldoutSealStore
from .hypothesis_registry import HypothesisRegistry, HypothesisState
from .manifest import ManifestIntegrityError, verify_frozen_manifest


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

    @staticmethod
    def _csv_times(path: Path, *, label: str) -> tuple[datetime, ...]:
        try:
            with path.open("r", encoding="utf-8", newline="") as handle:
                reader = csv.DictReader(handle)
                if "time" not in (reader.fieldnames or ()):
                    raise HoldoutSealerError(f"{label} must contain a time column")
                values: list[datetime] = []
                for row in reader:
                    raw = (row.get("time") or "").strip()
                    if not raw:
                        raise HoldoutSealerError(f"{label} contains an empty time value")
                    value = datetime.fromisoformat(raw)
                    if value.tzinfo is None or value.utcoffset() is None:
                        raise HoldoutSealerError(f"{label} time values must be timezone-aware")
                    values.append(value.astimezone(timezone.utc))
        except HoldoutSealerError:
            raise
        except (OSError, UnicodeDecodeError, csv.Error, ValueError) as exc:
            raise HoldoutSealerError(f"cannot parse {label} time boundary safely") from exc
        if not values:
            raise HoldoutSealerError(f"{label} must contain at least one data row")
        if values != sorted(values):
            raise HoldoutSealerError(f"{label} time values must be non-decreasing")
        return tuple(values)

    def _verify_public_manifest_boundary(
        self,
        *,
        manifest_path: str | Path,
        expected_manifest_hash: str,
        holdout_path: Path,
    ) -> dict[str, object]:
        try:
            document = verify_frozen_manifest(
                manifest_path,
                expected_manifest_hash=expected_manifest_hash,
                verify_datasets=True,
            )
        except (ManifestIntegrityError, OSError, ValueError) as exc:
            raise HoldoutSealerError("frozen manifest verification failed before holdout seal") from exc

        datasets = document["manifest"].get("datasets")
        if not isinstance(datasets, list) or not datasets:
            raise HoldoutSealerError("frozen manifest has no public datasets")
        holdout_times = self._csv_times(holdout_path, label="final holdout")
        holdout_start = min(holdout_times)
        holdout_end = max(holdout_times)

        public_times: list[datetime] = []
        for raw in datasets:
            if not isinstance(raw, dict) or not raw.get("file_path"):
                raise HoldoutSealerError("frozen manifest contains an invalid dataset artifact")
            public_times.extend(
                self._csv_times(Path(str(raw["file_path"])), label="frozen public dataset")
            )
        if not public_times:
            raise HoldoutSealerError("frozen public datasets contain no rows")
        public_max = max(public_times)
        if public_max >= holdout_start:
            raise HoldoutSealerError(
                "frozen manifest dataset overlaps or reaches into final-holdout time range"
            )

        return {
            "public_max_time": public_max.isoformat(),
            "holdout_start_time": holdout_start.isoformat(),
            "holdout_end_time": holdout_end.isoformat(),
            "public_row_count": len(public_times),
            "holdout_row_count": len(holdout_times),
        }

    @staticmethod
    def _evaluator_hash(path: str | Path) -> str:
        artifact = Path(path).expanduser().resolve()
        if not artifact.is_file():
            raise FileNotFoundError(artifact)
        return sha256_file(artifact)

    def seal_file(
        self,
        *,
        hypothesis_id: str,
        plaintext_path: str | Path,
        destination_path: str | Path,
        key_id: str,
        evaluator_id: str,
        evaluator_artifact_path: str | Path,
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
        evaluator_hash = self._evaluator_hash(evaluator_artifact_path)
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
        manifest_path: str | Path,
        research_root: str | Path,
        quarantine_directory: str | Path,
        key_id: str,
        evaluator_id: str,
        evaluator_artifact_path: str | Path,
    ) -> HoldoutSealReceipt:
        """Verify public-only manifest data, seal, quarantine, and attest isolation."""
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

        record = self.registry.get(hypothesis_id)
        if record.state is not HypothesisState.FROZEN or record.manifest_hash is None:
            raise HoldoutSealerError("hypothesis must be FROZEN with manifest_hash before isolation")
        boundary = self._verify_public_manifest_boundary(
            manifest_path=manifest_path,
            expected_manifest_hash=record.manifest_hash,
            holdout_path=source,
        )

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
            evaluator_artifact_path=evaluator_artifact_path,
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
                **boundary,
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
                public_max_time=str(boundary["public_max_time"]),
                holdout_start_time=str(boundary["holdout_start_time"]),
                holdout_end_time=str(boundary["holdout_end_time"]),
                public_row_count=int(boundary["public_row_count"]),
                holdout_row_count=int(boundary["holdout_row_count"]),
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
