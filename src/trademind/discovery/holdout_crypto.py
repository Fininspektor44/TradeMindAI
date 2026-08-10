"""Authenticated encryption envelope for final-holdout artifacts."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
from datetime import datetime, timezone
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class HoldoutCryptoError(RuntimeError):
    """Raised when a protected holdout envelope cannot be trusted."""


_SCHEMA_VERSION = "trademind-final-holdout-v1"
_CIPHER = "AES-256-GCM"
_NONCE_BYTES = 12
_KEY_BYTES = 32
_KEY_CHECK_DOMAIN = b"TRADEMIND_FINAL_HOLDOUT_KEY_CHECK_V1\x00"


def _canonical(payload: Any) -> bytes:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _validate_sha256(value: str, label: str) -> str:
    cleaned = value.strip().lower()
    if len(cleaned) != 64 or any(char not in "0123456789abcdef" for char in cleaned):
        raise ValueError(f"{label} must be a SHA-256 hex digest")
    return cleaned


def _validate_nonempty(value: str, label: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{label} must not be empty")
    return cleaned


def _validate_key(key: bytes) -> bytes:
    if not isinstance(key, bytes) or len(key) != _KEY_BYTES:
        raise ValueError("holdout encryption key must be exactly 32 bytes")
    return key


def _b64encode(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _b64decode(value: object, label: str) -> bytes:
    if not isinstance(value, str):
        raise HoldoutCryptoError(f"{label} must be base64 text")
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HoldoutCryptoError(f"{label} is not valid base64") from exc


def _body_hash(body: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical(body)).hexdigest()


def _key_check(key: bytes, header: dict[str, Any]) -> str:
    return hmac.new(key, _KEY_CHECK_DOMAIN + _canonical(header), hashlib.sha256).hexdigest()


def seal_bytes(
    plaintext: bytes,
    *,
    key: bytes,
    key_id: str,
    hypothesis_family_id: str,
    manifest_hash: str,
) -> dict[str, Any]:
    """Encrypt final-holdout bytes with AES-256-GCM and authenticated metadata."""
    if not isinstance(plaintext, bytes) or not plaintext:
        raise ValueError("final holdout plaintext must be non-empty bytes")
    key = _validate_key(key)
    key_id = _validate_nonempty(key_id, "key_id")
    family_id = _validate_nonempty(hypothesis_family_id, "hypothesis_family_id")
    manifest_hash = _validate_sha256(manifest_hash, "manifest_hash")

    header = {
        "schema_version": _SCHEMA_VERSION,
        "cipher": _CIPHER,
        "key_id": key_id,
        "hypothesis_family_id": family_id,
        "manifest_hash": manifest_hash,
        "plaintext_sha256": hashlib.sha256(plaintext).hexdigest(),
        "plaintext_size": len(plaintext),
        "sealed_at": datetime.now(timezone.utc).isoformat(),
    }
    nonce = os.urandom(_NONCE_BYTES)
    ciphertext = AESGCM(key).encrypt(nonce, plaintext, _canonical(header))
    body = {
        "header": header,
        "nonce_b64": _b64encode(nonce),
        "ciphertext_b64": _b64encode(ciphertext),
        "key_check": _key_check(key, header),
    }
    return {**body, "envelope_hash": _body_hash(body)}


def verify_envelope(document: dict[str, Any]) -> dict[str, Any]:
    """Validate envelope structure and unkeyed integrity before secret access."""
    if not isinstance(document, dict):
        raise HoldoutCryptoError("holdout envelope must be a JSON object")
    header = document.get("header")
    if not isinstance(header, dict):
        raise HoldoutCryptoError("holdout envelope header is missing")
    if header.get("schema_version") != _SCHEMA_VERSION:
        raise HoldoutCryptoError("unsupported holdout envelope schema")
    if header.get("cipher") != _CIPHER:
        raise HoldoutCryptoError("unsupported holdout cipher")

    key_id = header.get("key_id")
    family_id = header.get("hypothesis_family_id")
    manifest_hash = header.get("manifest_hash")
    plaintext_hash = header.get("plaintext_sha256")
    plaintext_size = header.get("plaintext_size")
    sealed_at = header.get("sealed_at")
    if not all(isinstance(value, str) and value.strip() for value in (key_id, family_id, sealed_at)):
        raise HoldoutCryptoError("holdout envelope identity fields are invalid")
    try:
        _validate_sha256(str(manifest_hash), "manifest_hash")
        _validate_sha256(str(plaintext_hash), "plaintext_sha256")
    except ValueError as exc:
        raise HoldoutCryptoError("holdout envelope hash fields are invalid") from exc
    if not isinstance(plaintext_size, int) or plaintext_size <= 0:
        raise HoldoutCryptoError("holdout plaintext_size is invalid")

    nonce = _b64decode(document.get("nonce_b64"), "nonce_b64")
    ciphertext = _b64decode(document.get("ciphertext_b64"), "ciphertext_b64")
    if len(nonce) != _NONCE_BYTES:
        raise HoldoutCryptoError("holdout nonce has invalid length")
    if len(ciphertext) <= 16:
        raise HoldoutCryptoError("holdout ciphertext is too short")
    key_check = document.get("key_check")
    envelope_hash = document.get("envelope_hash")
    if not isinstance(key_check, str) or len(key_check) != 64:
        raise HoldoutCryptoError("holdout key_check is invalid")
    if not isinstance(envelope_hash, str) or len(envelope_hash) != 64:
        raise HoldoutCryptoError("holdout envelope_hash is invalid")

    body = {
        "header": header,
        "nonce_b64": document["nonce_b64"],
        "ciphertext_b64": document["ciphertext_b64"],
        "key_check": key_check,
    }
    if not hmac.compare_digest(envelope_hash, _body_hash(body)):
        raise HoldoutCryptoError("holdout envelope hash mismatch")
    return document


def verify_key(document: dict[str, Any], key: bytes) -> None:
    """Verify the injected key against authenticated public metadata only."""
    verify_envelope(document)
    key = _validate_key(key)
    header = document["header"]
    expected = _key_check(key, header)
    if not hmac.compare_digest(str(document["key_check"]), expected):
        raise HoldoutCryptoError("holdout key does not match sealed artifact")


def decrypt_bytes(document: dict[str, Any], key: bytes) -> bytes:
    """Decrypt and authenticate a final holdout entirely in memory."""
    verify_key(document, key)
    header = document["header"]
    nonce = _b64decode(document["nonce_b64"], "nonce_b64")
    ciphertext = _b64decode(document["ciphertext_b64"], "ciphertext_b64")
    try:
        plaintext = AESGCM(key).decrypt(nonce, ciphertext, _canonical(header))
    except InvalidTag as exc:
        raise HoldoutCryptoError("holdout ciphertext authentication failed") from exc
    if len(plaintext) != int(header["plaintext_size"]):
        raise HoldoutCryptoError("holdout plaintext size mismatch after decrypt")
    actual = hashlib.sha256(plaintext).hexdigest()
    if not hmac.compare_digest(actual, str(header["plaintext_sha256"])):
        raise HoldoutCryptoError("holdout plaintext hash mismatch after decrypt")
    return plaintext
