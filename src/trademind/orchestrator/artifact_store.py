"""Content-addressed local artifact storage for orchestrator evidence."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9_.-]+$")


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
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _safe(value: str, label: str) -> str:
        if not value or not _SAFE_COMPONENT.fullmatch(value):
            raise ValueError(f"unsafe {label}: {value!r}")
        return value

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
        return len(data) == artifact.size_bytes and hashlib.sha256(data).hexdigest() == artifact.sha256
