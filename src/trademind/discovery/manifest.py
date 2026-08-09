"""Immutable experiment manifests for Discovery Engine research."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .data_layer import sha256_file


class ManifestIntegrityError(RuntimeError):
    pass


def _canonical(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


@dataclass(frozen=True, slots=True)
class DatasetArtifact:
    file_path: str
    sha256: str
    size_bytes: int

    @classmethod
    def from_path(cls, path: str | Path) -> "DatasetArtifact":
        source = Path(path)
        if not source.is_file():
            raise FileNotFoundError(source)
        return cls(
            file_path=str(source),
            sha256=sha256_file(source),
            size_bytes=source.stat().st_size,
        )

    def verify(self) -> None:
        source = Path(self.file_path)
        if not source.is_file():
            raise ManifestIntegrityError(f"dataset artifact missing: {source}")
        if source.stat().st_size != self.size_bytes:
            raise ManifestIntegrityError(f"dataset artifact size changed: {source}")
        actual = sha256_file(source)
        if actual != self.sha256:
            raise ManifestIntegrityError(f"dataset artifact hash changed: {source}")


@dataclass(frozen=True, slots=True)
class ExperimentManifest:
    hypothesis_id: str
    hypothesis_family_id: str
    test_family: str
    primary_metric: str
    alpha: float
    q: float
    minimum_effect_size: float
    max_hypotheses_tests: int
    schema_version: str
    git_commit: str
    datasets: tuple[DatasetArtifact, ...]
    parameters: dict[str, Any]
    created_at: str

    @classmethod
    def new(
        cls,
        *,
        hypothesis_id: str,
        hypothesis_family_id: str,
        test_family: str,
        primary_metric: str,
        alpha: float,
        q: float,
        minimum_effect_size: float,
        max_hypotheses_tests: int,
        schema_version: str,
        git_commit: str,
        datasets: tuple[DatasetArtifact, ...],
        parameters: dict[str, Any] | None = None,
    ) -> "ExperimentManifest":
        values = {
            "hypothesis_id": hypothesis_id,
            "hypothesis_family_id": hypothesis_family_id,
            "test_family": test_family,
            "primary_metric": primary_metric,
            "schema_version": schema_version,
            "git_commit": git_commit,
        }
        if any(not value.strip() for value in values.values()):
            raise ValueError("manifest identifiers and metric/version fields must not be empty")
        if not 0 < alpha <= 1 or not 0 < q <= 1:
            raise ValueError("alpha and q must be in (0, 1]")
        if minimum_effect_size < 0:
            raise ValueError("minimum_effect_size must be non-negative")
        if max_hypotheses_tests < 1:
            raise ValueError("max_hypotheses_tests must be positive")
        if not datasets:
            raise ValueError("at least one dataset artifact is required")
        return cls(
            hypothesis_id=hypothesis_id.strip(),
            hypothesis_family_id=hypothesis_family_id.strip(),
            test_family=test_family.strip(),
            primary_metric=primary_metric.strip(),
            alpha=float(alpha),
            q=float(q),
            minimum_effect_size=float(minimum_effect_size),
            max_hypotheses_tests=int(max_hypotheses_tests),
            schema_version=schema_version.strip(),
            git_commit=git_commit.strip(),
            datasets=tuple(datasets),
            parameters=dict(parameters or {}),
            created_at=datetime.now(timezone.utc).isoformat(),
        )

    def payload(self) -> dict[str, Any]:
        return {
            "hypothesis_id": self.hypothesis_id,
            "hypothesis_family_id": self.hypothesis_family_id,
            "test_family": self.test_family,
            "primary_metric": self.primary_metric,
            "alpha": self.alpha,
            "q": self.q,
            "minimum_effect_size": self.minimum_effect_size,
            "max_hypotheses_tests": self.max_hypotheses_tests,
            "schema_version": self.schema_version,
            "git_commit": self.git_commit,
            "datasets": [asdict(item) for item in self.datasets],
            "parameters": self.parameters,
            "created_at": self.created_at,
        }

    @property
    def manifest_hash(self) -> str:
        return hashlib.sha256(_canonical(self.payload()).encode("utf-8")).hexdigest()

    def freeze(self, path: str | Path) -> str:
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            raise FileExistsError(f"frozen manifest already exists: {destination}")
        for artifact in self.datasets:
            artifact.verify()
        document = {"manifest_hash": self.manifest_hash, "manifest": self.payload()}
        encoded = (_canonical(document) + "\n").encode("utf-8")
        fd, temporary_name = tempfile.mkstemp(prefix=".manifest-", dir=destination.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
        finally:
            if temporary.exists():
                temporary.unlink()
        return self.manifest_hash


def verify_frozen_manifest(path: str | Path, *, verify_datasets: bool = True) -> dict[str, Any]:
    source = Path(path)
    try:
        document = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ManifestIntegrityError(f"cannot read frozen manifest: {source}") from exc
    if not isinstance(document, dict) or not isinstance(document.get("manifest"), dict):
        raise ManifestIntegrityError("invalid frozen manifest document")
    payload = document["manifest"]
    expected = document.get("manifest_hash")
    actual = hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()
    if expected != actual:
        raise ManifestIntegrityError("frozen manifest hash mismatch")
    if verify_datasets:
        for raw in payload.get("datasets", []):
            try:
                artifact = DatasetArtifact(
                    file_path=str(raw["file_path"]),
                    sha256=str(raw["sha256"]),
                    size_bytes=int(raw["size_bytes"]),
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise ManifestIntegrityError("invalid dataset artifact in manifest") from exc
            artifact.verify()
    return document
