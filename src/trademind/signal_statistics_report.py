"""Machine-readable signal statistics report for AI-agent consumption."""

from __future__ import annotations

import argparse
import csv
import io
import json
from collections import Counter, defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Sequence

from trademind.orchestrator.artifact_store import ArtifactRef, ArtifactStore
from trademind.signal_statistics_provenance import (
    CandidateContentV2,
    CodeProvenance,
    ProvenanceError,
    ReportContentHashProjection,
    canonical_json_bytes,
    freeze_json_object,
    parse_json,
    report_content_hash,
    sha256_bytes,
    validate_sha256_ref,
)
from trademind.validation import PatternValidation, validate_symbol_patterns

REPORT_SCHEMA_VERSION = "signal-statistics-report-v1"
REPORT_V2_SCHEMA_VERSION = "signal-statistics-report-v2"
REPORT_V2_MEDIA_TYPE = "application/vnd.trademind.signal-statistics-report-v2+json"
DEFAULT_HORIZONS = (3, 6, 12)


def _result_payload(item: PatternValidation) -> dict[str, object]:
    result = item.result
    return {
        "symbol": item.symbol,
        "pattern": item.label,
        "horizon": item.horizon,
        "observations": item.observations,
        "status": result.status,
        "trades": result.total.trades,
        "win_rate": result.total.win_rate,
        "profit_factor_atr": result.total.profit_factor_atr,
        "avg_net_atr": result.total.avg_net_atr,
        "early": {
            "trades": result.early.trades,
            "win_rate": result.early.win_rate,
            "profit_factor_atr": result.early.profit_factor_atr,
            "avg_net_atr": result.early.avg_net_atr,
        },
        "late": {
            "trades": result.late.trades,
            "win_rate": result.late.win_rate,
            "profit_factor_atr": result.late.profit_factor_atr,
            "avg_net_atr": result.late.avg_net_atr,
        },
        "max_drawdown_atr": result.max_drawdown_atr,
        "max_loss_streak": result.max_loss_streak,
        "mean_ci95": [result.mean_ci_low, result.mean_ci_high],
        "reasons": list(result.reasons),
    }


def build_report(
    rows: Iterable[dict[str, str]],
    *,
    horizons: Sequence[int] = DEFAULT_HORIZONS,
    schema_version: str = "1.1",
    candidate_minimum: int = 30,
    research_minimum: int = 300,
    volume_threshold: float = 1.2,
    spread_atr_threshold: float = 0.10,
    generated_at: datetime | None = None,
) -> dict[str, object]:
    """Return a stable JSON-ready snapshot without changing signal weights or execution state."""
    filtered = [
        dict(row) for row in rows if row.get("schema_version", "").strip() == schema_version
    ]
    by_symbol: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in filtered:
        by_symbol[row.get("symbol", "UNKNOWN").strip().upper()].append(row)

    validations: list[PatternValidation] = []
    selected_horizons = tuple(sorted(set(int(value) for value in horizons)))
    for symbol in sorted(by_symbol):
        validations.extend(
            validate_symbol_patterns(
                symbol,
                by_symbol[symbol],
                list(selected_horizons),
                candidate_minimum=candidate_minimum,
                research_minimum=research_minimum,
                volume_threshold=volume_threshold,
                spread_atr_threshold=spread_atr_threshold,
            )
        )

    pattern_payloads = [_result_payload(item) for item in validations]
    status_counts = Counter(str(item["status"]) for item in pattern_payloads)
    now = generated_at or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "generated_at": now.astimezone(timezone.utc).isoformat(),
        "source_schema_version": schema_version,
        "read_only": True,
        "orders_enabled": False,
        "journal_rows": len(filtered),
        "symbols": sorted(by_symbol),
        "horizons": list(selected_horizons),
        "thresholds": {
            "candidate_minimum": candidate_minimum,
            "research_minimum": research_minimum,
            "volume_threshold": volume_threshold,
            "spread_atr_threshold": spread_atr_threshold,
        },
        "status_counts": dict(sorted(status_counts.items())),
        "patterns": pattern_payloads,
    }


def load_report_from_journal(
    journal_path: Path,
    **kwargs: object,
) -> dict[str, object]:
    with journal_path.open("r", encoding="utf-8", newline="") as handle:
        rows = [dict(row) for row in csv.DictReader(handle)]
    return build_report(rows, **kwargs)


class SignalStatisticsReportV2Error(ProvenanceError):
    """Raised when a report v2 payload violates its strict semantic contract."""


def _require_exact_fields(
    payload: Mapping[str, object],
    *,
    required: frozenset[str],
    name: str,
) -> None:
    fields = frozenset(payload)
    missing = required - fields
    if missing:
        raise SignalStatisticsReportV2Error(
            f"{name} is missing fields: {', '.join(sorted(missing))}"
        )
    unknown = fields - required
    if unknown:
        raise SignalStatisticsReportV2Error(
            f"{name} contains unknown fields: {', '.join(sorted(unknown))}"
        )


def _optional_diagnostic_text(value: object, *, field_name: str) -> str | None:
    if value is None:
        return None
    if type(value) is not str:
        raise SignalStatisticsReportV2Error(f"{field_name} must be an exact string or null")
    if not value:
        raise SignalStatisticsReportV2Error(f"{field_name} must not be empty")
    return value


def _generated_at_timestamp(value: object) -> str:
    """Validate the repository's canonical UTC ``datetime.isoformat()`` convention."""
    if type(value) is not str:
        raise SignalStatisticsReportV2Error("generated_at must be an exact string")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise SignalStatisticsReportV2Error(
            "generated_at must be a canonical UTC ISO timestamp"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise SignalStatisticsReportV2Error("generated_at must include the UTC timezone")
    if parsed.isoformat() != value:
        raise SignalStatisticsReportV2Error(
            "generated_at must use canonical datetime.isoformat() encoding"
        )
    return value


def _candidate_entry(candidate: CandidateContentV2) -> dict[str, object]:
    return {
        "candidate_id": candidate.candidate_id,
        "candidate_content_hash": candidate.content_hash,
        "content": candidate.to_payload(),
    }


@dataclass(frozen=True, slots=True)
class SignalStatisticsReportV2:
    """Strict immutable signal-statistics evidence report.

    ``report_semantic_hash`` is domain-bound to the semantic projection only.
    ``generated_at`` and ``source_path`` are explicit diagnostic values: they are
    excluded from semantic identity but included in the exact canonical artifact
    bytes. ``source_path`` is never opened or used for verification and may expose
    local diagnostic information if callers choose to persist it.

    ``code_provenance`` is a validated claim supplied by a trusted external
    boundary. As documented by :class:`CodeProvenance`, it is not an attestation.
    """

    source_snapshot_hash_ref: str
    source_schema_version: str
    code_provenance: CodeProvenance
    journal_rows: int
    candidates: tuple[CandidateContentV2, ...]
    generated_at: str
    source_path: str | None = None
    schema_version: str = REPORT_V2_SCHEMA_VERSION
    report_semantic_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if type(self.schema_version) is not str or self.schema_version != REPORT_V2_SCHEMA_VERSION:
            raise SignalStatisticsReportV2Error("unsupported signal statistics report version")
        validate_sha256_ref(self.source_snapshot_hash_ref)
        if type(self.source_schema_version) is not str or not self.source_schema_version:
            raise SignalStatisticsReportV2Error(
                "source_schema_version must be a non-empty exact string"
            )
        if type(self.code_provenance) is not CodeProvenance:
            raise SignalStatisticsReportV2Error("code_provenance must be CodeProvenance")
        if type(self.journal_rows) is not int or self.journal_rows < 0:
            raise SignalStatisticsReportV2Error(
                "journal_rows must be an exact non-negative integer"
            )
        if type(self.candidates) is not tuple:
            raise SignalStatisticsReportV2Error("candidates must be an immutable tuple")
        if not all(type(candidate) is CandidateContentV2 for candidate in self.candidates):
            raise SignalStatisticsReportV2Error(
                "candidates must contain only CandidateContentV2 values"
            )

        ordered = tuple(
            sorted(
                self.candidates,
                key=lambda candidate: (candidate.candidate_id, candidate.content_hash),
            )
        )
        candidate_ids = [candidate.candidate_id for candidate in ordered]
        if len(candidate_ids) != len(set(candidate_ids)):
            raise SignalStatisticsReportV2Error(
                "report contains multiple contents for one candidate definition"
            )
        object.__setattr__(self, "candidates", ordered)
        object.__setattr__(
            self,
            "generated_at",
            _generated_at_timestamp(self.generated_at),
        )
        object.__setattr__(
            self,
            "source_path",
            _optional_diagnostic_text(self.source_path, field_name="source_path"),
        )

        projection = ReportContentHashProjection(self.semantic_projection())
        object.__setattr__(self, "report_semantic_hash", report_content_hash(projection))
        canonical_json_bytes(self.to_payload())

    def semantic_projection(self) -> dict[str, object]:
        """Return the complete report-domain projection, excluding diagnostics."""
        return {
            "schema_version": self.schema_version,
            "read_only": True,
            "orders_enabled": False,
            "source": {
                "snapshot_hash_ref": self.source_snapshot_hash_ref,
                "schema_version": self.source_schema_version,
            },
            "code_provenance": self.code_provenance.to_payload(),
            "journal_rows": self.journal_rows,
            "candidates": [_candidate_entry(candidate) for candidate in self.candidates],
        }

    def to_payload(self) -> dict[str, object]:
        payload = self.semantic_projection()
        payload["report_semantic_hash"] = self.report_semantic_hash
        payload["diagnostics"] = {
            "generated_at": self.generated_at,
            "source_path": self.source_path,
        }
        return payload

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_payload())

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> SignalStatisticsReportV2:
        frozen = freeze_json_object(payload, field_name="signal_statistics_report_v2")
        _require_exact_fields(
            frozen,
            required=frozenset(
                {
                    "schema_version",
                    "report_semantic_hash",
                    "read_only",
                    "orders_enabled",
                    "source",
                    "code_provenance",
                    "journal_rows",
                    "candidates",
                    "diagnostics",
                }
            ),
            name="signal statistics report v2",
        )
        if frozen["schema_version"] != REPORT_V2_SCHEMA_VERSION:
            raise SignalStatisticsReportV2Error("unsupported signal statistics report version")
        if frozen["read_only"] is not True or frozen["orders_enabled"] is not False:
            raise SignalStatisticsReportV2Error("report v2 must be read-only with orders disabled")

        source = frozen["source"]
        code_provenance = frozen["code_provenance"]
        candidates = frozen["candidates"]
        diagnostics = frozen["diagnostics"]
        if not isinstance(source, Mapping):
            raise SignalStatisticsReportV2Error("source must be a JSON object")
        if not isinstance(code_provenance, Mapping):
            raise SignalStatisticsReportV2Error("code_provenance must be a JSON object")
        if type(candidates) is not tuple:
            raise SignalStatisticsReportV2Error("candidates must be a JSON array")
        if not isinstance(diagnostics, Mapping):
            raise SignalStatisticsReportV2Error("diagnostics must be a JSON object")

        _require_exact_fields(
            source,
            required=frozenset({"snapshot_hash_ref", "schema_version"}),
            name="report source",
        )
        _require_exact_fields(
            diagnostics,
            required=frozenset({"generated_at", "source_path"}),
            name="report diagnostics",
        )

        parsed_candidates: list[CandidateContentV2] = []
        for index, entry in enumerate(candidates):
            if not isinstance(entry, Mapping):
                raise SignalStatisticsReportV2Error(f"candidates[{index}] must be a JSON object")
            _require_exact_fields(
                entry,
                required=frozenset({"candidate_id", "candidate_content_hash", "content"}),
                name=f"candidates[{index}]",
            )
            content_payload = entry["content"]
            if not isinstance(content_payload, Mapping):
                raise SignalStatisticsReportV2Error(
                    f"candidates[{index}].content must be a JSON object"
                )
            candidate = CandidateContentV2.from_payload(content_payload)
            if (
                type(entry["candidate_id"]) is not str
                or entry["candidate_id"] != candidate.candidate_id
            ):
                raise SignalStatisticsReportV2Error(
                    f"candidates[{index}] candidate definition identity mismatch"
                )
            validate_sha256_ref(entry["candidate_content_hash"])
            if entry["candidate_content_hash"] != candidate.content_hash:
                raise SignalStatisticsReportV2Error(
                    f"candidates[{index}] candidate content identity mismatch"
                )
            parsed_candidates.append(candidate)

        claimed_semantic_hash = validate_sha256_ref(frozen["report_semantic_hash"])
        report = cls(
            schema_version=frozen["schema_version"],
            source_snapshot_hash_ref=source["snapshot_hash_ref"],
            source_schema_version=source["schema_version"],
            code_provenance=CodeProvenance.from_payload(code_provenance),
            journal_rows=frozen["journal_rows"],
            candidates=tuple(parsed_candidates),
            generated_at=diagnostics["generated_at"],
            source_path=diagnostics["source_path"],
        )
        if claimed_semantic_hash != report.report_semantic_hash:
            raise SignalStatisticsReportV2Error("report semantic hash mismatch")
        if canonical_json_bytes(frozen) != report.canonical_bytes():
            raise SignalStatisticsReportV2Error("report payload is not in canonical semantic order")
        return report


def build_report_v2(
    candidates: Iterable[CandidateContentV2],
    *,
    source_snapshot_hash_ref: str,
    source_schema_version: str,
    code_provenance: CodeProvenance,
    journal_rows: int,
    generated_at: str,
    source_path: str | None = None,
) -> SignalStatisticsReportV2:
    """Build report v2 from already validated, explicitly injected evidence."""
    try:
        snapshot = tuple(candidates)
    except TypeError as exc:
        raise SignalStatisticsReportV2Error("candidates must be iterable") from exc
    return SignalStatisticsReportV2(
        source_snapshot_hash_ref=source_snapshot_hash_ref,
        source_schema_version=source_schema_version,
        code_provenance=code_provenance,
        journal_rows=journal_rows,
        candidates=snapshot,
        generated_at=generated_at,
        source_path=source_path,
    )


def verify_report_v2(
    encoded: str | bytes,
) -> SignalStatisticsReportV2:
    """Verify strict canonical wire data and report semantic provenance only.

    This function does not attest that ``encoded`` came from a persisted CAS
    artifact. Call :func:`load_report_v2` with a Verified CAS hash reference when
    both exact persisted-artifact integrity and report semantic integrity are
    required.
    """
    if type(encoded) is bytes:
        exact_bytes = encoded
    elif type(encoded) is str:
        try:
            exact_bytes = encoded.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise SignalStatisticsReportV2Error("report must be valid UTF-8") from exc
    else:
        raise SignalStatisticsReportV2Error("report wire payload must be exact str or bytes")

    parsed = parse_json(exact_bytes)
    if not isinstance(parsed, Mapping):
        raise SignalStatisticsReportV2Error("report v2 root must be a JSON object")
    if canonical_json_bytes(parsed) != exact_bytes:
        raise SignalStatisticsReportV2Error("report v2 wire payload must use canonical JSON bytes")
    return SignalStatisticsReportV2.from_payload(parsed)


def persist_report_v2(
    report: SignalStatisticsReportV2,
    *,
    artifact_store: ArtifactStore,
) -> ArtifactRef:
    """Persist exact canonical report bytes through Verified CAS v1."""
    if type(report) is not SignalStatisticsReportV2:
        raise SignalStatisticsReportV2Error("report must be SignalStatisticsReportV2")
    exact_bytes = report.canonical_bytes()
    artifact = artifact_store.import_snapshot(
        io.BytesIO(exact_bytes),
        media_type=REPORT_V2_MEDIA_TYPE,
    )
    if artifact.hash_ref != sha256_bytes(exact_bytes):
        raise SignalStatisticsReportV2Error("Verified CAS returned an unexpected artifact hash")
    return artifact


def load_report_v2(
    artifact_hash_ref: str,
    *,
    artifact_store: ArtifactStore,
) -> SignalStatisticsReportV2:
    """Verify exact persisted CAS integrity and report semantic integrity.

    The caller supplies only a strict CAS hash reference. Verified CAS resolves
    and verifies the expected media type and exact bytes before the semantic/wire
    report verifier runs; no ``ArtifactRef.path`` or caller filesystem path is
    trusted.
    """
    validated_hash_ref = validate_sha256_ref(artifact_hash_ref)
    resolved = artifact_store.resolve_verified(
        validated_hash_ref,
        expected_media_type=REPORT_V2_MEDIA_TYPE,
    )
    if resolved.hash_ref != validated_hash_ref:
        raise SignalStatisticsReportV2Error("resolved report artifact identity mismatch")
    exact_bytes = artifact_store.read_verified(
        validated_hash_ref,
        expected_media_type=REPORT_V2_MEDIA_TYPE,
    )
    return verify_report_v2(exact_bytes)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Export TradeMind validation results as machine-readable JSON"
    )
    parser.add_argument("--journal", type=Path, default=Path("data/journal_ecn/signals.csv"))
    parser.add_argument("--schema-version", default="1.1")
    parser.add_argument("--candidate-min", type=int, default=30)
    parser.add_argument("--min-sample", type=int, default=300)
    parser.add_argument("--horizon", type=int, action="append")
    parser.add_argument("--volume-threshold", type=float, default=1.2)
    parser.add_argument("--spread-atr-threshold", type=float, default=0.10)
    parser.add_argument("--output", type=Path, help="Optional JSON file. Stdout is used otherwise.")
    args = parser.parse_args(argv)

    if not args.journal.is_file():
        parser.error(f"journal not found: {args.journal}")
    if args.candidate_min < 2:
        parser.error("--candidate-min must be at least 2")
    if args.min_sample < args.candidate_min:
        parser.error("--min-sample must be at least --candidate-min")

    report = load_report_from_journal(
        args.journal,
        horizons=args.horizon or DEFAULT_HORIZONS,
        schema_version=args.schema_version,
        candidate_minimum=args.candidate_min,
        research_minimum=args.min_sample,
        volume_threshold=args.volume_threshold,
        spread_atr_threshold=args.spread_atr_threshold,
    )
    encoded = json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded + "\n", encoding="utf-8")
    else:
        print(encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
