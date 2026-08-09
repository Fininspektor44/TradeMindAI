"""Tamper-evident append-only result ledger for Discovery Engine."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class LedgerIntegrityError(RuntimeError):
    pass


_GENESIS = "TRADEMIND_DISCOVERY_LEDGER_V1"


def _canonical(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class ResultLedger:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.meta_path = self.path.with_suffix(self.path.suffix + ".head.json")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists() and not self.meta_path.exists():
            self.path.touch()
            self._write_meta({"head_hash": _GENESIS, "record_count": 0})
        elif self.path.exists() != self.meta_path.exists():
            raise LedgerIntegrityError("ledger file and head anchor must exist together")

    def _write_meta(self, payload: dict[str, Any]) -> None:
        fd, temporary_name = tempfile.mkstemp(prefix=".ledger-head-", dir=self.path.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(_canonical(payload) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.meta_path)
        finally:
            if temporary.exists():
                temporary.unlink()

    def _read_meta(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.meta_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise LedgerIntegrityError("cannot read ledger head anchor") from exc
        if not isinstance(payload, dict):
            raise LedgerIntegrityError("invalid ledger head anchor")
        return payload

    def verify(self) -> bool:
        try:
            meta = self._read_meta()
            expected_prev = _GENESIS
            count = 0
            with self.path.open("r", encoding="utf-8") as handle:
                for raw in handle:
                    if not raw.strip():
                        continue
                    record = json.loads(raw)
                    if not isinstance(record, dict):
                        return False
                    record_hash = record.get("record_hash")
                    body = {key: value for key, value in record.items() if key != "record_hash"}
                    if body.get("index") != count + 1:
                        return False
                    if body.get("prev_hash") != expected_prev:
                        return False
                    actual = hashlib.sha256(
                        (expected_prev + _canonical(body)).encode("utf-8")
                    ).hexdigest()
                    if record_hash != actual:
                        return False
                    expected_prev = actual
                    count += 1
            return (
                int(meta.get("record_count", -1)) == count
                and meta.get("head_hash") == expected_prev
            )
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError):
            return False

    def append(self, payload: dict[str, Any]) -> str:
        if not self.verify():
            raise LedgerIntegrityError("ledger integrity verification failed before append")
        meta = self._read_meta()
        index = int(meta["record_count"]) + 1
        prev_hash = str(meta["head_hash"])
        body = {
            "index": index,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "prev_hash": prev_hash,
            "payload": payload,
        }
        record_hash = hashlib.sha256(
            (prev_hash + _canonical(body)).encode("utf-8")
        ).hexdigest()
        record = {**body, "record_hash": record_hash}
        with self.path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(_canonical(record) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        self._write_meta({"head_hash": record_hash, "record_count": index})
        return record_hash
