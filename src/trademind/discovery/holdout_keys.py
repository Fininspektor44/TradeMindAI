"""External key-provider contract for protected final holdouts.

Keys are injected into the trusted sealer/runner process. They are never persisted
in the repository, manifest, result ledger, or Orchestrator task artifacts.
"""

from __future__ import annotations

import base64
import binascii
import os
from dataclasses import dataclass
from typing import Protocol


class HoldoutKeyError(RuntimeError):
    """Raised when protected holdout key material is unavailable or invalid."""


class HoldoutKeyProvider(Protocol):
    """Minimal replaceable interface for an external secret store."""

    def load_key(self, key_id: str) -> bytes: ...


def decode_aes256_key(encoded: str) -> bytes:
    """Decode one base64-encoded AES-256 key and require exactly 32 bytes."""
    text = encoded.strip()
    if not text:
        raise HoldoutKeyError("holdout key material is empty")
    try:
        key = base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HoldoutKeyError("holdout key must be strict base64") from exc
    if len(key) != 32:
        raise HoldoutKeyError("holdout key must decode to exactly 32 bytes")
    return key


@dataclass(frozen=True, slots=True)
class EnvironmentKeyProvider:
    """Load one named key from an explicitly configured environment variable.

    This adapter is intentionally narrow. The environment variable must be
    injected only into the isolated sealer/runner process. It is not a replacement
    for an OS/cloud secret manager when stronger host isolation is required.
    """

    key_id: str
    environment_variable: str

    def __post_init__(self) -> None:
        if not self.key_id.strip():
            raise ValueError("key_id must not be empty")
        if not self.environment_variable.strip():
            raise ValueError("environment_variable must not be empty")

    def load_key(self, key_id: str) -> bytes:
        if key_id != self.key_id:
            raise HoldoutKeyError("requested holdout key_id is not configured")
        encoded = os.environ.get(self.environment_variable)
        if encoded is None:
            raise HoldoutKeyError("configured holdout key environment variable is missing")
        return decode_aes256_key(encoded)
