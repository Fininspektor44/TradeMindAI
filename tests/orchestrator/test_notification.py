import json

from trademind.orchestrator.notification import (
    JsonlFileNotificationSink,
    Notification,
    NotificationKind,
)


def test_jsonl_notification_sink_persists_structured_event(tmp_path):
    path = tmp_path / "notifications.jsonl"
    sink = JsonlFileNotificationSink(path)
    sink.send(
        Notification(
            kind=NotificationKind.HUMAN_REQUIRED,
            task_id="T1",
            revision=2,
            message="approval required",
            metadata={"reason": "budget"},
        )
    )

    rows = path.read_text(encoding="utf-8").splitlines()
    assert len(rows) == 1
    payload = json.loads(rows[0])
    assert payload["kind"] == "HUMAN_REQUIRED"
    assert payload["task_id"] == "T1"
    assert payload["revision"] == 2
    assert payload["metadata"]["reason"] == "budget"
