"""Read-only, content-addressed SER8 broker-history datasets.

The only live source adapter in this module is MetaTrader 5's Python
``copy_rates_range`` API.  It verifies the already-authenticated terminal and
account before reading rates and deliberately exposes no login, order, deal,
position, or symbol-selection operation.  Collection never repairs gaps or
mutates bar content: validation failures remain visible in the detailed
inventory and are excluded from the compatibility ``symbol,rows`` CSV.
"""

from __future__ import annotations

import csv
import hashlib
import importlib
import io
import json
import math
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

from trademind.ser8_symbol_universe import (
    ASSET_CLASS_FX,
    classify_asset_class,
    risk_model_support_for_symbol_row,
)
from trademind.signal_statistics_provenance import canonical_json_bytes, sha256_bytes

DATASET_SCHEMA_VERSION = "ser8-historical-market-data-v3"
INVENTORY_SCHEMA_VERSION = "ser8-historical-data-inventory-v3"
SOURCE_PROOF_SCHEMA_VERSION = "ser8-mt5-history-source-proof-v1"
COLLECTOR_VERSION = "1.3.0"
CHUNK_COLLECTOR_VERSION = "1.1.0"
CHUNK_POLICY_VERSION = "ser8-calendar-month-utc-v1"
CHUNK_CACHE_SCHEMA_VERSION = "ser8-mt5-history-chunk-v1"
COVERAGE_DISCOVERY_POLICY_VERSION = "ser8-backward-suffix-coverage-discovery-v1"
COVERAGE_TRUNCATED_REASON_CODE = "HISTORICAL_COVERAGE_STARTS_LATER_THAN_REQUESTED"
# A boundary may be established ONLY by a chunk failure positively classified
# as genuine broker historical unavailability. Every other failure code —
# transient, unknown, or a data-integrity violation — must never be
# reinterpreted as "short history"; see _classify_chunk_failure_code().
GENUINE_HISTORICAL_UNAVAILABLE_ERROR_CODES = frozenset(
    {"BROKER_HISTORY_NOT_RETAINED_FOR_RANGE"}
)
DATA_INTEGRITY_CHUNK_ERROR_CODES = frozenset(
    {
        "BAR_IDENTITY_MISMATCH",
        "CONFLICTING_DUPLICATE_BAR",
        "CHUNK_BAR_OUTSIDE_REQUEST",
        "CHUNK_DATA_INTEGRITY_FAILED",
    }
)
# Deterministic, bounded retry budget applied only to a chunk failure that is
# NOT yet positively classified as genuine-unavailable or data-integrity: a
# transient/unknown failure gets this many total attempts before the symbol's
# coverage is declared unresolved and failed closed.
TRANSIENT_CHUNK_RETRY_ATTEMPTS = 2
# MT5 last_error() numeric codes that positively confirm "no data found" for
# an already symbol_info()-verified, visible symbol — as opposed to a
# connection/timeout/internal failure, which stays generic/transient. Mapped
# from the official MetaTrader5 Python package RES_* constants: RES_E_FAIL,
# RES_E_INTERNAL_FAIL*, and IPC/connect/timeout codes are all left OUT of this
# set on purpose because they do not prove historical unavailability.
_MT5_GENUINE_NO_HISTORY_LAST_ERROR_CODES = frozenset({-4})  # RES_E_NOT_FOUND
EXECUTION_UNIVERSE_CANONICAL_SCHEMA_VERSION = "ser8-execution-universe-canonical-v1"
CHUNK_ACQUISITION_CODE_SHA256 = (
    "sha256:34a3d2633b744942eee35ab72d291bb5205275abfc4c5a38bd122f83e02607da"
)
SOURCE_TYPE = "MT5_PYTHON_COPY_RATES_RANGE"
SER8_EXECUTION_ACCOUNT_LOGIN = "67206924"
SER8_ACTIVE_MARKET_DATA_ACCOUNT_LOGIN = "77053345"
READ_ONLY_MT5_OPERATIONS = (
    "initialize",
    "terminal_info",
    "account_info",
    "version",
    "symbol_info",
    "copy_rates_range",
)
BAR_FIELDS = (
    "time_utc",
    "symbol",
    "timeframe",
    "open",
    "high",
    "low",
    "close",
    "tick_volume",
    "spread",
    "real_volume",
)
_DATASET_HASH_DOMAIN = b"trademind:ser8:historical-market-data:v2"
_INVENTORY_HASH_DOMAIN = b"trademind:ser8:historical-data-inventory:v2"
_CHUNK_SOURCE_HASH_DOMAIN = b"trademind:ser8:historical-chunk-source:v1"
_EXECUTION_UNIVERSE_HASH_DOMAIN = b"trademind:ser8:execution-universe-canonical:v1"
EXECUTION_UNIVERSE_SOURCE_FIELDS = (
    "time_msc",
    "account_login",
    "server",
    "currency",
    "symbol",
    "digits",
    "trade_mode",
    "bid",
    "ask",
    "tick_size",
    "tick_value",
    "tick_value_profit",
    "tick_value_loss",
    "volume_min",
    "volume_max",
    "volume_step",
    "contract_size",
    "margin_initial",
    "margin_maintenance",
    "margin_buy_per_volume",
    "margin_sell_per_volume",
    "leverage",
    "expiration_mode_flags",
)
EXECUTION_UNIVERSE_AUDIT_VOLATILE_FIELDS = (
    "time_msc",
    "bid",
    "ask",
    "margin_buy_per_volume",
    "margin_sell_per_volume",
)
EXECUTION_UNIVERSE_IDENTITY_RELEVANT_FIELDS = tuple(
    field
    for field in EXECUTION_UNIVERSE_SOURCE_FIELDS
    if field not in EXECUTION_UNIVERSE_AUDIT_VOLATILE_FIELDS
)
EXECUTION_UNIVERSE_DERIVED_IDENTITY_FIELDS = (
    "asset_class",
    "risk_model_supported",
)
_EXECUTION_UNIVERSE_NUMERIC_FIELDS = (
    "digits",
    "tick_size",
    "tick_value",
    "tick_value_profit",
    "tick_value_loss",
    "volume_min",
    "volume_max",
    "volume_step",
    "contract_size",
    "margin_initial",
    "margin_maintenance",
    "leverage",
    "expiration_mode_flags",
)
_LIVE_ARTIFACT_PATH_PARTS = frozenset({
    "live_signal_runtime_v1",
    "signal_intelligence_v1_16",
    "paper_signals",
})


class HistoricalDataError(RuntimeError):
    """Fail-closed acquisition, integrity, or publication error."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def validate_active_account_contract(
    *,
    execution_account_login: str,
    market_data_account_login: str,
) -> tuple[str, str]:
    execution_account = str(execution_account_login).strip()
    market_data_account = str(market_data_account_login).strip()
    if execution_account != SER8_EXECUTION_ACCOUNT_LOGIN:
        raise HistoricalDataError(
            "SER8_EXECUTION_ACCOUNT_NOT_ACTIVE",
            f"active SER8 execution account must be {SER8_EXECUTION_ACCOUNT_LOGIN}",
        )
    if market_data_account != SER8_ACTIVE_MARKET_DATA_ACCOUNT_LOGIN:
        raise HistoricalDataError(
            "SER8_MARKET_DATA_ACCOUNT_NOT_ACTIVE",
            f"active SER8 market-data account must be {SER8_ACTIVE_MARKET_DATA_ACCOUNT_LOGIN}",
        )
    return execution_account, market_data_account


@dataclass(frozen=True, slots=True)
class BrokerSymbolV1:
    symbol: str
    trade_mode: str
    source_row: Mapping[str, str]
    asset_class: str
    risk_model_supported: bool
    risk_model_reason: str


@dataclass(frozen=True, slots=True)
class CanonicalExecutionUniverseV1:
    """Parsed broker universe plus stable semantic and volatile raw identities."""

    symbols: tuple[BrokerSymbolV1, ...]
    raw_sha256: str
    canonical_sha256: str
    canonical_snapshot: Mapping[str, object]

    @property
    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(dict(self.canonical_snapshot))


@dataclass(frozen=True, slots=True)
class HistoricalChunkV1:
    """One deterministic half-open logical UTC acquisition window."""

    chunk_id: str
    chunk_from_utc: datetime
    chunk_to_utc: datetime

    def __post_init__(self) -> None:
        start = _as_utc(self.chunk_from_utc)
        end = _as_utc(self.chunk_to_utc)
        if end <= start:
            raise HistoricalDataError("INVALID_CHUNK_RANGE", "chunk end must follow chunk start")
        object.__setattr__(self, "chunk_from_utc", start)
        object.__setattr__(self, "chunk_to_utc", end)


@dataclass(frozen=True, slots=True)
class ChunkedAcquisitionV1:
    """Bars and audit state from a complete deterministic chunk plan attempt."""

    bars: tuple[HistoricalBarV1, ...]
    chunk_audit: tuple[Mapping[str, object], ...]
    requested_chunk_count: int
    completed_chunk_count: int
    empty_chunk_count: int
    failed_chunk_count: int
    cached_chunk_count: int
    acquired_chunk_count: int
    merge_integrity_error_code: str | None = None
    merge_integrity_error: str | None = None

    @property
    def capture_complete(self) -> bool:
        return (
            self.requested_chunk_count > 0
            and self.completed_chunk_count == self.requested_chunk_count
            and self.failed_chunk_count == 0
            and self.merge_integrity_error_code is None
        )

    def manifest_summary(self) -> dict[str, object]:
        return {
            "chunk_policy_version": CHUNK_POLICY_VERSION,
            "requested_chunk_count": self.requested_chunk_count,
            "completed_chunk_count": self.completed_chunk_count,
            "empty_chunk_count": self.empty_chunk_count,
            "failed_chunk_count": self.failed_chunk_count,
            "cached_chunk_count": self.cached_chunk_count,
            "acquired_chunk_count": self.acquired_chunk_count,
            "chunk_audit": [dict(item) for item in self.chunk_audit],
            "historical_capture_complete": self.capture_complete,
        }


@dataclass(frozen=True, slots=True)
class CoverageDiscoveryV1:
    """Backward-suffix broker-coverage discovery result for one symbol.

    Chunks are attempted from ``requested_to_utc`` backward. A chunk failure
    is never itself the boundary: it is first positively classified.

    - ``GENUINE_HISTORICAL_UNAVAILABLE`` (a closed, explicit error-code
      allowlist — see ``GENUINE_HISTORICAL_UNAVAILABLE_ERROR_CODES``) is the
      ONLY classification allowed to fix a truncated coverage boundary. Once
      fixed, every older chunk is recorded as ``SKIPPED_UNAVAILABLE_PREFIX``
      without any further cache lookup or broker call.
    - ``DATA_INTEGRITY`` (``DATA_INTEGRITY_CHUNK_ERROR_CODES``, or a merge
      conflict between two already-accepted chunks) always fails the whole
      symbol closed: nothing is published, and whatever chunks had already
      been individually accepted are discarded rather than silently kept as
      a "truncated" dataset that hides the failing chunk.
    - Anything else (``UNRESOLVED_TRANSIENT_OR_UNKNOWN`` — a transient read
      failure, or any other unrecognized code) gets a bounded, deterministic
      retry (``TRANSIENT_CHUNK_RETRY_ATTEMPTS``); if it still fails, coverage
      for the whole symbol is left unresolved and fails closed exactly like a
      data-integrity failure. It is never reinterpreted as "short history".

    Because only a positively-classified genuine boundary can ever truncate
    coverage, an intermittent valid/failed/valid pattern is never silently
    bridged: a transient or integrity failure anywhere in the walk discards
    the whole symbol rather than exposing a partial suffix around it.
    """

    bars: tuple[HistoricalBarV1, ...]
    requested_from_utc: datetime
    requested_to_utc: datetime
    effective_from_utc: datetime | None
    effective_to_utc: datetime | None
    accepted_chunk_audit: tuple[Mapping[str, object], ...]
    unavailable_prefix_chunk_audit: tuple[Mapping[str, object], ...]
    discarded_chunk_audit: tuple[Mapping[str, object], ...]
    abandoned_chunk_audit: tuple[Mapping[str, object], ...]
    requested_chunk_count: int
    accepted_chunk_count: int
    empty_chunk_count: int
    cached_chunk_count: int
    acquired_chunk_count: int
    coverage_truncated_at_requested_start: bool
    truncation_reason_code: str | None
    truncation_reason: str | None
    unresolved_error_code: str | None = None
    unresolved_error: str | None = None
    integrity_error_code: str | None = None
    integrity_error: str | None = None
    merge_integrity_error_code: str | None = None
    merge_integrity_error: str | None = None

    @property
    def resolution(self) -> str:
        if self.unresolved_error_code is not None:
            return "UNRESOLVED_TRANSIENT_FAILURE"
        if self.integrity_error_code is not None or self.merge_integrity_error_code is not None:
            return "DATA_INTEGRITY_FAILED"
        if self.coverage_truncated_at_requested_start:
            return "TRUNCATED_GENUINE_BOUNDARY"
        return "COMPLETE"

    @property
    def coverage_available(self) -> bool:
        return (
            self.accepted_chunk_count > 0
            and self.unresolved_error_code is None
            and self.integrity_error_code is None
            and self.merge_integrity_error_code is None
        )

    @property
    def capture_complete(self) -> bool:
        return (
            self.requested_chunk_count > 0
            and self.accepted_chunk_count == self.requested_chunk_count
            and self.resolution == "COMPLETE"
        )

    def manifest_summary(self) -> dict[str, object]:
        unavailable_prefix_chunk_count = len(self.unavailable_prefix_chunk_audit)
        return {
            "coverage_discovery_policy_version": COVERAGE_DISCOVERY_POLICY_VERSION,
            "chunk_policy_version": CHUNK_POLICY_VERSION,
            "coverage_resolution": self.resolution,
            "requested_chunk_count": self.requested_chunk_count,
            "accepted_chunk_count": self.accepted_chunk_count,
            "empty_chunk_count": self.empty_chunk_count,
            "cached_chunk_count": self.cached_chunk_count,
            "acquired_chunk_count": self.acquired_chunk_count,
            "unavailable_prefix_chunk_count": unavailable_prefix_chunk_count,
            "discarded_chunk_count": len(self.discarded_chunk_audit),
            "abandoned_chunk_count": len(self.abandoned_chunk_audit),
            "chunk_audit": [dict(item) for item in self.accepted_chunk_audit],
            "unavailable_prefix_chunk_audit": [
                dict(item) for item in self.unavailable_prefix_chunk_audit
            ],
            "discarded_chunk_audit": [dict(item) for item in self.discarded_chunk_audit],
            "abandoned_chunk_audit": [dict(item) for item in self.abandoned_chunk_audit],
            "coverage_truncated_at_requested_start": self.coverage_truncated_at_requested_start,
            "truncation_reason_code": self.truncation_reason_code,
            "truncation_reason": self.truncation_reason,
            "requested_coverage_start_utc": _utc_text(self.requested_from_utc),
            "requested_coverage_end_utc": _utc_text(self.requested_to_utc),
            "effective_coverage_start_utc": (
                _utc_text(self.effective_from_utc) if self.effective_from_utc is not None else None
            ),
            "effective_coverage_end_utc": (
                _utc_text(self.effective_to_utc) if self.effective_to_utc is not None else None
            ),
            "historical_capture_complete": self.capture_complete,
            "unresolved_error_code": self.unresolved_error_code,
            "unresolved_error": self.unresolved_error,
            "integrity_error_code": self.integrity_error_code,
            "integrity_error": self.integrity_error,
            "merge_integrity_error_code": self.merge_integrity_error_code,
            "merge_integrity_error": self.merge_integrity_error,
        }


@dataclass(frozen=True, slots=True)
class HistoricalBarV1:
    time_utc: datetime
    symbol: str
    timeframe: str
    open: float
    high: float
    low: float
    close: float
    tick_volume: int
    spread: int
    real_volume: int

    def __post_init__(self) -> None:
        if self.time_utc.tzinfo is None or self.time_utc.utcoffset() is None:
            raise HistoricalDataError("BAR_TIME_NOT_UTC_AWARE", "bar time must be timezone-aware")
        object.__setattr__(self, "time_utc", self.time_utc.astimezone(timezone.utc))
        object.__setattr__(self, "symbol", self.symbol.strip().upper())
        object.__setattr__(self, "timeframe", self.timeframe.strip().upper())
        if not self.symbol or not self.timeframe:
            raise HistoricalDataError("BAR_IDENTITY_MISSING", "bar symbol/timeframe is required")

    def csv_row(self) -> dict[str, str]:
        return {
            "time_utc": _utc_text(self.time_utc),
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "open": _number_text(self.open),
            "high": _number_text(self.high),
            "low": _number_text(self.low),
            "close": _number_text(self.close),
            "tick_volume": str(self.tick_volume),
            "spread": str(self.spread),
            "real_volume": str(self.real_volume),
        }


class HistoricalRateSource(Protocol):
    def source_proof(self) -> Mapping[str, object]: ...

    def symbol_metadata(self, symbol: str) -> Mapping[str, object]: ...

    def copy_rates(
        self,
        symbol: str,
        timeframe: str,
        requested_from_utc: datetime,
        requested_to_utc: datetime,
    ) -> Sequence[HistoricalBarV1]: ...


def _utc_text(value: datetime) -> str:
    return _as_utc(value).isoformat(timespec="seconds").replace("+00:00", "Z")


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise HistoricalDataError("TIME_NOT_AWARE", "timestamp must include timezone information")
    return value.astimezone(timezone.utc)


def parse_utc(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise HistoricalDataError("INVALID_UTC_TIMESTAMP", f"invalid UTC timestamp: {value!r}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise HistoricalDataError("INVALID_UTC_TIMESTAMP", "timestamp must include a UTC offset")
    return parsed.astimezone(timezone.utc)


def _next_utc_month(value: datetime) -> datetime:
    value = _as_utc(value)
    if value.month == 12:
        return datetime(value.year + 1, 1, 1, tzinfo=timezone.utc)
    return datetime(value.year, value.month + 1, 1, tzinfo=timezone.utc)


def plan_calendar_month_chunks(
    requested_from_utc: datetime,
    requested_to_utc: datetime,
) -> tuple[HistoricalChunkV1, ...]:
    """Split an authoritative range at deterministic calendar-month UTC boundaries."""
    start = _as_utc(requested_from_utc)
    end = _as_utc(requested_to_utc)
    if end <= start:
        raise HistoricalDataError("INVALID_TIME_RANGE", "requested_to_utc must follow requested_from_utc")
    chunks: list[HistoricalChunkV1] = []
    cursor = start
    while cursor < end:
        boundary = min(_next_utc_month(cursor), end)
        chunk_id = f"{cursor:%Y%m%dT%H%M%SZ}__{boundary:%Y%m%dT%H%M%SZ}"
        chunks.append(
            HistoricalChunkV1(
                chunk_id=chunk_id,
                chunk_from_utc=cursor,
                chunk_to_utc=boundary,
            )
        )
        cursor = boundary
    return tuple(chunks)


def _number_text(value: float) -> str:
    return format(float(value), ".17g")


def _positive_finite_or_none(value: object) -> float | None:
    try:
        parsed = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) and parsed > 0 else None


def _mt5_last_error_code(last_error: object) -> int | None:
    """Best-effort parse of MT5 ``last_error()``'s leading numeric code."""
    if isinstance(last_error, (tuple, list)) and last_error:
        try:
            return int(last_error[0])
        except (TypeError, ValueError):
            return None
    return None


def _classify_chunk_failure_code(error_code: str | None) -> str:
    """Positively classify one chunk-level failure code.

    Only ``GENUINE_HISTORICAL_UNAVAILABLE`` may ever establish a coverage
    boundary. ``DATA_INTEGRITY`` always fails the symbol closed. Everything
    else — including plain acquisition/read failures and unrecognized codes —
    is ``UNRESOLVED_TRANSIENT_OR_UNKNOWN`` and must never silently shorten
    coverage; it is only ever subject to the bounded retry policy and, if
    still unresolved, fails the symbol closed.
    """
    if error_code in GENUINE_HISTORICAL_UNAVAILABLE_ERROR_CODES:
        return "GENUINE_HISTORICAL_UNAVAILABLE"
    if error_code in DATA_INTEGRITY_CHUNK_ERROR_CODES:
        return "DATA_INTEGRITY"
    return "UNRESOLVED_TRANSIENT_OR_UNKNOWN"


def _sha256_hex(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _assert_not_live_artifact_path(path: Path, *, field_name: str) -> Path:
    resolved = path.expanduser().resolve()
    lowered = {part.lower() for part in resolved.parts}
    forbidden = lowered & _LIVE_ARTIFACT_PATH_PARTS
    if forbidden or any(part.startswith("live_signal_runtime") for part in lowered):
        raise HistoricalDataError(
            "LIVE_ARTIFACT_PATH_FORBIDDEN",
            f"{field_name} cannot target a live runtime/journal path: {resolved}",
        )
    return resolved


def assert_historical_artifact_isolation(
    *,
    dataset_root: Path,
    inventory_path: Path,
    compatibility_path: Path,
) -> tuple[Path, Path, Path]:
    root = _assert_not_live_artifact_path(dataset_root, field_name="dataset_root")
    inventory = _assert_not_live_artifact_path(inventory_path, field_name="inventory")
    compatibility = _assert_not_live_artifact_path(
        compatibility_path, field_name="compatibility_csv"
    )
    for path, expected_name in (
        (inventory, "historical_inventory.json"),
        (compatibility, "historical_rows.csv"),
    ):
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise HistoricalDataError(
                "HISTORICAL_ARTIFACT_PATH_OUTSIDE_ROOT",
                f"{path.name} must be inside dataset_root",
            ) from exc
        if path.name != expected_name:
            raise HistoricalDataError(
                "HISTORICAL_ARTIFACT_NAME_INVALID",
                f"historical artifact must be named {expected_name}",
            )
    return root, inventory, compatibility


def _canonical_decimal_text(value: object, *, field_name: str) -> str:
    text = str(value or "").strip().replace(",", ".")
    try:
        parsed = Decimal(text)
    except (InvalidOperation, ValueError) as exc:
        raise HistoricalDataError(
            "BROKER_UNIVERSE_FIELD_MALFORMED",
            f"execution-universe field {field_name} must be numeric",
        ) from exc
    if not parsed.is_finite():
        raise HistoricalDataError(
            "BROKER_UNIVERSE_FIELD_MALFORMED",
            f"execution-universe field {field_name} must be finite",
        )
    if parsed == 0:
        return "0"
    canonical = format(parsed.normalize(), "f")
    return canonical.rstrip("0").rstrip(".") if "." in canonical else canonical


def _canonicalize_execution_universe_row(
    row: Mapping[str, object],
    *,
    account_login: str,
) -> dict[str, object]:
    actual_login = str(row.get("account_login") or "").strip()
    if actual_login != account_login:
        raise HistoricalDataError(
            "BROKER_UNIVERSE_ACCOUNT_MISMATCH",
            f"symbol export account {actual_login!r} does not match {account_login!r}",
        )
    symbol = str(row.get("symbol") or "").strip().upper()
    server = str(row.get("server") or "").strip()
    currency = str(row.get("currency") or "").strip().upper()
    trade_mode = str(row.get("trade_mode") or "").strip().upper()
    if not symbol:
        raise HistoricalDataError("BROKER_SYMBOL_MISSING", "broker universe contains an empty symbol")
    if not server or not currency or not trade_mode:
        raise HistoricalDataError(
            "BROKER_UNIVERSE_FIELD_MALFORMED",
            f"{symbol} server, currency, and trade_mode must be non-empty",
        )
    normalized: dict[str, object] = {
        "account_login": actual_login,
        "server": server,
        "currency": currency,
        "symbol": symbol,
        "trade_mode": trade_mode,
    }
    for field in _EXECUTION_UNIVERSE_NUMERIC_FIELDS:
        normalized[field] = _canonical_decimal_text(row.get(field), field_name=field)
    string_row = {key: str(value) for key, value in normalized.items()}
    supported, _reason = risk_model_support_for_symbol_row(string_row)
    normalized["asset_class"] = classify_asset_class(symbol)
    normalized["risk_model_supported"] = supported
    return normalized


def _canonical_universe_contract() -> dict[str, object]:
    return {
        "identity_relevant_source_fields": list(EXECUTION_UNIVERSE_IDENTITY_RELEVANT_FIELDS),
        "audit_volatile_source_fields": list(EXECUTION_UNIVERSE_AUDIT_VOLATILE_FIELDS),
        "derived_identity_fields": list(EXECUTION_UNIVERSE_DERIVED_IDENTITY_FIELDS),
        "numeric_representation": "FINITE_DECIMAL_CANONICAL_STRING",
        "server_representation": "TRIMMED_CASE_PRESERVED_EXECUTION_SOURCE_IDENTITY",
        "row_order": "EXACT_NORMALIZED_SYMBOL_ASCENDING",
        "duplicate_symbol_policy": "IDENTICAL_NORMALIZED_ROWS_DEDUPLICATED_CONFLICTS_REJECTED",
    }


def _canonical_execution_universe_sha256(snapshot: Mapping[str, object]) -> str:
    return sha256_bytes(
        _EXECUTION_UNIVERSE_HASH_DOMAIN + b"\x00" + canonical_json_bytes(dict(snapshot))
    )


def verify_canonical_execution_universe_snapshot(
    snapshot: Mapping[str, object],
    *,
    expected_sha256: str | None = None,
) -> str:
    expected_keys = {
        "schema_version",
        "execution_account_login",
        "canonicalization_contract",
        "row_count",
        "rows",
    }
    if set(snapshot) != expected_keys:
        raise HistoricalDataError(
            "CANONICAL_UNIVERSE_SNAPSHOT_INVALID",
            "canonical execution-universe snapshot fields are invalid",
        )
    if snapshot.get("schema_version") != EXECUTION_UNIVERSE_CANONICAL_SCHEMA_VERSION:
        raise HistoricalDataError(
            "CANONICAL_UNIVERSE_SCHEMA_INVALID",
            "canonical execution-universe schema version is invalid",
        )
    account_login = str(snapshot.get("execution_account_login") or "")
    if account_login != SER8_EXECUTION_ACCOUNT_LOGIN:
        raise HistoricalDataError(
            "SER8_EXECUTION_ACCOUNT_NOT_ACTIVE",
            f"canonical universe must belong to {SER8_EXECUTION_ACCOUNT_LOGIN}",
        )
    if snapshot.get("canonicalization_contract") != _canonical_universe_contract():
        raise HistoricalDataError(
            "CANONICAL_UNIVERSE_CONTRACT_INVALID",
            "canonical execution-universe field classification is invalid",
        )
    rows = snapshot.get("rows")
    if not isinstance(rows, list) or not rows or snapshot.get("row_count") != len(rows):
        raise HistoricalDataError(
            "CANONICAL_UNIVERSE_SNAPSHOT_INVALID",
            "canonical execution-universe rows are missing or inconsistent",
        )
    expected_row_keys = set(EXECUTION_UNIVERSE_IDENTITY_RELEVANT_FIELDS) | set(
        EXECUTION_UNIVERSE_DERIVED_IDENTITY_FIELDS
    )
    symbols: list[str] = []
    for row in rows:
        if not isinstance(row, dict) or set(row) != expected_row_keys:
            raise HistoricalDataError(
                "CANONICAL_UNIVERSE_ROW_INVALID",
                "canonical execution-universe row fields are invalid",
            )
        normalized = _canonicalize_execution_universe_row(row, account_login=account_login)
        if normalized != row:
            raise HistoricalDataError(
                "CANONICAL_UNIVERSE_ROW_NOT_CANONICAL",
                f"canonical execution-universe row is not normalized: {row.get('symbol')}",
            )
        symbols.append(str(row["symbol"]))
    if symbols != sorted(symbols) or len(symbols) != len(set(symbols)):
        raise HistoricalDataError(
            "CANONICAL_UNIVERSE_ORDER_INVALID",
            "canonical execution-universe symbols must be sorted and unique",
        )
    actual_sha256 = _canonical_execution_universe_sha256(snapshot)
    if expected_sha256 is not None and actual_sha256 != expected_sha256:
        raise HistoricalDataError(
            "CANONICAL_UNIVERSE_HASH_MISMATCH",
            "canonical execution-universe snapshot hash mismatch",
        )
    return actual_sha256


def build_canonical_execution_universe(
    rows: Sequence[Mapping[str, object]],
    *,
    account_login: str,
    raw_sha256: str,
) -> CanonicalExecutionUniverseV1:
    """Build stable semantics from already parsed authoritative export rows."""
    expected_login = str(account_login).strip()
    if expected_login != SER8_EXECUTION_ACCOUNT_LOGIN:
        raise HistoricalDataError(
            "SER8_EXECUTION_ACCOUNT_NOT_ACTIVE",
            f"active SER8 execution account must be {SER8_EXECUTION_ACCOUNT_LOGIN}",
        )
    if re.fullmatch(r"sha256:[0-9a-f]{64}", raw_sha256) is None:
        raise HistoricalDataError(
            "BROKER_UNIVERSE_RAW_HASH_INVALID",
            "raw execution-universe SHA-256 is malformed",
        )
    by_symbol: dict[str, dict[str, object]] = {}
    for raw_row in rows:
        normalized = _canonicalize_execution_universe_row(raw_row, account_login=expected_login)
        symbol = str(normalized["symbol"])
        existing = by_symbol.get(symbol)
        if existing is not None and existing != normalized:
            raise HistoricalDataError(
                "BROKER_SYMBOL_CONFLICT",
                f"broker universe contains conflicting normalized rows for {symbol}",
            )
        by_symbol[symbol] = normalized
    if not by_symbol:
        raise HistoricalDataError("BROKER_UNIVERSE_EMPTY", "broker universe contains no rows")
    servers = {str(row["server"]) for row in by_symbol.values()}
    if len(servers) != 1:
        raise HistoricalDataError(
            "BROKER_UNIVERSE_SERVER_MISMATCH",
            "broker universe contains multiple execution-account servers",
        )
    canonical_rows = [by_symbol[symbol] for symbol in sorted(by_symbol)]
    snapshot: dict[str, object] = {
        "schema_version": EXECUTION_UNIVERSE_CANONICAL_SCHEMA_VERSION,
        "execution_account_login": expected_login,
        "canonicalization_contract": _canonical_universe_contract(),
        "row_count": len(canonical_rows),
        "rows": canonical_rows,
    }
    canonical_sha256 = verify_canonical_execution_universe_snapshot(snapshot)
    symbols: list[BrokerSymbolV1] = []
    for row in canonical_rows:
        string_row = {
            key: str(value)
            for key, value in row.items()
            if key in EXECUTION_UNIVERSE_IDENTITY_RELEVANT_FIELDS
        }
        supported, reason = risk_model_support_for_symbol_row(string_row)
        symbols.append(
            BrokerSymbolV1(
                symbol=str(row["symbol"]),
                trade_mode=str(row["trade_mode"]),
                source_row=string_row,
                asset_class=str(row["asset_class"]),
                risk_model_supported=supported,
                risk_model_reason=reason,
            )
        )
    return CanonicalExecutionUniverseV1(
        symbols=tuple(symbols),
        raw_sha256=raw_sha256,
        canonical_sha256=canonical_sha256,
        canonical_snapshot=snapshot,
    )


def load_canonical_execution_universe(
    symbols_csv: Path,
    *,
    account_login: str,
) -> CanonicalExecutionUniverseV1:
    """Parse the authoritative raw export into stable execution semantics."""
    if not symbols_csv.is_file():
        raise HistoricalDataError("BROKER_UNIVERSE_MISSING", f"broker universe not found: {symbols_csv}")
    raw_bytes = symbols_csv.read_bytes()
    try:
        text = raw_bytes.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise HistoricalDataError(
            "BROKER_UNIVERSE_ENCODING_INVALID",
            "broker universe must be UTF-8 CSV",
        ) from exc
    reader = csv.DictReader(io.StringIO(text, newline=""))
    fieldnames = reader.fieldnames or []
    if len(fieldnames) != len(set(fieldnames)):
        raise HistoricalDataError(
            "BROKER_UNIVERSE_COLUMNS_DUPLICATE",
            "broker universe contains duplicate column names",
        )
    missing = [field for field in EXECUTION_UNIVERSE_SOURCE_FIELDS if field not in fieldnames]
    if missing:
        raise HistoricalDataError(
            "BROKER_UNIVERSE_COLUMNS_MISSING",
            f"broker universe is missing required columns: {missing}",
        )
    unclassified = [field for field in fieldnames if field not in EXECUTION_UNIVERSE_SOURCE_FIELDS]
    if unclassified:
        raise HistoricalDataError(
            "BROKER_UNIVERSE_COLUMNS_UNCLASSIFIED",
            f"broker universe contains unclassified columns: {unclassified}",
        )
    rows: list[dict[str, object]] = []
    for raw_row in reader:
        if None in raw_row:
            raise HistoricalDataError(
                "BROKER_UNIVERSE_ROW_MALFORMED",
                "broker universe row has more values than classified columns",
            )
        rows.append(dict(raw_row))
    return build_canonical_execution_universe(
        rows,
        account_login=account_login,
        raw_sha256=sha256_bytes(raw_bytes),
    )


def load_broker_universe(symbols_csv: Path, *, account_login: str) -> tuple[BrokerSymbolV1, ...]:
    """Backward-compatible view of the canonical authoritative universe."""
    return load_canonical_execution_universe(
        symbols_csv,
        account_login=account_login,
    ).symbols


def verify_cross_account_symbol_compatibility(
    *,
    broker_symbol: BrokerSymbolV1,
    market_data_symbol_metadata: Mapping[str, object],
) -> dict[str, object]:
    """Bind source metadata to one symbol already present in the execution universe."""
    execution_symbol = broker_symbol.symbol.strip().upper()
    market_data_symbol = str(market_data_symbol_metadata.get("name") or "").strip().upper()
    if market_data_symbol != execution_symbol:
        raise HistoricalDataError(
            "BROKER_SYMBOL_IDENTITY_MISMATCH",
            f"market-data source returned {market_data_symbol!r} for execution symbol {execution_symbol!r}",
        )

    execution_tick_size = _positive_finite_or_none(broker_symbol.source_row.get("tick_size"))
    market_data_tick_size = _positive_finite_or_none(
        market_data_symbol_metadata.get("trade_tick_size")
    )
    tick_size_compared = execution_tick_size is not None and market_data_tick_size is not None
    tick_size_compatible: bool | None = None
    if tick_size_compared:
        tick_size_compatible = math.isclose(
            execution_tick_size,
            market_data_tick_size,
            rel_tol=1e-9,
            abs_tol=max(execution_tick_size, market_data_tick_size) * 1e-12,
        )
        if not tick_size_compatible:
            raise HistoricalDataError(
                "BROKER_SYMBOL_SEMANTICS_MISMATCH",
                f"{execution_symbol} trade tick size differs: execution={execution_tick_size!r}, "
                f"market_data={market_data_tick_size!r}",
            )

    return {
        "execution_symbol_in_universe": True,
        "execution_symbol": execution_symbol,
        "market_data_symbol": market_data_symbol,
        "exact_symbol_identity_verified": True,
        "execution_trade_mode": broker_symbol.trade_mode,
        "market_data_trade_mode": market_data_symbol_metadata.get("trade_mode_name"),
        "execution_tick_size": execution_tick_size,
        "market_data_trade_tick_size": market_data_tick_size,
        "trade_tick_size_compared": tick_size_compared,
        "trade_tick_size_compatible": tick_size_compatible,
    }


class MetaTrader5HistorySource:
    """Narrow read-only adapter around the official MetaTrader5 package."""

    def __init__(
        self,
        *,
        market_data_account_login: str,
        terminal_path: Path | None = None,
        module: Any | None = None,
    ) -> None:
        self.market_data_account_login = str(market_data_account_login).strip()
        if self.market_data_account_login != SER8_ACTIVE_MARKET_DATA_ACCOUNT_LOGIN:
            raise HistoricalDataError(
                "SER8_MARKET_DATA_ACCOUNT_NOT_ACTIVE",
                f"active SER8 market-data account must be {SER8_ACTIVE_MARKET_DATA_ACCOUNT_LOGIN}",
            )
        self.terminal_path = terminal_path
        self._mt5 = module
        self._proof: dict[str, object] | None = None

    def initialize(self) -> Mapping[str, object]:
        if self._mt5 is None:
            try:
                self._mt5 = importlib.import_module("MetaTrader5")
            except ImportError as exc:
                raise HistoricalDataError(
                    "MT5_PYTHON_UNAVAILABLE",
                    "MetaTrader5 Python package is not installed in this environment",
                ) from exc
        arguments = (str(self.terminal_path),) if self.terminal_path is not None else ()
        if not self._mt5.initialize(*arguments):
            raise HistoricalDataError("MT5_INITIALIZE_FAILED", f"MT5 initialize failed: {self._last_error()}")
        terminal = self._mt5.terminal_info()
        account = self._mt5.account_info()
        if terminal is None or account is None:
            self.close()
            raise HistoricalDataError("MT5_IDENTITY_UNAVAILABLE", "terminal/account identity is unavailable")
        if not bool(getattr(terminal, "connected", False)):
            self.close()
            raise HistoricalDataError("MT5_NOT_CONNECTED", "MT5 terminal is not connected")
        actual_login = str(getattr(account, "login", ""))
        if actual_login != self.market_data_account_login:
            self.close()
            raise HistoricalDataError(
                "MT5_ACCOUNT_MISMATCH",
                "active MT5 market-data account "
                f"{actual_login!r} does not match {self.market_data_account_login!r}",
            )
        server = str(getattr(account, "server", "") or "").strip()
        company = str(getattr(account, "company", "") or "").strip()
        terminal_company = str(getattr(terminal, "company", "") or "").strip()
        if not server or not (company or terminal_company):
            self.close()
            raise HistoricalDataError("MT5_BROKER_IDENTITY_MISSING", "MT5 broker/server identity is incomplete")
        version = self._mt5.version()
        self._proof = {
            "schema_version": SOURCE_PROOF_SCHEMA_VERSION,
            "source_type": SOURCE_TYPE,
            "market_data_account_login": actual_login,
            "market_data_account_server": server,
            "market_data_account_company": company or None,
            "market_data_account_currency": str(getattr(account, "currency", "") or "") or None,
            "terminal_company": terminal_company or None,
            "terminal_name": str(getattr(terminal, "name", "") or "") or None,
            "terminal_path": str(getattr(terminal, "path", "") or "") or None,
            "terminal_connected": True,
            "terminal_trade_allowed_observed": bool(getattr(terminal, "trade_allowed", False)),
            "mt5_version": list(version) if version is not None else None,
            "authenticated_market_data_account_verified": True,
            "utc_contract": "copy_rates_range inputs and returned epoch seconds interpreted as UTC",
            "read_only_operations": list(READ_ONLY_MT5_OPERATIONS),
        }
        return dict(self._proof)

    def _last_error(self) -> object:
        return self._mt5.last_error() if self._mt5 is not None else None

    def close(self) -> None:
        if self._mt5 is not None:
            self._mt5.shutdown()

    def source_proof(self) -> Mapping[str, object]:
        if self._proof is None:
            return self.initialize()
        return dict(self._proof)

    def symbol_metadata(self, symbol: str) -> Mapping[str, object]:
        self.source_proof()
        requested = symbol.strip().upper()
        info = self._mt5.symbol_info(requested)
        if info is None:
            raise HistoricalDataError("BROKER_SYMBOL_UNAVAILABLE", f"MT5 symbol_info unavailable for {requested}")
        actual = str(getattr(info, "name", "") or "").strip().upper()
        if actual != requested:
            raise HistoricalDataError(
                "BROKER_SYMBOL_IDENTITY_MISMATCH",
                f"MT5 returned symbol identity {actual!r} for {requested!r}",
            )
        point = float(getattr(info, "point", 0.0) or 0.0)
        if not math.isfinite(point) or point <= 0:
            raise HistoricalDataError("BROKER_SYMBOL_POINT_INVALID", f"MT5 point is invalid for {requested}")
        trade_mode_code = int(getattr(info, "trade_mode", -1))
        trade_mode_name = next(
            (
                name
                for name in ("DISABLED", "CLOSEONLY", "LONGONLY", "SHORTONLY", "FULL")
                if trade_mode_code == getattr(self._mt5, f"SYMBOL_TRADE_MODE_{name}", object())
            ),
            "UNKNOWN",
        )
        return {
            "name": actual,
            "visible": bool(getattr(info, "visible", False)),
            "select": bool(getattr(info, "select", False)),
            "trade_mode_code": trade_mode_code,
            "trade_mode_name": trade_mode_name,
            "digits": int(getattr(info, "digits", 0)),
            "point": point,
            "trade_tick_size": _positive_finite_or_none(
                getattr(info, "trade_tick_size", None)
            ),
        }

    def copy_rates(
        self,
        symbol: str,
        timeframe: str,
        requested_from_utc: datetime,
        requested_to_utc: datetime,
    ) -> tuple[HistoricalBarV1, ...]:
        self.source_proof()
        normalized_timeframe = timeframe.strip().upper()
        mt5_timeframe = getattr(self._mt5, f"TIMEFRAME_{normalized_timeframe}", None)
        if mt5_timeframe is None:
            raise HistoricalDataError("MT5_TIMEFRAME_UNSUPPORTED", f"unsupported MT5 timeframe: {timeframe}")
        if requested_from_utc.tzinfo is None or requested_to_utc.tzinfo is None:
            raise HistoricalDataError("TIME_NOT_AWARE", "rate request timestamps must be timezone-aware")
        if requested_to_utc <= requested_from_utc:
            raise HistoricalDataError("INVALID_TIME_RANGE", "requested_to_utc must follow requested_from_utc")
        requested = symbol.strip().upper()
        metadata = self.symbol_metadata(requested)
        if not metadata["visible"]:
            raise HistoricalDataError(
                "BROKER_SYMBOL_DISABLED",
                f"{requested} is not enabled/visible; collector will not mutate Market Watch selection",
            )
        rates = self._mt5.copy_rates_range(
            requested,
            mt5_timeframe,
            requested_from_utc.astimezone(timezone.utc),
            requested_to_utc.astimezone(timezone.utc),
        )
        if rates is None:
            last_error = self._last_error()
            if _mt5_last_error_code(last_error) in _MT5_GENUINE_NO_HISTORY_LAST_ERROR_CODES:
                # last_error() positively reports "not found" for an already
                # symbol_info()-verified, visible symbol: the broker has
                # confirmed there is no data for this window, as opposed to a
                # connection/timeout/internal failure. This is the ONLY signal
                # allowed to establish a genuine historical-unavailability
                # boundary; every other None-return stays generic/transient.
                raise HistoricalDataError(
                    "BROKER_HISTORY_NOT_RETAINED_FOR_RANGE",
                    f"MT5 positively reports no retained history for this window "
                    f"(last_error={last_error})",
                )
            raise HistoricalDataError(
                "MT5_COPY_RATES_FAILED",
                f"copy_rates_range failed with MT5 last_error={last_error}",
            )
        names = tuple(getattr(getattr(rates, "dtype", None), "names", ()) or ())
        bars: list[HistoricalBarV1] = []
        for rate in rates:
            row = {name: rate[name] for name in names} if names else dict(rate)
            bars.append(
                HistoricalBarV1(
                    time_utc=datetime.fromtimestamp(int(row["time"]), tz=timezone.utc),
                    symbol=requested,
                    timeframe=normalized_timeframe,
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    tick_volume=int(row["tick_volume"]),
                    spread=int(row["spread"]),
                    real_volume=int(row["real_volume"]),
                )
            )
        return tuple(bars)


def canonical_bars_csv(bars: Sequence[HistoricalBarV1]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=BAR_FIELDS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(bar.csv_row() for bar in bars)
    return output.getvalue().encode("utf-8")


def load_canonical_bars(path: Path) -> tuple[HistoricalBarV1, ...]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if tuple(reader.fieldnames or ()) != BAR_FIELDS:
            raise HistoricalDataError("BARS_SCHEMA_INVALID", f"canonical bar columns are invalid: {path}")
        bars = []
        for row in reader:
            bars.append(
                HistoricalBarV1(
                    time_utc=parse_utc(row["time_utc"]),
                    symbol=row["symbol"],
                    timeframe=row["timeframe"],
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    tick_volume=int(row["tick_volume"]),
                    spread=int(row["spread"]),
                    real_volume=int(row["real_volume"]),
                )
            )
    return tuple(bars)


def _bar_observation_values(bar: HistoricalBarV1) -> tuple[object, ...]:
    return (
        bar.symbol,
        bar.timeframe,
        bar.open,
        bar.high,
        bar.low,
        bar.close,
        bar.tick_volume,
        bar.spread,
        bar.real_volume,
    )


def merge_historical_bars(
    bars: Sequence[HistoricalBarV1],
    *,
    symbol: str,
    timeframe: str,
) -> tuple[HistoricalBarV1, ...]:
    """Sort and deduplicate identical observations, rejecting conflicts."""
    normalized_symbol = symbol.strip().upper()
    normalized_timeframe = timeframe.strip().upper()
    by_timestamp: dict[datetime, HistoricalBarV1] = {}
    for bar in bars:
        if bar.symbol != normalized_symbol or bar.timeframe != normalized_timeframe:
            raise HistoricalDataError(
                "BAR_IDENTITY_MISMATCH",
                f"bar identity {bar.symbol}/{bar.timeframe} does not match "
                f"{normalized_symbol}/{normalized_timeframe}",
            )
        existing = by_timestamp.get(bar.time_utc)
        if existing is None:
            by_timestamp[bar.time_utc] = bar
            continue
        if _bar_observation_values(existing) != _bar_observation_values(bar):
            raise HistoricalDataError(
                "CONFLICTING_DUPLICATE_BAR",
                f"conflicting {normalized_symbol}/{normalized_timeframe} observations at "
                f"{_utc_text(bar.time_utc)}",
            )
    return tuple(by_timestamp[timestamp] for timestamp in sorted(by_timestamp))


def _chunk_source_identity(source_proof: Mapping[str, object]) -> dict[str, object]:
    identity = {
        "source_type": source_proof.get("source_type"),
        "market_data_account_login": source_proof.get("market_data_account_login"),
        "market_data_account_server": source_proof.get("market_data_account_server"),
        "market_data_account_company": source_proof.get("market_data_account_company")
        or source_proof.get("terminal_company"),
    }
    validate_active_account_contract(
        execution_account_login=SER8_EXECUTION_ACCOUNT_LOGIN,
        market_data_account_login=str(identity["market_data_account_login"] or ""),
    )
    if identity["source_type"] != SOURCE_TYPE or not identity["market_data_account_server"]:
        raise HistoricalDataError(
            "CHUNK_SOURCE_IDENTITY_INVALID",
            "verified MT5 source type, account, and server are required for chunk staging",
        )
    return identity


def _safe_cache_component(value: str) -> str:
    normalized = value.strip().upper()
    visible = re.sub(r"[^A-Z0-9._-]+", "_", normalized).strip("._-") or "IDENTITY"
    suffix = _sha256_hex(normalized.encode("utf-8"))[:12]
    return f"{visible[:48]}-{suffix}"


def chunk_cache_directory(
    staging_root: Path,
    *,
    source_proof: Mapping[str, object],
    symbol: str,
    timeframe: str,
    chunk: HistoricalChunkV1,
) -> Path:
    source_identity = _chunk_source_identity(source_proof)
    digest = hashlib.sha256()
    digest.update(_CHUNK_SOURCE_HASH_DOMAIN)
    digest.update(b"\x00")
    digest.update(canonical_json_bytes(source_identity))
    return (
        staging_root
        / digest.hexdigest()
        / _safe_cache_component(symbol)
        / _safe_cache_component(timeframe)
        / chunk.chunk_id
    )


def _chunk_cache_identity(
    *,
    source_proof: Mapping[str, object],
    symbol: str,
    timeframe: str,
    chunk: HistoricalChunkV1,
    collector_code_sha256: str,
) -> dict[str, object]:
    return {
        "schema_version": CHUNK_CACHE_SCHEMA_VERSION,
        "chunk_policy_version": CHUNK_POLICY_VERSION,
        **_chunk_source_identity(source_proof),
        "symbol": symbol.strip().upper(),
        "timeframe": timeframe.strip().upper(),
        "chunk_id": chunk.chunk_id,
        "chunk_from_utc": _utc_text(chunk.chunk_from_utc),
        "chunk_to_utc": _utc_text(chunk.chunk_to_utc),
        "collector_version": CHUNK_COLLECTOR_VERSION,
        "collector_code_sha256": collector_code_sha256,
    }


def _load_valid_chunk_cache(
    cache_dir: Path,
    *,
    expected_identity: Mapping[str, object],
    chunk: HistoricalChunkV1,
) -> tuple[tuple[HistoricalBarV1, ...], dict[str, object]]:
    manifest_path = cache_dir / "manifest.json"
    bars_path = cache_dir / "bars.csv"
    if cache_dir.is_symlink() or manifest_path.is_symlink() or bars_path.is_symlink():
        raise HistoricalDataError("CHUNK_CACHE_SYMLINK_FORBIDDEN", f"chunk cache contains a symlink: {cache_dir}")
    if not manifest_path.is_file() or not bars_path.is_file():
        raise HistoricalDataError("CHUNK_CACHE_INCOMPLETE", f"chunk cache is incomplete: {cache_dir}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HistoricalDataError("CHUNK_CACHE_INVALID", f"chunk cache manifest is invalid: {cache_dir}") from exc
    if not isinstance(manifest, dict):
        raise HistoricalDataError("CHUNK_CACHE_INVALID", f"chunk cache manifest is not an object: {cache_dir}")
    supplied_manifest_hash = manifest.get("manifest_sha256")
    semantic_manifest = dict(manifest)
    semantic_manifest.pop("manifest_sha256", None)
    if supplied_manifest_hash != _sha256_hex(canonical_json_bytes(semantic_manifest)):
        raise HistoricalDataError("CHUNK_CACHE_MANIFEST_HASH_MISMATCH", f"chunk cache manifest hash mismatch: {cache_dir}")
    for key, expected in expected_identity.items():
        if manifest.get(key) != expected:
            raise HistoricalDataError(
                "CHUNK_CACHE_IDENTITY_MISMATCH",
                f"chunk cache {key} does not match the active request",
            )
    try:
        bars_bytes = bars_path.read_bytes()
    except OSError as exc:
        raise HistoricalDataError("CHUNK_CACHE_INVALID", f"chunk cache bars are unreadable: {cache_dir}") from exc
    if manifest.get("bars_sha256") != sha256_bytes(bars_bytes):
        raise HistoricalDataError("CHUNK_CACHE_BARS_HASH_MISMATCH", f"chunk cache bars hash mismatch: {cache_dir}")
    try:
        bars = load_canonical_bars(bars_path)
    except (HistoricalDataError, OSError, ValueError, TypeError) as exc:
        raise HistoricalDataError("CHUNK_CACHE_BARS_INVALID", f"chunk cache bars are invalid: {cache_dir}") from exc
    status = manifest.get("status")
    if status not in {"COMPLETED", "EMPTY"} or (status == "EMPTY") != (len(bars) == 0):
        raise HistoricalDataError("CHUNK_CACHE_STATUS_INVALID", f"chunk cache status is invalid: {cache_dir}")
    if manifest.get("row_count") != len(bars):
        raise HistoricalDataError("CHUNK_CACHE_ROW_COUNT_MISMATCH", f"chunk cache row count mismatch: {cache_dir}")
    merged = _validate_chunk_bars(
        bars,
        symbol=str(expected_identity["symbol"]),
        timeframe=str(expected_identity["timeframe"]),
        chunk=chunk,
    )
    if merged != bars:
        raise HistoricalDataError("CHUNK_CACHE_ORDER_INVALID", f"chunk cache bars are not canonical: {cache_dir}")
    return bars, manifest


def _validate_chunk_bars(
    bars: Sequence[HistoricalBarV1],
    *,
    symbol: str,
    timeframe: str,
    chunk: HistoricalChunkV1,
) -> tuple[HistoricalBarV1, ...]:
    merged = merge_historical_bars(bars, symbol=symbol, timeframe=timeframe)
    if any(
        bar.time_utc < chunk.chunk_from_utc or bar.time_utc > chunk.chunk_to_utc
        for bar in merged
    ):
        raise HistoricalDataError(
            "CHUNK_BAR_OUTSIDE_REQUEST",
            f"chunk {chunk.chunk_id} returned a bar outside its requested UTC bounds",
        )
    quality = validate_historical_bars(
        merged,
        symbol=symbol,
        timeframe=timeframe,
        expected_interval_seconds=1,
    )
    if merged and (
        quality["symbol_timeframe_identity_pass"] is not True
        or quality["ohlc_integrity_pass"] is not True
        or quality["numeric_integrity_pass"] is not True
        or quality["monotonic_timestamp_pass"] is not True
        or quality["duplicate_timestamp_count"] != 0
    ):
        raise HistoricalDataError(
            "CHUNK_DATA_INTEGRITY_FAILED",
            f"chunk {chunk.chunk_id} failed deterministic bar integrity validation",
        )
    return merged


def _write_chunk_cache(
    cache_dir: Path,
    *,
    identity: Mapping[str, object],
    bars: Sequence[HistoricalBarV1],
) -> dict[str, object]:
    if cache_dir.is_symlink() or cache_dir.is_file():
        cache_dir.unlink()
    cache_dir.mkdir(parents=True, exist_ok=True)
    bars_bytes = canonical_bars_csv(bars)
    manifest: dict[str, object] = {
        **dict(identity),
        "status": "EMPTY" if not bars else "COMPLETED",
        "row_count": len(bars),
        "bars_sha256": sha256_bytes(bars_bytes),
    }
    manifest["manifest_sha256"] = _sha256_hex(canonical_json_bytes(manifest))
    bars_temporary = cache_dir / ".bars.csv.tmp"
    manifest_temporary = cache_dir / ".manifest.json.tmp"
    _write_synced(bars_temporary, bars_bytes)
    _write_synced(manifest_temporary, canonical_json_bytes(manifest) + b"\n")
    bars_temporary.replace(cache_dir / "bars.csv")
    manifest_temporary.replace(cache_dir / "manifest.json")
    _fsync_directory(cache_dir)
    return manifest


def _attempt_chunk_acquisition(
    *,
    source: HistoricalRateSource,
    source_proof: Mapping[str, object],
    symbol: str,
    timeframe: str,
    chunk: HistoricalChunkV1,
    staging_root: Path,
    collector_code_sha256: str,
) -> tuple[tuple[HistoricalBarV1, ...] | None, dict[str, object]]:
    """Fetch or reuse one identity-verified chunk.

    Returns ``(bars, audit)``. ``bars`` is ``None`` exactly when the chunk
    failed; a successful chunk always returns a tuple (possibly empty, for a
    genuinely quiet broker period).
    """
    identity = _chunk_cache_identity(
        source_proof=source_proof,
        symbol=symbol,
        timeframe=timeframe,
        chunk=chunk,
        collector_code_sha256=collector_code_sha256,
    )
    cache_dir = chunk_cache_directory(
        staging_root,
        source_proof=source_proof,
        symbol=symbol,
        timeframe=timeframe,
        chunk=chunk,
    )
    cache_validation = "NOT_PRESENT"
    cache_error_code: str | None = None
    bars: tuple[HistoricalBarV1, ...] | None = None
    method = "MT5"
    if cache_dir.exists():
        try:
            bars, _ = _load_valid_chunk_cache(
                cache_dir,
                expected_identity=identity,
                chunk=chunk,
            )
            cache_validation = "VERIFIED"
            method = "CACHE"
        except HistoricalDataError as exc:
            cache_validation = "REJECTED"
            cache_error_code = exc.code
    if bars is None:
        try:
            returned = source.copy_rates(
                symbol,
                timeframe,
                chunk.chunk_from_utc,
                chunk.chunk_to_utc,
            )
            bars = _validate_chunk_bars(
                returned,
                symbol=symbol,
                timeframe=timeframe,
                chunk=chunk,
            )
            cache_manifest = _write_chunk_cache(cache_dir, identity=identity, bars=bars)
            if cache_manifest["bars_sha256"] != sha256_bytes(canonical_bars_csv(bars)):
                raise HistoricalDataError(
                    "CHUNK_CACHE_PUBLICATION_FAILED",
                    f"chunk cache publication did not preserve {chunk.chunk_id}",
                )
        except Exception as exc:
            error_code = exc.code if isinstance(exc, HistoricalDataError) else "CHUNK_ACQUISITION_FAILED"
            audit = {
                "chunk_id": chunk.chunk_id,
                "chunk_from_utc": _utc_text(chunk.chunk_from_utc),
                "chunk_to_utc": _utc_text(chunk.chunk_to_utc),
                "status": "FAILED",
                "acquisition_method": method,
                "cache_validation": cache_validation,
                "cache_error_code": cache_error_code,
                "row_count": 0,
                "bars_sha256": None,
                "error_code": error_code,
                "error": str(exc),
            }
            return None, audit
    audit = {
        "chunk_id": chunk.chunk_id,
        "chunk_from_utc": _utc_text(chunk.chunk_from_utc),
        "chunk_to_utc": _utc_text(chunk.chunk_to_utc),
        "status": "EMPTY" if not bars else "COMPLETED",
        "acquisition_method": method,
        "cache_validation": cache_validation,
        "cache_error_code": cache_error_code,
        "row_count": len(bars),
        "bars_sha256": sha256_bytes(canonical_bars_csv(bars)),
        "error_code": None,
        "error": None,
    }
    return bars, audit


def acquire_chunked_history(
    *,
    source: HistoricalRateSource,
    source_proof: Mapping[str, object],
    symbol: str,
    timeframe: str,
    requested_from_utc: datetime,
    requested_to_utc: datetime,
    staging_root: Path,
    collector_code_sha256: str,
    progress: Any | None = None,
) -> ChunkedAcquisitionV1:
    """Acquire every planned chunk, reusing only identity-verified staging artifacts."""
    normalized_symbol = symbol.strip().upper()
    normalized_timeframe = timeframe.strip().upper()
    chunks = plan_calendar_month_chunks(requested_from_utc, requested_to_utc)
    staged_bars: list[HistoricalBarV1] = []
    audits: list[dict[str, object]] = []
    completed = empty = failed = cached = acquired = 0
    for index, chunk in enumerate(chunks, start=1):
        bars, audit = _attempt_chunk_acquisition(
            source=source,
            source_proof=source_proof,
            symbol=normalized_symbol,
            timeframe=normalized_timeframe,
            chunk=chunk,
            staging_root=staging_root,
            collector_code_sha256=collector_code_sha256,
        )
        audits.append(audit)
        if progress is not None:
            progress(index, len(chunks), audit)
        if bars is None:
            failed += 1
            continue
        completed += 1
        empty += int(not bars)
        cached += int(audit["acquisition_method"] == "CACHE")
        acquired += int(audit["acquisition_method"] == "MT5")
        staged_bars.extend(bars)

    merged: tuple[HistoricalBarV1, ...] = ()
    merge_error_code: str | None = None
    merge_error: str | None = None
    if failed == 0:
        try:
            merged = merge_historical_bars(
                staged_bars,
                symbol=normalized_symbol,
                timeframe=normalized_timeframe,
            )
        except HistoricalDataError as exc:
            merge_error_code = exc.code
            merge_error = str(exc)
    return ChunkedAcquisitionV1(
        bars=merged,
        chunk_audit=tuple(audits),
        requested_chunk_count=len(chunks),
        completed_chunk_count=completed,
        empty_chunk_count=empty,
        failed_chunk_count=failed,
        cached_chunk_count=cached,
        acquired_chunk_count=acquired,
        merge_integrity_error_code=merge_error_code,
        merge_integrity_error=merge_error,
    )


def _skipped_chunk_audit(chunk: HistoricalChunkV1, *, status: str) -> dict[str, object]:
    return {
        "chunk_id": chunk.chunk_id,
        "chunk_from_utc": _utc_text(chunk.chunk_from_utc),
        "chunk_to_utc": _utc_text(chunk.chunk_to_utc),
        "status": status,
        "acquisition_method": "NOT_ATTEMPTED",
        "cache_validation": "NOT_ATTEMPTED",
        "cache_error_code": None,
        "row_count": 0,
        "bars_sha256": None,
        "error_code": None,
        "error": None,
    }


def _attempt_chunk_with_bounded_retry(
    *,
    source: HistoricalRateSource,
    source_proof: Mapping[str, object],
    symbol: str,
    timeframe: str,
    chunk: HistoricalChunkV1,
    staging_root: Path,
    collector_code_sha256: str,
    max_attempts: int,
) -> tuple[tuple[HistoricalBarV1, ...] | None, dict[str, object]]:
    """Attempt one chunk, retrying only a not-yet-positively-classified failure.

    A failure classified as ``GENUINE_HISTORICAL_UNAVAILABLE`` or
    ``DATA_INTEGRITY`` is conclusive on the first observation and is never
    retried. Anything else (``UNRESOLVED_TRANSIENT_OR_UNKNOWN``) gets up to
    ``max_attempts`` total tries — all read-only re-reads of the same
    window — before being reported back as still-failing.
    """
    attempt = 0
    while True:
        attempt += 1
        bars, audit = _attempt_chunk_acquisition(
            source=source,
            source_proof=source_proof,
            symbol=symbol,
            timeframe=timeframe,
            chunk=chunk,
            staging_root=staging_root,
            collector_code_sha256=collector_code_sha256,
        )
        audit = dict(audit)
        audit["retry_attempts"] = attempt - 1
        if bars is not None:
            return bars, audit
        classification = _classify_chunk_failure_code(audit.get("error_code"))
        if classification != "UNRESOLVED_TRANSIENT_OR_UNKNOWN" or attempt >= max_attempts:
            return None, audit


def discover_available_coverage(
    *,
    source: HistoricalRateSource,
    source_proof: Mapping[str, object],
    symbol: str,
    timeframe: str,
    requested_from_utc: datetime,
    requested_to_utc: datetime,
    staging_root: Path,
    collector_code_sha256: str,
    progress: Any | None = None,
    transient_retry_attempts: int = TRANSIENT_CHUNK_RETRY_ATTEMPTS,
) -> CoverageDiscoveryV1:
    """Discover the broker-supported contiguous coverage, working backward.

    Chunks are attempted from ``requested_to_utc`` backward toward
    ``requested_from_utc``. A chunk failure is never itself the boundary — it
    is classified first via ``_classify_chunk_failure_code``:

    - ``GENUINE_HISTORICAL_UNAVAILABLE`` fixes the coverage boundary: the
      accepted suffix gathered so far is kept, and every older chunk is
      recorded as ``SKIPPED_UNAVAILABLE_PREFIX`` without any further cache
      lookup or broker call.
    - ``DATA_INTEGRITY`` fails the whole symbol closed: chunks already
      accepted are moved to ``discarded_chunk_audit`` (never published), and
      no older chunk is probed.
    - ``UNRESOLVED_TRANSIENT_OR_UNKNOWN`` gets a bounded, deterministic retry;
      if still unresolved it fails the whole symbol closed exactly like a
      data-integrity failure — it is never reinterpreted as a coverage
      boundary or silently bridged around.

    Because only a positively-classified genuine boundary can ever truncate
    coverage, a merge conflict between two already-accepted chunks (detected
    only after the walk completes) still fails the whole discovery closed via
    ``merge_integrity_error_code``, discarding those chunks from publication.
    """
    normalized_symbol = symbol.strip().upper()
    normalized_timeframe = timeframe.strip().upper()
    chunks = plan_calendar_month_chunks(requested_from_utc, requested_to_utc)
    total = len(chunks)
    accepted_audits: list[dict[str, object]] = []
    accepted_bars: list[HistoricalBarV1] = []
    prefix_audits: list[dict[str, object]] = []
    discarded_audits: list[dict[str, object]] = []
    abandoned_audits: list[dict[str, object]] = []
    cached = acquired = empty = 0
    truncated = False
    truncation_reason_code: str | None = None
    truncation_reason: str | None = None
    unresolved_error_code: str | None = None
    unresolved_error: str | None = None
    integrity_error_code: str | None = None
    integrity_error: str | None = None
    walking = True

    for offset, chunk in enumerate(reversed(chunks)):
        position = total - offset
        if not walking:
            status = "SKIPPED_UNAVAILABLE_PREFIX" if truncated else "NOT_ATTEMPTED_COVERAGE_UNRESOLVED"
            audit = _skipped_chunk_audit(chunk, status=status)
            (prefix_audits if truncated else abandoned_audits).append(audit)
            if progress is not None:
                progress(position, total, audit)
            continue

        bars, audit = _attempt_chunk_with_bounded_retry(
            source=source,
            source_proof=source_proof,
            symbol=normalized_symbol,
            timeframe=normalized_timeframe,
            chunk=chunk,
            staging_root=staging_root,
            collector_code_sha256=collector_code_sha256,
            max_attempts=transient_retry_attempts,
        )
        if progress is not None:
            progress(position, total, audit)
        if bars is not None:
            accepted_audits.append(audit)
            accepted_bars.extend(bars)
            empty += int(not bars)
            cached += int(audit["acquisition_method"] == "CACHE")
            acquired += int(audit["acquisition_method"] == "MT5")
            continue

        error_code = audit.get("error_code")
        classification = _classify_chunk_failure_code(error_code)
        if classification == "GENUINE_HISTORICAL_UNAVAILABLE":
            truncated = True
            truncation_reason_code = COVERAGE_TRUNCATED_REASON_CODE
            truncation_reason = (
                f"broker positively reports no retained history before "
                f"{_utc_text(chunk.chunk_to_utc)} (chunk {chunk.chunk_id}, "
                f"error_code={error_code})"
            )
            prefix_audits.append(audit)
        elif classification == "DATA_INTEGRITY":
            integrity_error_code = error_code
            integrity_error = str(audit.get("error"))
            discarded_audits.extend(accepted_audits)
            accepted_audits = []
            accepted_bars = []
            abandoned_audits.append(audit)
        else:
            unresolved_error_code = error_code
            unresolved_error = str(audit.get("error"))
            discarded_audits.extend(accepted_audits)
            accepted_audits = []
            accepted_bars = []
            abandoned_audits.append(audit)
        walking = False

    accepted_audits.reverse()
    prefix_audits.reverse()
    discarded_audits.reverse()
    abandoned_audits.reverse()

    merge_error_code: str | None = None
    merge_error: str | None = None
    merged: tuple[HistoricalBarV1, ...] = ()
    if accepted_audits and unresolved_error_code is None and integrity_error_code is None:
        try:
            merged = merge_historical_bars(
                accepted_bars,
                symbol=normalized_symbol,
                timeframe=normalized_timeframe,
            )
        except HistoricalDataError as exc:
            merge_error_code = exc.code
            merge_error = str(exc)

    effective_from = (
        parse_utc(str(accepted_audits[0]["chunk_from_utc"])) if accepted_audits else None
    )
    effective_to = (
        parse_utc(str(accepted_audits[-1]["chunk_to_utc"])) if accepted_audits else None
    )
    return CoverageDiscoveryV1(
        bars=merged,
        requested_from_utc=_as_utc(requested_from_utc),
        requested_to_utc=_as_utc(requested_to_utc),
        effective_from_utc=effective_from,
        effective_to_utc=effective_to,
        accepted_chunk_audit=tuple(accepted_audits),
        unavailable_prefix_chunk_audit=tuple(prefix_audits),
        discarded_chunk_audit=tuple(discarded_audits),
        abandoned_chunk_audit=tuple(abandoned_audits),
        requested_chunk_count=total,
        accepted_chunk_count=len(accepted_audits),
        empty_chunk_count=empty,
        cached_chunk_count=cached,
        acquired_chunk_count=acquired,
        coverage_truncated_at_requested_start=truncated,
        truncation_reason_code=truncation_reason_code,
        truncation_reason=truncation_reason,
        unresolved_error_code=unresolved_error_code,
        unresolved_error=unresolved_error,
        integrity_error_code=integrity_error_code,
        integrity_error=integrity_error,
        merge_integrity_error_code=merge_error_code,
        merge_integrity_error=merge_error,
    )


def validate_historical_bars(
    bars: Sequence[HistoricalBarV1],
    *,
    symbol: str,
    timeframe: str,
    expected_interval_seconds: int,
) -> dict[str, object]:
    if expected_interval_seconds <= 0:
        raise HistoricalDataError("EXPECTED_INTERVAL_INVALID", "expected interval must be positive")
    requested_symbol = symbol.strip().upper()
    requested_timeframe = timeframe.strip().upper()
    timestamps = [bar.time_utc for bar in bars]
    unique_timestamps = set(timestamps)
    duplicate_count = len(timestamps) - len(unique_timestamps)
    monotonic = all(left < right for left, right in zip(timestamps, timestamps[1:], strict=False))
    identity_pass = all(
        bar.symbol == requested_symbol and bar.timeframe == requested_timeframe for bar in bars
    )
    finite_rows = [
        all(math.isfinite(value) for value in (bar.open, bar.high, bar.low, bar.close))
        and all(type(value) is int for value in (bar.tick_volume, bar.spread, bar.real_volume))
        for bar in bars
    ]
    negative_volume_count = sum(
        bar.tick_volume < 0 or bar.spread < 0 or bar.real_volume < 0 for bar in bars
    )
    numeric_pass = all(finite_rows) and negative_volume_count == 0
    zero_or_negative = sum(
        any(value <= 0 for value in (bar.open, bar.high, bar.low, bar.close)) for bar in bars
    )
    high_low = sum(bar.high < bar.low for bar in bars)
    open_outside = sum(not bar.low <= bar.open <= bar.high for bar in bars)
    close_outside = sum(not bar.low <= bar.close <= bar.high for bar in bars)
    ohlc_pass = zero_or_negative == high_low == open_outside == close_outside == 0

    sorted_unique = sorted(unique_timestamps)
    gaps: list[int] = []
    weekend_overlap = 0
    for left, right in zip(sorted_unique, sorted_unique[1:], strict=False):
        delta = int((right - left).total_seconds())
        if delta > expected_interval_seconds:
            gaps.append(delta)
            cursor = left
            touches_weekend = False
            while cursor < right and not touches_weekend:
                cursor += timedelta(seconds=min(expected_interval_seconds, 86_400))
                touches_weekend = cursor.weekday() >= 5
            weekend_overlap += int(touches_weekend)
    quality_pass = bool(bars) and identity_pass and monotonic and duplicate_count == 0 and numeric_pass and ohlc_pass
    return {
        "row_count": len(bars),
        "unique_timestamp_count": len(unique_timestamps),
        "duplicate_timestamp_count": duplicate_count,
        "monotonic_timestamp_pass": monotonic,
        "symbol_timeframe_identity_pass": identity_pass,
        "ohlc_integrity_pass": ohlc_pass,
        "numeric_integrity_pass": numeric_pass,
        "gap_count": len(gaps),
        "largest_gap_seconds": max(gaps, default=0),
        "expected_interval_seconds": expected_interval_seconds,
        "weekend_session_gap_classification": "UTC_WEEKEND_OVERLAP_OBSERVED_ONLY_BROKER_SESSION_NOT_ASSUMED",
        "weekend_overlap_gap_count": weekend_overlap,
        "unexplained_gap_count": len(gaps),
        "zero_or_negative_price_count": zero_or_negative,
        "high_low_violation_count": high_low,
        "open_outside_high_low_count": open_outside,
        "close_outside_high_low_count": close_outside,
        "negative_volume_or_spread_count": negative_volume_count,
        "data_integrity_pass": quality_pass,
    }


def _default_chunk_acquisition_summary(
    *,
    bars: Sequence[HistoricalBarV1],
    requested_from_utc: datetime,
    requested_to_utc: datetime,
) -> dict[str, object]:
    chunks = plan_calendar_month_chunks(requested_from_utc, requested_to_utc)
    audit: list[dict[str, object]] = []
    for index, chunk in enumerate(chunks):
        owned = tuple(
            bar
            for bar in bars
            if bar.time_utc >= chunk.chunk_from_utc
            and (
                bar.time_utc < chunk.chunk_to_utc
                or (index == len(chunks) - 1 and bar.time_utc == chunk.chunk_to_utc)
            )
        )
        audit.append(
            {
                "chunk_id": chunk.chunk_id,
                "chunk_from_utc": _utc_text(chunk.chunk_from_utc),
                "chunk_to_utc": _utc_text(chunk.chunk_to_utc),
                "status": "EMPTY" if not owned else "COMPLETED",
                "acquisition_method": "DIRECT_VALIDATED_INPUT",
                "cache_validation": "NOT_APPLICABLE",
                "cache_error_code": None,
                "row_count": len(owned),
                "bars_sha256": sha256_bytes(canonical_bars_csv(owned)),
                "error_code": None,
                "error": None,
            }
        )
    return {
        "chunk_policy_version": CHUNK_POLICY_VERSION,
        "requested_chunk_count": len(chunks),
        "completed_chunk_count": len(chunks),
        "empty_chunk_count": sum(item["status"] == "EMPTY" for item in audit),
        "failed_chunk_count": 0,
        "cached_chunk_count": 0,
        "acquired_chunk_count": len(chunks),
        "chunk_audit": audit,
        "historical_capture_complete": True,
    }


def _validate_chunk_acquisition_summary(
    summary: Mapping[str, object],
    *,
    requested_from_utc: datetime | None = None,
    requested_to_utc: datetime | None = None,
) -> dict[str, object]:
    normalized = dict(summary)
    if normalized.get("chunk_policy_version") != CHUNK_POLICY_VERSION:
        raise HistoricalDataError(
            "CHUNK_POLICY_MISMATCH",
            f"chunk policy must be {CHUNK_POLICY_VERSION}",
        )
    count_fields = (
        "requested_chunk_count",
        "completed_chunk_count",
        "empty_chunk_count",
        "failed_chunk_count",
        "cached_chunk_count",
        "acquired_chunk_count",
    )
    if any(type(normalized.get(field)) is not int or int(normalized[field]) < 0 for field in count_fields):
        raise HistoricalDataError("CHUNK_AUDIT_INVALID", "chunk acquisition counts must be non-negative integers")
    requested = int(normalized["requested_chunk_count"])
    completed = int(normalized["completed_chunk_count"])
    empty = int(normalized["empty_chunk_count"])
    failed = int(normalized["failed_chunk_count"])
    cached = int(normalized["cached_chunk_count"])
    acquired = int(normalized["acquired_chunk_count"])
    audit = normalized.get("chunk_audit")
    if (
        requested <= 0
        or not isinstance(audit, list)
        or len(audit) != requested
        or completed + failed != requested
        or empty > completed
        or cached + acquired != completed
    ):
        raise HistoricalDataError("CHUNK_AUDIT_INVALID", "chunk acquisition summary is inconsistent")
    status_counts = {
        status: sum(isinstance(item, dict) and item.get("status") == status for item in audit)
        for status in ("COMPLETED", "EMPTY", "FAILED")
    }
    if (
        status_counts["COMPLETED"] + status_counts["EMPTY"] != completed
        or status_counts["EMPTY"] != empty
        or status_counts["FAILED"] != failed
    ):
        raise HistoricalDataError("CHUNK_AUDIT_INVALID", "chunk audit statuses do not match counts")
    for item in audit:
        if not isinstance(item, dict):
            raise HistoricalDataError("CHUNK_AUDIT_INVALID", "chunk audit entry must be an object")
        status = item.get("status")
        row_count = item.get("row_count")
        bars_sha256 = item.get("bars_sha256")
        if type(row_count) is not int or row_count < 0:
            raise HistoricalDataError("CHUNK_AUDIT_INVALID", "chunk row count is invalid")
        if status == "EMPTY" and (row_count != 0 or not isinstance(bars_sha256, str)):
            raise HistoricalDataError("CHUNK_AUDIT_INVALID", "empty chunk audit is invalid")
        if status == "COMPLETED" and (row_count <= 0 or not isinstance(bars_sha256, str)):
            raise HistoricalDataError("CHUNK_AUDIT_INVALID", "completed chunk audit is invalid")
        if status == "FAILED" and (row_count != 0 or bars_sha256 is not None):
            raise HistoricalDataError("CHUNK_AUDIT_INVALID", "failed chunk audit is invalid")
    if (requested_from_utc is None) != (requested_to_utc is None):
        raise HistoricalDataError("CHUNK_AUDIT_INVALID", "both requested range endpoints are required")
    if requested_from_utc is not None and requested_to_utc is not None:
        planned = plan_calendar_month_chunks(requested_from_utc, requested_to_utc)
        if len(planned) != requested:
            raise HistoricalDataError("CHUNK_AUDIT_INVALID", "chunk count does not match requested range")
        for item, chunk in zip(audit, planned, strict=True):
            if (
                item.get("chunk_id") != chunk.chunk_id
                or item.get("chunk_from_utc") != _utc_text(chunk.chunk_from_utc)
                or item.get("chunk_to_utc") != _utc_text(chunk.chunk_to_utc)
            ):
                raise HistoricalDataError(
                    "CHUNK_AUDIT_INVALID",
                    "chunk audit boundaries do not match the deterministic UTC plan",
                )
    expected_complete = completed == requested and failed == 0
    if normalized.get("historical_capture_complete") is not expected_complete:
        raise HistoricalDataError("CHUNK_AUDIT_INVALID", "historical capture completion flag is inconsistent")
    return normalized


def _default_coverage_discovery_summary(
    *,
    bars: Sequence[HistoricalBarV1],
    requested_from_utc: datetime,
    requested_to_utc: datetime,
) -> dict[str, object]:
    """Build a fully-accepted, untruncated coverage summary from direct input.

    Used when a manifest is built from an already-validated bar list rather
    than through :func:`discover_available_coverage` (e.g. legacy callers and
    tests that supply bars directly).
    """
    base = _default_chunk_acquisition_summary(
        bars=bars,
        requested_from_utc=requested_from_utc,
        requested_to_utc=requested_to_utc,
    )
    return {
        "coverage_discovery_policy_version": COVERAGE_DISCOVERY_POLICY_VERSION,
        "chunk_policy_version": CHUNK_POLICY_VERSION,
        "coverage_resolution": "COMPLETE",
        "requested_chunk_count": base["requested_chunk_count"],
        "accepted_chunk_count": base["completed_chunk_count"],
        "empty_chunk_count": base["empty_chunk_count"],
        "cached_chunk_count": base["cached_chunk_count"],
        "acquired_chunk_count": base["acquired_chunk_count"],
        "unavailable_prefix_chunk_count": 0,
        "discarded_chunk_count": 0,
        "abandoned_chunk_count": 0,
        "chunk_audit": base["chunk_audit"],
        "unavailable_prefix_chunk_audit": [],
        "discarded_chunk_audit": [],
        "abandoned_chunk_audit": [],
        "coverage_truncated_at_requested_start": False,
        "truncation_reason_code": None,
        "truncation_reason": None,
        "requested_coverage_start_utc": _utc_text(requested_from_utc),
        "requested_coverage_end_utc": _utc_text(requested_to_utc),
        "effective_coverage_start_utc": _utc_text(requested_from_utc),
        "effective_coverage_end_utc": _utc_text(requested_to_utc),
        "historical_capture_complete": True,
        "unresolved_error_code": None,
        "unresolved_error": None,
        "integrity_error_code": None,
        "integrity_error": None,
        "merge_integrity_error_code": None,
        "merge_integrity_error": None,
    }


def _validate_coverage_discovery_summary(
    summary: Mapping[str, object],
    *,
    requested_from_utc: datetime,
    requested_to_utc: datetime,
) -> dict[str, object]:
    """Validate a per-symbol coverage-discovery summary against its plan.

    Enforces that the accepted chunk audit is exactly the newest contiguous
    suffix of the deterministic calendar-month plan, that the unavailable
    prefix is exactly the older complement, that at most one boundary
    failure exists and it is a POSITIVELY classified genuine-unavailable
    error code (never a generic/transient/integrity code), and that
    truncation bookkeeping is internally consistent. A summary carrying an
    unresolved-transient, data-integrity, or merge-integrity error can never
    be accepted for publication — this never weakens integrity checks on the
    accepted chunks themselves, which are validated exactly like the legacy
    full-range summary.
    """
    normalized = dict(summary)
    if normalized.get("coverage_discovery_policy_version") != COVERAGE_DISCOVERY_POLICY_VERSION:
        raise HistoricalDataError(
            "COVERAGE_DISCOVERY_POLICY_MISMATCH",
            f"coverage discovery policy must be {COVERAGE_DISCOVERY_POLICY_VERSION}",
        )
    if normalized.get("chunk_policy_version") != CHUNK_POLICY_VERSION:
        raise HistoricalDataError("CHUNK_POLICY_MISMATCH", f"chunk policy must be {CHUNK_POLICY_VERSION}")
    for field, code in (
        ("merge_integrity_error_code", "COVERAGE_MERGE_INTEGRITY_FAILED"),
        ("integrity_error_code", "COVERAGE_DATA_INTEGRITY_FAILED"),
        ("unresolved_error_code", "COVERAGE_UNRESOLVED_TRANSIENT_FAILURE"),
    ):
        if normalized.get(field) is not None:
            raise HistoricalDataError(
                code,
                f"coverage discovery {field} must be resolved before publication",
            )
    count_fields = (
        "requested_chunk_count",
        "accepted_chunk_count",
        "empty_chunk_count",
        "cached_chunk_count",
        "acquired_chunk_count",
        "unavailable_prefix_chunk_count",
        "discarded_chunk_count",
        "abandoned_chunk_count",
    )
    if any(type(normalized.get(field)) is not int or int(normalized[field]) < 0 for field in count_fields):
        raise HistoricalDataError("COVERAGE_AUDIT_INVALID", "coverage discovery counts must be non-negative integers")
    requested = int(normalized["requested_chunk_count"])
    accepted = int(normalized["accepted_chunk_count"])
    empty = int(normalized["empty_chunk_count"])
    cached = int(normalized["cached_chunk_count"])
    acquired = int(normalized["acquired_chunk_count"])
    unavailable_prefix = int(normalized["unavailable_prefix_chunk_count"])
    discarded_count = int(normalized["discarded_chunk_count"])
    abandoned_count = int(normalized["abandoned_chunk_count"])
    accepted_audit = normalized.get("chunk_audit")
    prefix_audit = normalized.get("unavailable_prefix_chunk_audit")
    discarded_audit = normalized.get("discarded_chunk_audit")
    abandoned_audit = normalized.get("abandoned_chunk_audit")
    if (
        requested <= 0
        or accepted == 0
        or accepted + unavailable_prefix != requested
        or discarded_count != 0
        or abandoned_count != 0
        or not isinstance(accepted_audit, list)
        or len(accepted_audit) != accepted
        or not isinstance(prefix_audit, list)
        or len(prefix_audit) != unavailable_prefix
        or not isinstance(discarded_audit, list)
        or discarded_audit
        or not isinstance(abandoned_audit, list)
        or abandoned_audit
        or empty > accepted
        or cached + acquired != accepted
    ):
        raise HistoricalDataError("COVERAGE_AUDIT_INVALID", "coverage discovery summary is inconsistent")
    status_counts = {
        status: sum(isinstance(item, dict) and item.get("status") == status for item in accepted_audit)
        for status in ("COMPLETED", "EMPTY")
    }
    if status_counts["COMPLETED"] + status_counts["EMPTY"] != accepted or status_counts["EMPTY"] != empty:
        raise HistoricalDataError("COVERAGE_AUDIT_INVALID", "accepted chunk audit statuses do not match counts")
    for item in accepted_audit:
        if not isinstance(item, dict):
            raise HistoricalDataError("COVERAGE_AUDIT_INVALID", "accepted chunk audit entry must be an object")
        status = item.get("status")
        row_count = item.get("row_count")
        bars_sha256 = item.get("bars_sha256")
        if type(row_count) is not int or row_count < 0:
            raise HistoricalDataError("COVERAGE_AUDIT_INVALID", "chunk row count is invalid")
        if status not in {"COMPLETED", "EMPTY"}:
            raise HistoricalDataError("COVERAGE_AUDIT_INVALID", "accepted chunk audit contains a non-accepted status")
        if status == "EMPTY" and (row_count != 0 or not isinstance(bars_sha256, str)):
            raise HistoricalDataError("COVERAGE_AUDIT_INVALID", "empty chunk audit is invalid")
        if status == "COMPLETED" and (row_count <= 0 or not isinstance(bars_sha256, str)):
            raise HistoricalDataError("COVERAGE_AUDIT_INVALID", "completed chunk audit is invalid")
    failed_prefix_count = 0
    for position, item in enumerate(prefix_audit):
        if not isinstance(item, dict):
            raise HistoricalDataError(
                "COVERAGE_AUDIT_INVALID", "unavailable-prefix chunk audit entry must be an object"
            )
        status = item.get("status")
        if status not in {"FAILED", "SKIPPED_UNAVAILABLE_PREFIX"}:
            raise HistoricalDataError("COVERAGE_AUDIT_INVALID", "unavailable-prefix chunk status is invalid")
        if status == "FAILED":
            failed_prefix_count += 1
            if position != len(prefix_audit) - 1:
                raise HistoricalDataError(
                    "COVERAGE_AUDIT_INVALID",
                    "only the newest unavailable-prefix chunk may carry a probed failure",
                )
            error_code = item.get("error_code")
            if (
                error_code not in GENUINE_HISTORICAL_UNAVAILABLE_ERROR_CODES
                or item.get("row_count") != 0
                or item.get("bars_sha256") is not None
            ):
                raise HistoricalDataError(
                    "COVERAGE_AUDIT_INVALID",
                    "the boundary chunk failure must be a positively classified genuine-unavailable error code",
                )
        else:
            if (
                item.get("acquisition_method") != "NOT_ATTEMPTED"
                or item.get("row_count") != 0
                or item.get("bars_sha256") is not None
                or item.get("error_code") is not None
            ):
                raise HistoricalDataError("COVERAGE_AUDIT_INVALID", "skipped prefix chunk audit is invalid")
    if failed_prefix_count > 1:
        raise HistoricalDataError("COVERAGE_AUDIT_INVALID", "at most one boundary failure may appear in the prefix audit")
    planned = plan_calendar_month_chunks(requested_from_utc, requested_to_utc)
    if len(planned) != requested:
        raise HistoricalDataError("COVERAGE_AUDIT_INVALID", "chunk count does not match requested range")
    expected_prefix = planned[:unavailable_prefix]
    expected_accepted = planned[unavailable_prefix:]
    for item, chunk in zip(prefix_audit, expected_prefix, strict=True):
        if (
            item.get("chunk_id") != chunk.chunk_id
            or item.get("chunk_from_utc") != _utc_text(chunk.chunk_from_utc)
            or item.get("chunk_to_utc") != _utc_text(chunk.chunk_to_utc)
        ):
            raise HistoricalDataError(
                "COVERAGE_AUDIT_INVALID",
                "unavailable-prefix boundaries do not match the deterministic UTC plan",
            )
    for item, chunk in zip(accepted_audit, expected_accepted, strict=True):
        if (
            item.get("chunk_id") != chunk.chunk_id
            or item.get("chunk_from_utc") != _utc_text(chunk.chunk_from_utc)
            or item.get("chunk_to_utc") != _utc_text(chunk.chunk_to_utc)
        ):
            raise HistoricalDataError(
                "COVERAGE_AUDIT_INVALID",
                "accepted chunk boundaries do not match the deterministic UTC plan",
            )
    expected_truncated = unavailable_prefix > 0
    if bool(normalized.get("coverage_truncated_at_requested_start")) != expected_truncated:
        raise HistoricalDataError("COVERAGE_AUDIT_INVALID", "truncation flag is inconsistent with prefix count")
    expected_resolution = "TRUNCATED_GENUINE_BOUNDARY" if expected_truncated else "COMPLETE"
    if normalized.get("coverage_resolution") != expected_resolution:
        raise HistoricalDataError(
            "COVERAGE_AUDIT_INVALID", "coverage_resolution does not match the truncation state"
        )
    if expected_truncated:
        if normalized.get("truncation_reason_code") != COVERAGE_TRUNCATED_REASON_CODE:
            raise HistoricalDataError("COVERAGE_AUDIT_INVALID", "truncation reason code is invalid")
        if not isinstance(normalized.get("truncation_reason"), str) or not normalized["truncation_reason"]:
            raise HistoricalDataError("COVERAGE_AUDIT_INVALID", "truncation reason text is required when truncated")
    elif normalized.get("truncation_reason_code") is not None or normalized.get("truncation_reason") is not None:
        raise HistoricalDataError("COVERAGE_AUDIT_INVALID", "truncation reason must be absent when not truncated")
    expected_start = _utc_text(expected_accepted[0].chunk_from_utc)
    expected_end = _utc_text(expected_accepted[-1].chunk_to_utc)
    if (
        normalized.get("requested_coverage_start_utc") != _utc_text(requested_from_utc)
        or normalized.get("requested_coverage_end_utc") != _utc_text(requested_to_utc)
        or normalized.get("effective_coverage_start_utc") != expected_start
        or normalized.get("effective_coverage_end_utc") != expected_end
    ):
        raise HistoricalDataError("COVERAGE_AUDIT_INVALID", "requested/effective coverage bounds are inconsistent")
    expected_complete = accepted == requested
    if normalized.get("historical_capture_complete") is not expected_complete:
        raise HistoricalDataError("COVERAGE_AUDIT_INVALID", "historical capture completion flag is inconsistent")
    return normalized


def build_dataset_manifest(
    *,
    bars: Sequence[HistoricalBarV1],
    source_proof: Mapping[str, object],
    symbol_metadata: Mapping[str, object],
    broker_symbol: BrokerSymbolV1,
    execution_account_login: str,
    execution_universe_source: str,
    execution_universe: CanonicalExecutionUniverseV1,
    timeframe: str,
    requested_from_utc: datetime,
    requested_to_utc: datetime,
    expected_interval_seconds: int,
    source_capture_utc: datetime,
    collector_code_sha256: str,
    coverage_discovery: Mapping[str, object] | None = None,
) -> tuple[dict[str, object], bytes]:
    execution_account = str(execution_account_login).strip()
    market_data_account = str(source_proof.get("market_data_account_login") or "").strip()
    validate_active_account_contract(
        execution_account_login=execution_account,
        market_data_account_login=market_data_account,
    )
    if broker_symbol.source_row.get("account_login") != execution_account:
        raise HistoricalDataError(
            "BROKER_UNIVERSE_ACCOUNT_MISMATCH",
            "dataset execution account does not match its broker-universe row",
        )
    canonical_sha256 = verify_canonical_execution_universe_snapshot(
        execution_universe.canonical_snapshot,
        expected_sha256=execution_universe.canonical_sha256,
    )
    if re.fullmatch(r"sha256:[0-9a-f]{64}", execution_universe.raw_sha256) is None:
        raise HistoricalDataError(
            "BROKER_UNIVERSE_RAW_HASH_INVALID",
            "raw execution-universe SHA-256 is malformed",
        )
    canonical_rows = execution_universe.canonical_snapshot.get("rows")
    if not isinstance(canonical_rows, list):
        raise HistoricalDataError(
            "CANONICAL_UNIVERSE_SNAPSHOT_INVALID",
            "canonical execution-universe rows are missing",
        )
    canonical_symbol_row = next(
        (
            row
            for row in canonical_rows
            if isinstance(row, dict) and row.get("symbol") == broker_symbol.symbol
        ),
        None,
    )
    if canonical_symbol_row is None or canonical_symbol_row != _canonicalize_execution_universe_row(
        broker_symbol.source_row,
        account_login=execution_account,
    ):
        raise HistoricalDataError(
            "DATASET_CANONICAL_UNIVERSE_SYMBOL_MISMATCH",
            f"{broker_symbol.symbol} does not match the canonical execution-universe snapshot",
        )
    if not market_data_account or source_proof.get("source_type") != SOURCE_TYPE:
        raise HistoricalDataError(
            "MARKET_DATA_SOURCE_PROOF_INVALID",
            "verified market-data account/source identity is required",
        )
    if source_proof.get("authenticated_market_data_account_verified") is not True:
        raise HistoricalDataError(
            "MARKET_DATA_SOURCE_PROOF_INVALID",
            "market-data account verification is absent",
        )
    compatibility = verify_cross_account_symbol_compatibility(
        broker_symbol=broker_symbol,
        market_data_symbol_metadata=symbol_metadata,
    )
    quality = validate_historical_bars(
        bars,
        symbol=broker_symbol.symbol,
        timeframe=timeframe,
        expected_interval_seconds=expected_interval_seconds,
    )
    bars_bytes = canonical_bars_csv(bars)
    bars_sha256 = sha256_bytes(bars_bytes)
    requested_from = _as_utc(requested_from_utc)
    requested_to = _as_utc(requested_to_utc)
    coverage = _validate_coverage_discovery_summary(
        coverage_discovery
        or _default_coverage_discovery_summary(
            bars=bars,
            requested_from_utc=requested_from_utc,
            requested_to_utc=requested_to_utc,
        ),
        requested_from_utc=requested_from,
        requested_to_utc=requested_to,
    )
    actual_first = min((bar.time_utc for bar in bars), default=None)
    actual_last = max((bar.time_utc for bar in bars), default=None)
    requested_duration_seconds = int((requested_to - requested_from).total_seconds())
    observed_span_seconds = (
        int((actual_last - actual_first).total_seconds())
        if actual_first is not None and actual_last is not None
        else 0
    )
    dataset_identity = {
        "schema_version": DATASET_SCHEMA_VERSION,
        "source_type": SOURCE_TYPE,
        "market_data_source_type": SOURCE_TYPE,
        "execution_account_login": execution_account,
        "market_data_account_login": market_data_account,
        "execution_universe_source": execution_universe_source,
        "execution_universe_sha256": canonical_sha256,
        "execution_universe_canonical_sha256": canonical_sha256,
        "execution_universe_canonical_schema_version": (
            EXECUTION_UNIVERSE_CANONICAL_SCHEMA_VERSION
        ),
        "market_data_account_server": source_proof.get("market_data_account_server"),
        "market_data_account_company": source_proof.get("market_data_account_company")
        or source_proof.get("terminal_company"),
        "market_data_account_currency": source_proof.get("market_data_account_currency"),
        "symbol": broker_symbol.symbol,
        "timeframe": timeframe.strip().upper(),
        "requested_from_utc": _utc_text(requested_from),
        "requested_to_utc": _utc_text(requested_to),
        "effective_coverage_start_utc": coverage["effective_coverage_start_utc"],
        "effective_coverage_end_utc": coverage["effective_coverage_end_utc"],
        "coverage_truncated_at_requested_start": coverage["coverage_truncated_at_requested_start"],
        "coverage_truncation_reason_code": coverage["truncation_reason_code"],
        "actual_first_bar_utc": _utc_text(actual_first) if actual_first else None,
        "actual_last_bar_utc": _utc_text(actual_last) if actual_last else None,
        "requested_duration_seconds": requested_duration_seconds,
        "observed_span_seconds": observed_span_seconds,
        "first_bar_offset_seconds": (
            int((actual_first - requested_from).total_seconds()) if actual_first else None
        ),
        "last_bar_offset_seconds": (
            int((requested_to - actual_last).total_seconds()) if actual_last else None
        ),
        "bars_sha256": bars_sha256,
        "symbol_point": symbol_metadata.get("point"),
        "symbol_digits": symbol_metadata.get("digits"),
        "symbol_compatibility_verified": True,
        "execution_symbol_tick_size": compatibility["execution_tick_size"],
        "market_data_symbol_trade_tick_size": compatibility["market_data_trade_tick_size"],
        "expected_interval_seconds": expected_interval_seconds,
        "chunk_policy_version": CHUNK_POLICY_VERSION,
    }
    digest = hashlib.sha256()
    digest.update(_DATASET_HASH_DOMAIN)
    digest.update(b"\x00")
    digest.update(canonical_json_bytes(dataset_identity))
    dataset_sha256 = digest.hexdigest()
    manifest: dict[str, object] = {
        **dataset_identity,
        "dataset_id": f"ser8-historical:sha256:{dataset_sha256}",
        "dataset_sha256": dataset_sha256,
        "source_capture_utc": _utc_text(source_capture_utc),
        "execution_universe_raw_sha256": execution_universe.raw_sha256,
        "execution_universe_canonical_snapshot": dict(execution_universe.canonical_snapshot),
        "source_proof": dict(source_proof),
        "cross_account_provenance": {
            "execution_account_login": execution_account,
            "market_data_account_login": market_data_account,
            "accounts_claimed_equivalent": False,
            "price_feeds_claimed_byte_identical": False,
            "research_evidence_scope": "MARKET_DATA_SOURCE_OBSERVED_NOT_EXECUTION_PRICE_EQUIVALENCE",
        },
        "symbol_compatibility": compatibility,
        "execution_symbol_metadata": {
            "account_login": execution_account,
            "name": broker_symbol.symbol,
            "trade_mode": broker_symbol.trade_mode,
            "trade_tick_size": compatibility["execution_tick_size"],
            "canonical_tick_size": canonical_symbol_row["tick_size"],
        },
        "source_symbol_metadata": dict(symbol_metadata),
        "broker_trade_mode": broker_symbol.trade_mode,
        "asset_class": broker_symbol.asset_class,
        "risk_model_supported": broker_symbol.risk_model_supported,
        "risk_model_reason": broker_symbol.risk_model_reason or None,
        "collector_version": COLLECTOR_VERSION,
        "collector_code_sha256": collector_code_sha256,
        **coverage,
        **quality,
        "quality": quality,
        "accepted_historical_data": bool(
            quality["data_integrity_pass"] and coverage["accepted_chunk_count"] > 0
        ),
        "gap_repair_performed": False,
        "synthetic_bars_added": 0,
    }
    manifest["manifest_sha256"] = _sha256_hex(canonical_json_bytes(manifest))
    return manifest, bars_bytes


def _write_synced(path: Path, content: bytes) -> None:
    with path.open("wb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        try:
            os.fsync(descriptor)
        except OSError:
            # Directory fsync is not available on every Windows filesystem;
            # file fsync + same-volume atomic rename remains the V1 contract.
            pass
    finally:
        os.close(descriptor)


def verify_canonical_execution_universe_artifact(
    artifact_path: Path,
    *,
    expected_sha256: str,
) -> dict[str, object]:
    try:
        snapshot = json.loads(artifact_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HistoricalDataError(
            "CANONICAL_UNIVERSE_ARTIFACT_INVALID",
            f"cannot read canonical execution-universe artifact: {artifact_path}",
        ) from exc
    if not isinstance(snapshot, dict):
        raise HistoricalDataError(
            "CANONICAL_UNIVERSE_ARTIFACT_INVALID",
            f"canonical execution-universe artifact is not an object: {artifact_path}",
        )
    verify_canonical_execution_universe_snapshot(snapshot, expected_sha256=expected_sha256)
    return snapshot


def publish_canonical_execution_universe_snapshot(
    root: Path,
    universe: CanonicalExecutionUniverseV1,
) -> Path:
    """Atomically persist one content-addressed, independently verifiable snapshot."""
    root = _assert_not_live_artifact_path(root, field_name="dataset_root")
    verify_canonical_execution_universe_snapshot(
        universe.canonical_snapshot,
        expected_sha256=universe.canonical_sha256,
    )
    digest = universe.canonical_sha256.removeprefix("sha256:")
    snapshot_root = root / "execution_universe_snapshots"
    destination = snapshot_root / digest
    artifact_path = destination / "snapshot.json"
    snapshot_root.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        verify_canonical_execution_universe_artifact(
            artifact_path,
            expected_sha256=universe.canonical_sha256,
        )
        return artifact_path
    temporary = Path(tempfile.mkdtemp(prefix=".ser8-universe-", dir=snapshot_root))
    try:
        _write_synced(
            temporary / "snapshot.json",
            canonical_json_bytes(dict(universe.canonical_snapshot)) + b"\n",
        )
        _fsync_directory(temporary)
        temporary.replace(destination)
        _fsync_directory(snapshot_root)
    except BaseException:
        if temporary.exists():
            shutil.rmtree(temporary)
        raise
    return artifact_path


def publish_dataset(
    root: Path,
    manifest: Mapping[str, object],
    bars_bytes: bytes,
) -> tuple[Path, dict[str, object], bool]:
    """Atomically publish or verify an idempotently pre-existing dataset."""
    root = _assert_not_live_artifact_path(root, field_name="dataset_root")
    dataset_sha256 = str(manifest["dataset_sha256"])
    if not len(dataset_sha256) == 64 or any(char not in "0123456789abcdef" for char in dataset_sha256):
        raise HistoricalDataError("DATASET_HASH_INVALID", "dataset_sha256 is malformed")
    root.mkdir(parents=True, exist_ok=True)
    if manifest.get("bars_sha256") != sha256_bytes(bars_bytes):
        raise HistoricalDataError("DATASET_BARS_HASH_MISMATCH", "manifest does not match supplied bars")
    semantic_manifest = dict(manifest)
    supplied_manifest_hash = semantic_manifest.pop("manifest_sha256", None)
    if supplied_manifest_hash != _sha256_hex(canonical_json_bytes(semantic_manifest)):
        raise HistoricalDataError("DATASET_MANIFEST_HASH_MISMATCH", "manifest hash is invalid before publication")
    destination = root / dataset_sha256
    manifest_bytes = canonical_json_bytes(dict(manifest)) + b"\n"
    if destination.exists():
        existing_manifest_path = destination / "manifest.json"
        existing_bars_path = destination / "bars.csv"
        if not existing_manifest_path.is_file() or not existing_bars_path.is_file():
            raise HistoricalDataError("DATASET_COLLISION", f"incomplete dataset already exists: {destination}")
        existing_manifest = verify_dataset(destination)
        if (
            existing_manifest.get("dataset_sha256") != dataset_sha256
            or existing_bars_path.read_bytes() != bars_bytes
            or existing_manifest.get("bars_sha256") != sha256_bytes(bars_bytes)
        ):
            raise HistoricalDataError("DATASET_COLLISION", "different content exists under the same dataset identity")
        return destination, existing_manifest, False

    temporary = Path(tempfile.mkdtemp(prefix=".ser8-history-", dir=root))
    try:
        _write_synced(temporary / "bars.csv", bars_bytes)
        _write_synced(temporary / "manifest.json", manifest_bytes)
        _fsync_directory(temporary)
        temporary.replace(destination)
        _fsync_directory(root)
    except BaseException:
        if temporary.exists():
            shutil.rmtree(temporary)
        raise
    return destination, dict(manifest), True


def verify_dataset(dataset_dir: Path) -> dict[str, object]:
    manifest_path = dataset_dir / "manifest.json"
    bars_path = dataset_dir / "bars.csv"
    if not manifest_path.is_file() or not bars_path.is_file():
        raise HistoricalDataError("DATASET_FILES_MISSING", f"dataset files missing: {dataset_dir}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise HistoricalDataError("DATASET_MANIFEST_INVALID", f"invalid dataset manifest: {manifest_path}") from exc
    if manifest.get("schema_version") != DATASET_SCHEMA_VERSION:
        raise HistoricalDataError("DATASET_SCHEMA_INVALID", f"unsupported dataset schema: {dataset_dir}")
    supplied_manifest_hash = manifest.get("manifest_sha256")
    semantic_manifest = dict(manifest)
    semantic_manifest.pop("manifest_sha256", None)
    if supplied_manifest_hash != _sha256_hex(canonical_json_bytes(semantic_manifest)):
        raise HistoricalDataError("DATASET_MANIFEST_HASH_MISMATCH", f"manifest hash mismatch: {dataset_dir}")
    raw_universe_sha256 = manifest.get("execution_universe_raw_sha256")
    canonical_universe_sha256 = manifest.get("execution_universe_canonical_sha256")
    canonical_snapshot = manifest.get("execution_universe_canonical_snapshot")
    if (
        not isinstance(raw_universe_sha256, str)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", raw_universe_sha256) is None
    ):
        raise HistoricalDataError(
            "DATASET_RAW_UNIVERSE_HASH_INVALID",
            f"raw execution-universe hash is invalid: {dataset_dir}",
        )
    if (
        manifest.get("execution_universe_canonical_schema_version")
        != EXECUTION_UNIVERSE_CANONICAL_SCHEMA_VERSION
        or not isinstance(canonical_universe_sha256, str)
        or not isinstance(canonical_snapshot, dict)
    ):
        raise HistoricalDataError(
            "DATASET_CANONICAL_UNIVERSE_PROVENANCE_MISSING",
            f"canonical execution-universe provenance is missing: {dataset_dir}",
        )
    verify_canonical_execution_universe_snapshot(
        canonical_snapshot,
        expected_sha256=canonical_universe_sha256,
    )
    if (
        manifest.get("execution_universe_sha256") != canonical_universe_sha256
        or canonical_snapshot.get("execution_account_login")
        != manifest.get("execution_account_login")
    ):
        raise HistoricalDataError(
            "DATASET_CANONICAL_UNIVERSE_PROVENANCE_MISMATCH",
            f"canonical execution-universe provenance does not match manifest: {dataset_dir}",
        )
    bars_bytes = bars_path.read_bytes()
    if manifest.get("bars_sha256") != sha256_bytes(bars_bytes):
        raise HistoricalDataError("DATASET_BARS_HASH_MISMATCH", f"bars checksum mismatch: {dataset_dir}")
    bars = load_canonical_bars(bars_path)
    quality = validate_historical_bars(
        bars,
        symbol=str(manifest["symbol"]),
        timeframe=str(manifest["timeframe"]),
        expected_interval_seconds=int(manifest["expected_interval_seconds"]),
    )
    if quality != manifest.get("quality"):
        raise HistoricalDataError("DATASET_QUALITY_MISMATCH", f"quality manifest mismatch: {dataset_dir}")
    requested_from = parse_utc(str(manifest.get("requested_from_utc") or ""))
    requested_to = parse_utc(str(manifest.get("requested_to_utc") or ""))
    coverage = _validate_coverage_discovery_summary(
        {
            key: manifest.get(key)
            for key in (
                "coverage_discovery_policy_version",
                "chunk_policy_version",
                "coverage_resolution",
                "requested_chunk_count",
                "accepted_chunk_count",
                "empty_chunk_count",
                "cached_chunk_count",
                "acquired_chunk_count",
                "unavailable_prefix_chunk_count",
                "discarded_chunk_count",
                "abandoned_chunk_count",
                "chunk_audit",
                "unavailable_prefix_chunk_audit",
                "discarded_chunk_audit",
                "abandoned_chunk_audit",
                "coverage_truncated_at_requested_start",
                "truncation_reason_code",
                "truncation_reason",
                "requested_coverage_start_utc",
                "requested_coverage_end_utc",
                "effective_coverage_start_utc",
                "effective_coverage_end_utc",
                "historical_capture_complete",
                "unresolved_error_code",
                "unresolved_error",
                "integrity_error_code",
                "integrity_error",
                "merge_integrity_error_code",
                "merge_integrity_error",
            )
        },
        requested_from_utc=requested_from,
        requested_to_utc=requested_to,
    )
    if (
        manifest.get("effective_coverage_start_utc") != coverage["effective_coverage_start_utc"]
        or manifest.get("effective_coverage_end_utc") != coverage["effective_coverage_end_utc"]
        or manifest.get("coverage_truncated_at_requested_start")
        != coverage["coverage_truncated_at_requested_start"]
        or manifest.get("coverage_truncation_reason_code") != coverage["truncation_reason_code"]
    ):
        raise HistoricalDataError(
            "DATASET_COVERAGE_PROVENANCE_MISMATCH",
            f"effective coverage provenance mismatch: {dataset_dir}",
        )
    expected_acceptance = bool(
        quality["data_integrity_pass"] and coverage["accepted_chunk_count"] > 0
    )
    if manifest.get("accepted_historical_data") is not expected_acceptance:
        raise HistoricalDataError("DATASET_ACCEPTANCE_MISMATCH", f"acceptance mismatch: {dataset_dir}")
    actual_first = min((bar.time_utc for bar in bars), default=None)
    actual_last = max((bar.time_utc for bar in bars), default=None)
    expected_coverage = {
        "actual_first_bar_utc": _utc_text(actual_first) if actual_first else None,
        "actual_last_bar_utc": _utc_text(actual_last) if actual_last else None,
        "requested_duration_seconds": int((requested_to - requested_from).total_seconds()),
        "observed_span_seconds": (
            int((actual_last - actual_first).total_seconds())
            if actual_first is not None and actual_last is not None
            else 0
        ),
        "first_bar_offset_seconds": (
            int((actual_first - requested_from).total_seconds()) if actual_first else None
        ),
        "last_bar_offset_seconds": (
            int((requested_to - actual_last).total_seconds()) if actual_last else None
        ),
    }
    if any(manifest.get(key) != expected for key, expected in expected_coverage.items()):
        raise HistoricalDataError("DATASET_COVERAGE_MISMATCH", f"coverage manifest mismatch: {dataset_dir}")
    identity_keys = (
        "schema_version", "source_type", "market_data_source_type",
        "execution_account_login", "market_data_account_login", "execution_universe_source",
        "execution_universe_sha256", "execution_universe_canonical_sha256",
        "execution_universe_canonical_schema_version", "market_data_account_server",
        "market_data_account_company", "market_data_account_currency", "symbol", "timeframe",
        "requested_from_utc", "requested_to_utc",
        "effective_coverage_start_utc", "effective_coverage_end_utc",
        "coverage_truncated_at_requested_start", "coverage_truncation_reason_code",
        "actual_first_bar_utc",
        "actual_last_bar_utc", "requested_duration_seconds", "observed_span_seconds",
        "first_bar_offset_seconds", "last_bar_offset_seconds", "bars_sha256",
        "symbol_point", "symbol_digits",
        "symbol_compatibility_verified", "execution_symbol_tick_size",
        "market_data_symbol_trade_tick_size", "expected_interval_seconds",
        "chunk_policy_version",
    )
    for key in (
        "execution_account_login",
        "market_data_account_login",
        "execution_universe_source",
        "execution_universe_sha256",
        "execution_universe_canonical_sha256",
        "execution_universe_canonical_schema_version",
        "market_data_account_server",
    ):
        if not manifest.get(key):
            raise HistoricalDataError("DATASET_ACCOUNT_IDENTITY_MISSING", f"manifest is missing {key}")
    validate_active_account_contract(
        execution_account_login=str(manifest.get("execution_account_login") or ""),
        market_data_account_login=str(manifest.get("market_data_account_login") or ""),
    )
    source_proof = manifest.get("source_proof")
    if not isinstance(source_proof, dict) or (
        source_proof.get("market_data_account_login") != manifest.get("market_data_account_login")
        or source_proof.get("market_data_account_server") != manifest.get("market_data_account_server")
        or source_proof.get("source_type") != SOURCE_TYPE
        or source_proof.get("authenticated_market_data_account_verified") is not True
        or source_proof.get("read_only_operations") != list(READ_ONLY_MT5_OPERATIONS)
    ):
        raise HistoricalDataError(
            "DATASET_SOURCE_PROOF_MISMATCH",
            f"market-data source proof does not match manifest: {dataset_dir}",
        )
    compatibility = manifest.get("symbol_compatibility")
    if not isinstance(compatibility, dict) or (
        compatibility.get("execution_symbol_in_universe") is not True
        or compatibility.get("exact_symbol_identity_verified") is not True
        or compatibility.get("execution_symbol") != manifest.get("symbol")
        or compatibility.get("market_data_symbol") != manifest.get("symbol")
        or manifest.get("symbol_compatibility_verified") is not True
    ):
        raise HistoricalDataError(
            "DATASET_SYMBOL_COMPATIBILITY_INVALID",
            f"cross-account symbol compatibility is invalid: {dataset_dir}",
        )
    canonical_symbol_row = next(
        (
            row
            for row in canonical_snapshot["rows"]
            if isinstance(row, dict) and row.get("symbol") == manifest.get("symbol")
        ),
        None,
    )
    execution_metadata = manifest.get("execution_symbol_metadata")
    if not isinstance(execution_metadata, dict) or canonical_symbol_row is None or (
        canonical_symbol_row.get("trade_mode") != manifest.get("broker_trade_mode")
        or canonical_symbol_row.get("asset_class") != manifest.get("asset_class")
        or canonical_symbol_row.get("risk_model_supported")
        is not manifest.get("risk_model_supported")
        or canonical_symbol_row.get("tick_size")
        != execution_metadata.get("canonical_tick_size")
    ):
        raise HistoricalDataError(
            "DATASET_CANONICAL_UNIVERSE_SYMBOL_MISMATCH",
            f"dataset symbol does not match its canonical execution universe: {dataset_dir}",
        )
    identity = {key: manifest.get(key) for key in identity_keys}
    digest = hashlib.sha256()
    digest.update(_DATASET_HASH_DOMAIN)
    digest.update(b"\x00")
    digest.update(canonical_json_bytes(identity))
    expected = digest.hexdigest()
    if manifest.get("dataset_sha256") != expected or dataset_dir.name != expected:
        raise HistoricalDataError("DATASET_IDENTITY_MISMATCH", f"dataset identity mismatch: {dataset_dir}")
    return manifest


def inventory_hash(payload: Mapping[str, object]) -> str:
    semantic = dict(payload)
    semantic.pop("inventory_sha256", None)
    digest = hashlib.sha256()
    digest.update(_INVENTORY_HASH_DOMAIN)
    digest.update(b"\x00")
    digest.update(canonical_json_bytes(semantic))
    return digest.hexdigest()


def verify_inventory(payload: Mapping[str, object]) -> None:
    if payload.get("schema_version") != INVENTORY_SCHEMA_VERSION:
        raise HistoricalDataError("INVENTORY_SCHEMA_INVALID", "unsupported historical inventory schema")
    supplied = payload.get("inventory_sha256")
    if not isinstance(supplied, str) or supplied != inventory_hash(payload):
        raise HistoricalDataError("INVENTORY_HASH_MISMATCH", "historical inventory hash mismatch")
    execution_account = str(payload.get("execution_account_login") or "").strip()
    market_data_account = str(payload.get("market_data_account_login") or "").strip()
    universe_source = str(payload.get("execution_universe_source") or "").strip()
    validate_active_account_contract(
        execution_account_login=execution_account,
        market_data_account_login=market_data_account,
    )
    expected_source = f"mt5_risk_symbols_utc_{execution_account}.csv"
    if Path(universe_source).name != expected_source:
        raise HistoricalDataError(
            "INVENTORY_EXECUTION_UNIVERSE_MISMATCH",
            f"execution universe must be {expected_source}, got {universe_source!r}",
        )
    raw_universe_sha256 = payload.get("execution_universe_raw_sha256")
    canonical_universe_sha256 = payload.get("execution_universe_canonical_sha256")
    canonical_snapshot = payload.get("execution_universe_canonical_snapshot")
    if (
        not isinstance(raw_universe_sha256, str)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", raw_universe_sha256) is None
        or payload.get("broker_universe_raw_sha256") != raw_universe_sha256
    ):
        raise HistoricalDataError(
            "INVENTORY_RAW_UNIVERSE_PROVENANCE_INVALID",
            "historical inventory raw execution-universe provenance is invalid",
        )
    if (
        payload.get("execution_universe_canonical_schema_version")
        != EXECUTION_UNIVERSE_CANONICAL_SCHEMA_VERSION
        or not isinstance(canonical_universe_sha256, str)
        or payload.get("execution_universe_sha256") != canonical_universe_sha256
        or not isinstance(canonical_snapshot, dict)
    ):
        raise HistoricalDataError(
            "INVENTORY_CANONICAL_UNIVERSE_PROVENANCE_INVALID",
            "historical inventory canonical execution-universe provenance is invalid",
        )
    verify_canonical_execution_universe_snapshot(
        canonical_snapshot,
        expected_sha256=canonical_universe_sha256,
    )
    if canonical_snapshot.get("execution_account_login") != execution_account:
        raise HistoricalDataError(
            "INVENTORY_CANONICAL_UNIVERSE_PROVENANCE_INVALID",
            "historical inventory canonical universe account is invalid",
        )
    if payload.get("market_data_source_type") != SOURCE_TYPE:
        raise HistoricalDataError(
            "INVENTORY_MARKET_DATA_SOURCE_INVALID",
            "historical inventory market-data source type is invalid",
        )
    source_proof = payload.get("source_proof")
    if not isinstance(source_proof, dict) or (
        source_proof.get("market_data_account_login") != market_data_account
        or source_proof.get("market_data_account_server")
        != payload.get("market_data_account_server")
        or source_proof.get("source_type") != SOURCE_TYPE
        or source_proof.get("authenticated_market_data_account_verified") is not True
        or source_proof.get("read_only_operations") != list(READ_ONLY_MT5_OPERATIONS)
    ):
        raise HistoricalDataError(
            "INVENTORY_SOURCE_PROOF_MISMATCH",
            "historical inventory market-data source proof is invalid",
        )
    entries = payload.get("entries")
    if not isinstance(entries, list):
        raise HistoricalDataError("INVENTORY_ENTRIES_INVALID", "historical inventory entries must be a list")
    symbols: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("symbol"), str):
            raise HistoricalDataError("INVENTORY_ENTRY_INVALID", "historical inventory entry is malformed")
        symbol = entry["symbol"]
        if symbol in symbols:
            raise HistoricalDataError("INVENTORY_SYMBOL_DUPLICATE", f"duplicate inventory symbol: {symbol}")
        symbols.add(symbol)


def verify_inventory_account_identities(
    payload: Mapping[str, object],
    *,
    execution_account_login: str,
    market_data_account_login: str,
) -> None:
    expected_execution = str(execution_account_login).strip()
    expected_market_data = str(market_data_account_login).strip()
    if payload.get("execution_account_login") != expected_execution:
        raise HistoricalDataError(
            "INVENTORY_EXECUTION_ACCOUNT_MISMATCH",
            f"inventory execution account does not match {expected_execution!r}",
        )
    if payload.get("market_data_account_login") != expected_market_data:
        raise HistoricalDataError(
            "INVENTORY_MARKET_DATA_ACCOUNT_MISMATCH",
            f"inventory market-data account does not match {expected_market_data!r}",
        )


def load_inventory(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HistoricalDataError("INVENTORY_READ_FAILED", f"cannot read inventory: {path}") from exc
    if not isinstance(payload, dict):
        raise HistoricalDataError("INVENTORY_ROOT_INVALID", "historical inventory root must be an object")
    verify_inventory(payload)
    return payload


def write_inventory_artifacts(
    *,
    inventory_path: Path,
    compatibility_path: Path,
    payload: Mapping[str, object],
) -> None:
    inventory_payload = dict(payload)
    inventory_payload["inventory_sha256"] = inventory_hash(inventory_payload)
    entries = inventory_payload.get("entries", [])
    accepted = sorted(
        (
            (str(entry["symbol"]), int(entry["row_count"]))
            for entry in entries
            if isinstance(entry, dict) and entry.get("accepted_historical_data") is True
        ),
        key=lambda item: item[0],
    )
    compatibility = io.StringIO(newline="")
    writer = csv.writer(compatibility, lineterminator="\n")
    writer.writerow(("symbol", "rows"))
    writer.writerows(accepted)
    for path, content in (
        (inventory_path, canonical_json_bytes(inventory_payload) + b"\n"),
        (compatibility_path, compatibility.getvalue().encode("utf-8")),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.tmp")
        _write_synced(temporary, content)
        temporary.replace(path)
        _fsync_directory(path.parent)


def collector_code_sha256() -> str:
    return sha256_bytes(Path(__file__).read_bytes())


def source_proof_result(
    proof: Mapping[str, object],
    *,
    execution_account_login: str,
    execution_universe_source: str,
    execution_universe: CanonicalExecutionUniverseV1,
) -> dict[str, object]:
    market_data_account_login = str(proof.get("market_data_account_login") or "")
    execution_account_login, market_data_account_login = validate_active_account_contract(
        execution_account_login=execution_account_login,
        market_data_account_login=market_data_account_login,
    )
    return {
        "status": "SOURCE_VERIFIED",
        "execution_account_login": str(execution_account_login),
        "market_data_account_login": market_data_account_login,
        "execution_universe_source": execution_universe_source,
        "execution_universe_sha256": execution_universe.canonical_sha256,
        "execution_universe_raw_sha256": execution_universe.raw_sha256,
        "execution_universe_canonical_sha256": execution_universe.canonical_sha256,
        "execution_universe_canonical_schema_version": (
            EXECUTION_UNIVERSE_CANONICAL_SCHEMA_VERSION
        ),
        "market_data_source_type": proof.get("source_type"),
        "cross_account_provenance": {
            "execution_account_login": str(execution_account_login),
            "market_data_account_login": market_data_account_login,
            "accounts_claimed_equivalent": False,
            "price_feeds_claimed_byte_identical": False,
        },
        "source_proof": dict(proof),
        "orders_sent": 0,
        "orders_canceled": 0,
        "positions_modified": 0,
    }


__all__ = [
    "ASSET_CLASS_FX",
    "BAR_FIELDS",
    "CHUNK_ACQUISITION_CODE_SHA256",
    "CHUNK_POLICY_VERSION",
    "COLLECTOR_VERSION",
    "COVERAGE_DISCOVERY_POLICY_VERSION",
    "COVERAGE_TRUNCATED_REASON_CODE",
    "DATA_INTEGRITY_CHUNK_ERROR_CODES",
    "GENUINE_HISTORICAL_UNAVAILABLE_ERROR_CODES",
    "TRANSIENT_CHUNK_RETRY_ATTEMPTS",
    "EXECUTION_UNIVERSE_AUDIT_VOLATILE_FIELDS",
    "EXECUTION_UNIVERSE_CANONICAL_SCHEMA_VERSION",
    "EXECUTION_UNIVERSE_IDENTITY_RELEVANT_FIELDS",
    "EXECUTION_UNIVERSE_SOURCE_FIELDS",
    "DATASET_SCHEMA_VERSION",
    "INVENTORY_SCHEMA_VERSION",
    "READ_ONLY_MT5_OPERATIONS",
    "SER8_ACTIVE_MARKET_DATA_ACCOUNT_LOGIN",
    "SER8_EXECUTION_ACCOUNT_LOGIN",
    "BrokerSymbolV1",
    "CanonicalExecutionUniverseV1",
    "ChunkedAcquisitionV1",
    "CoverageDiscoveryV1",
    "HistoricalBarV1",
    "HistoricalDataError",
    "HistoricalRateSource",
    "MetaTrader5HistorySource",
    "acquire_chunked_history",
    "assert_historical_artifact_isolation",
    "build_dataset_manifest",
    "build_canonical_execution_universe",
    "canonical_bars_csv",
    "discover_available_coverage",
    "load_canonical_execution_universe",
    "collector_code_sha256",
    "inventory_hash",
    "load_broker_universe",
    "load_canonical_bars",
    "load_inventory",
    "merge_historical_bars",
    "parse_utc",
    "plan_calendar_month_chunks",
    "publish_dataset",
    "publish_canonical_execution_universe_snapshot",
    "source_proof_result",
    "validate_historical_bars",
    "validate_active_account_contract",
    "verify_dataset",
    "verify_canonical_execution_universe_artifact",
    "verify_canonical_execution_universe_snapshot",
    "verify_inventory",
    "verify_inventory_account_identities",
    "verify_cross_account_symbol_compatibility",
    "write_inventory_artifacts",
]
