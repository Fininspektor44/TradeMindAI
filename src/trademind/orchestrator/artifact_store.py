"""Content-addressed local artifact storage for orchestrator evidence.

Verified CAS v1 uses a trusted-root security model: the artifact root and its
``.verified-cas-v1`` namespace must be exclusively managed by the owning
application. Untrusted callers receive hash-based APIs and must not have
filesystem write access to that namespace. The verified APIs reject malformed
identities, caller-supplied paths, corrupt bytes, and static link/reparse-point
substitution. Concurrent mutation by an attacker who already has write access
inside the trusted CAS root is a compromise of this storage boundary and is not
claimed to be sandboxed by v1.

Verified CAS v1 requires an atomic no-replace hard-link primitive. The intended
Windows production target is NTFS and must be integration-validated on the SER8
before merge or release. Other or unknown filesystems are supported only when
the required primitive succeeds. Capability failure is deterministic and
fail-closed. Legacy ArtifactStore APIs do not depend on this CAS capability.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9_.-]+$")
_HASH_REF = re.compile(r"^sha256:([0-9a-f]{64})$")
_CAS_SCHEMA_VERSION = "artifact-store-verified-cas-v1"
_CAS_DIRECTORY = ".verified-cas-v1"
_COPY_CHUNK_BYTES = 1024 * 1024
_MAX_METADATA_BYTES = 16 * 1024
_DEFAULT_IMPORT_MAX_BYTES = 1024 * 1024 * 1024
_DEFAULT_READ_MAX_BYTES = 64 * 1024 * 1024
_MAX_CONFIGURED_OBJECT_BYTES = 1024 * 1024 * 1024 * 1024


class ArtifactStoreError(RuntimeError):
    """Base class for deterministic verified-CAS failures."""


class InvalidArtifactHashRef(ValueError):
    """Raised when a verified API receives anything except a strict SHA-256 ref."""


class ArtifactNotFoundError(ArtifactStoreError):
    """Raised when the deterministic CAS object or its metadata is absent."""


class ArtifactIntegrityError(ArtifactStoreError):
    """Raised when persisted CAS state is malformed, unsafe, or does not hash."""


class ArtifactMediaTypeError(ArtifactStoreError):
    """Raised when persisted and expected media types differ."""


class ArtifactResourceLimitError(ArtifactStoreError):
    """Raised before a verified import/read exceeds its configured byte boundary."""


class ArtifactStoreCapabilityError(ArtifactStoreError):
    """Raised when the filesystem lacks required atomic Verified CAS primitives."""


@dataclass(frozen=True, slots=True)
class ArtifactRef:
    artifact_id: str
    path: str
    sha256: str
    size_bytes: int
    media_type: str

    @property
    def hash_ref(self) -> str:
        return f"sha256:{self.sha256}"


class ArtifactStore:
    """Legacy artifact storage plus a bounded, trusted-root Verified CAS v1.

    ``media_type`` in Verified CAS v1 is a persisted advisory label, not a
    cryptographic property or attestation. The first verified writer establishes
    the exact case-sensitive label for a digest; a different later label is a
    deterministic conflict. Changing that rule requires a new metadata schema.

    Corrupt metadata is never silently repaired. Operators must quarantine or
    remove the affected CAS entry and re-import it from a trusted source.
    """

    def __init__(
        self,
        root: str | Path,
        *,
        verified_import_max_bytes: int = _DEFAULT_IMPORT_MAX_BYTES,
        verified_read_max_bytes: int = _DEFAULT_READ_MAX_BYTES,
    ) -> None:
        self._verified_import_max_bytes = self._resource_limit(
            verified_import_max_bytes,
            "verified_import_max_bytes",
        )
        self._verified_read_max_bytes = self._resource_limit(
            verified_read_max_bytes,
            "verified_read_max_bytes",
        )
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _resource_limit(value: object, label: str) -> int:
        if type(value) is not int or not 1 <= value <= _MAX_CONFIGURED_OBJECT_BYTES:
            raise ValueError(
                f"{label} must be an exact positive integer no greater than "
                f"{_MAX_CONFIGURED_OBJECT_BYTES}"
            )
        return value

    @staticmethod
    def _safe(value: str, label: str) -> str:
        if not value or not _SAFE_COMPONENT.fullmatch(value):
            raise ValueError(f"unsafe {label}: {value!r}")
        return value

    @staticmethod
    def _media_type(value: object) -> str:
        if type(value) is not str or not value or len(value) > 255:
            raise ValueError("media_type must be a non-empty bounded ASCII string")
        try:
            encoded = value.encode("ascii")
        except UnicodeEncodeError as exc:
            raise ValueError("media_type must contain ASCII characters only") from exc
        if any(byte < 0x20 or byte > 0x7E for byte in encoded) or value != value.strip():
            raise ValueError("media_type contains unsafe whitespace or control characters")
        return value

    @staticmethod
    def _hash_digest(hash_ref: object) -> str:
        if type(hash_ref) is not str:
            raise InvalidArtifactHashRef("hash_ref must be sha256:<64 lowercase hex>")
        match = _HASH_REF.fullmatch(hash_ref)
        if match is None:
            raise InvalidArtifactHashRef("hash_ref must be sha256:<64 lowercase hex>")
        return match.group(1)

    @staticmethod
    def _stat_is_link_or_reparse(details: os.stat_result) -> bool:
        if stat.S_ISLNK(details.st_mode):
            return True
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        return bool(getattr(details, "st_file_attributes", 0) & reparse_flag)

    @classmethod
    def _is_link_or_reparse(cls, path: Path) -> bool:
        try:
            details = path.lstat()
        except FileNotFoundError:
            return False
        return cls._stat_is_link_or_reparse(details)

    def _assert_confined(self, path: Path) -> None:
        try:
            relative = path.relative_to(self.root)
        except ValueError as exc:
            raise ArtifactIntegrityError("CAS path escapes artifact root") from exc
        current = self.root
        for component in relative.parts:
            current /= component
            if self._is_link_or_reparse(current):
                raise ArtifactIntegrityError("CAS path contains a symlink or reparse point")
        try:
            resolved = path.resolve(strict=False)
        except (OSError, RuntimeError) as exc:
            raise ArtifactIntegrityError("CAS path cannot be resolved safely") from exc
        if not resolved.is_relative_to(self.root):
            raise ArtifactIntegrityError("CAS path resolves outside artifact root")

    def _ensure_directory(self, directory: Path) -> None:
        self._assert_confined(directory)
        relative = directory.relative_to(self.root)
        current = self.root
        for component in relative.parts:
            current /= component
            if self._is_link_or_reparse(current):
                raise ArtifactIntegrityError("CAS directory is a symlink or reparse point")
            try:
                current.mkdir()
            except FileExistsError:
                pass
            if self._is_link_or_reparse(current) or not current.is_dir():
                raise ArtifactIntegrityError("CAS directory is not a safe directory")

    def _cas_paths(self, digest: str) -> tuple[Path, Path]:
        directory = self.root / _CAS_DIRECTORY / "objects" / digest[:2] / digest[2:4]
        artifact = directory / f"{digest}.blob"
        metadata = directory / f"{digest}.meta.json"
        self._assert_confined(artifact)
        self._assert_confined(metadata)
        return artifact, metadata

    @property
    def _cas_temp_directory(self) -> Path:
        return self.root / _CAS_DIRECTORY / "tmp"

    @staticmethod
    def _fsync_directory(directory: Path) -> None:
        if os.name != "posix":
            return
        descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _open_regular_no_follow(self, path: Path) -> BinaryIO:
        self._assert_confined(path)
        initial = path.lstat()
        if self._stat_is_link_or_reparse(initial):
            raise ArtifactIntegrityError("CAS object is a symlink or reparse point")
        if not stat.S_ISREG(initial.st_mode):
            raise ArtifactIntegrityError("CAS object is not a regular file")
        flags = (
            os.O_RDONLY
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        try:
            descriptor = os.open(path, flags)
        except FileNotFoundError:
            raise
        except OSError as exc:
            raise ArtifactIntegrityError("CAS object cannot be opened safely") from exc
        try:
            opened = os.fstat(descriptor)
            # Detect an ordinary replacement observed during the pathname open.
            # The trusted-root contract excludes a hostile concurrent writer.
            self._assert_confined(path)
            current = path.lstat()
            if not stat.S_ISREG(opened.st_mode) or not stat.S_ISREG(current.st_mode):
                raise ArtifactIntegrityError("CAS object is not a regular file")
            if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
                raise ArtifactIntegrityError("CAS object changed while being opened")
            return os.fdopen(descriptor, "rb")
        except Exception:
            os.close(descriptor)
            raise

    def _read_metadata(self, path: Path, digest: str) -> tuple[int, str]:
        try:
            with self._open_regular_no_follow(path) as handle:
                encoded = handle.read(_MAX_METADATA_BYTES + 1)
        except FileNotFoundError as exc:
            raise ArtifactNotFoundError(f"artifact metadata not found: sha256:{digest}") from exc
        if len(encoded) > _MAX_METADATA_BYTES:
            raise ArtifactIntegrityError("CAS metadata exceeds its size limit")

        def strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
            result: dict[str, object] = {}
            for key, value in pairs:
                if key in result:
                    raise ArtifactIntegrityError("CAS metadata contains duplicate keys")
                result[key] = value
            return result

        def reject_constant(value: str) -> None:
            raise ArtifactIntegrityError(f"CAS metadata contains invalid constant {value}")

        try:
            payload = json.loads(
                encoded.decode("utf-8"),
                object_pairs_hook=strict_object,
                parse_constant=reject_constant,
            )
        except ArtifactIntegrityError:
            raise
        except (UnicodeDecodeError, ValueError, RecursionError) as exc:
            raise ArtifactIntegrityError("CAS metadata is malformed") from exc
        required = {"schema_version", "hash_ref", "size_bytes", "media_type"}
        if type(payload) is not dict or set(payload) != required:
            raise ArtifactIntegrityError("CAS metadata fields do not match its schema")
        if payload["schema_version"] != _CAS_SCHEMA_VERSION:
            raise ArtifactIntegrityError("CAS metadata schema is unsupported")
        if payload["hash_ref"] != f"sha256:{digest}":
            raise ArtifactIntegrityError("CAS metadata hash does not match its object")
        size_bytes = payload["size_bytes"]
        if (
            type(size_bytes) is not int
            or size_bytes < 0
            or size_bytes > _MAX_CONFIGURED_OBJECT_BYTES
        ):
            raise ArtifactIntegrityError("CAS metadata size is invalid")
        try:
            media_type = self._media_type(payload["media_type"])
        except ValueError as exc:
            raise ArtifactIntegrityError("CAS metadata media type is invalid") from exc
        return size_bytes, media_type

    def _verify_object(
        self,
        path: Path,
        *,
        digest: str,
        expected_size: int,
        return_bytes: bool,
    ) -> bytes | None:
        checksum = hashlib.sha256()
        remaining = expected_size
        chunks: list[bytes] | None = [] if return_bytes else None
        try:
            with self._open_regular_no_follow(path) as handle:
                while remaining:
                    requested = min(_COPY_CHUNK_BYTES, remaining)
                    chunk = handle.read(requested)
                    if not chunk:
                        raise ArtifactIntegrityError(
                            "CAS artifact is truncated relative to its metadata"
                        )
                    if len(chunk) > requested:
                        raise ArtifactIntegrityError(
                            "CAS artifact reader exceeded the requested byte count"
                        )
                    checksum.update(chunk)
                    remaining -= len(chunk)
                    if chunks is not None:
                        chunks.append(chunk)
                if handle.read(1):
                    raise ArtifactIntegrityError(
                        "CAS artifact is larger than its persisted metadata"
                    )
        except FileNotFoundError as exc:
            raise ArtifactNotFoundError(f"artifact not found: sha256:{digest}") from exc
        if checksum.hexdigest() != digest:
            raise ArtifactIntegrityError("CAS artifact bytes do not match their verified identity")
        if chunks is None:
            return None
        return b"".join(chunks)

    @staticmethod
    def _metadata_bytes(*, digest: str, size_bytes: int, media_type: str) -> bytes:
        return json.dumps(
            {
                "schema_version": _CAS_SCHEMA_VERSION,
                "hash_ref": f"sha256:{digest}",
                "size_bytes": size_bytes,
                "media_type": media_type,
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")

    def _install_no_replace(self, temporary: Path, destination: Path) -> bool:
        self._assert_confined(temporary)
        self._assert_confined(destination)
        try:
            os.link(temporary, destination)
        except FileExistsError:
            return False
        except OSError as exc:
            raise ArtifactStoreCapabilityError(
                "filesystem does not provide the required atomic no-replace hard-link "
                "capability for Verified CAS v1"
            ) from exc
        self._fsync_directory(destination.parent)
        return True

    def _write_metadata_no_replace(
        self,
        *,
        destination: Path,
        digest: str,
        size_bytes: int,
        media_type: str,
    ) -> None:
        encoded = self._metadata_bytes(
            digest=digest,
            size_bytes=size_bytes,
            media_type=media_type,
        )
        descriptor, name = tempfile.mkstemp(prefix=".metadata-", dir=destination.parent)
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            self._install_no_replace(temporary, destination)
            existing_size, existing_media = self._read_metadata(destination, digest)
            if existing_size != size_bytes:
                raise ArtifactIntegrityError("existing CAS metadata has a conflicting size")
            if existing_media != media_type:
                raise ArtifactMediaTypeError("existing CAS object has a different media type")
        finally:
            if os.path.lexists(temporary):
                temporary.unlink()

    def _finalize_snapshot(
        self,
        *,
        temporary: Path,
        digest: str,
        size_bytes: int,
        media_type: str,
    ) -> ArtifactRef:
        destination, metadata = self._cas_paths(digest)
        self._ensure_directory(destination.parent)
        self._install_no_replace(temporary, destination)
        self._verify_object(
            destination,
            digest=digest,
            expected_size=size_bytes,
            return_bytes=False,
        )
        self._write_metadata_no_replace(
            destination=metadata,
            digest=digest,
            size_bytes=size_bytes,
            media_type=media_type,
        )
        return ArtifactRef(
            artifact_id=f"cas-v1-{digest}",
            path=str(destination),
            sha256=digest,
            size_bytes=size_bytes,
            media_type=media_type,
        )

    def import_snapshot(self, stream: BinaryIO, *, media_type: str) -> ArtifactRef:
        """Persist a bounded binary stream under the trusted-root CAS v1 contract."""
        media_type = self._media_type(media_type)
        reader = getattr(stream, "read", None)
        if not callable(reader):
            raise TypeError("stream must be an opened binary stream")
        self._ensure_directory(self._cas_temp_directory)
        descriptor, name = tempfile.mkstemp(prefix=".snapshot-", dir=self._cas_temp_directory)
        temporary = Path(name)
        checksum = hashlib.sha256()
        size_bytes = 0
        try:
            with os.fdopen(descriptor, "wb") as destination:
                while True:
                    remaining = self._verified_import_max_bytes - size_bytes
                    chunk = reader(min(_COPY_CHUNK_BYTES, remaining + 1))
                    if type(chunk) is not bytes:
                        raise TypeError("binary stream read() must return exact bytes")
                    if len(chunk) > min(_COPY_CHUNK_BYTES, remaining + 1):
                        raise ValueError(
                            "binary stream returned more than the requested chunk size"
                        )
                    if not chunk:
                        break
                    if len(chunk) > remaining:
                        raise ArtifactResourceLimitError(
                            "snapshot exceeds verified_import_max_bytes"
                        )
                    destination.write(chunk)
                    checksum.update(chunk)
                    size_bytes += len(chunk)
                destination.flush()
                os.fsync(destination.fileno())
            return self._finalize_snapshot(
                temporary=temporary,
                digest=checksum.hexdigest(),
                size_bytes=size_bytes,
                media_type=media_type,
            )
        finally:
            if os.path.lexists(temporary):
                temporary.unlink()

    def _write(
        self,
        *,
        task_id: str,
        revision: int,
        kind: str,
        data: bytes,
        suffix: str,
        media_type: str,
    ) -> ArtifactRef:
        task_id = self._safe(task_id, "task_id")
        kind = self._safe(kind, "kind")
        if revision < 1:
            raise ValueError("revision must be positive")
        if not suffix.startswith(".") or "/" in suffix or "\\" in suffix:
            raise ValueError("suffix must be a simple file extension")

        digest = hashlib.sha256(data).hexdigest()
        artifact_id = f"{task_id}_r{revision}_{kind}_{digest[:16]}"
        directory = (self.root / task_id / f"r{revision}").resolve()
        if not directory.is_relative_to(self.root):
            raise ValueError("artifact path escapes store root")
        directory.mkdir(parents=True, exist_ok=True)
        destination = directory / f"{artifact_id}{suffix}"

        if destination.exists():
            existing = destination.read_bytes()
            if hashlib.sha256(existing).hexdigest() != digest:
                raise RuntimeError("content-addressed artifact collision")
        else:
            fd, temporary_name = tempfile.mkstemp(prefix=".artifact-", dir=directory)
            temporary = Path(temporary_name)
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
                temporary.replace(destination)
            finally:
                if temporary.exists():
                    temporary.unlink()

        return ArtifactRef(
            artifact_id=artifact_id,
            path=str(destination),
            sha256=digest,
            size_bytes=len(data),
            media_type=media_type,
        )

    def store_json(
        self,
        *,
        task_id: str,
        revision: int,
        kind: str,
        payload: dict[str, Any],
    ) -> ArtifactRef:
        encoded = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        return self._write(
            task_id=task_id,
            revision=revision,
            kind=kind,
            data=encoded,
            suffix=".json",
            media_type="application/json",
        )

    def store_text(
        self,
        *,
        task_id: str,
        revision: int,
        kind: str,
        text: str,
    ) -> ArtifactRef:
        return self._write(
            task_id=task_id,
            revision=revision,
            kind=kind,
            data=text.encode("utf-8"),
            suffix=".txt",
            media_type="text/plain; charset=utf-8",
        )

    def verify(self, artifact: ArtifactRef) -> bool:
        path = Path(artifact.path).expanduser().resolve()
        if not path.is_relative_to(self.root) or not path.is_file():
            return False
        data = path.read_bytes()
        return (
            len(data) == artifact.size_bytes and hashlib.sha256(data).hexdigest() == artifact.sha256
        )

    def resolve_verified(
        self,
        hash_ref: str,
        *,
        expected_media_type: str | None = None,
    ) -> ArtifactRef:
        """Resolve bytes in the trusted CAS root using only a strict SHA-256 identity."""
        digest = self._hash_digest(hash_ref)
        if expected_media_type is not None:
            expected_media_type = self._media_type(expected_media_type)
        artifact, metadata = self._cas_paths(digest)
        size_bytes, media_type = self._read_metadata(metadata, digest)
        if expected_media_type is not None and media_type != expected_media_type:
            raise ArtifactMediaTypeError("artifact media type does not match the expectation")
        self._verify_object(
            artifact,
            digest=digest,
            expected_size=size_bytes,
            return_bytes=False,
        )
        return ArtifactRef(
            artifact_id=f"cas-v1-{digest}",
            path=str(artifact),
            sha256=digest,
            size_bytes=size_bytes,
            media_type=media_type,
        )

    def read_verified(
        self,
        hash_ref: str,
        *,
        expected_media_type: str | None = None,
    ) -> bytes:
        """Return bounded exact bytes after media, size, and SHA verification."""
        digest = self._hash_digest(hash_ref)
        if expected_media_type is not None:
            expected_media_type = self._media_type(expected_media_type)
        artifact, metadata = self._cas_paths(digest)
        size_bytes, media_type = self._read_metadata(metadata, digest)
        if size_bytes > self._verified_read_max_bytes:
            raise ArtifactResourceLimitError("artifact exceeds verified_read_max_bytes")
        if expected_media_type is not None and media_type != expected_media_type:
            raise ArtifactMediaTypeError("artifact media type does not match the expectation")
        verified = self._verify_object(
            artifact,
            digest=digest,
            expected_size=size_bytes,
            return_bytes=True,
        )
        if verified is None:  # Defensive type narrowing.
            raise ArtifactIntegrityError("verified CAS read did not return bytes")
        return verified
