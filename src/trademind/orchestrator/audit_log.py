"""Tamper-evident SQLite audit log for orchestrator decisions."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict
from pathlib import Path

from .models import AuditEvent

_GENESIS = "TRADEMIND_ORCHESTRATOR_AUDIT_V1"


def _canonical(payload: dict) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class AuditLog:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def _init_schema(self) -> None:
        with self._connect() as db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    payload TEXT NOT NULL,
                    prev_hash TEXT NOT NULL,
                    record_hash TEXT NOT NULL UNIQUE
                )
                """
            )

    def append(self, event: AuditEvent) -> str:
        payload = asdict(event)
        for key in ("actor_role", "from_state", "to_state", "policy_result"):
            value = payload.get(key)
            if value is not None:
                payload[key] = value.value
        payload_text = _canonical(payload)
        with self._connect() as db:
            row = db.execute(
                "SELECT record_hash FROM audit_events ORDER BY id DESC LIMIT 1"
            ).fetchone()
            prev_hash = row["record_hash"] if row else _GENESIS
            record_hash = hashlib.sha256(
                (prev_hash + payload_text).encode("utf-8")
            ).hexdigest()
            db.execute(
                "INSERT INTO audit_events(payload, prev_hash, record_hash) VALUES (?, ?, ?)",
                (payload_text, prev_hash, record_hash),
            )
        return record_hash

    def verify(self) -> bool:
        expected_prev = _GENESIS
        with self._connect() as db:
            rows = db.execute(
                "SELECT payload, prev_hash, record_hash FROM audit_events ORDER BY id ASC"
            ).fetchall()
        for row in rows:
            if row["prev_hash"] != expected_prev:
                return False
            expected_hash = hashlib.sha256(
                (expected_prev + row["payload"]).encode("utf-8")
            ).hexdigest()
            if row["record_hash"] != expected_hash:
                return False
            expected_prev = row["record_hash"]
        return True
