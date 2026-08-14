"""Chronological split planning that never returns final-holdout rows."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Sequence, TypeVar

from trademind.signal_statistics_provenance import (
    MAX_JSON_INTEGER_ABS,
    ProvenanceError,
    canonical_json_bytes,
    freeze_json_object,
    sha256_bytes,
)

T = TypeVar("T")

SPLIT_PLAN_SCHEMA_VERSION = "discovery-split-plan-v1"
_SPLIT_PLAN_HASH_DOMAIN = b"trademind:discovery:split-plan:v1"
_SPLIT_PLAN_FIELDS = frozenset(
    {
        "schema_version",
        "total_rows",
        "discovery_count",
        "validation_count",
        "holdout_count",
        "discovery_start",
        "discovery_end",
        "validation_start",
        "validation_end",
        "holdout_start",
        "holdout_end",
    }
)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("split timestamps must be timezone-aware")
    return value.astimezone(timezone.utc)


def _canonical_utc(value: object, *, field_name: str) -> str:
    if type(value) is not str:
        raise ValueError(f"{field_name} must be an exact canonical UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an exact canonical UTC timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValueError(f"{field_name} must include the UTC timezone")
    if parsed.isoformat() != value:
        raise ValueError(f"{field_name} must use canonical datetime.isoformat() encoding")
    return value


def _positive_count(value: object, *, field_name: str) -> int:
    if type(value) is not int or not 1 <= value <= MAX_JSON_INTEGER_ABS:
        raise ValueError(f"{field_name} must be an exact bounded positive integer")
    return value


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

    def __post_init__(self) -> None:
        counts = {
            "total_rows": self.total_rows,
            "discovery_count": self.discovery_count,
            "validation_count": self.validation_count,
            "holdout_count": self.holdout_count,
        }
        for field_name, value in counts.items():
            _positive_count(value, field_name=field_name)
        if self.discovery_count + self.validation_count + self.holdout_count != self.total_rows:
            raise ValueError("split segment counts must exactly equal total_rows")

        timestamps = {
            "discovery_start": self.discovery_start,
            "discovery_end": self.discovery_end,
            "validation_start": self.validation_start,
            "validation_end": self.validation_end,
            "holdout_start": self.holdout_start,
            "holdout_end": self.holdout_end,
        }
        parsed: dict[str, datetime] = {}
        for field_name, value in timestamps.items():
            canonical = _canonical_utc(value, field_name=field_name)
            parsed[field_name] = datetime.fromisoformat(canonical)
        if not (
            parsed["discovery_start"]
            <= parsed["discovery_end"]
            < parsed["validation_start"]
            <= parsed["validation_end"]
            < parsed["holdout_start"]
            <= parsed["holdout_end"]
        ):
            raise ValueError(
                "split must order discovery strictly before validation and final holdout"
            )
        canonical_json_bytes(self.to_payload())

    @property
    def public_count(self) -> int:
        return self.discovery_count + self.validation_count

    def to_payload(self) -> dict[str, object]:
        """Serialize only boundaries/counts; final-holdout observations are never exposed."""
        return {
            "schema_version": SPLIT_PLAN_SCHEMA_VERSION,
            "total_rows": self.total_rows,
            "discovery_count": self.discovery_count,
            "validation_count": self.validation_count,
            "holdout_count": self.holdout_count,
            "discovery_start": self.discovery_start,
            "discovery_end": self.discovery_end,
            "validation_start": self.validation_start,
            "validation_end": self.validation_end,
            "holdout_start": self.holdout_start,
            "holdout_end": self.holdout_end,
        }

    @property
    def semantic_hash(self) -> str:
        return sha256_bytes(
            _SPLIT_PLAN_HASH_DOMAIN + b"\x00" + canonical_json_bytes(self.to_payload())
        )

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> SplitPlan:
        try:
            frozen = freeze_json_object(payload, field_name="split_plan")
        except ProvenanceError as exc:
            raise ValueError("split_plan must be strict bounded JSON") from exc
        fields = frozenset(frozen)
        missing = _SPLIT_PLAN_FIELDS - fields
        unknown = fields - _SPLIT_PLAN_FIELDS
        if missing:
            raise ValueError(f"split_plan is missing fields: {', '.join(sorted(missing))}")
        if unknown:
            raise ValueError(f"split_plan contains unknown fields: {', '.join(sorted(unknown))}")
        if frozen["schema_version"] != SPLIT_PLAN_SCHEMA_VERSION:
            raise ValueError("unsupported split_plan schema_version")
        return cls(
            total_rows=frozen["total_rows"],
            discovery_count=frozen["discovery_count"],
            validation_count=frozen["validation_count"],
            holdout_count=frozen["holdout_count"],
            discovery_start=frozen["discovery_start"],
            discovery_end=frozen["discovery_end"],
            validation_start=frozen["validation_start"],
            validation_end=frozen["validation_end"],
            holdout_start=frozen["holdout_start"],
            holdout_end=frozen["holdout_end"],
        )


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
