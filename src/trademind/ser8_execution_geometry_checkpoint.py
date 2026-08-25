"""Named, finite JSON safety budget and per-symbol resume checkpoints for
the SER8 execution-geometry A/B experiment
(``trademind.ser8_execution_geometry_experiment``).

RESEARCH/SCREENING ONLY -- this module never regenerates signals, never
touches historical acquisition, never reads or writes the protected
hypothesis/holdout lifecycle, and never mutates the authoritative
historical-market-data or replay artifacts on disk. It only reads their
already-verified identity fields (dataset/replay/candidates/outcomes
SHA-256) and writes small, experiment-owned checkpoint files under a
dedicated checkpoint directory that is never the authoritative dataset or
replay directory.

WHY THIS MODULE EXISTS
-----------------------
A real Windows 28-symbol x 4-variant run terminated with:

    payload exceeds maximum canonical JSON bytes 262144

at the FINAL report-hashing step, after every symbol had already been fully,
correctly evaluated (see ``build_multisymbol_geometry_experiment_report``'s
own per-symbol loop, which runs to completion before any hashing happens).
That failure is a report-CAPACITY failure, not evidence that execution-
geometry evaluation itself failed: ``canonical_json_bytes`` was called
without an explicit ``budget=``, so it used
``trademind.signal_statistics_provenance.DEFAULT_JSON_SAFETY_BUDGET``,
whose ``max_canonical_bytes`` is the module-wide default of 262,144 bytes --
a ceiling sized for much smaller artifacts, not a multi-symbol,
four-variant experiment report.

This module fixes that two ways, WITHOUT ever touching
``DEFAULT_JSON_SAFETY_BUDGET`` (every other ``canonical_json_bytes`` caller
in the codebase keeps its original 262,144-byte ceiling unchanged):

1. ``EXECUTION_GEOMETRY_REPORT_JSON_BUDGET`` -- one named, finite,
   artifact-specific :class:`~trademind.signal_statistics_provenance.
   JsonSafetyBudget`, sized for exactly this report shape, following the
   same convention as ``trademind.ser8_historical_data.
   HISTORICAL_INVENTORY_JSON_BUDGET``. It must be used consistently for
   report canonical serialization, hash creation, hash verification,
   writing, and loading -- see
   ``trademind.ser8_execution_geometry_experiment``.

2. Per-symbol resume checkpoints, so an hours-long run never has to redo
   already-completed, expensive symbol evaluation just because a LATER
   step (final report serialization) failed. A checkpoint is written only
   after one symbol's four variants are completely evaluated AND its
   CONTROL reproduction gate has been resolved (i.e. after
   ``build_symbol_geometry_experiment`` returns), and is reused ONLY when
   its identity/hash verifies EXACTLY against the current run's inputs.
   Missing, corrupt, tampered, stale, or identity-mismatched checkpoints
   are never silently accepted -- they always mean "recompute this symbol".
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Mapping, Sequence

from trademind.signal_statistics_provenance import (
    JsonSafetyBudget,
    ProvenanceError,
    canonical_json_bytes,
)

CHECKPOINT_SCHEMA_VERSION = "ser8-execution-geometry-checkpoint-v1"
_CHECKPOINT_HASH_DOMAIN = b"trademind:ser8:execution-geometry-checkpoint:v1"
_IDENTITY_HASH_DOMAIN = b"trademind:ser8:execution-geometry-checkpoint-identity:v1"

# --- execution-geometry-experiment-report bounded JSON capacity ----------
#
# Named, finite, artifact-specific budget for the SER8 execution-geometry
# A/B experiment report AND its per-symbol resume checkpoints (a checkpoint
# holds at most one symbol's share of the report, so the report-level
# budget is generously sufficient for one). This NEVER changes
# DEFAULT_JSON_SAFETY_BUDGET / MAX_CANONICAL_JSON_BYTES -- every other
# canonical_json_bytes caller in the codebase keeps its original
# 262,144-byte ceiling unchanged. This is a distinct, still-finite budget
# sized for exactly ONE artifact shape, following the same convention as
# trademind.ser8_historical_data.HISTORICAL_INVENTORY_JSON_BUDGET.
#
# Sizing basis: the real Windows run that failed covers the current 28
# HISTORICAL_DATA_READY FX symbols x 4 execution-geometry variants. This
# budget provides headroom up to EXECUTION_GEOMETRY_MAX_SYMBOLS symbols --
# the same broker-universe scale ceiling as
# trademind.ser8_historical_data.HISTORICAL_INVENTORY_MAX_SYMBOLS -- so
# growing the ready-symbol set toward the full broker universe does not
# require re-deriving this budget. It intentionally does NOT scale to an
# unlimited or absurdly large value.
EXECUTION_GEOMETRY_MAX_SYMBOLS = 128
EXECUTION_GEOMETRY_VARIANTS_PER_SYMBOL = 4  # len(ALL_VARIANTS); fixed by experiment design
# Sizing context only (not multiplied into the byte estimate below, which
# is driven by the bounded `skipped` list -- see
# _EXECUTION_GEOMETRY_MAX_SKIPPED_PER_VARIANT): years of M5 FX history for
# one LIQUIDITY_SWEEP_OTE setup family realistically produces at most a few
# thousand replay candidates per symbol; the report itself only ever
# serializes COUNTS of candidates (O(1) per variant), never the candidate
# population itself, so a growing candidate population does not grow the
# report.
EXECUTION_GEOMETRY_TYPICAL_MAX_CANDIDATES_PER_SYMBOL = 8_000
# Conservative worst-case count of candidates whose execution-geometry is
# structurally invalid for one (symbol, variant) pair -- each such
# candidate is recorded in the `skipped` list with an explicit reason,
# never silently dropped. Real published SER8 candidates have shown ZERO
# skips for every real fixture exercised so far (their MARKET-only geometry
# is always structurally valid); 64 gives two-plus orders of magnitude of
# headroom over that observed reality, matching the same 64-per-symbol
# order of magnitude trademind.ser8_historical_data.
# HISTORICAL_INVENTORY_MAX_CHUNKS_PER_SYMBOL already uses for a comparably
# bounded per-symbol list.
_EXECUTION_GEOMETRY_MAX_SKIPPED_PER_VARIANT = 64
# Conservative worst-case chronological-stability window count; the
# experiment's own DEFAULT_STABILITY_WINDOW_COUNT is 3 -- this is 4x that.
_EXECUTION_GEOMETRY_MAX_STABILITY_WINDOWS = 12

# Worst-case string-byte estimate for one skipped-candidate row: signal_id,
# variant, reason_code, and a free-text reason, quoting/keys included.
_EXECUTION_GEOMETRY_BYTES_PER_SKIPPED_ROW = 512
# Worst-case string-byte estimate for one chronological-stability window
# summary (trade_count, win_rate, expectancy_r, net_r_total,
# first_completed_at, last_completed_at).
_EXECUTION_GEOMETRY_BYTES_PER_STABILITY_WINDOW = 256
# Worst-case string-byte estimate for one variant's fixed metrics dict (the
# 19 scalar fields compute_symbol_replay_metrics always returns) plus its
# comparative-vs-control dict (6 fields), excluding the
# chronological-stability windows list (sized separately above).
_EXECUTION_GEOMETRY_BYTES_PER_VARIANT_METRICS = 2_048
# Worst-case string-byte estimate for the rest of one variant entry:
# symbol, variant, candidate_count / evaluated_candidate_count /
# skipped_count keys+values -- excluding the skipped list and metrics
# (both sized separately above).
_EXECUTION_GEOMETRY_BYTES_PER_VARIANT_FIXED = 256
# Worst-case string-byte estimate for the rest of one symbol entry:
# symbol, control_reproduction_verified, and control_reproduction_detail
# free text, carried once per symbol (shared across all 4 variants).
_EXECUTION_GEOMETRY_BYTES_PER_SYMBOL_FIXED = 512
# Worst-case string-byte estimate for report-level provenance carried once:
# schema_version, captured_at_utc, account logins, inventory hashes, the
# variants list, and the summary_by_variant aggregation (4 variants x a
# handful of scalars plus up to 5 top5 entries each).
_EXECUTION_GEOMETRY_FIXED_OVERHEAD_BYTES = 65_536

_EXECUTION_GEOMETRY_BYTES_PER_VARIANT = (
    _EXECUTION_GEOMETRY_BYTES_PER_VARIANT_FIXED
    + _EXECUTION_GEOMETRY_BYTES_PER_VARIANT_METRICS
    + _EXECUTION_GEOMETRY_MAX_STABILITY_WINDOWS * _EXECUTION_GEOMETRY_BYTES_PER_STABILITY_WINDOW
    + _EXECUTION_GEOMETRY_MAX_SKIPPED_PER_VARIANT * _EXECUTION_GEOMETRY_BYTES_PER_SKIPPED_ROW
)
_EXECUTION_GEOMETRY_MAX_STRING_BYTES = (
    EXECUTION_GEOMETRY_MAX_SYMBOLS
    * (
        _EXECUTION_GEOMETRY_BYTES_PER_SYMBOL_FIXED
        + EXECUTION_GEOMETRY_VARIANTS_PER_SYMBOL * _EXECUTION_GEOMETRY_BYTES_PER_VARIANT
    )
    + _EXECUTION_GEOMETRY_FIXED_OVERHEAD_BYTES
)
# Canonical JSON serialization adds quoting/escaping and punctuation on top
# of the raw aggregate string content; double it for headroom, matching
# HISTORICAL_INVENTORY_JSON_BUDGET's own convention.
_EXECUTION_GEOMETRY_MAX_CANONICAL_BYTES = _EXECUTION_GEOMETRY_MAX_STRING_BYTES * 2

# Node-count estimate: one skipped-row is ~5 nodes (its object plus 4 leaf
# fields); one stability-window summary is ~7 nodes (its object plus 6 leaf
# fields); one variant's metrics dict is ~20 nodes (19 scalars plus the
# wrapping object), its chronological_stability sub-object contributes ~4
# nodes of its own plus the windows list container plus each window's
# nodes, and its comparative dict is ~7 nodes; the variant entry itself
# adds ~7 nodes (its object, the skipped-list container, and 5 fixed
# fields). One symbol entry adds ~5 nodes of its own (its object, the
# variants-dict container, and 3 fixed fields) on top of its 4 variants.
_EXECUTION_GEOMETRY_NODES_PER_VARIANT = (
    7
    + 20
    + 4
    + 1
    + _EXECUTION_GEOMETRY_MAX_STABILITY_WINDOWS * 7
    + 7
    + 1
    + _EXECUTION_GEOMETRY_MAX_SKIPPED_PER_VARIANT * 5
)
_EXECUTION_GEOMETRY_MAX_NODES = (
    EXECUTION_GEOMETRY_MAX_SYMBOLS
    * (5 + EXECUTION_GEOMETRY_VARIANTS_PER_SYMBOL * _EXECUTION_GEOMETRY_NODES_PER_VARIANT)
    + 8_192  # fixed report-level allowance: top-level scalars + summary_by_variant
)

EXECUTION_GEOMETRY_REPORT_JSON_BUDGET = JsonSafetyBudget(
    max_nodes=_EXECUTION_GEOMETRY_MAX_NODES,
    max_total_string_bytes=_EXECUTION_GEOMETRY_MAX_STRING_BYTES,
    max_canonical_bytes=_EXECUTION_GEOMETRY_MAX_CANONICAL_BYTES,
)
# --- end execution-geometry-experiment-specific bounded JSON capacity ----


def _sha256_hex(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def build_symbol_checkpoint_identity(
    *,
    experiment_schema_version: str,
    symbol: str,
    dataset_sha256: str,
    replay_sha256: object,
    candidates_sha256: object,
    outcomes_sha256: object,
    shadow_max_bars: int,
    shadow_cost_r: float,
    stability_window_count: int,
    variants: Sequence[str],
) -> dict[str, object]:
    """Build the canonical identity binding for one symbol's checkpoint.

    Every input that can change the semantics of that symbol's result is
    included: the experiment schema/version, the symbol itself, the
    already-verified historical-dataset identity (``dataset_sha256``), the
    already-verified replay/candidate/outcome evidence identity
    (``replay_sha256``, ``candidates_sha256``, ``outcomes_sha256`` --
    ``replay_sha256`` alone already transitively binds dataset identity,
    the shadow parameters, and the research policy, but the individual
    hashes are included explicitly too so this identity remains legible and
    resilient to any future change in what ``replay_sha256`` itself binds),
    the shadow evaluator parameters (``shadow_max_bars``,
    ``shadow_cost_r``), the report-level ``stability_window_count`` (NOT
    part of the replay manifest's own identity, since it is a
    report-building parameter, not a replay-generation one -- so it must be
    bound here explicitly), and the ordered variant-definition list.

    A checkpoint whose stored identity does not compare EQUAL to the
    identity computed for the current run is never reused.
    """
    return {
        "checkpoint_schema_version": CHECKPOINT_SCHEMA_VERSION,
        "experiment_schema_version": experiment_schema_version,
        "symbol": symbol,
        "dataset_sha256": dataset_sha256,
        "replay_sha256": replay_sha256,
        "candidates_sha256": candidates_sha256,
        "outcomes_sha256": outcomes_sha256,
        "shadow_max_bars": shadow_max_bars,
        "shadow_cost_r": shadow_cost_r,
        "stability_window_count": stability_window_count,
        "variants": list(variants),
    }


def _identity_sha256(identity: Mapping[str, object]) -> str:
    return _sha256_hex(
        _IDENTITY_HASH_DOMAIN + b"\x00" + canonical_json_bytes(dict(identity), budget=EXECUTION_GEOMETRY_REPORT_JSON_BUDGET)
    )


def checkpoint_path_for(checkpoint_dir: Path, symbol: str) -> Path:
    """The dedicated, experiment-owned checkpoint file path for one symbol.
    Never a path inside the authoritative historical-dataset or replay
    directories -- callers are expected to pass a checkpoint_dir distinct
    from both (e.g. ``data/ser8_execution_geometry_experiment/
    checkpoints``)."""
    safe_symbol = symbol.strip()
    if not safe_symbol or any(char in safe_symbol for char in "/\\"):
        raise ValueError(f"invalid symbol for checkpoint path: {symbol!r}")
    return checkpoint_dir / f"{safe_symbol}.json"


def _write_synced(path: Path, content: bytes) -> None:
    with path.open("wb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def write_symbol_checkpoint(
    path: Path,
    *,
    identity: Mapping[str, object],
    symbol_report: Mapping[str, object],
) -> Path:
    """Atomically persist one symbol's fully-resolved result (temp file +
    fsync + replace, the same pattern
    trademind.ser8_execution_geometry_experiment.
    write_multisymbol_geometry_experiment_report already uses). The
    checkpoint content is self-hashed so any later tampering or corruption
    is detected on load, never silently trusted."""
    identity_payload = dict(identity)
    payload: dict[str, object] = {
        "checkpoint_schema_version": CHECKPOINT_SCHEMA_VERSION,
        "identity": identity_payload,
        "identity_sha256": _identity_sha256(identity_payload),
        "symbol_report": dict(symbol_report),
    }
    payload["checkpoint_sha256"] = _sha256_hex(
        _CHECKPOINT_HASH_DOMAIN
        + b"\x00"
        + canonical_json_bytes(payload, budget=EXECUTION_GEOMETRY_REPORT_JSON_BUDGET)
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    _write_synced(
        temporary,
        canonical_json_bytes(payload, budget=EXECUTION_GEOMETRY_REPORT_JSON_BUDGET) + b"\n",
    )
    temporary.replace(path)
    return path


def load_verified_symbol_checkpoint(
    path: Path,
    *,
    expected_identity: Mapping[str, object],
) -> dict[str, object] | None:
    """Load and verify one per-symbol checkpoint.

    Returns the checkpointed ``symbol_report`` dict ONLY if the file
    exists, parses as JSON, is a canonical object under
    ``EXECUTION_GEOMETRY_REPORT_JSON_BUDGET``, its self-referential content
    hash verifies, its schema version matches, and its stored identity
    compares EQUAL to ``expected_identity``.

    Any other condition -- missing file, unreadable file, corrupt/invalid
    JSON, a payload that is not canonical or exceeds the budget, a content
    hash mismatch, a schema-version mismatch, or an identity mismatch --
    returns ``None``, meaning "recompute this symbol". This function NEVER
    raises for those expected conditions and NEVER partially trusts a
    checkpoint: reuse happens only when identity/hash verifies exactly.
    """
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(parsed, dict):
        return None

    supplied_hash = parsed.get("checkpoint_sha256")
    semantic = dict(parsed)
    semantic.pop("checkpoint_sha256", None)
    try:
        expected_hash = _sha256_hex(
            _CHECKPOINT_HASH_DOMAIN
            + b"\x00"
            + canonical_json_bytes(semantic, budget=EXECUTION_GEOMETRY_REPORT_JSON_BUDGET)
        )
    except ProvenanceError:
        return None
    if not isinstance(supplied_hash, str) or supplied_hash != expected_hash:
        return None

    if parsed.get("checkpoint_schema_version") != CHECKPOINT_SCHEMA_VERSION:
        return None

    stored_identity = parsed.get("identity")
    if not isinstance(stored_identity, dict) or stored_identity != dict(expected_identity):
        return None
    try:
        if parsed.get("identity_sha256") != _identity_sha256(stored_identity):
            return None
    except ProvenanceError:
        return None

    symbol_report = parsed.get("symbol_report")
    if not isinstance(symbol_report, dict):
        return None
    return symbol_report


__all__ = [
    "CHECKPOINT_SCHEMA_VERSION",
    "EXECUTION_GEOMETRY_MAX_SYMBOLS",
    "EXECUTION_GEOMETRY_VARIANTS_PER_SYMBOL",
    "EXECUTION_GEOMETRY_TYPICAL_MAX_CANDIDATES_PER_SYMBOL",
    "EXECUTION_GEOMETRY_REPORT_JSON_BUDGET",
    "build_symbol_checkpoint_identity",
    "checkpoint_path_for",
    "write_symbol_checkpoint",
    "load_verified_symbol_checkpoint",
]
