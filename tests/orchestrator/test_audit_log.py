import sqlite3
from datetime import datetime, timezone

from trademind.orchestrator.audit_log import AuditLog
from trademind.orchestrator.models import AuditEvent, Role


def _event(action: str) -> AuditEvent:
    return AuditEvent(
        timestamp=datetime.now(timezone.utc).isoformat(),
        task_id="T",
        revision=1,
        actor_role=Role.OPERATOR,
        action=action,
    )


def test_chain_detects_payload_tamper(tmp_path):
    path = tmp_path / "audit.db"
    log = AuditLog(path)
    log.append(_event("A"))
    log.append(_event("B"))
    assert log.verify()

    with sqlite3.connect(path) as db:
        db.execute("UPDATE audit_events SET payload='{}' WHERE id=1")
    assert not log.verify()


def test_chain_detects_tail_truncation(tmp_path):
    path = tmp_path / "audit.db"
    log = AuditLog(path)
    log.append(_event("A"))
    log.append(_event("B"))
    assert log.verify()

    with sqlite3.connect(path) as db:
        db.execute("DELETE FROM audit_events WHERE id=(SELECT MAX(id) FROM audit_events)")
    assert not log.verify()
