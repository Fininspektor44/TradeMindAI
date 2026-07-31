from __future__ import annotations

from datetime import datetime, timedelta, timezone

from trademind.action_validation import (
    ActionPatternValidation,
    ActionValidationResult,
    SegmentMetrics,
    apply_benjamini_hochberg,
    feature_labels,
    validate_action_rows,
)


def _rows(values: list[float], action: str, horizon: int = 3) -> list[dict[str, str]]:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows: list[dict[str, str]] = []
    for index, value in enumerate(values):
        rows.append(
            {
                "signal_time": (start + timedelta(minutes=15 * index)).isoformat(),
                "symbol": "XAUUSD",
                "timeframe": "M5",
                "action": action,
                f"outcome_{horizon}": "WIN" if value > 0 else "LOSS" if value < 0 else "FLAT",
                f"progress_atr_{horizon}": str(value),
                "atr": "1.0",
            }
        )
    return rows


def test_validation_separates_buy_and_sell() -> None:
    rows = _rows([0.5, -0.1] * 20, "BUY") + _rows([-0.4, 0.1] * 20, "SELL")

    buy = validate_action_rows(
        rows,
        3,
        "BUY",
        candidate_minimum=30,
        research_minimum=300,
        minimum_trading_days=1,
    )
    sell = validate_action_rows(
        rows,
        3,
        "SELL",
        candidate_minimum=30,
        research_minimum=300,
        minimum_trading_days=1,
    )

    assert buy.status == "RESEARCH_CANDIDATE"
    assert buy.total.avg_net_atr > 0
    assert sell.status == "UNSTABLE"
    assert sell.total.avg_net_atr < 0


def test_directional_structure_labels_are_not_merged() -> None:
    bullish = {
        "internal_break": "BULLISH_BOS",
        "swing_break": "BEARISH_CHOCH",
        "fvg_direction": "BULLISH",
        "bsl_sweep": "0",
        "ssl_sweep": "1",
        "volume_ratio_20": "1.4",
        "spread_cost_atr": "0.05",
        "internal_bias": "BULLISH",
        "swing_bias": "BEARISH",
    }

    labels = feature_labels(bullish)

    assert "INTERNAL_BOS" in labels
    assert "BULLISH_INTERNAL_BOS" in labels
    assert "BEARISH_INTERNAL_BOS" not in labels
    assert "SWING_CHOCH" in labels
    assert "BEARISH_SWING_CHOCH" in labels
    assert "BULLISH_FVG" in labels
    assert "SSL_SWEEP" in labels
    assert "HIGH_VOLUME" in labels
    assert "LOW_SPREAD" in labels
    assert "STRUCTURE_CONFLICT" in labels


def _validation(p_value: float, status: str = "VALIDATED") -> ActionPatternValidation:
    segment = SegmentMetrics(300, 60.0, 1.5, 0.2)
    result = ActionValidationResult(
        status=status,
        total=segment,
        early=segment,
        late=segment,
        trading_days=30,
        late_to_early_ratio=1.0,
        max_drawdown_atr=5.0,
        max_loss_streak=3,
        mean_ci_low=0.01,
        mean_ci_high=0.39,
        p_value=p_value,
        q_value=1.0,
        reasons=(),
    )
    return ActionPatternValidation("XAUUSD", f"P{p_value}", "BUY", 3, 320, result)


def test_bh_correction_blocks_false_validation() -> None:
    strong = _validation(0.001)
    weak = _validation(0.20)

    adjusted = apply_benjamini_hochberg([strong, weak], fdr_alpha=0.10)

    assert adjusted[0].result.q_value <= 0.10
    assert adjusted[0].result.status == "VALIDATED"
    assert adjusted[1].result.q_value > 0.10
    assert adjusted[1].result.status == "RESEARCH_CANDIDATE"
