"""Prospective Confirmation Protocol V1: a pre-registered, forward-only re-test.

The completed candidate validation for ``.USTECHCASH`` SELL (horizon=12)
passed its predeclared validation criterion but its one-shot final holdout
had too few non-overlapping trades (24 < the predeclared minimum of 30) to
be conclusive -- PROMISING / INCONCLUSIVE, not PASS or FAIL. That holdout is
now permanently spent; it can never be reopened or recounted. The only
scientifically sound way to learn more is to pre-register a fixed rule for
*new* data that has not existed yet, and evaluate it later without ever
touching what has already been observed.

This module is deliberately NOT another pipeline layer. It introduces no new
registry, no new canonicalization scheme, and no new CAS: the frozen
protocol reuses ``trademind.discovery.manifest.EvaluationCriterionV1`` for
its two criteria, ``trademind.signal_statistics_provenance``'s canonical
JSON/hash helpers for its own immutability (the exact pattern every closed
layer in this pipeline already uses), and the existing
``ArtifactStore`` (Verified CAS) for persistence. Evaluation reuses
``trademind.validation.validate_rows`` unchanged -- the same non-overlapping-
trade, ATR-normalized methodology used throughout. There is no
``HypothesisRegistry`` binding here: the one-shot discovery/validation/
holdout lifecycle for this candidate is already terminal
(``REJECTED_FINAL``), a closed book; a prospective protocol is a standing,
repeatable rule re-evaluated against a growing snapshot, not another
one-shot consumption event, so it does not belong in that state machine.

The cutoff is a hard, structural gate: only rows with ``signal_time``
*strictly after* the frozen cutoff are ever eligible. This is enforced by
straightforward comparison in ``evaluate_prospective_snapshot`` -- it is not
a policy note, it is the entire filter.

``ProspectiveConfirmationProtocolV2`` is a purely additive schema variant,
added the same way ``ExperimentManifestV2`` sits alongside the closed
``ExperimentManifest`` in ``discovery/manifest.py``: V1 is untouched (its
already-frozen ``.USTECHCASH`` SELL artifact stays byte-identical and fully
loadable), and V2 exists only because some discovery-only-stable candidates
are defined by an SMC event pattern (``BULLISH_FVG``, ``BSL_SWEEP``, ...)
rather than by action alone. It reuses the exact same closed pattern
vocabulary and grouping logic ``trademind.validation.validate_symbol_patterns``
already uses -- ``trademind.smc_stats._EVENT_ORDER``/``_event_groups``,
unchanged -- so no new pattern taxonomy is invented here. V2 candidates were
never run through a formal manifest/validation/holdout experiment (that is
the entire point of pre-registering them instead), so their provenance
anchors to the discovery-only mining batch itself -- the same real dataset
and an explicit reference to the sibling V1 protocol from the same batch --
rather than to a completed experiment's hypothesis/manifest/holdout refs.

``ProspectiveEvaluationResult.last_eligible_signal_time`` reports the latest
``signal_time`` among the rows a given evaluation call actually counted as
eligible (or ``None``). It is purely descriptive -- it never influences
``outcome`` -- and exists so an operational monitor evaluating a repeatedly
growing snapshot (see ``trademind.mt5_prospective_monitor``) can report
freshness without re-deriving eligibility itself.
"""

from __future__ import annotations

import csv
import io
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path

from trademind.discovery.manifest import (
    CriteriaMode,
    CriterionOperator,
    EvaluationCriteriaV1,
    EvaluationCriterionV1,
)
from trademind.experiment_execution_contract import ObservedMetricsV1, evaluate_criteria
from trademind.orchestrator.artifact_store import ArtifactRef, ArtifactStore
from trademind.signal_statistics_provenance import (
    CodeProvenance,
    ProvenanceError,
    canonical_json_bytes,
    freeze_json_object,
    parse_json,
    sha256_bytes,
    validate_sha256_ref,
)
from trademind.smc_stats import _EVENT_ORDER, _event_groups
from trademind.validation import validate_rows

PROSPECTIVE_CONFIRMATION_PROTOCOL_V1_SCHEMA_VERSION = "prospective-confirmation-protocol-v1"
PROSPECTIVE_CONFIRMATION_PROTOCOL_V1_MEDIA_TYPE = (
    "application/vnd.trademind.prospective-confirmation-protocol-v1+json"
)
PROTOCOL_KIND = "PROSPECTIVE_CONFIRMATION_PROTOCOL_V1"

PROSPECTIVE_CONFIRMATION_PROTOCOL_V2_SCHEMA_VERSION = "prospective-confirmation-protocol-v2"
PROSPECTIVE_CONFIRMATION_PROTOCOL_V2_MEDIA_TYPE = (
    "application/vnd.trademind.prospective-confirmation-protocol-v2+json"
)
PROTOCOL_V2_KIND = "PROSPECTIVE_CONFIRMATION_PROTOCOL_V2"

_PROTOCOL_HASH_DOMAIN = b"trademind:research:prospective-confirmation-protocol:v1"
_PROTOCOL_V2_HASH_DOMAIN = b"trademind:research:prospective-confirmation-protocol:v2"

_BOUNDED_TEXT = re.compile(r"^\S(?:.{0,62}\S)?$")
_MACHINE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$")
_HYPOTHESIS_ID = re.compile(r"^rpi-v1:sha256:[0-9a-f]{64}:[0-7]$")
_ACTIONS = frozenset({"BUY", "SELL"})
_EVENT_PATTERNS = frozenset(_EVENT_ORDER)


class ProspectiveConfirmationError(ValueError):
    """Raised when a protocol or evaluation input is malformed or invalid."""


class ProspectiveConfirmationPersistenceError(RuntimeError):
    """Raised when Verified CAS does not preserve the exact protocol identity."""


def _bounded_text(value: object, *, field_name: str) -> str:
    if type(value) is not str or _BOUNDED_TEXT.fullmatch(value) is None:
        raise ProspectiveConfirmationError(f"{field_name} must be a bounded, trimmed, non-empty string")
    return value


def _machine_identifier(value: object, *, field_name: str) -> str:
    if type(value) is not str or _MACHINE_IDENTIFIER.fullmatch(value) is None:
        raise ProspectiveConfirmationError(
            f"{field_name} must be a bounded stable ASCII machine identifier"
        )
    return value


def _sha256_ref(value: object, *, field_name: str) -> str:
    try:
        return validate_sha256_ref(value)
    except ProvenanceError as exc:
        raise ProspectiveConfirmationError(f"{field_name} is invalid") from exc


def _positive_int(value: object, *, field_name: str) -> int:
    if type(value) is not int or value < 1:
        raise ProspectiveConfirmationError(f"{field_name} must be an exact positive integer")
    return value


def _utc_timestamp(value: object, *, field_name: str) -> str:
    if type(value) is not str:
        raise ProspectiveConfirmationError(f"{field_name} must be an exact UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ProspectiveConfirmationError(f"{field_name} must be a canonical UTC timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ProspectiveConfirmationError(f"{field_name} must include the UTC timezone")
    if parsed.isoformat() != value:
        raise ProspectiveConfirmationError(
            f"{field_name} must use canonical datetime.isoformat() encoding"
        )
    return value


def _audit_identity(value: object, *, field_name: str) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or len(value) > 256
        or any(ord(character) <= 0x1F or 0x7F <= ord(character) <= 0x9F for character in value)
    ):
        raise ProspectiveConfirmationError(f"{field_name} must be a bounded audit identity")
    return value


def _last_eligible_signal_time(eligible: Sequence[Mapping[str, str]]) -> str | None:
    """Latest ``signal_time`` (canonical UTC isoformat) among already-filtered,
    already-validated eligible rows, or ``None`` if none are eligible. Purely
    descriptive -- does not participate in the PASS/FAIL/WAITING_FOR_DATA
    decision, which remains entirely driven by ``validate_rows``/criteria."""
    if not eligible:
        return None
    return max(
        datetime.fromisoformat(row["signal_time"]).astimezone(timezone.utc) for row in eligible
    ).isoformat()


def _exact_fields(payload: Mapping[str, object], *, required: frozenset[str], name: str) -> None:
    fields = frozenset(payload)
    missing = required - fields
    unknown = fields - required
    if missing:
        raise ProspectiveConfirmationError(f"{name} is missing fields: {', '.join(sorted(missing))}")
    if unknown:
        raise ProspectiveConfirmationError(f"{name} contains unknown fields: {', '.join(sorted(unknown))}")


_DIAGNOSTICS_FIELDS = frozenset({"created_at", "created_by"})


@dataclass(frozen=True, slots=True)
class ProspectiveConfirmationDiagnostics:
    """Nonsemantic protocol metadata. Never affects semantic identity."""

    created_at: str
    created_by: str

    def __post_init__(self) -> None:
        _utc_timestamp(self.created_at, field_name="diagnostics.created_at")
        _audit_identity(self.created_by, field_name="diagnostics.created_by")
        canonical_json_bytes(self.to_payload())

    def to_payload(self) -> dict[str, object]:
        return {"created_at": self.created_at, "created_by": self.created_by}

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> "ProspectiveConfirmationDiagnostics":
        frozen = freeze_json_object(payload, field_name="protocol_diagnostics")
        _exact_fields(frozen, required=_DIAGNOSTICS_FIELDS, name="protocol_diagnostics")
        return cls(created_at=frozen["created_at"], created_by=frozen["created_by"])


_PROTOCOL_FIELDS = frozenset(
    {
        "schema_version",
        "protocol_kind",
        "candidate",
        "cutoff_time",
        "minimum_sample",
        "primary_metric",
        "sample_criterion",
        "success_criterion",
        "source_experiment",
        "code_provenance",
        "protocol_semantic_identity",
        "diagnostics",
    }
)


@dataclass(frozen=True, slots=True)
class ProspectiveConfirmationProtocolV1:
    """Immutable, machine-readable pre-registration of a forward-only re-test.

    Nothing in this object can ever authorize opening a holdout or mutating
    any registry -- it carries only identifiers, a cutoff, and the two
    predeclared criteria. ``evaluate_prospective_snapshot`` is the only
    function that reads it, and only to filter/score future rows.
    """

    symbol: str
    action: str
    horizon: int
    cutoff_time: str
    minimum_sample: int
    primary_metric: str
    sample_criterion: EvaluationCriterionV1
    success_criterion: EvaluationCriterionV1
    source_hypothesis_id: str
    source_manifest_semantic_hash: str
    source_manifest_artifact_hash_ref: str
    source_final_holdout_result_artifact_hash_ref: str
    code_provenance: CodeProvenance
    diagnostics: ProspectiveConfirmationDiagnostics
    schema_version: str = PROSPECTIVE_CONFIRMATION_PROTOCOL_V1_SCHEMA_VERSION
    protocol_kind: str = PROTOCOL_KIND
    protocol_semantic_identity: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != PROSPECTIVE_CONFIRMATION_PROTOCOL_V1_SCHEMA_VERSION:
            raise ProspectiveConfirmationError("unsupported prospective confirmation schema_version")
        if self.protocol_kind != PROTOCOL_KIND:
            raise ProspectiveConfirmationError("unsupported prospective confirmation protocol kind")
        _bounded_text(self.symbol, field_name="protocol.symbol")
        if type(self.action) is not str or self.action not in _ACTIONS:
            raise ProspectiveConfirmationError("protocol.action must be BUY or SELL")
        _positive_int(self.horizon, field_name="protocol.horizon")
        _utc_timestamp(self.cutoff_time, field_name="protocol.cutoff_time")
        _positive_int(self.minimum_sample, field_name="protocol.minimum_sample")
        _machine_identifier(self.primary_metric, field_name="protocol.primary_metric")

        if type(self.sample_criterion) is not EvaluationCriterionV1:
            raise ProspectiveConfirmationError("sample_criterion must be EvaluationCriterionV1")
        if (
            self.sample_criterion.metric != "trades"
            or self.sample_criterion.operator is not CriterionOperator.GREATER_THAN_OR_EQUAL
            or self.sample_criterion.threshold != self.minimum_sample
        ):
            raise ProspectiveConfirmationError(
                "sample_criterion must be exactly trades >= minimum_sample"
            )
        if type(self.success_criterion) is not EvaluationCriterionV1:
            raise ProspectiveConfirmationError("success_criterion must be EvaluationCriterionV1")
        if self.success_criterion.metric != self.primary_metric:
            raise ProspectiveConfirmationError("success_criterion must score the primary_metric")

        if (
            type(self.source_hypothesis_id) is not str
            or _HYPOTHESIS_ID.fullmatch(self.source_hypothesis_id) is None
        ):
            raise ProspectiveConfirmationError("source_hypothesis_id is not a valid intake identity")
        _sha256_ref(self.source_manifest_semantic_hash, field_name="source_manifest_semantic_hash")
        _sha256_ref(
            self.source_manifest_artifact_hash_ref, field_name="source_manifest_artifact_hash_ref"
        )
        _sha256_ref(
            self.source_final_holdout_result_artifact_hash_ref,
            field_name="source_final_holdout_result_artifact_hash_ref",
        )
        if type(self.code_provenance) is not CodeProvenance:
            raise ProspectiveConfirmationError("code_provenance must be CodeProvenance")

        object.__setattr__(
            self,
            "protocol_semantic_identity",
            sha256_bytes(
                _PROTOCOL_HASH_DOMAIN + b"\x00" + canonical_json_bytes(self.semantic_projection())
            ),
        )
        canonical_json_bytes(self.to_payload())

    def semantic_projection(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "protocol_kind": self.protocol_kind,
            "candidate": {"symbol": self.symbol, "action": self.action, "horizon": self.horizon},
            "cutoff_time": self.cutoff_time,
            "minimum_sample": self.minimum_sample,
            "primary_metric": self.primary_metric,
            "sample_criterion": self.sample_criterion.to_payload(),
            "success_criterion": self.success_criterion.to_payload(),
            "source_experiment": {
                "hypothesis_id": self.source_hypothesis_id,
                "manifest_semantic_hash": self.source_manifest_semantic_hash,
                "manifest_artifact_hash_ref": self.source_manifest_artifact_hash_ref,
                "final_holdout_result_artifact_hash_ref": (
                    self.source_final_holdout_result_artifact_hash_ref
                ),
            },
            "code_provenance": self.code_provenance.to_payload(),
        }

    def to_payload(self) -> dict[str, object]:
        payload = self.semantic_projection()
        payload["protocol_semantic_identity"] = self.protocol_semantic_identity
        payload["diagnostics"] = self.diagnostics.to_payload()
        return payload

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_payload())

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> "ProspectiveConfirmationProtocolV1":
        try:
            frozen = freeze_json_object(payload, field_name="prospective_confirmation_protocol_v1")
        except ProvenanceError as exc:
            raise ProspectiveConfirmationError(
                "prospective confirmation protocol v1 must be strict bounded JSON"
            ) from exc
        _exact_fields(
            frozen, required=_PROTOCOL_FIELDS, name="prospective_confirmation_protocol_v1"
        )
        candidate = frozen["candidate"]
        source = frozen["source_experiment"]
        if not isinstance(candidate, Mapping) or not isinstance(source, Mapping):
            raise ProspectiveConfirmationError("protocol nested objects are malformed")
        _exact_fields(candidate, required=frozenset({"symbol", "action", "horizon"}), name="candidate")
        _exact_fields(
            source,
            required=frozenset(
                {
                    "hypothesis_id",
                    "manifest_semantic_hash",
                    "manifest_artifact_hash_ref",
                    "final_holdout_result_artifact_hash_ref",
                }
            ),
            name="source_experiment",
        )
        built = cls(
            schema_version=frozen["schema_version"],
            protocol_kind=frozen["protocol_kind"],
            symbol=candidate["symbol"],
            action=candidate["action"],
            horizon=candidate["horizon"],
            cutoff_time=frozen["cutoff_time"],
            minimum_sample=frozen["minimum_sample"],
            primary_metric=frozen["primary_metric"],
            sample_criterion=EvaluationCriterionV1.from_payload(frozen["sample_criterion"]),
            success_criterion=EvaluationCriterionV1.from_payload(frozen["success_criterion"]),
            source_hypothesis_id=source["hypothesis_id"],
            source_manifest_semantic_hash=source["manifest_semantic_hash"],
            source_manifest_artifact_hash_ref=source["manifest_artifact_hash_ref"],
            source_final_holdout_result_artifact_hash_ref=(
                source["final_holdout_result_artifact_hash_ref"]
            ),
            code_provenance=CodeProvenance.from_payload(frozen["code_provenance"]),
            diagnostics=ProspectiveConfirmationDiagnostics.from_payload(frozen["diagnostics"]),
        )
        if built.protocol_semantic_identity != frozen["protocol_semantic_identity"]:
            raise ProspectiveConfirmationError(
                "prospective confirmation protocol semantic identity does not match its "
                "semantic projection"
            )
        return built


def build_prospective_confirmation_protocol_v1(
    *,
    symbol: str,
    action: str,
    horizon: int,
    cutoff_time: str,
    minimum_sample: int,
    primary_metric: str,
    source_hypothesis_id: str,
    source_manifest_semantic_hash: str,
    source_manifest_artifact_hash_ref: str,
    source_final_holdout_result_artifact_hash_ref: str,
    code_provenance: CodeProvenance,
    created_at: str,
    created_by: str,
) -> ProspectiveConfirmationProtocolV1:
    """Build one immutable prospective protocol.

    The two criteria are fixed by construction to exactly the frozen rule
    (``trades >= minimum_sample`` AND ``primary_metric >= 0.0``) -- there is
    no parameter through which a caller could supply a different threshold.
    """
    return ProspectiveConfirmationProtocolV1(
        symbol=symbol,
        action=action,
        horizon=horizon,
        cutoff_time=cutoff_time,
        minimum_sample=minimum_sample,
        primary_metric=primary_metric,
        sample_criterion=EvaluationCriterionV1(
            metric="trades",
            operator=CriterionOperator.GREATER_THAN_OR_EQUAL,
            threshold=minimum_sample,
        ),
        success_criterion=EvaluationCriterionV1(
            metric=primary_metric,
            operator=CriterionOperator.GREATER_THAN_OR_EQUAL,
            threshold=0.0,
        ),
        source_hypothesis_id=source_hypothesis_id,
        source_manifest_semantic_hash=source_manifest_semantic_hash,
        source_manifest_artifact_hash_ref=source_manifest_artifact_hash_ref,
        source_final_holdout_result_artifact_hash_ref=source_final_holdout_result_artifact_hash_ref,
        code_provenance=code_provenance,
        diagnostics=ProspectiveConfirmationDiagnostics(created_at=created_at, created_by=created_by),
    )


def persist_prospective_confirmation_protocol_v1(
    protocol: ProspectiveConfirmationProtocolV1,
    *,
    artifact_store: ArtifactStore,
) -> ArtifactRef:
    """Persist exact canonical protocol bytes under Verified CAS."""
    if type(protocol) is not ProspectiveConfirmationProtocolV1:
        raise ProspectiveConfirmationError("protocol must be ProspectiveConfirmationProtocolV1")
    exact_bytes = protocol.canonical_bytes()
    artifact = artifact_store.import_snapshot(
        io.BytesIO(exact_bytes),
        media_type=PROSPECTIVE_CONFIRMATION_PROTOCOL_V1_MEDIA_TYPE,
    )
    if artifact.hash_ref != sha256_bytes(exact_bytes):
        raise ProspectiveConfirmationPersistenceError(
            "Verified CAS returned an unexpected prospective confirmation protocol hash"
        )
    return artifact


def verify_prospective_confirmation_protocol_v1(
    encoded: str | bytes,
) -> ProspectiveConfirmationProtocolV1:
    """Verify strict canonical wire bytes and the recomputed semantic identity."""
    if type(encoded) is bytes:
        exact_bytes = encoded
    elif type(encoded) is str:
        try:
            exact_bytes = encoded.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ProspectiveConfirmationError(
                "prospective confirmation protocol v1 must be valid UTF-8"
            ) from exc
    else:
        raise ProspectiveConfirmationError(
            "prospective confirmation protocol v1 wire payload must be exact str or bytes"
        )
    try:
        parsed = parse_json(exact_bytes)
    except ProvenanceError as exc:
        raise ProspectiveConfirmationError(
            f"prospective confirmation protocol v1 wire payload is invalid: {exc}"
        ) from exc
    if not isinstance(parsed, Mapping):
        raise ProspectiveConfirmationError(
            "prospective confirmation protocol v1 root must be a JSON object"
        )
    if canonical_json_bytes(parsed) != exact_bytes:
        raise ProspectiveConfirmationError(
            "prospective confirmation protocol v1 wire payload must use canonical JSON bytes"
        )
    return ProspectiveConfirmationProtocolV1.from_payload(parsed)


def load_prospective_confirmation_protocol_v1(
    artifact_hash_ref: str,
    *,
    artifact_store: ArtifactStore,
) -> ProspectiveConfirmationProtocolV1:
    """Load exact CAS bytes and reverify semantic identity. Self-contained: this
    protocol does not bind to any other live manifest/result object."""
    artifact = artifact_store.resolve_verified(
        artifact_hash_ref, expected_media_type=PROSPECTIVE_CONFIRMATION_PROTOCOL_V1_MEDIA_TYPE
    )
    exact_bytes = artifact_store.read_verified(
        artifact_hash_ref, expected_media_type=PROSPECTIVE_CONFIRMATION_PROTOCOL_V1_MEDIA_TYPE
    )
    if artifact.hash_ref != sha256_bytes(exact_bytes):
        raise ProspectiveConfirmationPersistenceError(
            "Verified CAS prospective confirmation protocol identity mismatch"
        )
    return verify_prospective_confirmation_protocol_v1(exact_bytes)


# ---------------------------------------------------------------------------
# Evaluation against a future snapshot -- pure, deterministic, read-only.
# ---------------------------------------------------------------------------


class ProspectiveOutcome(StrEnum):
    """WAITING_FOR_DATA is this protocol's INSUFFICIENT_SAMPLE state: not
    enough post-cutoff non-overlapping trades exist yet to score PASS/FAIL."""

    WAITING_FOR_DATA = "WAITING_FOR_DATA"
    PASS = "PASS"
    FAIL = "FAIL"


@dataclass(frozen=True, slots=True)
class ProspectiveEvaluationResult:
    outcome: ProspectiveOutcome
    completed_non_overlapping_trades: int
    avg_net_atr: float
    win_rate: float
    eligible_rows_considered: int
    last_eligible_signal_time: str | None


def evaluate_prospective_snapshot(
    protocol: ProspectiveConfirmationProtocolV1,
    rows: Sequence[Mapping[str, str]],
) -> ProspectiveEvaluationResult:
    """Score a future journal snapshot against the frozen protocol.

    Only rows with ``signal_time`` STRICTLY after ``protocol.cutoff_time``,
    matching ``protocol.symbol``/``protocol.action``, are ever eligible --
    the cutoff row itself, and everything at or before it, is excluded by
    this comparison. Reuses ``trademind.validation.validate_rows`` (non-
    overlapping trades, ATR-normalized) unchanged. Pure and deterministic:
    identical (protocol, rows) always yields an identical result.
    """
    if type(protocol) is not ProspectiveConfirmationProtocolV1:
        raise ProspectiveConfirmationError("protocol must be ProspectiveConfirmationProtocolV1")
    cutoff = datetime.fromisoformat(protocol.cutoff_time)

    eligible: list[dict[str, str]] = []
    for row in rows:
        raw_time = (row.get("signal_time") or "").strip()
        if not raw_time:
            raise ProspectiveConfirmationError("snapshot row has an empty signal_time")
        try:
            parsed = datetime.fromisoformat(raw_time)
        except ValueError as exc:
            raise ProspectiveConfirmationError(
                f"snapshot row signal_time is not a valid ISO timestamp: {raw_time!r}"
            ) from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ProspectiveConfirmationError("snapshot row signal_time must be timezone-aware")
        parsed = parsed.astimezone(timezone.utc)
        if parsed <= cutoff:
            continue  # already-observed or exactly-at-cutoff: never eligible.
        if (row.get("symbol") or "").strip().upper() != protocol.symbol.upper():
            continue
        if row.get("action") != protocol.action:
            continue
        eligible.append(dict(row))

    result = validate_rows(
        eligible,
        protocol.horizon,
        candidate_minimum=protocol.minimum_sample,
        research_minimum=protocol.minimum_sample,
    )
    trades = int(result.total.trades)
    observed = ObservedMetricsV1(
        primary_metric=protocol.primary_metric,
        values={"trades": float(trades), protocol.primary_metric: float(result.total.avg_net_atr)},
    )
    decision = evaluate_criteria(
        EvaluationCriteriaV1(
            mode=CriteriaMode.ALL, criteria=(protocol.sample_criterion, protocol.success_criterion)
        ),
        observed,
    )
    sample_evaluation = next(e for e in decision.evaluations if e.metric == "trades")
    if not sample_evaluation.passed:
        outcome = ProspectiveOutcome.WAITING_FOR_DATA
    elif decision.passed:
        outcome = ProspectiveOutcome.PASS
    else:
        outcome = ProspectiveOutcome.FAIL

    return ProspectiveEvaluationResult(
        outcome=outcome,
        completed_non_overlapping_trades=trades,
        avg_net_atr=result.total.avg_net_atr,
        win_rate=result.total.win_rate,
        eligible_rows_considered=len(eligible),
        last_eligible_signal_time=_last_eligible_signal_time(eligible),
    )


def evaluate_prospective_snapshot_csv(
    protocol: ProspectiveConfirmationProtocolV1,
    snapshot_path: str | Path,
) -> ProspectiveEvaluationResult:
    """Convenience I/O wrapper: read a journal-schema CSV, then evaluate it."""
    with Path(snapshot_path).open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    return evaluate_prospective_snapshot(protocol, rows)


# ---------------------------------------------------------------------------
# V2: purely additive -- adds an optional SMC event-pattern filter and makes
# ``action`` optional. V1 above is completely unmodified; see module docstring.
# ---------------------------------------------------------------------------

_PROTOCOL_V2_FIELDS = frozenset(
    {
        "schema_version",
        "protocol_kind",
        "candidate",
        "cutoff_time",
        "minimum_sample",
        "primary_metric",
        "sample_criterion",
        "success_criterion",
        "source_batch",
        "code_provenance",
        "protocol_semantic_identity",
        "diagnostics",
    }
)


@dataclass(frozen=True, slots=True)
class ProspectiveConfirmationProtocolV2:
    """Immutable pre-registration with an optional SMC event-pattern filter.

    At least one of ``action``/``pattern`` must be set -- a bare symbol+
    horizon filter with no differentiating condition is not a candidate this
    protocol represents. ``pattern``, when set, must be one of the closed
    event labels ``trademind.smc_stats._EVENT_ORDER`` already defines
    (``BULLISH_FVG``, ``BSL_SWEEP``, ...); no new taxonomy is introduced.

    Provenance points at the discovery-only mining batch this candidate came
    from (dataset description, discovery row count, and the sibling V1
    protocol's own semantic identity) rather than at a completed experiment,
    since these candidates were deliberately never run through validation.
    """

    symbol: str
    horizon: int
    cutoff_time: str
    minimum_sample: int
    primary_metric: str
    sample_criterion: EvaluationCriterionV1
    success_criterion: EvaluationCriterionV1
    source_dataset_description: str
    source_discovery_row_count: int
    sibling_protocol_semantic_identity: str
    code_provenance: CodeProvenance
    diagnostics: ProspectiveConfirmationDiagnostics
    action: str | None = None
    pattern: str | None = None
    schema_version: str = PROSPECTIVE_CONFIRMATION_PROTOCOL_V2_SCHEMA_VERSION
    protocol_kind: str = PROTOCOL_V2_KIND
    protocol_semantic_identity: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != PROSPECTIVE_CONFIRMATION_PROTOCOL_V2_SCHEMA_VERSION:
            raise ProspectiveConfirmationError("unsupported prospective confirmation schema_version")
        if self.protocol_kind != PROTOCOL_V2_KIND:
            raise ProspectiveConfirmationError("unsupported prospective confirmation protocol kind")
        _bounded_text(self.symbol, field_name="protocol.symbol")
        _positive_int(self.horizon, field_name="protocol.horizon")
        _utc_timestamp(self.cutoff_time, field_name="protocol.cutoff_time")
        _positive_int(self.minimum_sample, field_name="protocol.minimum_sample")
        _machine_identifier(self.primary_metric, field_name="protocol.primary_metric")

        if self.action is not None and self.action not in _ACTIONS:
            raise ProspectiveConfirmationError("protocol.action must be BUY, SELL, or null")
        if self.pattern is not None and self.pattern not in _EVENT_PATTERNS:
            raise ProspectiveConfirmationError(
                f"protocol.pattern must be one of {sorted(_EVENT_PATTERNS)} or null"
            )
        if self.action is None and self.pattern is None:
            raise ProspectiveConfirmationError(
                "protocol must set action, pattern, or both -- a bare symbol+horizon "
                "filter is not a differentiated candidate"
            )

        if type(self.sample_criterion) is not EvaluationCriterionV1:
            raise ProspectiveConfirmationError("sample_criterion must be EvaluationCriterionV1")
        if (
            self.sample_criterion.metric != "trades"
            or self.sample_criterion.operator is not CriterionOperator.GREATER_THAN_OR_EQUAL
            or self.sample_criterion.threshold != self.minimum_sample
        ):
            raise ProspectiveConfirmationError(
                "sample_criterion must be exactly trades >= minimum_sample"
            )
        if type(self.success_criterion) is not EvaluationCriterionV1:
            raise ProspectiveConfirmationError("success_criterion must be EvaluationCriterionV1")
        if self.success_criterion.metric != self.primary_metric:
            raise ProspectiveConfirmationError("success_criterion must score the primary_metric")

        _bounded_text(
            self.source_dataset_description, field_name="source_batch.dataset_description"
        )
        if type(self.source_discovery_row_count) is not int or self.source_discovery_row_count < 1:
            raise ProspectiveConfirmationError(
                "source_batch.discovery_row_count must be an exact positive integer"
            )
        _sha256_ref(
            self.sibling_protocol_semantic_identity,
            field_name="source_batch.sibling_protocol_semantic_identity",
        )
        if type(self.code_provenance) is not CodeProvenance:
            raise ProspectiveConfirmationError("code_provenance must be CodeProvenance")

        object.__setattr__(
            self,
            "protocol_semantic_identity",
            sha256_bytes(
                _PROTOCOL_V2_HASH_DOMAIN + b"\x00" + canonical_json_bytes(self.semantic_projection())
            ),
        )
        canonical_json_bytes(self.to_payload())

    def semantic_projection(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "protocol_kind": self.protocol_kind,
            "candidate": {
                "symbol": self.symbol,
                "action": self.action,
                "pattern": self.pattern,
                "horizon": self.horizon,
            },
            "cutoff_time": self.cutoff_time,
            "minimum_sample": self.minimum_sample,
            "primary_metric": self.primary_metric,
            "sample_criterion": self.sample_criterion.to_payload(),
            "success_criterion": self.success_criterion.to_payload(),
            "source_batch": {
                "dataset_description": self.source_dataset_description,
                "discovery_row_count": self.source_discovery_row_count,
                "sibling_protocol_semantic_identity": self.sibling_protocol_semantic_identity,
            },
            "code_provenance": self.code_provenance.to_payload(),
        }

    def to_payload(self) -> dict[str, object]:
        payload = self.semantic_projection()
        payload["protocol_semantic_identity"] = self.protocol_semantic_identity
        payload["diagnostics"] = self.diagnostics.to_payload()
        return payload

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_payload())

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> "ProspectiveConfirmationProtocolV2":
        try:
            frozen = freeze_json_object(payload, field_name="prospective_confirmation_protocol_v2")
        except ProvenanceError as exc:
            raise ProspectiveConfirmationError(
                "prospective confirmation protocol v2 must be strict bounded JSON"
            ) from exc
        _exact_fields(
            frozen, required=_PROTOCOL_V2_FIELDS, name="prospective_confirmation_protocol_v2"
        )
        candidate = frozen["candidate"]
        source_batch = frozen["source_batch"]
        if not isinstance(candidate, Mapping) or not isinstance(source_batch, Mapping):
            raise ProspectiveConfirmationError("protocol nested objects are malformed")
        _exact_fields(
            candidate,
            required=frozenset({"symbol", "action", "pattern", "horizon"}),
            name="candidate",
        )
        _exact_fields(
            source_batch,
            required=frozenset(
                {"dataset_description", "discovery_row_count", "sibling_protocol_semantic_identity"}
            ),
            name="source_batch",
        )
        built = cls(
            schema_version=frozen["schema_version"],
            protocol_kind=frozen["protocol_kind"],
            symbol=candidate["symbol"],
            action=candidate["action"],
            pattern=candidate["pattern"],
            horizon=candidate["horizon"],
            cutoff_time=frozen["cutoff_time"],
            minimum_sample=frozen["minimum_sample"],
            primary_metric=frozen["primary_metric"],
            sample_criterion=EvaluationCriterionV1.from_payload(frozen["sample_criterion"]),
            success_criterion=EvaluationCriterionV1.from_payload(frozen["success_criterion"]),
            source_dataset_description=source_batch["dataset_description"],
            source_discovery_row_count=source_batch["discovery_row_count"],
            sibling_protocol_semantic_identity=source_batch["sibling_protocol_semantic_identity"],
            code_provenance=CodeProvenance.from_payload(frozen["code_provenance"]),
            diagnostics=ProspectiveConfirmationDiagnostics.from_payload(frozen["diagnostics"]),
        )
        if built.protocol_semantic_identity != frozen["protocol_semantic_identity"]:
            raise ProspectiveConfirmationError(
                "prospective confirmation protocol semantic identity does not match its "
                "semantic projection"
            )
        return built


def build_prospective_confirmation_protocol_v2(
    *,
    symbol: str,
    horizon: int,
    cutoff_time: str,
    minimum_sample: int,
    primary_metric: str,
    source_dataset_description: str,
    source_discovery_row_count: int,
    sibling_protocol_semantic_identity: str,
    code_provenance: CodeProvenance,
    created_at: str,
    created_by: str,
    action: str | None = None,
    pattern: str | None = None,
) -> ProspectiveConfirmationProtocolV2:
    """Build one immutable V2 protocol with the frozen rule (see V1's builder)."""
    return ProspectiveConfirmationProtocolV2(
        symbol=symbol,
        action=action,
        pattern=pattern,
        horizon=horizon,
        cutoff_time=cutoff_time,
        minimum_sample=minimum_sample,
        primary_metric=primary_metric,
        sample_criterion=EvaluationCriterionV1(
            metric="trades",
            operator=CriterionOperator.GREATER_THAN_OR_EQUAL,
            threshold=minimum_sample,
        ),
        success_criterion=EvaluationCriterionV1(
            metric=primary_metric,
            operator=CriterionOperator.GREATER_THAN_OR_EQUAL,
            threshold=0.0,
        ),
        source_dataset_description=source_dataset_description,
        source_discovery_row_count=source_discovery_row_count,
        sibling_protocol_semantic_identity=sibling_protocol_semantic_identity,
        code_provenance=code_provenance,
        diagnostics=ProspectiveConfirmationDiagnostics(created_at=created_at, created_by=created_by),
    )


def persist_prospective_confirmation_protocol_v2(
    protocol: ProspectiveConfirmationProtocolV2,
    *,
    artifact_store: ArtifactStore,
) -> ArtifactRef:
    """Persist exact canonical protocol bytes under Verified CAS."""
    if type(protocol) is not ProspectiveConfirmationProtocolV2:
        raise ProspectiveConfirmationError("protocol must be ProspectiveConfirmationProtocolV2")
    exact_bytes = protocol.canonical_bytes()
    artifact = artifact_store.import_snapshot(
        io.BytesIO(exact_bytes),
        media_type=PROSPECTIVE_CONFIRMATION_PROTOCOL_V2_MEDIA_TYPE,
    )
    if artifact.hash_ref != sha256_bytes(exact_bytes):
        raise ProspectiveConfirmationPersistenceError(
            "Verified CAS returned an unexpected prospective confirmation protocol hash"
        )
    return artifact


def verify_prospective_confirmation_protocol_v2(
    encoded: str | bytes,
) -> ProspectiveConfirmationProtocolV2:
    """Verify strict canonical wire bytes and the recomputed semantic identity."""
    if type(encoded) is bytes:
        exact_bytes = encoded
    elif type(encoded) is str:
        try:
            exact_bytes = encoded.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ProspectiveConfirmationError(
                "prospective confirmation protocol v2 must be valid UTF-8"
            ) from exc
    else:
        raise ProspectiveConfirmationError(
            "prospective confirmation protocol v2 wire payload must be exact str or bytes"
        )
    try:
        parsed = parse_json(exact_bytes)
    except ProvenanceError as exc:
        raise ProspectiveConfirmationError(
            f"prospective confirmation protocol v2 wire payload is invalid: {exc}"
        ) from exc
    if not isinstance(parsed, Mapping):
        raise ProspectiveConfirmationError(
            "prospective confirmation protocol v2 root must be a JSON object"
        )
    if canonical_json_bytes(parsed) != exact_bytes:
        raise ProspectiveConfirmationError(
            "prospective confirmation protocol v2 wire payload must use canonical JSON bytes"
        )
    return ProspectiveConfirmationProtocolV2.from_payload(parsed)


def load_prospective_confirmation_protocol_v2(
    artifact_hash_ref: str,
    *,
    artifact_store: ArtifactStore,
) -> ProspectiveConfirmationProtocolV2:
    """Load exact CAS bytes and reverify semantic identity. Self-contained."""
    artifact = artifact_store.resolve_verified(
        artifact_hash_ref, expected_media_type=PROSPECTIVE_CONFIRMATION_PROTOCOL_V2_MEDIA_TYPE
    )
    exact_bytes = artifact_store.read_verified(
        artifact_hash_ref, expected_media_type=PROSPECTIVE_CONFIRMATION_PROTOCOL_V2_MEDIA_TYPE
    )
    if artifact.hash_ref != sha256_bytes(exact_bytes):
        raise ProspectiveConfirmationPersistenceError(
            "Verified CAS prospective confirmation protocol identity mismatch"
        )
    return verify_prospective_confirmation_protocol_v2(exact_bytes)


def evaluate_prospective_snapshot_v2(
    protocol: ProspectiveConfirmationProtocolV2,
    rows: Sequence[Mapping[str, str]],
) -> ProspectiveEvaluationResult:
    """Score a future journal snapshot against a frozen V2 protocol.

    Identical cutoff/symbol discipline to ``evaluate_prospective_snapshot``,
    plus an optional action filter and an optional SMC event-pattern filter
    (reusing ``trademind.smc_stats._event_groups`` unchanged). Pure and
    deterministic.
    """
    if type(protocol) is not ProspectiveConfirmationProtocolV2:
        raise ProspectiveConfirmationError("protocol must be ProspectiveConfirmationProtocolV2")
    cutoff = datetime.fromisoformat(protocol.cutoff_time)

    candidate_rows: list[dict[str, str]] = []
    for row in rows:
        raw_time = (row.get("signal_time") or "").strip()
        if not raw_time:
            raise ProspectiveConfirmationError("snapshot row has an empty signal_time")
        try:
            parsed = datetime.fromisoformat(raw_time)
        except ValueError as exc:
            raise ProspectiveConfirmationError(
                f"snapshot row signal_time is not a valid ISO timestamp: {raw_time!r}"
            ) from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ProspectiveConfirmationError("snapshot row signal_time must be timezone-aware")
        parsed = parsed.astimezone(timezone.utc)
        if parsed <= cutoff:
            continue
        if (row.get("symbol") or "").strip().upper() != protocol.symbol.upper():
            continue
        if protocol.action is not None and row.get("action") != protocol.action:
            continue
        candidate_rows.append(dict(row))

    if protocol.pattern is not None:
        groups = _event_groups(candidate_rows)
        eligible = groups.get(protocol.pattern, [])
    else:
        eligible = candidate_rows

    result = validate_rows(
        eligible,
        protocol.horizon,
        candidate_minimum=protocol.minimum_sample,
        research_minimum=protocol.minimum_sample,
    )
    trades = int(result.total.trades)
    observed = ObservedMetricsV1(
        primary_metric=protocol.primary_metric,
        values={"trades": float(trades), protocol.primary_metric: float(result.total.avg_net_atr)},
    )
    decision = evaluate_criteria(
        EvaluationCriteriaV1(
            mode=CriteriaMode.ALL, criteria=(protocol.sample_criterion, protocol.success_criterion)
        ),
        observed,
    )
    sample_evaluation = next(e for e in decision.evaluations if e.metric == "trades")
    if not sample_evaluation.passed:
        outcome = ProspectiveOutcome.WAITING_FOR_DATA
    elif decision.passed:
        outcome = ProspectiveOutcome.PASS
    else:
        outcome = ProspectiveOutcome.FAIL

    return ProspectiveEvaluationResult(
        outcome=outcome,
        completed_non_overlapping_trades=trades,
        avg_net_atr=result.total.avg_net_atr,
        win_rate=result.total.win_rate,
        eligible_rows_considered=len(eligible),
        last_eligible_signal_time=_last_eligible_signal_time(eligible),
    )


def evaluate_prospective_snapshot_v2_csv(
    protocol: ProspectiveConfirmationProtocolV2,
    snapshot_path: str | Path,
) -> ProspectiveEvaluationResult:
    """Convenience I/O wrapper: read a journal-schema CSV, then evaluate it."""
    with Path(snapshot_path).open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    return evaluate_prospective_snapshot_v2(protocol, rows)


__all__ = [
    "PROSPECTIVE_CONFIRMATION_PROTOCOL_V1_MEDIA_TYPE",
    "PROSPECTIVE_CONFIRMATION_PROTOCOL_V1_SCHEMA_VERSION",
    "PROSPECTIVE_CONFIRMATION_PROTOCOL_V2_MEDIA_TYPE",
    "PROSPECTIVE_CONFIRMATION_PROTOCOL_V2_SCHEMA_VERSION",
    "PROTOCOL_KIND",
    "PROTOCOL_V2_KIND",
    "ProspectiveConfirmationDiagnostics",
    "ProspectiveConfirmationError",
    "ProspectiveConfirmationPersistenceError",
    "ProspectiveConfirmationProtocolV1",
    "ProspectiveConfirmationProtocolV2",
    "ProspectiveEvaluationResult",
    "ProspectiveOutcome",
    "build_prospective_confirmation_protocol_v1",
    "build_prospective_confirmation_protocol_v2",
    "evaluate_prospective_snapshot",
    "evaluate_prospective_snapshot_csv",
    "evaluate_prospective_snapshot_v2",
    "evaluate_prospective_snapshot_v2_csv",
    "load_prospective_confirmation_protocol_v1",
    "load_prospective_confirmation_protocol_v2",
    "persist_prospective_confirmation_protocol_v1",
    "persist_prospective_confirmation_protocol_v2",
    "verify_prospective_confirmation_protocol_v1",
    "verify_prospective_confirmation_protocol_v2",
]
