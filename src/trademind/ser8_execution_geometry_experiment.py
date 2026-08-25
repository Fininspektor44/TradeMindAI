"""SER8 Execution Geometry A/B Experiment V1 -- RESEARCH/SCREENING ONLY.

Tests whether the existing MARKET+LIMIT+LIMIT basket execution geometry is
the structural cause of the negative expectancy observed across all 28
HISTORICAL_DATA_READY FX symbols, by re-evaluating the SAME already-
published replay candidates against the SAME already-published historical
bars under four execution-geometry variants:

  CONTROL_BASKET            -- existing plan, unchanged. Must exactly
                                reproduce the authoritative, already-
                                published replay outcomes; if it cannot,
                                that symbol fails closed and NO variant is
                                interpreted for it.
  MARKET_ONLY_SAME_TARGET   -- one MARKET entry (allocation 1.0), no LIMIT
                                entries, same stop, same primary target.
  MARKET_ONLY_1_5R          -- one MARKET entry, same stop, target
                                recomputed at 1.5R from the market entry.
  MARKET_ONLY_2_0R          -- one MARKET entry, same stop, target
                                recomputed at 2.0R from the market entry.

This module NEVER regenerates signals -- trademind.signals.SignalEngine and
trademind.fx_signal_adapter.build_candidate are never called here; every
candidate is loaded verbatim from the already-published, hash-verified
candidates.jsonl. It NEVER re-runs historical acquisition, NEVER calls MT5
or a broker, NEVER reads or writes the protected hypothesis/holdout
lifecycle, and NEVER mutates the existing replay artifacts on disk (they
are only read, hash-verified, and reused). The only evaluator used is the
existing, unmodified trademind.signal_shadow.evaluate_shadow_candidate --
called once per (candidate, variant) with a variant-specific
trademind.signal_intelligence.TradePlan; the evaluator's own semantics
(MARKET filled immediately, LIMIT filled when touched, newly-filled entries
added before stop/target evaluation, same-bar stop+target resolves
conservatively to STOP, net_r via the existing cost model) are never
modified. Metrics reuse trademind.ser8_historical_multisymbol_screening.
compute_symbol_replay_metrics verbatim -- nothing here reinvents win-rate /
profit-factor / expectancy / drawdown / chronological-stability logic.
"""

from __future__ import annotations

import bisect
import dataclasses
import hashlib
import json
import os
import statistics
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Sequence

from trademind.ser8_execution_geometry_checkpoint import (
    EXECUTION_GEOMETRY_REPORT_JSON_BUDGET,
    build_symbol_checkpoint_identity,
    checkpoint_path_for,
    load_verified_symbol_checkpoint,
    write_symbol_checkpoint,
)
from trademind.ser8_historical_data import (
    HistoricalBarV1,
    HistoricalDataError,
    load_canonical_bars,
    verify_dataset,
)
from trademind.ser8_historical_multisymbol_screening import (
    compute_symbol_replay_metrics,
    load_verified_replay_rows,
)
from trademind.signal_intelligence import EntryOrder, SignalCandidate, TradePlan
from trademind.signal_shadow import Bar, evaluate_shadow_candidate, load_candidates
from trademind.signal_statistics_provenance import canonical_json_bytes

EXPERIMENT_SCHEMA_VERSION = "ser8-execution-geometry-experiment-v1"
_EXPERIMENT_HASH_DOMAIN = b"trademind:ser8:execution-geometry-experiment:v1"
DEFAULT_STABILITY_WINDOW_COUNT = 3

VARIANT_CONTROL = "CONTROL_BASKET"
VARIANT_MARKET_SAME_TARGET = "MARKET_ONLY_SAME_TARGET"
VARIANT_MARKET_1_5R = "MARKET_ONLY_1_5R"
VARIANT_MARKET_2_0R = "MARKET_ONLY_2_0R"
ALL_VARIANTS = (
    VARIANT_CONTROL,
    VARIANT_MARKET_SAME_TARGET,
    VARIANT_MARKET_1_5R,
    VARIANT_MARKET_2_0R,
)
_RR_MULTIPLE_BY_VARIANT = {
    VARIANT_MARKET_1_5R: 1.5,
    VARIANT_MARKET_2_0R: 2.0,
}

STATUS_CONTROL_REPRODUCTION_FAILED = "CONTROL_REPRODUCTION_FAILED"
STATUS_EVIDENCE_UNAVAILABLE = "EVIDENCE_UNAVAILABLE"

_PUBLISHED_OUTCOME_COMPARISON_FIELDS = (
    "schema_version",
    "signal_id",
    "setup_key",
    "completed_at",
    "outcome",
    "net_r",
    "exit_reason",
    "exit_price",
    "filled_entries",
    "allocation_filled",
    "average_entry",
    "mfe_r",
    "mae_r",
    "bars_observed",
)


def _extract_market_entry(plan: TradePlan) -> EntryOrder:
    market_entries = [item for item in plan.entries if item.order_type == "MARKET"]
    if len(market_entries) != 1:
        raise HistoricalDataError(
            "EXPERIMENT_MARKET_ENTRY_MISSING",
            f"expected exactly one MARKET entry in the control plan, found {len(market_entries)}",
        )
    return market_entries[0]


def _recomputed_target(*, action: str, entry_price: float, stop_price: float, rr_multiple: float) -> float:
    risk = abs(entry_price - stop_price)
    return entry_price + rr_multiple * risk if action == "BUY" else entry_price - rr_multiple * risk


def variant_trade_plan(control_plan: TradePlan, variant: str) -> TradePlan:
    """Build one execution-geometry variant's TradePlan from the control
    plan. Fails closed (raises HistoricalDataError) rather than silently
    adjusting an invalid geometry -- e.g. if a variant's target would not
    satisfy TradePlan's own BUY/SELL target-vs-entry validation, this
    raises instead of fabricating a workaround."""
    if variant == VARIANT_CONTROL:
        return control_plan
    if variant not in ALL_VARIANTS:
        raise ValueError(f"unknown variant: {variant!r}")
    market_entry = _extract_market_entry(control_plan)
    if variant == VARIANT_MARKET_SAME_TARGET:
        target = control_plan.targets[0]
    else:
        target = _recomputed_target(
            action=control_plan.action,
            entry_price=market_entry.price,
            stop_price=control_plan.stop_price,
            rr_multiple=_RR_MULTIPLE_BY_VARIANT[variant],
        )
    try:
        return TradePlan(
            action=control_plan.action,
            entries=(
                EntryOrder(
                    price=market_entry.price,
                    allocation=1.0,
                    rationale=f"{variant} execution-geometry experiment (SCREENING ONLY)",
                    order_type="MARKET",
                ),
            ),
            stop_price=control_plan.stop_price,
            targets=(target,),
            invalidation=control_plan.invalidation,
            target_rationale=(f"{variant} experimental target",),
        )
    except ValueError as exc:
        raise HistoricalDataError("EXPERIMENT_INVALID_VARIANT_GEOMETRY", str(exc)) from exc


def _variant_candidate(control: SignalCandidate, variant: str) -> SignalCandidate:
    if variant == VARIANT_CONTROL:
        return control
    return dataclasses.replace(control, plan=variant_trade_plan(control.plan, variant))


def _shadow_bars(bars: Sequence[HistoricalBarV1]) -> list[Bar]:
    return [
        Bar(
            time=bar.time_utc,
            symbol=bar.symbol,
            timeframe=bar.timeframe,
            open=bar.open,
            high=bar.high,
            low=bar.low,
            close=bar.close,
        )
        for bar in bars
    ]


def evaluate_variant_for_symbol(
    *,
    candidates: Sequence[SignalCandidate],
    bars: Sequence[HistoricalBarV1],
    variant: str,
    max_bars: int,
    cost_r: float,
) -> tuple[list[dict[str, object]], list[dict[str, object]], list[dict[str, object]]]:
    """Evaluate every candidate under one execution-geometry variant, using
    the SAME already-published candidate population, the SAME already-
    published bars, the SAME future-bar-window slicing (the identical
    bisect logic trademind.ser8_historical_replay.build_replay_payloads
    uses), the SAME max_bars/cost_r, and the existing, unmodified
    trademind.signal_shadow.evaluate_shadow_candidate.

    Returns (candidate_rows, outcome_rows, skipped_rows) shaped for
    trademind.ser8_historical_multisymbol_screening.compute_symbol_replay_
    metrics. Outcome rows are re-keyed to each candidate's ORIGINAL
    (control) signal_id -- the identical population is tracked across all
    four variants even though a MARKET-ONLY variant's own recomputed
    signal_id differs (its plan differs). A candidate whose variant
    geometry is structurally invalid is never silently dropped or
    miscounted: it is recorded in skipped_rows with an explicit reason.
    """
    if variant not in ALL_VARIANTS:
        raise ValueError(f"unknown variant: {variant!r}")
    shadow_bars = _shadow_bars(bars)
    bar_times = [item.time for item in shadow_bars]
    candidate_rows: list[dict[str, object]] = []
    outcome_rows: list[dict[str, object]] = []
    skipped_rows: list[dict[str, object]] = []
    for control in candidates:
        original_signal_id = control.signal_id
        try:
            variant_candidate = _variant_candidate(control, variant)
        except HistoricalDataError as exc:
            skipped_rows.append(
                {
                    "signal_id": original_signal_id,
                    "variant": variant,
                    "reason_code": exc.code,
                    "reason": str(exc),
                }
            )
            continue
        candidate_rows.append(
            {"signal_id": original_signal_id, "plan": {"action": variant_candidate.plan.action}}
        )
        first_future = bisect.bisect_right(bar_times, control.observed_at.astimezone(timezone.utc))
        future_window = shadow_bars[first_future : first_future + max_bars]
        outcome = evaluate_shadow_candidate(
            variant_candidate,
            future_window,
            max_bars=max_bars,
            target_index=0,
            cost_r=cost_r,
        )
        if outcome is None:
            continue
        row = outcome.as_dict()
        row["signal_id"] = original_signal_id
        outcome_rows.append(row)
    return candidate_rows, outcome_rows, skipped_rows


def verify_control_reproduces_published_outcomes(
    *,
    control_outcome_rows: Sequence[Mapping[str, object]],
    published_outcome_rows: Sequence[Mapping[str, object]],
) -> tuple[bool, str]:
    """Prove CONTROL_BASKET's freshly re-evaluated outcomes are identical,
    field for field, to the already-published, hash-verified replay
    outcomes for the same candidates. This is the experiment's mandatory
    validity gate: if it fails, no execution-geometry variant may be
    interpreted for that symbol -- the counterfactual comparison would not
    be trustworthy."""
    fresh_by_id = {str(row["signal_id"]): row for row in control_outcome_rows}
    published_by_id = {str(row["signal_id"]): row for row in published_outcome_rows}
    if set(fresh_by_id) != set(published_by_id):
        missing = sorted(set(published_by_id) - set(fresh_by_id))
        extra = sorted(set(fresh_by_id) - set(published_by_id))
        return False, f"completed-outcome signal_id sets differ: missing={missing[:5]} extra={extra[:5]}"
    for signal_id, published in published_by_id.items():
        fresh = fresh_by_id[signal_id]
        for field in _PUBLISHED_OUTCOME_COMPARISON_FIELDS:
            if fresh.get(field) != published.get(field):
                return False, (
                    f"signal_id {signal_id!r} field {field!r} diverges: "
                    f"control={fresh.get(field)!r} published={published.get(field)!r}"
                )
    return True, "CONTROL_BASKET exactly reproduces the published replay outcomes"


def _comparative_metrics(control: Mapping[str, object], variant: Mapping[str, object]) -> dict[str, object]:
    def _delta(key: str) -> float | None:
        control_value, variant_value = control.get(key), variant.get(key)
        if control_value is None or variant_value is None:
            return None
        return variant_value - control_value

    control_expectancy = control.get("expectancy_r")
    variant_expectancy = variant.get("expectancy_r")
    flips_to_positive = bool(
        control_expectancy is not None
        and variant_expectancy is not None
        and control_expectancy <= 0.0
        and variant_expectancy > 0.0
    )
    return {
        "expectancy_delta_r": _delta("expectancy_r"),
        "profit_factor_delta": _delta("profit_factor"),
        "payoff_delta": _delta("payoff_ratio"),
        "drawdown_delta_r": _delta("max_drawdown_r"),
        "win_rate_delta": _delta("win_rate"),
        "changes_negative_expectancy_to_positive": flips_to_positive,
    }


def build_symbol_geometry_experiment(
    *,
    symbol: str,
    candidates: Sequence[SignalCandidate],
    published_outcome_rows: Sequence[Mapping[str, object]],
    bars: Sequence[HistoricalBarV1],
    max_bars: int,
    cost_r: float,
    stability_window_count: int = DEFAULT_STABILITY_WINDOW_COUNT,
) -> dict[str, object]:
    """Evaluate all four execution-geometry variants for one symbol,
    gated on CONTROL_BASKET exactly reproducing the already-published
    replay outcomes. If CONTROL cannot reproduce them, every other variant
    is reported as CONTROL_REPRODUCTION_FAILED with metrics=None -- never
    interpreted, never fabricated."""
    control_candidate_rows, control_outcome_rows, control_skipped = evaluate_variant_for_symbol(
        candidates=candidates, bars=bars, variant=VARIANT_CONTROL, max_bars=max_bars, cost_r=cost_r
    )
    reproduced, detail = verify_control_reproduces_published_outcomes(
        control_outcome_rows=control_outcome_rows, published_outcome_rows=published_outcome_rows
    )
    control_metrics = compute_symbol_replay_metrics(
        candidates=control_candidate_rows,
        outcomes=control_outcome_rows,
        cost_r=cost_r,
        stability_window_count=stability_window_count,
    )
    variants: dict[str, object] = {
        VARIANT_CONTROL: {
            "symbol": symbol,
            "variant": VARIANT_CONTROL,
            "candidate_count": len(candidates),
            "evaluated_candidate_count": len(control_candidate_rows),
            "skipped_count": len(control_skipped),
            "skipped": control_skipped,
            "metrics": control_metrics,
            "comparative": None,
        }
    }
    if not reproduced:
        for variant in ALL_VARIANTS[1:]:
            variants[variant] = {
                "symbol": symbol,
                "variant": variant,
                "candidate_count": len(candidates),
                "status": STATUS_CONTROL_REPRODUCTION_FAILED,
                "metrics": None,
                "comparative": None,
            }
        return {
            "symbol": symbol,
            "control_reproduction_verified": False,
            "control_reproduction_detail": detail,
            "variants": variants,
        }

    for variant in ALL_VARIANTS[1:]:
        variant_candidate_rows, variant_outcome_rows, variant_skipped = evaluate_variant_for_symbol(
            candidates=candidates, bars=bars, variant=variant, max_bars=max_bars, cost_r=cost_r
        )
        variant_metrics = compute_symbol_replay_metrics(
            candidates=variant_candidate_rows,
            outcomes=variant_outcome_rows,
            cost_r=cost_r,
            stability_window_count=stability_window_count,
        )
        variants[variant] = {
            "symbol": symbol,
            "variant": variant,
            "candidate_count": len(candidates),
            "evaluated_candidate_count": len(variant_candidate_rows),
            "skipped_count": len(variant_skipped),
            "skipped": variant_skipped,
            "metrics": variant_metrics,
            "comparative": _comparative_metrics(control_metrics, variant_metrics),
        }
    return {
        "symbol": symbol,
        "control_reproduction_verified": True,
        "control_reproduction_detail": detail,
        "variants": variants,
    }


def _build_variant_summary(symbol_reports: Sequence[Mapping[str, object]]) -> dict[str, object]:
    summary: dict[str, object] = {}
    for variant in ALL_VARIANTS:
        expectancies: list[tuple[str, float]] = []
        profit_factors: list[float] = []
        payoffs: list[float] = []
        improved: list[str] = []
        worsened: list[str] = []
        expectancy_improvements: list[tuple[str, float]] = []
        positive = 0
        negative = 0
        for item in symbol_reports:
            entry = item.get("variants", {}).get(variant)  # type: ignore[union-attr]
            if not entry or entry.get("metrics") is None:
                continue
            metrics = entry["metrics"]
            symbol = str(item["symbol"])
            expectancy = metrics.get("expectancy_r")
            if expectancy is not None:
                expectancies.append((symbol, expectancy))
                if expectancy > 0.0:
                    positive += 1
                else:
                    negative += 1
            if metrics.get("profit_factor") is not None:
                profit_factors.append(metrics["profit_factor"])
            if metrics.get("payoff_ratio") is not None:
                payoffs.append(metrics["payoff_ratio"])
            comparative = entry.get("comparative")
            if comparative is not None:
                delta = comparative.get("expectancy_delta_r")
                if delta is not None:
                    expectancy_improvements.append((symbol, delta))
                    if delta > 0.0:
                        improved.append(symbol)
                    elif delta < 0.0:
                        worsened.append(symbol)
        top5_expectancy = sorted(expectancies, key=lambda item: (-item[1], item[0]))[:5]
        top5_improvement = sorted(expectancy_improvements, key=lambda item: (-item[1], item[0]))[:5]
        summary[variant] = {
            "positive_expectancy_symbol_count": positive,
            "negative_expectancy_symbol_count": negative,
            "median_expectancy_r": statistics.median(v for _, v in expectancies) if expectancies else None,
            "mean_expectancy_r": statistics.fmean(v for _, v in expectancies) if expectancies else None,
            "median_profit_factor": statistics.median(profit_factors) if profit_factors else None,
            "mean_profit_factor": statistics.fmean(profit_factors) if profit_factors else None,
            "median_payoff_ratio": statistics.median(payoffs) if payoffs else None,
            "symbols_improved_vs_control": sorted(improved),
            "symbols_worsened_vs_control": sorted(worsened),
            "top5_by_expectancy": [{"symbol": s, "expectancy_r": v} for s, v in top5_expectancy],
            "top5_by_expectancy_improvement": [
                {"symbol": s, "expectancy_delta_r": v} for s, v in top5_improvement
            ],
        }
    return summary


def build_multisymbol_geometry_experiment_report(
    *,
    historical_inventory: Mapping[str, object],
    readiness_payload: Mapping[str, object],
    stability_window_count: int = DEFAULT_STABILITY_WINDOW_COUNT,
    captured_at: datetime,
    checkpoint_dir: Path | None = None,
    resume: bool = True,
    resume_report: dict[str, list[str]] | None = None,
) -> dict[str, object]:
    """Build the full, deterministic, hash-verified four-variant execution-
    geometry experiment report across every HISTORICAL_DATA_READY symbol.
    Symbol selection is read verbatim from the historical inventory's own
    authoritative status field -- never re-derived -- exactly matching
    trademind.ser8_historical_multisymbol_screening.
    build_multisymbol_screening_report's convention.

    ``checkpoint_dir`` (optional): when supplied, one already-fully-
    evaluated symbol (four variants evaluated AND its CONTROL reproduction
    gate resolved) is atomically persisted per symbol under this
    experiment-owned directory (never the authoritative historical-dataset
    or replay directories -- see
    trademind.ser8_execution_geometry_checkpoint). When omitted (the
    default), behavior is IDENTICAL to before checkpointing existed: no
    checkpoint is read or written, and every symbol is computed exactly as
    build_symbol_geometry_experiment always has.

    ``resume`` (default True): when a checkpoint_dir is supplied, an
    existing checkpoint for a symbol is reused ONLY when its identity/hash
    verifies EXACTLY against this run's own inputs for that symbol
    (dataset, replay/candidate/outcome evidence, shadow parameters,
    stability_window_count, and variant definitions -- see
    build_symbol_checkpoint_identity). Any missing, corrupt, tampered,
    stale, or identity-mismatched checkpoint is never accepted and that
    symbol is simply recomputed and (re)checkpointed. Passing
    ``resume=False`` forces recomputation of every symbol while still
    writing fresh checkpoints for the next run.

    ``resume_report`` (optional): when supplied, this caller-owned dict is
    populated in place with ``"resumed"`` and ``"recomputed"`` symbol lists
    for CLI/output visibility. It is NEVER read from, never influences the
    computation, and is NOT part of the returned report payload or its
    hash -- a fresh run and a resumed run over identical evidence produce
    byte-identical report content and the identical
    ``experiment_report_sha256``, regardless of what is recorded here.
    """
    if stability_window_count < 1:
        raise ValueError("stability_window_count must be a positive integer")
    if resume_report is not None:
        resume_report.setdefault("resumed", [])
        resume_report.setdefault("recomputed", [])
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
            "EXPERIMENT_READINESS_ENTRY_MISSING",
            f"readiness inventory is missing HISTORICAL_DATA_READY symbols: {missing}",
        )

    symbol_reports: list[dict[str, object]] = []
    for symbol in ready_symbols:
        readiness_entry = readiness_by_symbol[symbol]
        replay_dir = readiness_entry.get("replay_dir")
        dataset_dir = readiness_entry.get("dataset_dir")
        if (
            not isinstance(replay_dir, str)
            or not replay_dir
            or not isinstance(dataset_dir, str)
            or not dataset_dir
        ):
            symbol_reports.append(
                {
                    "symbol": symbol,
                    "control_reproduction_verified": False,
                    "control_reproduction_detail": "no replay/dataset artifact available for this symbol",
                    "variants": {
                        variant: {
                            "symbol": symbol,
                            "variant": variant,
                            "status": STATUS_EVIDENCE_UNAVAILABLE,
                            "metrics": None,
                            "comparative": None,
                        }
                        for variant in ALL_VARIANTS
                    },
                }
            )
            continue
        _candidates_raw, published_outcome_rows, replay_manifest = load_verified_replay_rows(Path(replay_dir))
        candidates = load_candidates(Path(replay_dir) / "candidates.jsonl")
        dataset_manifest = verify_dataset(Path(dataset_dir))
        bars = load_canonical_bars(Path(dataset_dir) / "bars.csv")
        max_bars = int(replay_manifest["shadow_max_bars"])
        cost_r = float(replay_manifest["shadow_cost_r"])

        symbol_report: dict[str, object] | None = None
        checkpoint_identity: dict[str, object] | None = None
        checkpoint_file: Path | None = None
        if checkpoint_dir is not None:
            checkpoint_identity = build_symbol_checkpoint_identity(
                experiment_schema_version=EXPERIMENT_SCHEMA_VERSION,
                symbol=symbol,
                dataset_sha256=str(dataset_manifest.get("dataset_sha256")),
                replay_sha256=replay_manifest.get("replay_sha256"),
                candidates_sha256=replay_manifest.get("candidates_sha256"),
                outcomes_sha256=replay_manifest.get("outcomes_sha256"),
                shadow_max_bars=max_bars,
                shadow_cost_r=cost_r,
                stability_window_count=stability_window_count,
                variants=ALL_VARIANTS,
            )
            checkpoint_file = checkpoint_path_for(checkpoint_dir, symbol)
            if resume:
                symbol_report = load_verified_symbol_checkpoint(
                    checkpoint_file, expected_identity=checkpoint_identity
                )
                if symbol_report is not None and resume_report is not None:
                    resume_report["resumed"].append(symbol)

        if symbol_report is None:
            symbol_report = build_symbol_geometry_experiment(
                symbol=symbol,
                candidates=candidates,
                published_outcome_rows=published_outcome_rows,
                bars=bars,
                max_bars=max_bars,
                cost_r=cost_r,
                stability_window_count=stability_window_count,
            )
            if checkpoint_dir is not None and checkpoint_file is not None and checkpoint_identity is not None:
                write_symbol_checkpoint(
                    checkpoint_file, identity=checkpoint_identity, symbol_report=symbol_report
                )
            if resume_report is not None:
                resume_report["recomputed"].append(symbol)

        symbol_reports.append(symbol_report)

    experiment_valid = bool(symbol_reports) and all(
        item["control_reproduction_verified"] for item in symbol_reports
    )
    payload: dict[str, object] = {
        "schema_version": EXPERIMENT_SCHEMA_VERSION,
        "captured_at_utc": captured_at.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "execution_account_login": historical_inventory.get("execution_account_login"),
        "market_data_account_login": historical_inventory.get("market_data_account_login"),
        "historical_inventory_sha256": historical_inventory.get("inventory_sha256"),
        "readiness_inventory_sha256": readiness_payload.get("readiness_inventory_sha256"),
        "stability_window_count": stability_window_count,
        "ready_symbol_count": len(ready_symbols),
        "experiment_valid": experiment_valid,
        "variants": list(ALL_VARIANTS),
        "symbols": symbol_reports,
        "summary_by_variant": _build_variant_summary(symbol_reports),
        "screening_authority": "SCREENING_ONLY_NOT_ACCEPTANCE",
        "execution_authority_granted": False,
        "hypotheses_created": 0,
        "hypotheses_accepted": 0,
        "protected_holdout_accessed": False,
    }
    # Report canonical serialization + hash creation: this artifact-specific,
    # named, finite EXECUTION_GEOMETRY_REPORT_JSON_BUDGET is used
    # consistently for hash creation here, hash verification in
    # verify_multisymbol_geometry_experiment_report, writing in
    # write_multisymbol_geometry_experiment_report, and loading in
    # load_verified_multisymbol_geometry_experiment_report -- never the bare
    # DEFAULT_JSON_SAFETY_BUDGET, which every other canonical_json_bytes
    # caller in the codebase keeps unchanged. See
    # trademind.ser8_execution_geometry_checkpoint for the sizing
    # derivation. The experiment's semantic hash remains fully
    # deterministic: only the budget's capacity ceiling changed, not the
    # payload content or the hashing formula.
    payload["experiment_report_sha256"] = _sha256_hex(
        _EXPERIMENT_HASH_DOMAIN
        + b"\x00"
        + canonical_json_bytes(payload, budget=EXECUTION_GEOMETRY_REPORT_JSON_BUDGET)
    )
    return payload


def _sha256_hex(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def verify_multisymbol_geometry_experiment_report(payload: Mapping[str, object]) -> None:
    semantic = dict(payload)
    supplied = semantic.pop("experiment_report_sha256", None)
    expected = _sha256_hex(
        _EXPERIMENT_HASH_DOMAIN
        + b"\x00"
        + canonical_json_bytes(semantic, budget=EXECUTION_GEOMETRY_REPORT_JSON_BUDGET)
    )
    if not isinstance(supplied, str) or supplied != expected:
        raise HistoricalDataError("EXPERIMENT_REPORT_HASH_MISMATCH", "experiment report hash mismatch")
    if payload.get("schema_version") != EXPERIMENT_SCHEMA_VERSION:
        raise HistoricalDataError(
            "EXPERIMENT_REPORT_SCHEMA_INVALID", "unsupported experiment report schema"
        )


def _write_synced(path: Path, content: bytes) -> None:
    with path.open("wb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def write_multisymbol_geometry_experiment_report(path: Path, payload: Mapping[str, object]) -> Path:
    verify_multisymbol_geometry_experiment_report(payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    _write_synced(
        temporary,
        canonical_json_bytes(dict(payload), budget=EXECUTION_GEOMETRY_REPORT_JSON_BUDGET) + b"\n",
    )
    temporary.replace(path)
    return path


def load_verified_multisymbol_geometry_experiment_report(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise HistoricalDataError(
            "EXPERIMENT_REPORT_ROOT_INVALID", "experiment report root must be an object"
        )
    verify_multisymbol_geometry_experiment_report(payload)
    return payload


def compact_report_lines(payload: Mapping[str, object], *, experiment_report_path: str) -> list[str]:
    """The exact plain-text === TRADEMIND REPORT === console/clipboard
    contract for the Windows one-line invocation."""
    summary = payload.get("summary_by_variant", {})  # type: ignore[union-attr]
    lines = [
        "=== TRADEMIND REPORT ===",
        "STEP: SER8 EXECUTION GEOMETRY A/B EXPERIMENT",
        f"STATUS: {'PASS' if payload.get('experiment_valid') else 'FAIL'}",
        f"EXECUTION_ACCOUNT: {payload.get('execution_account_login')}",
        f"MARKET_DATA_ACCOUNT: {payload.get('market_data_account_login')}",
        f"READY_SYMBOLS: {payload.get('ready_symbol_count')}",
        f"EXPERIMENT_VALID: {payload.get('experiment_valid')}",
    ]
    for variant in payload.get("variants", []):  # type: ignore[union-attr]
        row = summary.get(variant, {})
        lines.append(
            f"{variant}: POS={row.get('positive_expectancy_symbol_count')} "
            f"NEG={row.get('negative_expectancy_symbol_count')} "
            f"MEDIAN_EXP={row.get('median_expectancy_r')} "
            f"MEDIAN_PF={row.get('median_profit_factor')} "
            f"IMPROVED={len(row.get('symbols_improved_vs_control', []))} "
            f"WORSENED={len(row.get('symbols_worsened_vs_control', []))}"
        )
    lines.append(f"EXPERIMENT_REPORT_SHA256: {payload.get('experiment_report_sha256')}")
    lines.append(f"EXPERIMENT_REPORT_PATH: {experiment_report_path}")
    lines.append("=== END REPORT ===")
    return lines


__all__ = [
    "EXPERIMENT_SCHEMA_VERSION",
    "DEFAULT_STABILITY_WINDOW_COUNT",
    "EXECUTION_GEOMETRY_REPORT_JSON_BUDGET",
    "VARIANT_CONTROL",
    "VARIANT_MARKET_SAME_TARGET",
    "VARIANT_MARKET_1_5R",
    "VARIANT_MARKET_2_0R",
    "ALL_VARIANTS",
    "STATUS_CONTROL_REPRODUCTION_FAILED",
    "STATUS_EVIDENCE_UNAVAILABLE",
    "variant_trade_plan",
    "evaluate_variant_for_symbol",
    "verify_control_reproduces_published_outcomes",
    "build_symbol_geometry_experiment",
    "build_multisymbol_geometry_experiment_report",
    "verify_multisymbol_geometry_experiment_report",
    "write_multisymbol_geometry_experiment_report",
    "load_verified_multisymbol_geometry_experiment_report",
    "compact_report_lines",
]
