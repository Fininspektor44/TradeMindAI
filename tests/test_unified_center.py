from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from trademind.unified_center import (
    build_states,
    build_unified_rows,
    fx_components,
    fx_scenarios,
    ote_components,
    ote_scenarios,
    quality_score,
)


def _fx_row() -> dict[str, str]:
    return {
        "observation_id": "EURUSD:M5:1",
        "signal_time": "2026-07-31T10:00:00+00:00",
        "symbol": "EURUSD",
        "timeframe": "M5",
        "session": "LONDON",
        "action": "BUY",
        "score": "62",
        "entry_price": "1.10",
        "internal_break": "BULLISH_BOS",
        "swing_break": "NONE",
        "ssl_sweep": "1",
        "bsl_sweep": "0",
        "fvg_direction": "BULLISH",
        "labels": (
            "ALL_SIGNALS|ANY_SMC_EVENT|HIGH_RVOL|TICK_ACCELERATION|"
            "QUOTE_PRESSURE_ALIGNED|STRUCTURE_ALIGNED|LOW_SPREAD"
        ),
        "outcome_3": "WIN",
        "progress_atr_3": "0.4",
        "mfe_atr_3": "0.8",
        "mae_atr_3": "0.2",
        "outcome_6": "LOSS",
        "progress_atr_6": "-0.3",
        "mfe_atr_6": "0.2",
        "mae_atr_6": "0.5",
        "outcome_12": "WIN",
        "progress_atr_12": "0.7",
        "mfe_atr_12": "1.1",
        "mae_atr_12": "0.2",
        "signal_reasons": "trend and structure",
    }


def _ote_row() -> dict[str, str]:
    return {
        "signal_id": "XAUUSD:M5:2:TOUCH_705",
        "signal_time": "2026-07-31T11:00:00+00:00",
        "symbol": "XAUUSD",
        "timeframe": "M5",
        "session": "LONDON_NY_OVERLAP",
        "action": "BUY",
        "variant": "TOUCH_705",
        "score": "68",
        "entry_price": "4000",
        "stop_price": "3985",
        "target_price": "4040",
        "rr": "2.6667",
        "setup_break": "BULLISH_BOS",
        "liquidity_sweep": "1",
        "fvg_aligned": "1",
        "confirmation": "1",
        "h1_aligned": "1",
        "h4_aligned": "1",
        "h1_bias": "BULLISH",
        "h4_bias": "BULLISH",
        "rvol_20": "1.5",
        "tick_rate_ratio_20": "1.3",
        "direction_imbalance": "0.12",
        "spread_ratio_20": "0.9",
        "outcome_h3": "WIN",
        "result_r_h3": "2.0",
        "mfe_r_h3": "2.4",
        "mae_r_h3": "0.4",
        "outcome_h6": "WIN",
        "result_r_h6": "2.0",
        "mfe_r_h6": "2.5",
        "mae_r_h6": "0.4",
        "outcome_h12": "LOSS",
        "result_r_h12": "-1.0",
        "mfe_r_h12": "0.6",
        "mae_r_h12": "1.0",
        "reasons": "OTE touch and confirmation",
    }


def test_fx_scenarios_include_standalone_and_combinations() -> None:
    row = _fx_row()
    components = fx_components(row)
    names = {scenario.name for scenario in fx_scenarios(row)}
    assert {"INTERNAL_BOS", "LIQUIDITY_SWEEP", "FVG"} <= components
    assert {
        "BASE_SIGNAL",
        "INTERNAL_BOS",
        "LIQUIDITY_SWEEP",
        "FVG",
        "BOS_PLUS_SWEEP",
        "SWEEP_PLUS_FVG",
        "SMC_MULTI_FACTOR",
    } <= names


def test_ote_scenarios_keep_fib_variant_and_all_confluence() -> None:
    row = _ote_row()
    components = ote_components(row)
    names = {scenario.name for scenario in ote_scenarios(row)}
    assert {"OTE", "BOS", "LIQUIDITY_SWEEP", "FVG", "CONFIRMATION"} <= components
    assert {
        "OTE_ALL",
        "OTE_705",
        "BOS_PLUS_OTE",
        "SWEEP_PLUS_OTE",
        "FVG_PLUS_OTE",
        "CONFIRMED_OTE",
        "MULTI_FACTOR_OTE",
    } <= names


def test_horizon_semantics_and_metric_units_are_not_mixed() -> None:
    rows = build_unified_rows([_fx_row()], [_ote_row()])
    fx = [row for row in rows if row["source"] == "FX_RESEARCH"]
    ote = [row for row in rows if row["source"] == "SMC_OTE"]
    assert {row["horizon"] for row in fx} == {"M15", "M30", "H1"}
    assert {row["horizon_minutes"] for row in fx} == {"15", "30", "60"}
    assert {row["metric_unit"] for row in fx} == {"ATR"}
    assert {row["horizon"] for row in ote} == {"H3", "H6", "H12"}
    assert {row["horizon_minutes"] for row in ote} == {"180", "360", "720"}
    assert {row["metric_unit"] for row in ote} == {"R"}
    assert len({row["event_id"] for row in rows}) == len(rows)


def test_states_keep_source_and_unit_separate() -> None:
    rows = build_unified_rows([_fx_row()], [_ote_row()])
    states = build_states(rows, datetime(2026, 8, 1, tzinfo=timezone.utc))
    combinations = {(row["source"], row["metric_unit"]) for row in states}
    assert ("FX_RESEARCH", "ATR") in combinations
    assert ("SMC_OTE", "R") in combinations
    assert all(row["status"] == "INSUFFICIENT_SAMPLE" for row in states)


def test_quality_score_rewards_confluence_and_penalizes_bad_cost() -> None:
    positive = quality_score(50, {"LIQUIDITY_SWEEP", "FVG", "LOW_SPREAD"})
    negative = quality_score(50, {"HIGH_SPREAD", "SPREAD_EXPANDING"})
    assert positive > 50
    assert negative < 50
    assert 0 <= positive <= 100
    assert 0 <= negative <= 100


def test_unified_center_is_read_only() -> None:
    root = Path(__file__).resolve().parents[1]
    paths = [
        root / "src" / "trademind" / "unified_center.py",
        root / "scripts" / "run_v160_unified_center.ps1",
        root / "scripts" / "install_v160_unified_center_task.ps1",
    ]
    forbidden = (
        "CTrade",
        "OrderSend",
        "PositionClose",
        "TRADE_ACTION_DEAL",
        ".Buy(",
        ".Sell(",
    )
    for path in paths:
        text = path.read_text(encoding="utf-8")
        for token in forbidden:
            assert token not in text
