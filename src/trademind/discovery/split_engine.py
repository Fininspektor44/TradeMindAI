"""Chronological split planning that never returns final-holdout rows."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Sequence, TypeVar

T = TypeVar("T")


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("split timestamps must be timezone-aware")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class SplitPlan:
    total_rows: int
    discovery_count: int
    validation_count: int
    holdout_count: int
    discovery_start: str
    discovery_end: str
    validation_start: str
    validation_end: str
    holdout_start: str
    holdout_end: str

    @property
    def public_count(self) -> int:
        return self.discovery_count + self.validation_count


def chronological_split(
    timestamps: Sequence[datetime],
    *,
    discovery_fraction: float = 0.60,
    validation_fraction: float = 0.20,
    holdout_fraction: float = 0.20,
) -> SplitPlan:
    if not timestamps:
        raise ValueError("cannot split an empty dataset")
    total_fraction = discovery_fraction + validation_fraction + holdout_fraction
    if abs(total_fraction - 1.0) > 1e-9:
        raise ValueError("split fractions must sum to 1.0")
    if min(discovery_fraction, validation_fraction, holdout_fraction) <= 0:
        raise ValueError("all split fractions must be positive")

    ordered = [_utc(value) for value in timestamps]
    if ordered != sorted(ordered) or len(set(ordered)) != len(ordered):
        raise ValueError("timestamps must be strictly increasing")

    total = len(ordered)
    discovery_count = int(total * discovery_fraction)
    validation_count = int(total * validation_fraction)
    holdout_count = total - discovery_count - validation_count
    if min(discovery_count, validation_count, holdout_count) < 1:
        raise ValueError("dataset is too small for three non-empty chronological segments")

    validation_start_index = discovery_count
    holdout_start_index = discovery_count + validation_count
    return SplitPlan(
        total_rows=total,
        discovery_count=discovery_count,
        validation_count=validation_count,
        holdout_count=holdout_count,
        discovery_start=ordered[0].isoformat(),
        discovery_end=ordered[discovery_count - 1].isoformat(),
        validation_start=ordered[validation_start_index].isoformat(),
        validation_end=ordered[holdout_start_index - 1].isoformat(),
        holdout_start=ordered[holdout_start_index].isoformat(),
        holdout_end=ordered[-1].isoformat(),
    )


def public_rows(rows: Sequence[T], plan: SplitPlan) -> list[T]:
    """Return only discovery+validation rows. Final holdout is intentionally omitted."""
    if len(rows) != plan.total_rows:
        raise ValueError("row count does not match frozen split plan")
    return list(rows[: plan.public_count])
