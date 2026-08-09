"""Immutable experiment manifests for Discovery Engine research."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .data_layer import sha256_file
from .hypothesis_registry import derive_content_hash, derive_hypothesis_family_id


class ManifestIntegrityError(RuntimeError):
    pass


def _canonical(payload: Any) -> str:
    try:
        return json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("manifest fields must be canonical JSON data") from exc


def _canonical_mapping(payload: Mapping[str, Any], *, label: str, allow_empty: bool) -> str:
    if not isinstance(payload, Mapping) or (not payload and not allow_empty):
        qualifier = "mapping" if allow_empty else "non-empty mapping"
        raise ValueError(f"{label} must be a {qualifier}")
    return _canonical(dict(payload))


def _content_definition(
    *,
    family_definition: dict[str, Any],
    test_family: str,
    primary_metric: str,
    parameters: dict[str, Any],
) -> dict[str, Any]:
    return {
        "family_definition": family_definition,
        "test_family": test_family,
        "primary_metric": primary_metric,
        "parameters": parameters,
    }


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
    content_hash: str
    test_family: str
    primary_metric: str
    alpha: float
    q: float
    minimum_effect_size: float
    max_hypotheses_tests: int
    schema_version: str
    git_commit: str
    datasets: tuple[DatasetArtifact, ...]
    _family_definition_json: str
    _parameters_json: str
    created_at: str

    @classmethod
    def new(
        cls,
        *,
        hypothesis_id: str,
        family_definition: Mapping[str, Any],
        test_family: str,
        primary_metric: str,
        alpha: float,
        q: float,
        minimum_effect_size: float,
        max_hypotheses_tests: int,
        schema_version: str,
        git_commit: str,
        datasets: tuple[DatasetArtifact, ...],
        parameters: Mapping[str, Any] | None = None,
    ) -> "ExperimentManifest":
        values = {
            "hypothesis_id": hypothesis_id,
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

        family_json = _canonical_mapping(
            family_definition,
            label="family_definition",
            allow_empty=False,
        )
        parameters_json = _canonical_mapping(
            parameters or {},
            label="parameters",
            allow_empty=True,
        )
        family_payload = json.loads(family_json)
        parameter_payload = json.loads(parameters_json)
        family_id = derive_hypothesis_family_id(family_payload)
        content_hash = derive_content_hash(
            _content_definition(
                family_definition=family_payload,
                test_family=test_family.strip(),
                primary_metric=primary_metric.strip(),
                parameters=parameter_payload,
            )
        )
        return cls(
            hypothesis_id=hypothesis_id.strip(),
            hypothesis_family_id=family_id,
            content_hash=content_hash,
            test_family=test_family.strip(),
            primary_metric=primary_metric.strip(),
            alpha=float(alpha),
            q=float(q),
            minimum_effect_size=float(minimum_effect_size),
            max_hypotheses_tests=int(max_hypotheses_tests),
            schema_version=schema_version.strip(),
            git_commit=git_commit.strip(),
            datasets=tuple(datasets),
            _family_definition_json=family_json,
            _parameters_json=parameters_json,
            created_at=datetime.now(timezone.utc).isoformat(),
        )

    @property
    def family_definition(self) -> dict[str, Any]:
        return json.loads(self._family_definition_json)

    @property
    def parameters(self) -> dict[str, Any]:
        return json.loads(self._parameters_json)

    @property
    def content_definition(self) -> dict[str, Any]:
        return _content_definition(
            family_definition=self.family_definition,
            test_family=self.test_family,
            primary_metric=self.primary_metric,
            parameters=self.parameters,
        )

    def payload(self) -> dict[str, Any]:
        return {
            "hypothesis_id": self.hypothesis_id,
            "hypothesis_family_id": self.hypothesis_family_id,
            "content_hash": self.content_hash,
            "family_definition": self.family_definition,
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


def verify_frozen_manifest(
    path: str | Path,
    *,
    expected_manifest_hash: str,
    verify_datasets: bool = True,
) -> dict[str, Any]:
    """Verify a frozen manifest against an externally persisted registry hash."""
    expected_manifest_hash = expected_manifest_hash.strip().lower()
    if len(expected_manifest_hash) != 64 or any(
        char not in "0123456789abcdef" for char in expected_manifest_hash
    ):
        raise ValueError("expected_manifest_hash must be a SHA-256 hex digest")

    source = Path(path)
    try:
        document = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ManifestIntegrityError(f"cannot read frozen manifest: {source}") from exc
    if not isinstance(document, dict) or not isinstance(document.get("manifest"), dict):
        raise ManifestIntegrityError("invalid frozen manifest document")

    payload = document["manifest"]
    stored = document.get("manifest_hash")
    actual = hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()
    if stored != actual:
        raise ManifestIntegrityError("frozen manifest self-hash mismatch")
    if actual != expected_manifest_hash:
        raise ManifestIntegrityError("frozen manifest does not match externally frozen manifest_hash")

    try:
        family_definition = payload["family_definition"]
        if not isinstance(family_definition, dict) or not family_definition:
            raise TypeError
        parameters = payload["parameters"]
        if not isinstance(parameters, dict):
            raise TypeError
        derived_family_id = derive_hypothesis_family_id(family_definition)
        derived_content_hash = derive_content_hash(
            _content_definition(
                family_definition=family_definition,
                test_family=str(payload["test_family"]),
                primary_metric=str(payload["primary_metric"]),
                parameters=parameters,
            )
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ManifestIntegrityError("manifest identity fields are invalid") from exc
    if payload.get("hypothesis_family_id") != derived_family_id:
        raise ManifestIntegrityError("manifest hypothesis_family_id is not derived from family_definition")
    if payload.get("content_hash") != derived_content_hash:
        raise ManifestIntegrityError("manifest content_hash is not derived from frozen hypothesis content")

    raw_datasets = payload.get("datasets")
    if not isinstance(raw_datasets, list) or not raw_datasets:
        raise ManifestIntegrityError("manifest must contain at least one dataset artifact")
    if verify_datasets:
        for raw in raw_datasets:
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
