"""Deterministic, read-only cross-symbol SCREENING report for SER8.

SCREENING ONLY. Applying the historical candidate population and conservative
shadow evaluator to a broader FX universe
is generalization evidence, not acceptance. This module never creates,
matches, or accepts a hypothesis; never reads or consumes a protected final
holdout; and never grants any symbol execution authority. The already
accepted EURUSD hypothesis
(``rpi-v1:sha256:205b5260711f7578a59cef2feea59550b777b3df0956ffd192076b37c4e5866d:0``)
is never touched, widened, or recreated by anything here.

Every metric in this report is derived exclusively from the ALREADY-computed,
content-addressed, atomically-published ``candidates.jsonl``/``outcomes.jsonl``
replay artifacts that ``trademind.ser8_historical_replay.create_replay``
produces. This module never touches raw bars, never regenerates signals or
the shadow evaluator, and never changes dataset or replay identity — it is a
pure, additive AGGREGATION and RANKING layer over already-verified evidence.

Ranking here (``SCREENING_RANKING_POLICY_VERSION``) is an explicit,
deterministic, fixed-weight, never-tuned-to-outcomes ordinal composite. It
is a research-priority screening aid only, not a hypothesis-acceptance
verdict: every symbol appears in the report regardless of its result, and a
high rank never implies ACCEPTED status or execution authority.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Sequence

from trademind.ser8_historical_data import HistoricalDataError
from trademind.ser8_historical_replay import verify_replay
from trademind.signal_statistics_provenance import canonical_json_bytes

SCREENING_SCHEMA_VERSION = "ser8-historical-multisymbol-screening-v1"
SCREENING_RANKING_POLICY_VERSION = "ser8-screening-ranking-v1"
_SCREENING_HASH_DOMAIN = b"trademind:ser8:historical-multisymbol-screening:v1"
DEFAULT_STABILITY_WINDOW_COUNT = 3

STATUS_SCREENED = "SCREENED"
STATUS_INSUFFICIENT_SAMPLE = "INSUFFICIENT_SAMPLE"
STATUS_REPLAY_UNAVAILABLE = "REPLAY_UNAVAILABLE"
STATUS_REPLAY_VERIFICATION_FAILED = "REPLAY_VERIFICATION_FAILED"
STATUS_NO_COMPLETED_OUTCOMES = "NO_COMPLETED_OUTCOMES"

TIER_SCREENED_POSITIVE = "SCREENED_POSITIVE_EXPECTANCY"
TIER_SCREENED_NEGATIVE = "SCREENED_NEGATIVE_EXPECTANCY"

# A profit factor with zero losses is mathematically infinite, but the
# report is persisted as canonical JSON (which rejects non-finite floats --
# `Infinity` is not valid JSON). Following the existing project convention
# for this exact situation (trademind.bybit_risk_plan_experiments._profit_factor),
# an all-winners symbol is recorded with this large finite sentinel instead.
PROFIT_FACTOR_NO_LOSSES_SENTINEL = 999.0


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for line in path.read_bytes().splitlines():
        if line:
            rows.append(json.loads(line))
    return rows


def load_verified_replay_rows(
    replay_dir: Path,
) -> tuple[list[dict[str, object]], list[dict[str, object]], dict[str, object]]:
    """Re-verify (never mutate) and load one symbol's already-published
    replay artifacts. Fails closed via ``verify_replay`` if tampered."""
    manifest = verify_replay(replay_dir)
    candidates = _read_jsonl(replay_dir / "candidates.jsonl")
    outcomes = _read_jsonl(replay_dir / "outcomes.jsonl")
    return candidates, outcomes, manifest


def _window_summary(rows: Sequence[Mapping[str, object]]) -> dict[str, object]:
    net_values = [float(row["net_r"]) for row in rows]
    wins = sum(1 for row in rows if row["outcome"] == "WIN")
    losses = sum(1 for row in rows if row["outcome"] == "LOSS")
    decided = wins + losses
    total = len(rows)
    return {
        "trade_count": total,
        "win_rate": (wins / decided * 100.0) if decided else None,
        "expectancy_r": (sum(net_values) / total) if total else None,
        "net_r_total": sum(net_values),
        "first_completed_at": str(rows[0]["completed_at"]) if rows else None,
        "last_completed_at": str(rows[-1]["completed_at"]) if rows else None,
    }


def _split_windows(
    ordered: Sequence[Mapping[str, object]], window_count: int
) -> list[list[Mapping[str, object]]]:
    total = len(ordered)
    if window_count < 1 or total == 0:
        return []
    base, remainder = divmod(total, window_count)
    windows: list[list[Mapping[str, object]]] = []
    start = 0
    for index in range(window_count):
        size = base + (1 if index < remainder else 0)
        if size <= 0:
            continue
        windows.append(list(ordered[start : start + size]))
        start += size
    return windows


def _chronological_stability(
    ordered: Sequence[Mapping[str, object]], *, window_count: int
) -> dict[str, object]:
    """Descriptive, non-authoritative diagnostic split of an already-computed
    outcome sequence into consecutive chronological windows. This is NOT the
    protected discovery/validation/holdout split engine (``trademind.
    discovery.split_engine``) and never creates or consumes any protected
    holdout; it only reports whether expectancy degrades later in time."""
    windows = _split_windows(ordered, window_count)
    summaries = [_window_summary(window) for window in windows]
    expectancies = [item["expectancy_r"] for item in summaries if item["expectancy_r"] is not None]
    degradation_r = (expectancies[-1] - expectancies[0]) if len(expectancies) >= 2 else None
    return {
        "window_count": len(windows),
        "windows": summaries,
        "first_to_last_expectancy_delta_r": degradation_r,
        "degraded": bool(degradation_r is not None and degradation_r < 0.0),
    }


_VALID_CANDIDATE_DIRECTIONS = frozenset({"BUY", "SELL"})


def _direction_field(value: object) -> str | None:
    """Normalize one embedded direction-like field for comparison; ``None``
    when the field is absent or not a recognizable direction string."""
    if not isinstance(value, str):
        return None
    normalized = value.strip().upper()
    return normalized if normalized in _VALID_CANDIDATE_DIRECTIONS else None


def _resolve_candidate_direction(candidate_row: Mapping[str, object]) -> str:
    """Resolve one candidate's authoritative BUY/SELL direction.

    ``plan.action`` (``trademind.signal_intelligence.TradePlan.action``) is
    the sole structurally-validated direction field -- ``TradePlan.__post_init__``
    rejects any value outside ``{BUY, SELL}`` at candidate-construction time --
    so it is the authoritative source: it is the action attached to the
    actual replay execution plan. There is no top-level ``candidate["action"]``
    field; reading one (as this module previously did) silently resolves to
    an empty string for every real candidate, which is the exact defect this
    resolver fixes.

    ``market_features.confirmation.action`` and ``similarity_dimensions.action``
    are, by construction (``trademind.fx_signal_adapter`` derives all three
    from one shared local ``action`` value within a single call), byte-identical
    echoes of ``plan.action`` in every candidate the existing engine produces.
    They are consulted here only as a defensive consistency check against a
    corrupted or hand-crafted artifact -- never as an independent source of
    truth, and never preferred over ``plan.action``.

    Fails closed (raises ``HistoricalDataError``) rather than silently
    defaulting to an empty string or miscounting the trade: when
    ``plan.action`` is missing or is not exactly ``BUY``/``SELL``, and when
    any present embedded field contradicts ``plan.action``.
    """
    plan = candidate_row.get("plan")
    plan_action = _direction_field(plan.get("action")) if isinstance(plan, Mapping) else None
    if plan_action is None:
        raise HistoricalDataError(
            "CANDIDATE_DIRECTION_UNRESOLVED",
            f"candidate {candidate_row.get('signal_id')!r} has no valid plan.action",
        )

    market_features = candidate_row.get("market_features")
    confirmation = market_features.get("confirmation") if isinstance(market_features, Mapping) else None
    embedded_fields = {
        "market_features.confirmation.action": (
            _direction_field(confirmation.get("action")) if isinstance(confirmation, Mapping) else None
        ),
        "similarity_dimensions.action": _direction_field(
            (candidate_row.get("similarity_dimensions") or {}).get("action")
            if isinstance(candidate_row.get("similarity_dimensions"), Mapping)
            else None
        ),
    }
    for path, value in embedded_fields.items():
        if value is not None and value != plan_action:
            raise HistoricalDataError(
                "CANDIDATE_DIRECTION_CONTRADICTION",
                f"candidate {candidate_row.get('signal_id')!r}: plan.action="
                f"{plan_action!r} contradicts {path}={value!r}",
            )
    return plan_action


def _build_action_by_signal(candidates: Sequence[Mapping[str, object]]) -> dict[str, str]:
    return {str(row.get("signal_id")): _resolve_candidate_direction(row) for row in candidates}


def compute_symbol_replay_metrics(
    *,
    candidates: Sequence[Mapping[str, object]],
    outcomes: Sequence[Mapping[str, object]],
    cost_r: float,
    stability_window_count: int = DEFAULT_STABILITY_WINDOW_COUNT,
) -> dict[str, object]:
    """Pure aggregation over already-computed replay rows.

    Deterministic and input-order independent: outcomes are re-sorted by
    ``(completed_at, signal_id)`` before any statistic is derived. ``net_r``
    is SER8's existing, authoritative, ALREADY cost-adjusted per-trade return
    metric (``trademind.signal_shadow._net_r``, subtracting ``cost_r``); this
    function only aggregates it and reconstructs the pre-cost value for
    comparison, it never recomputes or overrides it.

    Long/short direction is resolved per candidate via
    ``_resolve_candidate_direction`` (authoritative ``plan.action``, fails
    closed on a missing/invalid/contradictory direction) -- never a
    nonexistent top-level ``candidate["action"]``.
    """
    action_by_signal = _build_action_by_signal(candidates)
    ordered = sorted(outcomes, key=lambda row: (str(row["completed_at"]), str(row["signal_id"])))

    long_count = sum(1 for row in ordered if action_by_signal.get(str(row["signal_id"])) == "BUY")
    short_count = sum(1 for row in ordered if action_by_signal.get(str(row["signal_id"])) == "SELL")

    wins = sum(1 for row in ordered if row["outcome"] == "WIN")
    losses = sum(1 for row in ordered if row["outcome"] == "LOSS")
    flats = sum(1 for row in ordered if row["outcome"] == "FLAT")
    decided = wins + losses
    trade_count = len(ordered)

    net_values = [float(row["net_r"]) for row in ordered]
    winners = [value for value in net_values if value > 0.0]
    losers = [value for value in net_values if value < 0.0]
    gross_profit_r = sum(winners)
    gross_loss_r = -sum(losers)  # positive magnitude
    net_r_total = sum(net_values)

    profit_factor: float | None
    if gross_loss_r > 0.0:
        profit_factor = gross_profit_r / gross_loss_r
    elif gross_profit_r > 0.0:
        profit_factor = PROFIT_FACTOR_NO_LOSSES_SENTINEL
    else:
        profit_factor = None

    expectancy_r = (net_r_total / trade_count) if trade_count else None
    average_winner_r = (sum(winners) / len(winners)) if winners else None
    average_loser_r = (sum(losers) / len(losers)) if losers else None
    payoff_ratio = None
    if average_winner_r is not None and average_loser_r not in (None, 0.0):
        payoff_ratio = average_winner_r / abs(average_loser_r)

    cumulative = 0.0
    peak = 0.0
    max_drawdown_r = 0.0
    for value in net_values:
        cumulative += value
        peak = max(peak, cumulative)
        max_drawdown_r = max(max_drawdown_r, peak - cumulative)

    # Reconstruct the pre-cost (gross-of-cost) per-trade R to reveal a symbol
    # that only looks profitable before SER8's existing cost model is
    # applied. signal_shadow._net_r computes:
    #   gross * allocation - max(0, cost_r) * allocation
    # so gross * allocation == net_r + max(0, cost_r) * allocation_filled.
    before_cost_values = [
        float(row["net_r"]) + max(0.0, cost_r) * float(row.get("allocation_filled") or 1.0)
        for row in ordered
    ]
    net_r_total_before_cost = sum(before_cost_values)
    expectancy_r_before_cost = (net_r_total_before_cost / trade_count) if trade_count else None

    return {
        "trade_count": trade_count,
        "long_count": long_count,
        "short_count": short_count,
        "wins": wins,
        "losses": losses,
        "flats": flats,
        "win_rate": (wins / decided * 100.0) if decided else None,
        "gross_profit_r": gross_profit_r,
        "gross_loss_r": gross_loss_r,
        "net_r_total": net_r_total,
        "profit_factor": profit_factor,
        "expectancy_r": expectancy_r,
        "average_winner_r": average_winner_r,
        "average_loser_r": average_loser_r,
        "payoff_ratio": payoff_ratio,
        "max_drawdown_r": max_drawdown_r,
        "cost_r_per_trade_applied": max(0.0, cost_r),
        "net_r_total_before_cost": net_r_total_before_cost,
        "expectancy_r_before_cost": expectancy_r_before_cost,
        "profitable_only_before_costs": bool(
            expectancy_r_before_cost is not None
            and expectancy_r is not None
            and expectancy_r_before_cost > 0.0
            and expectancy_r <= 0.0
        ),
        "chronological_stability": _chronological_stability(
            ordered, window_count=stability_window_count
        ),
    }


def build_symbol_screening_entry(
    *,
    readiness_entry: Mapping[str, object],
    stability_window_count: int = DEFAULT_STABILITY_WINDOW_COUNT,
) -> dict[str, object]:
    """Build one symbol's full screening row from its readiness-inventory
    entry. Always returns a row -- including a rejection reason -- even when
    replay could not be validly completed; never silently omits a symbol."""
    symbol = str(readiness_entry["symbol"])
    base: dict[str, object] = {
        "symbol": symbol,
        "asset_class": readiness_entry.get("asset_class"),
        "broker_trade_mode": readiness_entry.get("broker_trade_mode"),
        "risk_model_supported": readiness_entry.get("risk_model_supported"),
        "usable_bar_count": readiness_entry.get("historical_rows"),
        "dataset_sha256": readiness_entry.get("dataset_sha256"),
        "dataset_dir": readiness_entry.get("dataset_dir"),
        "replay_sha256": readiness_entry.get("replay_sha256"),
        "signal_count": readiness_entry.get("candidate_count", 0),
        "executed_trade_count": readiness_entry.get("completed_outcome_count", 0),
        "research_minimum": readiness_entry.get("research_minimum"),
        "research_ready": readiness_entry.get("research_ready"),
        "readiness_reason": readiness_entry.get("readiness_reason"),
    }

    replay_dir = readiness_entry.get("replay_dir")
    if not isinstance(replay_dir, str) or not replay_dir:
        return {
            **base,
            "screening_status": STATUS_REPLAY_UNAVAILABLE,
            "rejection_reason": str(
                readiness_entry.get("readiness_reason") or "no replay artifact for this symbol"
            ),
            "metrics": None,
        }
    try:
        candidates, outcomes, replay_manifest = load_verified_replay_rows(Path(replay_dir))
    except (HistoricalDataError, OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        return {
            **base,
            "screening_status": STATUS_REPLAY_VERIFICATION_FAILED,
            "rejection_reason": str(exc),
            "metrics": None,
        }
    if not outcomes:
        return {
            **base,
            "screening_status": STATUS_NO_COMPLETED_OUTCOMES,
            "rejection_reason": "replay produced zero completed outcomes",
            "metrics": None,
        }

    cost_r = float(replay_manifest.get("shadow_cost_r", 0.0))
    try:
        metrics = compute_symbol_replay_metrics(
            candidates=candidates,
            outcomes=outcomes,
            cost_r=cost_r,
            stability_window_count=stability_window_count,
        )
    except HistoricalDataError as exc:
        # A candidate's direction could not be resolved or contradicts
        # itself (see _resolve_candidate_direction) -- fail closed rather
        # than silently reporting wrong/zeroed BUY/SELL counts.
        return {
            **base,
            "screening_status": STATUS_REPLAY_VERIFICATION_FAILED,
            "rejection_reason": str(exc),
            "metrics": None,
        }
    research_ready = readiness_entry.get("research_ready") is True
    return {
        **base,
        "screening_status": STATUS_SCREENED if research_ready else STATUS_INSUFFICIENT_SAMPLE,
        "rejection_reason": None if research_ready else str(readiness_entry.get("readiness_reason") or ""),
        "metrics": metrics,
    }


def _profit_factor_rank_key(entry: Mapping[str, object]) -> float:
    profit_factor = entry["metrics"]["profit_factor"]  # type: ignore[index]
    if profit_factor is None:
        return -1.0
    return float(profit_factor)


def _rank_position(ordered: Sequence[Mapping[str, object]], symbol: str) -> int:
    for index, item in enumerate(ordered, start=1):
        if item["symbol"] == symbol:
            return index
    return len(ordered) + 1


def rank_symbol_screening_entries(
    entries: Sequence[Mapping[str, object]],
) -> tuple[list[dict[str, object]], list[str]]:
    """Deterministic, versioned, SCREENING-ONLY cross-symbol ranking.

    Fixed, equal, never-tuned-to-outcomes weighting across four
    independently-ranked dimensions: expectancy, profit factor, drawdown,
    and chronological stability (smallest first-to-last expectancy
    degradation). ``composite_rank_score`` is the mean of the four ordinal
    rank positions (lower is better, like golf scoring) -- never a weighted
    sum of raw magnitudes, so it cannot be dominated by one unit-scale.
    Symbols with non-positive expectancy are always ranked strictly after
    every positive-expectancy symbol, but are NEVER dropped from the
    result. A symbol lacking a sufficient, valid sample is excluded from
    ranking (never fabricated) and returned separately, unranked.
    """
    # Sorted first by symbol so that any tie on the ranked dimension itself
    # breaks deterministically on symbol name, never on input list order --
    # ranking must be identical regardless of the order entries arrive in.
    eligible = sorted(
        (entry for entry in entries if entry.get("screening_status") == STATUS_SCREENED and entry.get("metrics")),
        key=lambda e: str(e["symbol"]),
    )

    by_expectancy = sorted(eligible, key=lambda e: (-e["metrics"]["expectancy_r"], e["symbol"]))  # type: ignore[index]
    by_profit_factor = sorted(eligible, key=lambda e: (-_profit_factor_rank_key(e), e["symbol"]))
    by_drawdown = sorted(eligible, key=lambda e: (e["metrics"]["max_drawdown_r"], e["symbol"]))  # type: ignore[index]
    by_stability = sorted(
        eligible,
        key=lambda e: (
            abs(
                e["metrics"]["chronological_stability"]["first_to_last_expectancy_delta_r"] or 0.0  # type: ignore[index]
            ),
            e["symbol"],
        ),
    )

    ranked_rows: list[dict[str, object]] = []
    for entry in eligible:
        symbol = str(entry["symbol"])
        rank_expectancy = _rank_position(by_expectancy, symbol)
        rank_profit_factor = _rank_position(by_profit_factor, symbol)
        rank_drawdown = _rank_position(by_drawdown, symbol)
        rank_stability = _rank_position(by_stability, symbol)
        composite = (rank_expectancy + rank_profit_factor + rank_drawdown + rank_stability) / 4.0
        expectancy_r = entry["metrics"]["expectancy_r"]  # type: ignore[index]
        tier = TIER_SCREENED_POSITIVE if (expectancy_r or 0.0) > 0.0 else TIER_SCREENED_NEGATIVE
        ranked_rows.append(
            {
                "symbol": symbol,
                "rank_expectancy": rank_expectancy,
                "rank_profit_factor": rank_profit_factor,
                "rank_drawdown": rank_drawdown,
                "rank_stability": rank_stability,
                "composite_rank_score": composite,
                "screening_tier": tier,
            }
        )

    ranked_rows.sort(
        key=lambda row: (
            row["screening_tier"] != TIER_SCREENED_POSITIVE,
            row["composite_rank_score"],
            row["symbol"],
        )
    )
    for position, row in enumerate(ranked_rows, start=1):
        row["overall_rank"] = position

    not_ranked_symbols = sorted(
        str(entry["symbol"]) for entry in entries if entry.get("screening_status") != STATUS_SCREENED
    )
    return ranked_rows, not_ranked_symbols


def _sha256_hex(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def build_multisymbol_screening_report(
    *,
    historical_inventory: Mapping[str, object],
    readiness_payload: Mapping[str, object],
    stability_window_count: int = DEFAULT_STABILITY_WINDOW_COUNT,
    captured_at: datetime,
) -> dict[str, object]:
    """Build the full, deterministic, hash-verified cross-symbol report.

    Symbol selection is read verbatim from the historical inventory's OWN
    authoritative ``status`` field (``HISTORICAL_DATA_READY``) -- never
    re-derived -- so this never invents acceptance of an insufficient or
    otherwise-not-ready dataset.
    """
    if stability_window_count < 1:
        raise ValueError("stability_window_count must be a positive integer")
    ready_symbols = sorted(
        {
            str(entry["symbol"])
            for entry in historical_inventory.get("entries", [])
            if isinstance(entry, dict) and entry.get("status") == "HISTORICAL_DATA_READY"
        }
    )
    readiness_by_symbol = {
        str(entry["symbol"]): entry
        for entry in readiness_payload.get("entries", [])
        if isinstance(entry, dict)
    }
    missing = [symbol for symbol in ready_symbols if symbol not in readiness_by_symbol]
    if missing:
        raise HistoricalDataError(
            "SCREENING_READINESS_ENTRY_MISSING",
            f"readiness inventory is missing HISTORICAL_DATA_READY symbols: {missing}",
        )

    entries = [
        build_symbol_screening_entry(
            readiness_entry=readiness_by_symbol[symbol],
            stability_window_count=stability_window_count,
        )
        for symbol in ready_symbols
    ]
    ranked, not_ranked = rank_symbol_screening_entries(entries)
    screened_count = sum(1 for entry in entries if entry["screening_status"] == STATUS_SCREENED)
    positive_count = sum(1 for row in ranked if row["screening_tier"] == TIER_SCREENED_POSITIVE)
    negative_count = sum(1 for row in ranked if row["screening_tier"] == TIER_SCREENED_NEGATIVE)

    payload: dict[str, object] = {
        "schema_version": SCREENING_SCHEMA_VERSION,
        "screening_ranking_policy_version": SCREENING_RANKING_POLICY_VERSION,
        "captured_at_utc": captured_at.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "execution_account_login": historical_inventory.get("execution_account_login"),
        "market_data_account_login": historical_inventory.get("market_data_account_login"),
        "historical_inventory_sha256": historical_inventory.get("inventory_sha256"),
        "readiness_inventory_sha256": readiness_payload.get("readiness_inventory_sha256"),
        "stability_window_count": stability_window_count,
        "ready_symbol_count": len(ready_symbols),
        "screened_count": screened_count,
        "positive_expectancy_count": positive_count,
        "negative_expectancy_count": negative_count,
        "not_ranked_count": len(not_ranked),
        "entries": entries,
        "ranking": {
            "screening_ranking_policy_version": SCREENING_RANKING_POLICY_VERSION,
            "ranked": ranked,
            "not_ranked_symbols": not_ranked,
        },
        "screening_authority": "SCREENING_ONLY_NOT_ACCEPTANCE",
        "execution_authority_granted": False,
        "hypotheses_created": 0,
        "hypotheses_accepted": 0,
        "protected_holdout_accessed": False,
    }
    payload["screening_report_sha256"] = _sha256_hex(
        _SCREENING_HASH_DOMAIN + b"\x00" + canonical_json_bytes(payload)
    )
    return payload


def verify_multisymbol_screening_report(payload: Mapping[str, object]) -> None:
    semantic = dict(payload)
    supplied = semantic.pop("screening_report_sha256", None)
    expected = _sha256_hex(_SCREENING_HASH_DOMAIN + b"\x00" + canonical_json_bytes(semantic))
    if not isinstance(supplied, str) or supplied != expected:
        raise HistoricalDataError(
            "SCREENING_REPORT_HASH_MISMATCH", "screening report hash mismatch"
        )
    if payload.get("schema_version") != SCREENING_SCHEMA_VERSION:
        raise HistoricalDataError(
            "SCREENING_REPORT_SCHEMA_INVALID", "unsupported screening report schema"
        )


def _write_synced(path: Path, content: bytes) -> None:
    with path.open("wb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def write_multisymbol_screening_report(path: Path, payload: Mapping[str, object]) -> Path:
    verify_multisymbol_screening_report(payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    _write_synced(temporary, canonical_json_bytes(dict(payload)) + b"\n")
    temporary.replace(path)
    return path


def load_verified_multisymbol_screening_report(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise HistoricalDataError(
            "SCREENING_REPORT_ROOT_INVALID", "screening report root must be an object"
        )
    verify_multisymbol_screening_report(payload)
    return payload


def compact_report_lines(payload: Mapping[str, object], *, screening_report_path: str) -> list[str]:
    """The exact plain-text ``=== TRADEMIND REPORT ===`` console/clipboard
    contract for the Windows one-line invocation."""
    ranked = payload.get("ranking", {}).get("ranked", [])  # type: ignore[union-attr]
    top5 = ",".join(f"{row['symbol']}#{row['overall_rank']}" for row in ranked[:5])
    bottom5 = ",".join(f"{row['symbol']}#{row['overall_rank']}" for row in ranked[-5:])
    return [
        "=== TRADEMIND REPORT ===",
        "STEP: SER8 28-SYMBOL HISTORICAL SCREENING",
        "STATUS: PASS",
        f"EXECUTION_ACCOUNT: {payload.get('execution_account_login')}",
        f"MARKET_DATA_ACCOUNT: {payload.get('market_data_account_login')}",
        f"READY_SYMBOLS: {payload.get('ready_symbol_count')}",
        f"SCREENED: {payload.get('screened_count')}",
        f"POSITIVE_EXPECTANCY: {payload.get('positive_expectancy_count')}",
        f"NEGATIVE_EXPECTANCY: {payload.get('negative_expectancy_count')}",
        f"NOT_RANKED: {payload.get('not_ranked_count')}",
        f"TOP5_RANKED: {top5}",
        f"BOTTOM5_RANKED: {bottom5}",
        f"SCREENING_REPORT_SHA256: {payload.get('screening_report_sha256')}",
        f"SCREENING_REPORT_PATH: {screening_report_path}",
        "=== END REPORT ===",
    ]


__all__ = [
    "SCREENING_SCHEMA_VERSION",
    "SCREENING_RANKING_POLICY_VERSION",
    "DEFAULT_STABILITY_WINDOW_COUNT",
    "STATUS_SCREENED",
    "STATUS_INSUFFICIENT_SAMPLE",
    "STATUS_REPLAY_UNAVAILABLE",
    "STATUS_REPLAY_VERIFICATION_FAILED",
    "STATUS_NO_COMPLETED_OUTCOMES",
    "PROFIT_FACTOR_NO_LOSSES_SENTINEL",
    "TIER_SCREENED_POSITIVE",
    "TIER_SCREENED_NEGATIVE",
    "load_verified_replay_rows",
    "compute_symbol_replay_metrics",
    "build_symbol_screening_entry",
    "rank_symbol_screening_entries",
    "build_multisymbol_screening_report",
    "verify_multisymbol_screening_report",
    "write_multisymbol_screening_report",
    "load_verified_multisymbol_screening_report",
    "compact_report_lines",
]
