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


def _event_payload(event: AuditEvent) -> str:
    payload = asdict(event)
    for key in ("actor_role", "from_state", "to_state", "policy_result"):
        value = payload.get(key)
        if value is not None:
            payload[key] = value.value
    return _canonical(payload)


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
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    payload TEXT NOT NULL,
                    prev_hash TEXT NOT NULL,
                    record_hash TEXT NOT NULL UNIQUE
                );
                CREATE TABLE IF NOT EXISTS audit_meta (
                    id INTEGER PRIMARY KEY CHECK(id = 1),
                    head_hash TEXT NOT NULL,
                    event_count INTEGER NOT NULL
                );
                """
            )
            db.execute(
                "INSERT OR IGNORE INTO audit_meta(id, head_hash, event_count) VALUES (1, ?, 0)",
                (_GENESIS,),
            )

    @staticmethod
    def verify_connection(db: sqlite3.Connection) -> bool:
        expected_prev = _GENESIS
        rows = db.execute(
            "SELECT payload, prev_hash, record_hash FROM audit_events ORDER BY id ASC"
        ).fetchall()
        meta = db.execute(
            "SELECT head_hash, event_count FROM audit_meta WHERE id=1"
        ).fetchone()

        if meta is None:
            return False
        for row in rows:
            if row["prev_hash"] != expected_prev:
                return False
            expected_hash = hashlib.sha256(
                (expected_prev + row["payload"]).encode("utf-8")
            ).hexdigest()
            if row["record_hash"] != expected_hash:
                return False
            expected_prev = row["record_hash"]

        return int(meta["event_count"]) == len(rows) and meta["head_hash"] == expected_prev

    @staticmethod
    def append_in_transaction(db: sqlite3.Connection, event: AuditEvent) -> str:
        """Append using an existing SQLite transaction without committing it."""
        if not AuditLog.verify_connection(db):
            raise RuntimeError("audit log integrity verification failed before append")

        payload_text = _event_payload(event)
        meta = db.execute(
            "SELECT head_hash, event_count FROM audit_meta WHERE id=1"
        ).fetchone()
        prev_hash = meta["head_hash"]
        record_hash = hashlib.sha256((prev_hash + payload_text).encode("utf-8")).hexdigest()
        db.execute(
            "INSERT INTO audit_events(payload, prev_hash, record_hash) VALUES (?, ?, ?)",
            (payload_text, prev_hash, record_hash),
        )
        db.execute(
            "UPDATE audit_meta SET head_hash=?, event_count=? WHERE id=1",
            (record_hash, int(meta["event_count"]) + 1),
        )
        return record_hash

    def append(self, event: AuditEvent) -> str:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            return self.append_in_transaction(db, event)

    def verify(self) -> bool:
        with self._connect() as db:
            return self.verify_connection(db)
