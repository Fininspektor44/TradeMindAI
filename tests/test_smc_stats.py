from __future__ import annotations

from trademind.smc_stats import (
    _context_groups,
    _event_labels,
    _normalized_metrics,
    _sample_status,
    _structure_relation,
)


def test_event_labels_detect_structure_sweeps_and_fvg() -> None:
    row = {
        "internal_break": "BEARISH_BOS",
        "swing_break": "BULLISH_CHOCH",
        "bsl_sweep": "1",
        "ssl_sweep": "0",
        "fvg_direction": "BULLISH",
    }

    assert _event_labels(row) == {
        "ANY_SMC_EVENT",
        "INTERNAL_BOS",
        "SWING_CHOCH",
        "BSL_SWEEP",
        "BULLISH_FVG",
    }


def test_structure_relation_detects_alignment_and_conflict() -> None:
    assert (
        _structure_relation({"internal_bias": "BULLISH", "swing_bias": "BULLISH"})
        == "STRUCTURE_ALIGNED"
    )
    assert (
        _structure_relation({"internal_bias": "BEARISH", "swing_bias": "BULLISH"})
        == "STRUCTURE_CONFLICT"
    )
    assert _structure_relation({"internal_bias": "NEUTRAL", "swing_bias": "BULLISH"}) is None


def test_context_groups_split_volume_spread_and_structure() -> None:
    rows = [
        {
            "volume_ratio_20": "1.5",
            "spread_cost_atr": "0.04",
            "internal_bias": "BULLISH",
            "swing_bias": "BULLISH",
        },
        {
            "volume_ratio_20": "0.9",
            "spread_cost_atr": "0.20",
            "internal_bias": "BEARISH",
            "swing_bias": "BULLISH",
        },
    ]

    groups = _context_groups(
        rows,
        volume_threshold=1.2,
        spread_atr_threshold=0.10,
    )

    assert groups["HIGH_VOLUME"] == [rows[0]]
    assert groups["NORMAL_VOLUME"] == [rows[1]]
    assert groups["LOW_SPREAD"] == [rows[0]]
    assert groups["HIGH_SPREAD"] == [rows[1]]
    assert groups["STRUCTURE_ALIGNED"] == [rows[0]]
    assert groups["STRUCTURE_CONFLICT"] == [rows[1]]


def test_sample_status_guards_small_samples() -> None:
    assert _sample_status(299, 300) == "INSUFFICIENT_SAMPLE"
    assert _sample_status(300, 300) == "RESEARCH_SAMPLE"


def test_normalized_metrics_do_not_mix_raw_price_scales() -> None:
    rows = [
        {
            "action": "BUY",
            "outcome_3": "WIN",
            "net_move_3": "10",
            "atr": "10",
            "progress_atr_3": "1.0",
        },
        {
            "action": "SELL",
            "outcome_3": "LOSS",
            "net_move_3": "-1000",
            "atr": "1000",
            "progress_atr_3": "-1.0",
        },
    ]

    metrics = _normalized_metrics(rows, horizon=3)

    assert metrics["win_rate"] == 50.0
    assert metrics["profit_factor_atr"] == 1.0
    assert metrics["avg_net_atr"] == 0.0
