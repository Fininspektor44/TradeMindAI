from __future__ import annotations

from trademind.fx_signal_adapter import build_candidate, build_candidates
from trademind.signal_evidence import similarity_key


def _row() -> dict[str, str]:
    return {
        "observation_id": "FX-001",
        "signal_time": "2026-08-05T13:30:00+00:00",
        "symbol": "EURUSD",
        "timeframe": "M5",
        "session": "LONDON_NY_OVERLAP",
        "signal_source": "trademind.ote_engine.build_ote_signals",
        "ote_signal_id": "EURUSD:M5:1:BUY:TOUCH_705:2",
        "action": "BUY",
        "variant": "TOUCH_705",
        "fib_ratio": "705",
        "score": "84",
        "entry_price": "1.1000",
        "bar_high": "1.1010",
        "bar_low": "1.0988",
        "atr": "0.0010",
        "anchor_price": "1.0940",
        "impulse_extreme": "1.1080",
        "impulse_atr": "2.1",
        "stop_price": "1.0938",
        "target_price": "1.1080",
        "h1_bias": "BULLISH",
        "h4_bias": "BULLISH",
        "setup_break": "BULLISH_BOS",
        "liquidity_sweep": "1",
        "fvg_aligned": "1",
        "signal_reasons": "bullish structure and volume impulse",
        "internal_bias": "BULLISH",
        "internal_reference_high": "1.1040",
        "internal_reference_low": "1.0960",
        "internal_break": "BULLISH",
        "swing_bias": "BULLISH",
        "swing_reference_high": "1.1080",
        "swing_reference_low": "1.0940",
        "swing_break": "BULLISH",
        "liquidity_reference_high": "1.1090",
        "liquidity_reference_low": "1.0950",
        "bsl_sweep": "0",
        "ssl_sweep": "1",
        "bsl_sweep_depth_atr": "0",
        "ssl_sweep_depth_atr": "0.35",
        "fvg_direction": "BULLISH",
        "fvg_size_atr": "0.42",
        "structure_event_count": "4",
        "bar_tick_volume": "1300",
        "rvol_20": "1.55",
        "volume_percentile_100": "87",
        "tick_rate_ratio_20": "1.35",
        "direction_imbalance": "0.18",
        "delta_proxy": "0.22",
        "spread_mean_points": "8",
        "spread_max_points": "11",
        "spread_ratio_20": "1.05",
        "spread_expansion_points": "0",
        "spread_cost": "0.00008",
        "spread_cost_atr": "0.08",
        "body_efficiency_ratio_20": "1.20",
        "range_efficiency_ratio_20": "1.10",
        "point": "0.00001",
        "labels": "HIGH_RVOL|SSL_SWEEP|BULLISH_FVG",
    }


def test_adapter_builds_explainable_shadow_candidate_from_market_features() -> None:
    candidate = build_candidate(_row())

    assert candidate.symbol == "EURUSD"
    assert candidate.plan.action == "BUY"
    assert len(candidate.plan.entries) == 3
    assert candidate.plan.entries[0].order_type == "MARKET"
    assert "SMC/OTE" in candidate.plan.entries[0].rationale
    assert candidate.plan.stop_price < min(item.price for item in candidate.plan.entries)
    assert candidate.plan.first_target_rr >= 1.5
    assert candidate.generated_from_market_data is True
    assert candidate.robot_context_only == {}
    assert candidate.market_features["liquidity"]["ssl_sweep"] is True
    assert candidate.market_features["volume"]["rvol_20"] == 1.55
    assert candidate.factor_scores["structure"] > 0.4
    assert candidate.factor_scores["liquidity"] > 0.8
    assert "version=SIM_V1" in similarity_key(candidate)


def test_adapter_rejects_non_directional_observation() -> None:
    row = _row()
    row["action"] = "WAIT"

    candidates, errors = build_candidates([row])

    assert candidates == []
    assert len(errors) == 1
    assert "not BUY/SELL" in errors[0]["reason"]


def test_adapter_rejects_row_without_authoritative_ote_identity() -> None:
    row = _row()
    row.pop("signal_source")

    candidates, errors = build_candidates([row])

    assert candidates == []
    assert "authoritative OTE" in errors[0]["reason"]


def test_adapter_is_deterministic_for_same_pre_move_observation() -> None:
    first = build_candidate(_row())
    second = build_candidate(dict(reversed(list(_row().items()))))

    assert first.signal_id == second.signal_id
    assert first.plan.as_dict() == second.plan.as_dict()
