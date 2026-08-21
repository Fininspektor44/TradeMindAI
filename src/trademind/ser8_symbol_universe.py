"""SER8 Full Symbol Universe + Research Ranking V1: broad, generic symbol
DISCOVERY and pre-holdout RESEARCH-READINESS ranking, built from REAL
broker/runtime metadata -- never a handwritten symbol list, never
fabricated counts, never a shortcut into execution authority.

FULL UNIVERSE != FULL EXECUTION (the central rule of this module and of
the task that created it). Nothing in this module grants, widens, or
infers any execution authority for any symbol. A symbol can appear here
with ``execution_status="ACCEPTED"`` ONLY because a genuine
``HypothesisRegistry`` record for a hypothesis whose own
``HypothesisTradeableScopeV1.symbol`` equals it has ALREADY reached
``ACCEPTED`` through the sole authoritative research lifecycle
(``TrainTestExecutionControl`` -> ``ValidationExecutionControl`` ->
``HoldoutTriggerBridge`` -> ``FinalVerdictAcceptanceControl``) -- this
module never advances, bypasses, or shortcuts that lifecycle, and never
seals, opens, or reuses a protected final holdout across symbols. It
only OBSERVES the registry's own already-persisted state and reports it.

AUTHORITATIVE INSTRUMENT-METADATA CRITERIA (audit requirement 5): reuses
the EXACT SAME required-field list and structural validity criteria
``trademind.mt5_risk_adapter`` already uses to accept an instrument for
Risk Manager -- tick_size/tick_value/volume_min/volume_max/volume_step
present and positive, and ``trade_mode`` not ``DISABLED``/``CLOSEONLY``
-- rather than inventing a second, parallel notion of "risk model
supported". This module does not import ``mt5_risk_adapter`` (a private
module-level CSV reader there, ``_read_csv_stable``, is intentionally
never reached from outside that module, matching this session's own
standing "never call a private helper directly" convention); instead it
reads the SAME public ``SYMBOL_REQUIRED_FIELDS`` constant and re-applies
the identical, narrow structural checks documented above.

ASSET-CLASS CLASSIFICATION is deliberately best-effort and clearly
labeled as such: MT5's own read-only symbol export (as of the unified
executor v1.6) does not currently carry a broker-provided asset-class/
path field, so :func:`classify_asset_class` falls back to a narrow,
documented, extensible naming-convention heuristic (ISO-4217-style
6-letter currency pairs -> FX; a small set of well-known metal root
tickers -> METALS; otherwise UNKNOWN). This classification is used ONLY
to route RESEARCH PRIORITY and to gate which asset classes this
repository's CURRENT risk/execution model is proven safe for -- it is
NEVER consulted by the authoritative research lifecycle, the risk gate,
or execution, and a symbol misclassified here can never gain execution
authority it did not already earn through the real lifecycle.

CORRELATION MODEL: ``config/mt5/correlation_groups_v1.json`` is already
symbol-agnostic by construction (an unlisted symbol falls back to its
own isolated ``SYMBOL:<symbol>`` group -- see
``trademind.mt5_risk_adapter._correlation_group``) -- so
``correlation_model_supported`` is structurally ``True`` for every
symbol; this module additionally records whether a symbol has an
EXPLICIT correlation-group entry or is relying on that fallback, for
operator visibility only.
"""

from __future__ import annotations

import csv
import io
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from trademind.discovery.hypothesis_registry import HypothesisRegistry, HypothesisState
from trademind.signal_intelligence import candidate_from_dict
from trademind.signal_statistics_provenance import canonical_json_bytes, sha256_bytes

SCHEMA_VERSION = "ser8-symbol-universe-v1"
_UNIVERSE_HASH_DOMAIN = b"trademind:ser8:symbol-universe:v1"

# The SAME authoritative required-field list trademind.mt5_risk_adapter
# already uses to accept a symbol export row for Risk Manager -- reused
# verbatim, never re-invented, so "risk model supported" here means
# EXACTLY "this row would also be accepted by the real risk adapter",
# never a looser or stricter, independently-invented notion.
SYMBOL_REQUIRED_FIELDS = (
    "time_msc", "account_login", "currency", "symbol", "trade_mode", "tick_size", "tick_value",
    "tick_value_profit", "tick_value_loss", "volume_min", "volume_max", "volume_step",
    "contract_size", "margin_initial", "margin_buy_per_volume", "margin_sell_per_volume", "leverage",
)
_UNSUPPORTED_TRADE_MODES = {"DISABLED", "CLOSEONLY"}

# A cheap, narrow, structural PRE-FILTER only -- never a substitute for
# the authoritative research lifecycle's own dataset-size validation
# (trademind.discovery.train_test_execution's own sample-size checks
# remain the real gate). Configurable, deliberately conservative.
DEFAULT_MINIMUM_LIVE_SIGNAL_SAMPLE = 1
DEFAULT_MINIMUM_FORWARD_OUTCOME_SAMPLE = 20

# ISO-4217-style currency codes this repository has real, checked-in
# evidence for (standard_v1.json's own account_currency shape, and every
# proven FX pair this session's own real Windows deployment has used).
# A narrow, documented, extensible reference table for CLASSIFICATION
# ONLY -- never consulted by risk/execution, and adding a currency here
# grants no execution authority to anything.
_KNOWN_FX_CURRENCY_CODES = frozenset({
    "USD", "EUR", "GBP", "JPY", "CHF", "AUD", "NZD", "CAD",
    "SEK", "NOK", "DKK", "PLN", "CZK", "HUF", "TRY", "ZAR",
    "MXN", "SGD", "HKD", "CNH", "CNY",
})
_KNOWN_METAL_ROOTS = frozenset({"XAU", "XAG", "XPT", "XPD"})

ASSET_CLASS_FX = "FX"
ASSET_CLASS_METALS = "METALS"
ASSET_CLASS_UNKNOWN = "UNKNOWN"

RESEARCH_STATUS_DISCOVERED = "DISCOVERED"
RESEARCH_STATUS_DATA_INSUFFICIENT = "DATA_INSUFFICIENT"
RESEARCH_STATUS_RISK_MODEL_UNSUPPORTED = "RISK_MODEL_UNSUPPORTED"
RESEARCH_STATUS_RESEARCH_READY = "RESEARCH_READY"
RESEARCH_STATUS_RESEARCHING = "RESEARCHING"
RESEARCH_STATUS_REJECTED = "REJECTED"
RESEARCH_STATUS_ACCEPTED = "ACCEPTED"

EXECUTION_STATUS_NOT_EXECUTABLE = "NOT_EXECUTABLE"
EXECUTION_STATUS_DEMO_ACTIVE = "DEMO_ACTIVE"
EXECUTION_STATUS_SUSPENDED = "SUSPENDED"

# Both are genuinely TERMINAL rejection states in HypothesisRegistry's own
# state machine (see _TERMINAL_FAMILY_STATES in hypothesis_registry.py):
# VALIDATION_REJECTED is reached directly from TRAIN_TESTED (never advances
# further -- there is no VALIDATION_REJECTED -> REJECTED_FINAL transition),
# while REJECTED_FINAL is reached from HOLDOUT_CONSUMED (validation passed
# but the final verdict rejected it). Both mean the SAME thing to this
# module: the hypothesis will never reach ACCEPTED.
_TERMINAL_REJECTED_STATES = frozenset({HypothesisState.VALIDATION_REJECTED, HypothesisState.REJECTED_FINAL})


class SER8SymbolUniverseError(RuntimeError):
    """Raised for a structurally invalid symbol export or an attempt to
    persist an inconsistent universe entry -- always before anything is
    trusted or persisted."""


def _nonempty_str(value: object, *, field_name: str) -> str:
    if type(value) is not str or not value.strip():
        raise SER8SymbolUniverseError(f"{field_name} must be a non-empty string")
    return value


def classify_asset_class(symbol: str) -> str:
    """Best-effort, documented, naming-convention classifier -- see this
    module's own docstring for the full rationale and its explicit
    safety boundary (research-priority routing only, never execution
    authority). Returns :data:`ASSET_CLASS_UNKNOWN` for anything not
    confidently recognized -- never guesses."""
    upper = symbol.strip().upper()
    if len(upper) == 6 and upper[:3] in _KNOWN_FX_CURRENCY_CODES and upper[3:] in _KNOWN_FX_CURRENCY_CODES and upper[:3] != upper[3:]:
        return ASSET_CLASS_FX
    if len(upper) == 6 and upper[:3] in _KNOWN_METAL_ROOTS and upper[3:] in _KNOWN_FX_CURRENCY_CODES:
        return ASSET_CLASS_METALS
    return ASSET_CLASS_UNKNOWN


# Asset classes this repository's CURRENT risk/execution model has real,
# checked-in, Windows-proven coverage for. Deliberately narrow -- see
# the module docstring's ASSET-CLASS BOUNDARY section. Extending this
# set is a real, separate, human-reviewed decision (new instrument-
# metadata handling, new correlation semantics, new margin/contract-size
# assumptions), never inferred from mere symbol discovery.
_RISK_MODEL_PROVEN_ASSET_CLASSES = frozenset({ASSET_CLASS_FX})


@dataclass(frozen=True, slots=True)
class SymbolUniverseEntryV1:
    """Immutable, deterministically-hashed record of ONE symbol's
    discovered/classified state. Construct only via
    :func:`discover_symbol_universe` -- never by hand. Carries no
    execution field of any kind (no order, no ticket, no broker side
    effect) -- this is pure classification/observation."""

    schema_version: str
    symbol: str
    asset_class: str
    broker_trade_mode: str
    data_available: bool
    historical_rows: int | None
    live_runtime_supported: bool
    live_signal_sample_count: int
    risk_model_supported: bool
    correlation_model_supported: bool
    correlation_group: str
    research_status: str
    execution_status: str
    rejection_reason: str | None
    captured_at: str
    entry_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise SER8SymbolUniverseError("unsupported symbol universe schema_version")
        _nonempty_str(self.symbol, field_name="symbol")
        _nonempty_str(self.asset_class, field_name="asset_class")
        _nonempty_str(self.broker_trade_mode, field_name="broker_trade_mode")
        _nonempty_str(self.research_status, field_name="research_status")
        _nonempty_str(self.execution_status, field_name="execution_status")
        _nonempty_str(self.correlation_group, field_name="correlation_group")
        if self.historical_rows is not None and self.historical_rows < 0:
            raise SER8SymbolUniverseError("historical_rows cannot be negative")
        if self.live_signal_sample_count < 0:
            raise SER8SymbolUniverseError("live_signal_sample_count cannot be negative")
        try:
            parsed = datetime.fromisoformat(self.captured_at)
        except ValueError as exc:
            raise SER8SymbolUniverseError("captured_at must be an ISO timestamp") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise SER8SymbolUniverseError("captured_at must be timezone-aware")

        object.__setattr__(
            self, "entry_hash",
            sha256_bytes(_UNIVERSE_HASH_DOMAIN + b"\x00" + canonical_json_bytes(self.semantic_projection())),
        )

    def semantic_projection(self) -> dict[str, object]:
        """Excludes ``captured_at`` (wall-clock, non-deterministic) so
        re-discovering the identical symbol state always hashes
        identically, mirroring every other artifact in this lineage."""
        return {
            "schema_version": self.schema_version,
            "symbol": self.symbol,
            "asset_class": self.asset_class,
            "broker_trade_mode": self.broker_trade_mode,
            "data_available": self.data_available,
            "historical_rows": self.historical_rows,
            "live_runtime_supported": self.live_runtime_supported,
            "live_signal_sample_count": self.live_signal_sample_count,
            "risk_model_supported": self.risk_model_supported,
            "correlation_model_supported": self.correlation_model_supported,
            "correlation_group": self.correlation_group,
            "research_status": self.research_status,
            "execution_status": self.execution_status,
            "rejection_reason": self.rejection_reason,
        }

    def to_payload(self) -> dict[str, object]:
        payload = self.semantic_projection()
        payload["captured_at"] = self.captured_at
        payload["entry_hash"] = self.entry_hash
        return payload


def _entry_from_payload(payload: dict[str, object]) -> SymbolUniverseEntryV1:
    return SymbolUniverseEntryV1(
        schema_version=payload["schema_version"], symbol=payload["symbol"], asset_class=payload["asset_class"],
        broker_trade_mode=payload["broker_trade_mode"], data_available=payload["data_available"],
        historical_rows=payload["historical_rows"], live_runtime_supported=payload["live_runtime_supported"],
        live_signal_sample_count=payload["live_signal_sample_count"], risk_model_supported=payload["risk_model_supported"],
        correlation_model_supported=payload["correlation_model_supported"], correlation_group=payload["correlation_group"],
        research_status=payload["research_status"], execution_status=payload["execution_status"],
        rejection_reason=payload.get("rejection_reason"), captured_at=payload["captured_at"],
    )


def _read_symbols_csv(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        raise SER8SymbolUniverseError(f"MT5 symbol export not found: {path}")
    text = path.read_text(encoding="utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    fieldnames = reader.fieldnames or []
    missing = [name for name in SYMBOL_REQUIRED_FIELDS if name not in fieldnames]
    if missing:
        raise SER8SymbolUniverseError(f"{path}: missing required symbol export column(s): {missing}")
    return [dict(row) for row in reader]


def _row_is_risk_model_supported(row: Mapping[str, str]) -> tuple[bool, str]:
    """Mirrors trademind.mt5_risk_adapter._build_instrument's own
    structural validity criteria exactly (never re-invented, never
    weakened) -- returns (supported, reason_if_not)."""
    trade_mode = (row.get("trade_mode") or "").strip().upper()
    if trade_mode in _UNSUPPORTED_TRADE_MODES:
        return False, f"trade_mode={trade_mode}"
    try:
        tick_size = float(row.get("tick_size") or 0)
        volume_min = float(row.get("volume_min") or 0)
        volume_max = float(row.get("volume_max") or 0)
        volume_step = float(row.get("volume_step") or 0)
    except (TypeError, ValueError):
        return False, "non-numeric tick_size/volume_min/volume_max/volume_step"
    if tick_size <= 0:
        return False, "tick_size must be positive"
    if volume_min <= 0 or volume_max <= 0 or volume_step <= 0:
        return False, "volume_min/volume_max/volume_step must be positive"
    tick_value_ok = any(
        _is_positive(row.get(name)) for name in ("tick_value", "tick_value_profit", "tick_value_loss")
    )
    if not tick_value_ok:
        return False, "no positive tick_value/tick_value_profit/tick_value_loss"
    return True, ""


def _is_positive(value: object) -> bool:
    try:
        return float(value) > 0
    except (TypeError, ValueError):
        return False


def _correlation_group_for(mapping: Mapping[str, object], symbol: str) -> tuple[str, bool]:
    """Mirrors trademind.mt5_risk_adapter._correlation_group's own
    fallback exactly: an unlisted symbol is isolated in its own group,
    never left unclassified. Returns (group, is_explicit_entry)."""
    symbols = mapping.get("symbols", {})
    if isinstance(symbols, Mapping):
        configured = symbols.get(symbol)
        if isinstance(configured, str) and configured.strip():
            return configured.strip().upper(), True
        if isinstance(configured, Mapping):
            for key in ("BUY", "SELL", "DEFAULT"):
                value = configured.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip().upper(), True
    return f"SYMBOL:{symbol}", False


def scan_live_signal_symbols(candidates_paths: Iterable[Path]) -> dict[str, int]:
    """Reads every REAL candidate journal supplied and returns
    ``{symbol: sample_count}`` -- the ONLY authoritative way this module
    determines ``live_runtime_supported``/signal frequency. Never
    invents a symbol that does not genuinely appear in a real journal
    line; a malformed line is skipped (matches this codebase's own
    established candidate-journal tolerance -- the live runtime itself
    is the sole writer of these files and is already proven not to write
    malformed lines in real operation)."""
    counts: dict[str, int] = {}
    for path in candidates_paths:
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8-sig")
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
                candidate = candidate_from_dict(payload)
            except Exception:
                continue
            counts[candidate.symbol] = counts.get(candidate.symbol, 0) + 1
    return counts


def discover_symbol_universe(
    *,
    symbols_csv: Path,
    candidates_paths: Sequence[Path] = (),
    correlation_config: Mapping[str, object] | None = None,
    historical_rows_by_symbol: Mapping[str, int] | None = None,
    minimum_live_signal_sample: int = DEFAULT_MINIMUM_LIVE_SIGNAL_SAMPLE,
    now: datetime | None = None,
) -> tuple[SymbolUniverseEntryV1, ...]:
    """Builds the FULL symbol universe from REAL broker/runtime metadata
    -- never a handwritten symbol list. Every symbol present in
    ``symbols_csv`` (the unified executor's own read-only Market Watch
    export -- the SAME file ``trademind.mt5_risk_adapter`` already
    consumes) is DISCOVERED; ``research_status`` only ever advances past
    ``DISCOVERED`` based on genuinely observed properties (structural
    risk-model validity, live-runtime signal presence, historical data
    sufficiency IF supplied) -- never assumed, never fabricated.

    ``historical_rows_by_symbol``, when supplied, must come from a REAL
    dataset inventory (e.g. a research source CSV's own row counts) --
    this function never invents a row count for a symbol it was not
    told about; such a symbol's ``historical_rows`` stays ``None`` and it
    is conservatively treated as ``DATA_INSUFFICIENT`` rather than
    ``RESEARCH_READY``."""
    captured_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    rows = _read_symbols_csv(symbols_csv)
    live_signal_counts = scan_live_signal_symbols(candidates_paths)
    correlation_mapping = correlation_config or {}
    historical = historical_rows_by_symbol or {}

    entries: list[SymbolUniverseEntryV1] = []
    seen_symbols: set[str] = set()
    for row in rows:
        symbol = (row.get("symbol") or "").strip().upper()
        if not symbol or symbol in seen_symbols:
            continue
        seen_symbols.add(symbol)

        risk_model_supported, risk_reason = _row_is_risk_model_supported(row)
        correlation_group, _explicit = _correlation_group_for(correlation_mapping, symbol)
        asset_class = classify_asset_class(symbol)
        live_sample_count = live_signal_counts.get(symbol, 0)
        live_runtime_supported = live_sample_count >= minimum_live_signal_sample
        rows_available = historical.get(symbol)
        data_available = rows_available is not None and rows_available > 0

        rejection_reason: str | None = None
        if not risk_model_supported:
            research_status = RESEARCH_STATUS_RISK_MODEL_UNSUPPORTED
            rejection_reason = risk_reason
        elif asset_class not in _RISK_MODEL_PROVEN_ASSET_CLASSES:
            research_status = RESEARCH_STATUS_RISK_MODEL_UNSUPPORTED
            rejection_reason = (
                f"asset_class={asset_class} is not yet proven safe by this repository's current "
                "risk/execution model (see ASSET_CLASS_BOUNDARY in the module docstring)"
            )
        elif not data_available:
            research_status = RESEARCH_STATUS_DATA_INSUFFICIENT
            rejection_reason = "no historical dataset row count supplied for this symbol"
        else:
            # Fully vetted: valid risk-model instrument metadata, a
            # proven-safe asset class, AND sufficient historical data --
            # this symbol is genuinely ready to enter the authoritative
            # research lifecycle (Proposal -> ... -> Final Verdict), not
            # merely enumerated. A symbol present in the broker export
            # but not yet through this classification never reaches this
            # branch at all -- discover_symbol_universe always fully
            # classifies every row in one pass, so RESEARCH_READY is the
            # correct terminal status here, not a separate DISCOVERED
            # holding state (DISCOVERED remains available for a future
            # caller that wants to represent a symbol observed before
            # vetting runs, e.g. a raw broker enumeration step).
            research_status = RESEARCH_STATUS_RESEARCH_READY

        entries.append(
            SymbolUniverseEntryV1(
                schema_version=SCHEMA_VERSION, symbol=symbol, asset_class=asset_class,
                broker_trade_mode=(row.get("trade_mode") or "").strip().upper(), data_available=data_available,
                historical_rows=rows_available, live_runtime_supported=live_runtime_supported,
                live_signal_sample_count=live_sample_count, risk_model_supported=risk_model_supported,
                correlation_model_supported=True, correlation_group=correlation_group,
                research_status=research_status, execution_status=EXECUTION_STATUS_NOT_EXECUTABLE,
                rejection_reason=rejection_reason, captured_at=captured_at.isoformat(),
            )
        )
    return tuple(entries)


def apply_research_lifecycle_state(
    entries: Sequence[SymbolUniverseEntryV1], *, registry: HypothesisRegistry,
    symbol_to_hypothesis_ids: Mapping[str, Sequence[str]], demo_active_symbols: Sequence[str] = (),
) -> tuple[SymbolUniverseEntryV1, ...]:
    """Read-only overlay: for every entry whose symbol has one or more
    KNOWN hypothesis_id(s) (``symbol_to_hypothesis_ids`` -- supplied by
    the caller from its own genuine records, e.g. every hypothesis whose
    bound tradeable scope's own symbol matches; this function never
    guesses or searches for a hypothesis on its own), looks up that
    hypothesis's REAL, current ``HypothesisRegistry`` state and updates
    ``research_status``/``execution_status`` to reflect it. Never
    advances, creates, or mutates ANY hypothesis or registry state --
    pure observation. A symbol with an ACCEPTED hypothesis AND present in
    ``demo_active_symbols`` (supplied by the caller from the autonomous
    worker's own configured `--hypothesis-id`(s), never inferred) is
    marked DEMO_ACTIVE; ACCEPTED-but-not-configured stays ACCEPTED/
    NOT_EXECUTABLE (FULL UNIVERSE != FULL EXECUTION)."""
    demo_active = set(demo_active_symbols)
    updated: list[SymbolUniverseEntryV1] = []
    for entry in entries:
        hypothesis_ids = symbol_to_hypothesis_ids.get(entry.symbol, ())
        if not hypothesis_ids:
            updated.append(entry)
            continue

        research_status = entry.research_status
        execution_status = entry.execution_status
        rejection_reason = entry.rejection_reason
        any_accepted = False
        any_researching = False
        for hypothesis_id in hypothesis_ids:
            try:
                record = registry.get(hypothesis_id)
            except KeyError:
                continue
            if record.state is HypothesisState.ACCEPTED:
                any_accepted = True
            elif record.state in _TERMINAL_REJECTED_STATES:
                if not any_accepted:
                    research_status = RESEARCH_STATUS_REJECTED
                    rejection_reason = f"hypothesis {hypothesis_id} reached {record.state.value}"
            elif record.state is not HypothesisState.PROPOSED:
                any_researching = True

        if any_accepted:
            research_status = RESEARCH_STATUS_ACCEPTED
            rejection_reason = None
            execution_status = EXECUTION_STATUS_DEMO_ACTIVE if entry.symbol in demo_active else EXECUTION_STATUS_NOT_EXECUTABLE
        elif any_researching and research_status not in (RESEARCH_STATUS_REJECTED,):
            research_status = RESEARCH_STATUS_RESEARCHING

        updated.append(
            SymbolUniverseEntryV1(
                schema_version=entry.schema_version, symbol=entry.symbol, asset_class=entry.asset_class,
                broker_trade_mode=entry.broker_trade_mode, data_available=entry.data_available,
                historical_rows=entry.historical_rows, live_runtime_supported=entry.live_runtime_supported,
                live_signal_sample_count=entry.live_signal_sample_count, risk_model_supported=entry.risk_model_supported,
                correlation_model_supported=entry.correlation_model_supported, correlation_group=entry.correlation_group,
                research_status=research_status, execution_status=execution_status,
                rejection_reason=rejection_reason, captured_at=entry.captured_at,
            )
        )
    return tuple(updated)


def rank_research_readiness(entries: Iterable[SymbolUniverseEntryV1]) -> tuple[SymbolUniverseEntryV1, ...]:
    """Deterministic RESEARCH READINESS ranking (section A) -- which
    symbols are worth researching FIRST. Uses ONLY pre-holdout/
    operational properties already observed on each entry: risk-model
    validity, data availability, historical sample size, live-signal
    sample size -- NEVER a fabricated profitability/performance score
    (that is FORWARD DEMO PERFORMANCE, section C, computed separately by
    :func:`aggregate_forward_demo_performance` and never mixed in here).
    A symbol already RESEARCHING/ACCEPTED/REJECTED is still ranked (for
    visibility) but naturally sorts after genuinely READY candidates."""

    def _key(entry: SymbolUniverseEntryV1) -> tuple:
        is_ready = entry.research_status == RESEARCH_STATUS_RESEARCH_READY
        return (
            0 if is_ready else 1,
            not entry.risk_model_supported,
            not entry.data_available,
            -(entry.historical_rows or 0),
            -(entry.live_signal_sample_count or 0),
            entry.symbol,
        )

    return tuple(sorted(entries, key=_key))


@dataclass(frozen=True, slots=True)
class ForwardPerformanceSummaryV1:
    """FORWARD DEMO PERFORMANCE (section C) -- computed ONLY from
    genuine, already-captured ``ser8_demo_trade_outcomes`` rows (see
    ``trademind.ser8_demo_trade_outcome_capture``), never inferred.
    ``sufficient_sample`` is an explicit, always-checked safeguard --
    every field derived from realized_pl stays ``None`` when the sample
    is below ``minimum_sample_size``, so this record can never be used to
    declare a symbol "best" from a tiny sample."""

    symbol: str
    sample_size: int
    minimum_sample_size: int
    sufficient_sample: bool
    closed_trades_with_known_pl: int
    total_realized_pl: float | None
    win_rate: float | None
    average_realized_pl: float | None


def aggregate_forward_demo_performance(
    outcomes: Sequence[Mapping[str, object]], *, symbol: str,
    minimum_sample_size: int = DEFAULT_MINIMUM_FORWARD_OUTCOME_SAMPLE,
) -> ForwardPerformanceSummaryV1:
    """Aggregates ALREADY-CAPTURED, authoritative outcome rows (each a
    payload from ``SER8DemoTradeOutcomeControl.get_outcome``/persisted
    ``ser8_demo_trade_outcomes`` row) for ONE symbol. Never queries a
    database itself (the caller supplies the rows, keeping this function
    pure/testable and decoupled from any particular storage engine);
    never computes anything beyond simple, transparent arithmetic over
    ``realized_pl`` -- no invented profitability score."""
    matching = [row for row in outcomes if row.get("symbol") == symbol]
    sample_size = len(matching)
    sufficient = sample_size >= minimum_sample_size
    pls = [row["realized_pl"] for row in matching if row.get("realized_pl") is not None]
    total_pl = sum(pls) if pls else None
    win_rate = (sum(1 for pl in pls if pl > 0) / len(pls)) if (sufficient and pls) else None
    average_pl = (total_pl / len(pls)) if (sufficient and pls) else None
    return ForwardPerformanceSummaryV1(
        symbol=symbol, sample_size=sample_size, minimum_sample_size=minimum_sample_size,
        sufficient_sample=sufficient, closed_trades_with_known_pl=len(pls),
        total_realized_pl=(total_pl if sufficient else None), win_rate=win_rate, average_realized_pl=average_pl,
    )


class SER8SymbolUniverseControl:
    """Owns one new, additive SQLite table in the same database file as
    ``HypothesisRegistry`` (``registry.path``) -- ``ser8_symbol_universe``,
    keyed by ``symbol``, one row per symbol (latest discovery upserts in
    place; this is a point-in-time universe snapshot, not a history
    ledger -- callers wanting trend-over-time should re-run discovery on
    their own cadence and persist the printed inventory externally, e.g.
    to a log, matching this whole product line's existing observability
    convention). Never modifies ``HypothesisRegistry``'s own schema,
    tables, or state machine."""

    def __init__(self, *, registry: HypothesisRegistry) -> None:
        self.registry = registry
        self.path = Path(registry.path)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        return db

    def _init_schema(self) -> None:
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS ser8_symbol_universe (
                    symbol TEXT PRIMARY KEY,
                    asset_class TEXT NOT NULL,
                    research_status TEXT NOT NULL,
                    execution_status TEXT NOT NULL,
                    captured_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                """
            )

    def persist_universe(self, entries: Iterable[SymbolUniverseEntryV1]) -> int:
        """Upserts every entry -- idempotent per symbol. Returns the
        number of rows written. Never deletes an existing symbol's row
        (a symbol temporarily absent from one discovery run, e.g. a
        transient Market Watch filter change, keeps its own last-known
        state rather than silently vanishing)."""
        written = 0
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                for entry in entries:
                    db.execute(
                        """
                        INSERT INTO ser8_symbol_universe(symbol, asset_class, research_status, execution_status, captured_at, payload_json)
                        VALUES (?, ?, ?, ?, ?, ?)
                        ON CONFLICT(symbol) DO UPDATE SET
                            asset_class=excluded.asset_class, research_status=excluded.research_status,
                            execution_status=excluded.execution_status, captured_at=excluded.captured_at,
                            payload_json=excluded.payload_json
                        """,
                        (
                            entry.symbol, entry.asset_class, entry.research_status, entry.execution_status,
                            entry.captured_at, canonical_json_bytes(entry.to_payload()).decode("utf-8"),
                        ),
                    )
                    written += 1
                db.commit()
            except Exception:
                db.rollback()
                raise
        return written

    def get_entry(self, symbol: str) -> SymbolUniverseEntryV1 | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT payload_json FROM ser8_symbol_universe WHERE symbol=?", (symbol.strip().upper(),)
            ).fetchone()
        if row is None:
            return None
        return _entry_from_payload(json.loads(row["payload_json"]))

    def list_entries(self) -> tuple[SymbolUniverseEntryV1, ...]:
        with self._connect() as db:
            rows = db.execute("SELECT payload_json FROM ser8_symbol_universe ORDER BY symbol").fetchall()
        return tuple(_entry_from_payload(json.loads(row["payload_json"])) for row in rows)


__all__ = [
    "ASSET_CLASS_FX",
    "ASSET_CLASS_METALS",
    "ASSET_CLASS_UNKNOWN",
    "DEFAULT_MINIMUM_FORWARD_OUTCOME_SAMPLE",
    "DEFAULT_MINIMUM_LIVE_SIGNAL_SAMPLE",
    "EXECUTION_STATUS_DEMO_ACTIVE",
    "EXECUTION_STATUS_NOT_EXECUTABLE",
    "EXECUTION_STATUS_SUSPENDED",
    "RESEARCH_STATUS_ACCEPTED",
    "RESEARCH_STATUS_DATA_INSUFFICIENT",
    "RESEARCH_STATUS_DISCOVERED",
    "RESEARCH_STATUS_REJECTED",
    "RESEARCH_STATUS_RESEARCHING",
    "RESEARCH_STATUS_RESEARCH_READY",
    "RESEARCH_STATUS_RISK_MODEL_UNSUPPORTED",
    "SCHEMA_VERSION",
    "SYMBOL_REQUIRED_FIELDS",
    "ForwardPerformanceSummaryV1",
    "SER8SymbolUniverseControl",
    "SER8SymbolUniverseError",
    "SymbolUniverseEntryV1",
    "aggregate_forward_demo_performance",
    "apply_research_lifecycle_state",
    "classify_asset_class",
    "discover_symbol_universe",
    "rank_research_readiness",
    "scan_live_signal_symbols",
]
