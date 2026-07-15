from __future__ import annotations

from datetime import datetime, timedelta, timezone

from trademind.validation import portfolio_only, validate_rows


def _rows(values: list[float], horizon: int = 3) -> list[dict[str, str]]:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows: list[dict[str, str]] = []
    for index, value in enumerate(values):
        rows.append(
            {
                "signal_time": (start + timedelta(minutes=15 * index)).isoformat(),
                "symbol": "XAUUSD",
                "timeframe": "M5",
                "action": "BUY",
                f"outcome_{horizon}": "WIN" if value > 0 else "LOSS" if value < 0 else "FLAT",
                f"progress_atr_{horizon}": str(value),
                "atr": "1.0",
            }
        )
    return rows


def test_candidate_requires_thirty_non_overlapping_trades() -> None:
    result = validate_rows(_rows([0.2] * 29), 3, candidate_minimum=30, research_minimum=300)

    assert result.status == "INSUFFICIENT_SAMPLE"
    assert result.total.trades == 29


def test_candidate_requires_both_time_halves_to_remain_positive() -> None:
    result = validate_rows(
        _rows([0.5] * 20 + [-0.2] * 20),
        3,
        candidate_minimum=30,
        research_minimum=300,
    )

    assert result.status == "UNSTABLE"
    assert result.early.avg_net_atr > 0
    assert result.late.avg_net_atr < 0
    assert "late half is not positive" in result.reasons


def test_stable_early_pattern_is_research_candidate() -> None:
    result = validate_rows(
        _rows([0.5, -0.1] * 15 + [0.4, -0.1] * 15),
        3,
        candidate_minimum=30,
        research_minimum=300,
    )

    assert result.status == "RESEARCH_CANDIDATE"
    assert result.total.trades == 60
    assert result.early.profit_factor_atr > 1
    assert result.late.profit_factor_atr > 1
    assert result.mean_ci_low > 0


def test_large_stable_pattern_with_positive_interval_is_validated() -> None:
    result = validate_rows(
        _rows([0.4, -0.1] * 150),
        3,
        candidate_minimum=30,
        research_minimum=300,
    )

    assert result.status == "VALIDATED"
    assert result.total.trades == 300
    assert result.mean_ci_low > 0


def test_drawdown_and_loss_streak_are_reported_in_atr() -> None:
    result = validate_rows(
        _rows([1.0, -0.5, -1.0, 0.2, -0.3]),
        3,
        candidate_minimum=2,
        research_minimum=10,
    )

    assert result.max_drawdown_atr == 1.6
    assert result.max_loss_streak == 2


def test_portfolio_result_cannot_be_promoted_to_candidate() -> None:
    result = validate_rows(
        _rows([0.4, -0.1] * 20),
        3,
        candidate_minimum=30,
        research_minimum=300,
    )

    portfolio = portfolio_only(result)

    assert result.status == "RESEARCH_CANDIDATE"
    assert portfolio.status == "PORTFOLIO_ONLY"
    assert not portfolio.stable
