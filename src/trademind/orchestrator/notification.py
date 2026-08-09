"""Notification contracts for human-by-exception orchestration."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol


class NotificationKind(StrEnum):
    INFO_DIGEST = "INFO_DIGEST"
    TASK_FAILED = "TASK_FAILED"
    HUMAN_REQUIRED = "HUMAN_REQUIRED"
    BUDGET_ALERT = "BUDGET_ALERT"
    SECURITY_ALERT = "SECURITY_ALERT"


@dataclass(frozen=True, slots=True)
class Notification:
    kind: NotificationKind
    message: str
    task_id: str | None = None
    revision: int | None = None
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.message.strip():
            raise ValueError("notification message must not be empty")
        if self.revision is not None and self.revision < 1:
            raise ValueError("notification revision must be positive")


class NotificationSink(Protocol):
    def send(self, notification: Notification) -> None: ...


class NullNotificationSink:
    """Explicit no-op sink for unattended local operation and tests."""

    def send(self, notification: Notification) -> None:
        _ = notification


class JsonlFileNotificationSink:
    """Append notifications to a local JSONL file with flush+fsync durability."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def send(self, notification: Notification) -> None:
        payload = asdict(notification)
        payload["kind"] = notification.kind.value
        encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        with self.path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(encoded + "\n")
            handle.flush()
            os.fsync(handle.fileno())
