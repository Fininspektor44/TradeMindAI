"""Machine-verifiable binding between a dataset artifact, its declared split
role, and the exact source dataset a ``SplitPlan`` was computed from.

Closes a gap ``DatasetArtifactV2`` (discovery/manifest.py) deliberately left
open: ``DatasetArtifactV2.role`` is only a caller-supplied label. Nothing
about it, on its own, proves a CAS artifact claiming ``role="DISCOVERY"`` or
``role="VALIDATION"`` does not actually contain final-holdout rows -- a
caller could label the full, unsplit source dataset (which legitimately
contains every row, including the future holdout range) as a "TRAIN"
artifact and nothing would object.

This module adds two purely additive, content-addressed primitives:

  BoundSplitPlanV1
    Binds an immutable ``SplitPlan`` (discovery/split_engine.py, unchanged)
    to the exact SHA-256 of the source dataset file it was computed from.
    ``SplitPlan.semantic_hash`` alone is a pure *shape* (row counts +
    boundary timestamps) -- it says nothing about which underlying file
    produced it. ``BoundSplitPlanV1.bound_split_plan_hash`` closes that by
    hashing ``{source_dataset_sha256, source_dataset_size_bytes,
    split_plan.semantic_hash}`` together, without modifying SplitPlan.

  DatasetSplitMembershipV1 / verify_dataset_split_membership
    Machine-verified proof that a specific CAS dataset artifact's ACTUAL row
    content -- read from Verified CAS, never trusted from a label -- lies
    entirely within one role's declared time boundary of a bound split
    plan, and strictly before the final-holdout boundary. ``FINAL_HOLDOUT``
    (or any name resembling it) is never a legal role for this contract:
    binding final-holdout rows through this plain-CAS path is refused
    structurally, not merely discouraged. This never reads, decrypts, or
    references the sealed final-holdout artifact
    (discovery/holdout_store.py / holdout_sealer.py / holdout_crypto.py are
    not imported here) -- only the PUBLIC boundary timestamps already
    present in ``SplitPlan.to_payload()`` (discovery_end/validation_start/
    validation_end/holdout_start) are used, which the split engine already
    treats as non-secret (only holdout ROW CONTENT is withheld by
    ``public_rows``, never the boundary timestamps themselves).

Neither ``derive_content_hash``, hypothesis identity, Research Proposal
Intake, nor Research Experiment Specification is touched by this module.
``DatasetArtifactV2``/``ExperimentManifestV2`` (discovery/manifest.py) are
also unmodified: this is an additive utility a caller uses to decide which
``artifact_hash_ref``/``role`` pair is safe to construct a
``DatasetArtifactV2`` from, not a change to that dataclass's own schema.

No persistence (no new SQLite table) is introduced: both artifacts here are
pure, deterministic, re-derivable values, exactly like ``ExperimentManifestV2``
itself, so idempotency/concurrency are structural (content-addressed
recomputation), not something a database transaction needs to arbitrate.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from trademind.orchestrator.artifact_store import ArtifactStore
from trademind.signal_statistics_provenance import (
    ProvenanceError,
    canonical_json_bytes,
    freeze_json_object,
    sha256_bytes,
    validate_sha256_ref,
)

from .data_layer import sha256_file
from .split_engine import SplitPlan

BOUND_SPLIT_PLAN_SCHEMA_VERSION = "discovery-bound-split-plan-v1"
DATASET_SPLIT_MEMBERSHIP_SCHEMA_VERSION = "discovery-dataset-split-membership-v1"

_BOUND_SPLIT_PLAN_HASH_DOMAIN = b"trademind:discovery:bound-split-plan:v1"
_DATASET_SPLIT_MEMBERSHIP_HASH_DOMAIN = b"trademind:discovery:dataset-split-membership:v1"

# FINAL_HOLDOUT/HOLDOUT is deliberately absent: no name resembling it is ever
# a legal role for this contract, structurally, regardless of caller intent.
DATASET_SPLIT_ROLES = ("DISCOVERY", "VALIDATION")

_BOUND_SPLIT_PLAN_FIELDS = frozenset(
    {
        "schema_version",
        "source_dataset_sha256",
        "source_dataset_size_bytes",
        "split_plan_semantic_hash",
        "split_plan",
    }
)
_DATASET_SPLIT_MEMBERSHIP_FIELDS = frozenset(
    {
        "schema_version",
        "bound_split_plan_hash",
        "role",
        "artifact_hash_ref",
        "artifact_media_type",
        "artifact_size_bytes",
        "row_count",
    }
)


class DatasetProvenanceError(ValueError):
    """Raised when a bound split plan or dataset split membership claim is
    malformed, internally inconsistent, or fails verification."""


def _sha256_hex(value: object, *, field_name: str) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise DatasetProvenanceError(f"{field_name} must be 64 lowercase hexadecimal digits")
    return value


def _positive_int(value: object, *, field_name: str) -> int:
    if type(value) is not int or type(value) is bool or value < 1:
        raise DatasetProvenanceError(f"{field_name} must be an exact positive integer")
    return value


@dataclass(frozen=True, slots=True)
class BoundSplitPlanV1:
    """Binds an immutable ``SplitPlan`` to the exact source dataset file it
    was computed from. Construct via :func:`bind_split_plan_to_source`."""

    source_dataset_sha256: str
    source_dataset_size_bytes: int
    split_plan: SplitPlan
    schema_version: str = BOUND_SPLIT_PLAN_SCHEMA_VERSION
    bound_split_plan_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != BOUND_SPLIT_PLAN_SCHEMA_VERSION:
            raise DatasetProvenanceError("unsupported bound split plan schema_version")
        _sha256_hex(self.source_dataset_sha256, field_name="source_dataset_sha256")
        _positive_int(self.source_dataset_size_bytes, field_name="source_dataset_size_bytes")
        if type(self.split_plan) is not SplitPlan:
            raise DatasetProvenanceError("split_plan must be an exact SplitPlan")
        object.__setattr__(
            self,
            "bound_split_plan_hash",
            sha256_bytes(
                _BOUND_SPLIT_PLAN_HASH_DOMAIN + b"\x00" + canonical_json_bytes(self.to_payload())
            ),
        )

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "source_dataset_sha256": self.source_dataset_sha256,
            "source_dataset_size_bytes": self.source_dataset_size_bytes,
            "split_plan_semantic_hash": self.split_plan.semantic_hash,
            "split_plan": self.split_plan.to_payload(),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> BoundSplitPlanV1:
        try:
            frozen = freeze_json_object(payload, field_name="bound_split_plan")
        except ProvenanceError as exc:
            raise DatasetProvenanceError("bound_split_plan must be strict bounded JSON") from exc
        fields = frozenset(frozen)
        missing = _BOUND_SPLIT_PLAN_FIELDS - fields
        unknown = fields - _BOUND_SPLIT_PLAN_FIELDS
        if missing:
            raise DatasetProvenanceError(f"bound_split_plan is missing fields: {', '.join(sorted(missing))}")
        if unknown:
            raise DatasetProvenanceError(
                f"bound_split_plan contains unknown fields: {', '.join(sorted(unknown))}"
            )
        raw_plan = frozen["split_plan"]
        if not isinstance(raw_plan, Mapping):
            raise DatasetProvenanceError("bound_split_plan.split_plan must be a JSON object")
        try:
            plan = SplitPlan.from_payload(raw_plan)
        except ValueError as exc:
            raise DatasetProvenanceError(f"bound_split_plan.split_plan is invalid: {exc}") from exc
        built = cls(
            source_dataset_sha256=frozen["source_dataset_sha256"],
            source_dataset_size_bytes=frozen["source_dataset_size_bytes"],
            split_plan=plan,
        )
        try:
            claimed_split_hash = validate_sha256_ref(frozen["split_plan_semantic_hash"])
        except ProvenanceError as exc:
            raise DatasetProvenanceError("bound_split_plan.split_plan_semantic_hash is invalid") from exc
        if claimed_split_hash != plan.semantic_hash:
            raise DatasetProvenanceError("bound_split_plan split identity mismatch")
        return built


def bind_split_plan_to_source(
    source_dataset_path: str,
    *,
    split_plan: SplitPlan,
) -> BoundSplitPlanV1:
    """Bind ``split_plan`` to the exact current SHA-256/size of a local
    source dataset file. Reads only metadata (hash, size) -- row content is
    never inspected here."""
    resolved = Path(source_dataset_path)
    if not resolved.is_file():
        raise DatasetProvenanceError(f"source dataset not found: {resolved}")
    return BoundSplitPlanV1(
        source_dataset_sha256=sha256_file(resolved),
        source_dataset_size_bytes=resolved.stat().st_size,
        split_plan=split_plan,
    )


@dataclass(frozen=True, slots=True)
class DatasetSplitMembershipV1:
    """Machine-verified proof that ``artifact_hash_ref``'s actual row content
    lies entirely within ``role``'s time boundary of ``bound_split_plan_hash``,
    and strictly before the final-holdout boundary. Construct only via
    :func:`verify_dataset_split_membership` -- never by hand -- since the
    proof is only meaningful if it was produced by actually reading and
    checking the artifact's rows."""

    bound_split_plan_hash: str
    role: str
    artifact_hash_ref: str
    artifact_media_type: str
    artifact_size_bytes: int
    row_count: int
    schema_version: str = DATASET_SPLIT_MEMBERSHIP_SCHEMA_VERSION
    membership_identity: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != DATASET_SPLIT_MEMBERSHIP_SCHEMA_VERSION:
            raise DatasetProvenanceError("unsupported dataset split membership schema_version")
        try:
            validate_sha256_ref(self.bound_split_plan_hash)
        except ProvenanceError as exc:
            raise DatasetProvenanceError("bound_split_plan_hash is invalid") from exc
        if self.role not in DATASET_SPLIT_ROLES:
            raise DatasetProvenanceError(
                f"role must be one of {DATASET_SPLIT_ROLES}; final-holdout-shaped roles "
                "are never legal for this contract"
            )
        try:
            validate_sha256_ref(self.artifact_hash_ref)
        except ProvenanceError as exc:
            raise DatasetProvenanceError("artifact_hash_ref is invalid") from exc
        if type(self.artifact_media_type) is not str or not self.artifact_media_type.strip():
            raise DatasetProvenanceError("artifact_media_type must be a non-empty string")
        _positive_int(self.artifact_size_bytes, field_name="artifact_size_bytes")
        _positive_int(self.row_count, field_name="row_count")
        object.__setattr__(
            self,
            "membership_identity",
            sha256_bytes(
                _DATASET_SPLIT_MEMBERSHIP_HASH_DOMAIN
                + b"\x00"
                + canonical_json_bytes(self.to_payload())
            ),
        )

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "bound_split_plan_hash": self.bound_split_plan_hash,
            "role": self.role,
            "artifact_hash_ref": self.artifact_hash_ref,
            "artifact_media_type": self.artifact_media_type,
            "artifact_size_bytes": self.artifact_size_bytes,
            "row_count": self.row_count,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> DatasetSplitMembershipV1:
        try:
            frozen = freeze_json_object(payload, field_name="dataset_split_membership")
        except ProvenanceError as exc:
            raise DatasetProvenanceError(
                "dataset_split_membership must be strict bounded JSON"
            ) from exc
        fields = frozenset(frozen)
        missing = _DATASET_SPLIT_MEMBERSHIP_FIELDS - fields
        unknown = fields - _DATASET_SPLIT_MEMBERSHIP_FIELDS
        if missing:
            raise DatasetProvenanceError(
                f"dataset_split_membership is missing fields: {', '.join(sorted(missing))}"
            )
        if unknown:
            raise DatasetProvenanceError(
                f"dataset_split_membership contains unknown fields: {', '.join(sorted(unknown))}"
            )
        return cls(
            bound_split_plan_hash=frozen["bound_split_plan_hash"],
            role=frozen["role"],
            artifact_hash_ref=frozen["artifact_hash_ref"],
            artifact_media_type=frozen["artifact_media_type"],
            artifact_size_bytes=frozen["artifact_size_bytes"],
            row_count=frozen["row_count"],
        )


def _role_time_bounds(split_plan: SplitPlan, role: str) -> tuple[datetime, datetime]:
    if role == "DISCOVERY":
        return (
            datetime.fromisoformat(split_plan.discovery_start),
            datetime.fromisoformat(split_plan.discovery_end),
        )
    if role == "VALIDATION":
        return (
            datetime.fromisoformat(split_plan.validation_start),
            datetime.fromisoformat(split_plan.validation_end),
        )
    raise DatasetProvenanceError(
        f"role must be one of {DATASET_SPLIT_ROLES}; final-holdout-shaped roles "
        "are never legal for this contract"
    )


def _extract_row_times(exact_bytes: bytes, *, time_column: str) -> list[datetime]:
    """Read only the declared time column of a CSV artifact. Reuses the same
    strict-ISO-8601 convention discovery/data_layer.py's
    ImmutableHistoricalDatasetReader already uses for the identical purpose
    -- no new timestamp schema is invented. Never inspects any other column,
    and never writes the bytes anywhere."""
    try:
        text = exact_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DatasetProvenanceError("dataset artifact must be valid UTF-8 CSV") from exc
    reader = csv.DictReader(io.StringIO(text))
    columns = frozenset(reader.fieldnames or ())
    if time_column not in columns:
        raise DatasetProvenanceError(f"dataset artifact is missing required column: {time_column!r}")
    times: list[datetime] = []
    for row in reader:
        raw = (row.get(time_column) or "").strip()
        if not raw:
            raise DatasetProvenanceError(f"dataset artifact has an empty {time_column!r} value")
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError as exc:
            raise DatasetProvenanceError(
                f"dataset artifact {time_column!r} value is not a valid ISO-8601 timestamp: {raw!r}"
            ) from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise DatasetProvenanceError(f"dataset artifact {time_column!r} values must be timezone-aware")
        times.append(parsed.astimezone(timezone.utc))
    if not times:
        raise DatasetProvenanceError("dataset artifact contains no rows to verify")
    return times


def verify_dataset_split_membership(
    *,
    artifact_store: ArtifactStore,
    bound_split_plan: BoundSplitPlanV1,
    role: str,
    artifact_hash_ref: str,
    media_type: str = "text/csv",
    time_column: str = "time",
) -> DatasetSplitMembershipV1:
    """Prove ``artifact_hash_ref``'s actual row content lies entirely within
    ``role``'s declared boundary of ``bound_split_plan``, by reading the
    artifact from Verified CAS and checking every row's own timestamp.

    Fails closed (raises DatasetProvenanceError) if: the role is not one of
    DATASET_SPLIT_ROLES (FINAL_HOLDOUT-shaped roles are never legal here);
    the artifact hash/media type does not verify in CAS (ArtifactStore's own
    guarantee, reused unchanged); the artifact is empty, malformed, or
    missing the declared time column; or ANY row's timestamp falls outside
    the role's boundary or at/after the final-holdout boundary -- this is
    what makes a caller-forged role label, a renamed holdout-range file, or
    the full unsplit source dataset all fail identically, regardless of
    filename, path, or the caller's own claim.
    """
    if type(bound_split_plan) is not BoundSplitPlanV1:
        raise DatasetProvenanceError("bound_split_plan must be an exact BoundSplitPlanV1")
    lower, upper = _role_time_bounds(bound_split_plan.split_plan, role)
    # SplitPlan.__post_init__ (closed, unmodified) already enforces
    # discovery_end < validation_start <= validation_end < holdout_start
    # strictly, so [lower, upper] for DISCOVERY/VALIDATION can never itself
    # reach holdout_start. The explicit check below is therefore always
    # redundant given today's two legal roles -- it is kept as
    # defense-in-depth against a future role/bound_split_plan change, not
    # because it is expected to ever fire; the [lower, upper] check above is
    # what actually rejects every scenario this contract targets.
    holdout_start = datetime.fromisoformat(bound_split_plan.split_plan.holdout_start)

    artifact = artifact_store.resolve_verified(artifact_hash_ref, expected_media_type=media_type)
    exact_bytes = artifact_store.read_verified(artifact_hash_ref, expected_media_type=media_type)

    times = _extract_row_times(exact_bytes, time_column=time_column)
    for observed in times:
        if not (lower <= observed <= upper):
            raise DatasetProvenanceError(
                f"dataset artifact row time {observed.isoformat()} is outside the declared "
                f"{role} boundary [{lower.isoformat()}, {upper.isoformat()}]"
            )
        if observed >= holdout_start:  # pragma: no cover -- see comment above.
            raise DatasetProvenanceError(
                f"dataset artifact row time {observed.isoformat()} overlaps or is at/after "
                "the final-holdout boundary"
            )

    return DatasetSplitMembershipV1(
        bound_split_plan_hash=bound_split_plan.bound_split_plan_hash,
        role=role,
        artifact_hash_ref=artifact.hash_ref,
        artifact_media_type=artifact.media_type,
        artifact_size_bytes=artifact.size_bytes,
        row_count=len(times),
    )


__all__ = [
    "BOUND_SPLIT_PLAN_SCHEMA_VERSION",
    "DATASET_SPLIT_MEMBERSHIP_SCHEMA_VERSION",
    "DATASET_SPLIT_ROLES",
    "BoundSplitPlanV1",
    "DatasetProvenanceError",
    "DatasetSplitMembershipV1",
    "bind_split_plan_to_source",
    "verify_dataset_split_membership",
]
