from __future__ import annotations

from datetime import datetime, timezone

from trademind.dashboard import (
    DashboardSnapshot,
    MetricLine,
    SymbolLine,
    _research_candidates,
    _validated_patterns,
    render_dashboard,
)
from trademind.health import DataHealth, JournalHealth
from trademind.validation import SegmentMetrics, ValidationResult


def _metric(
    *,
    scope: str = "XAUUSD",
    label: str = "INTERNAL_BOS",
    trades: int,
    status: str,
    profit_factor: float,
    average: float,
) -> MetricLine:
    segment = SegmentMetrics(
        trades=trades,
        win_rate=60.0,
        profit_factor_atr=profit_factor,
        avg_net_atr=average,
    )
    validation = ValidationResult(
        status=status,
        total=segment,
        early=segment,
        late=segment,
        max_drawdown_atr=1.2,
        max_loss_streak=3,
        mean_ci_low=average - 0.05,
        mean_ci_high=average + 0.05,
        reasons=(),
    )
    return MetricLine(
        scope=scope,
        label=label,
        horizon=3,
        observations=trades + 4,
        validation=validation,
    )


def test_validated_patterns_require_validation_status() -> None:
    validated = _metric(
        trades=300,
        status="VALIDATED",
        profit_factor=1.4,
        average=0.2,
    )
    candidate = _metric(
        trades=299,
        status="RESEARCH_CANDIDATE",
        profit_factor=3.0,
        average=0.8,
    )
    unstable = _metric(
        label="SSL_SWEEP",
        trades=320,
        status="UNSTABLE",
        profit_factor=1.5,
        average=0.1,
    )

    assert _validated_patterns((candidate, unstable, validated)) == [validated]


def test_candidates_exclude_portfolio_and_unstable_rows() -> None:
    candidate = _metric(
        trades=40,
        status="RESEARCH_CANDIDATE",
        profit_factor=1.5,
        average=0.2,
    )
    portfolio = _metric(
        scope="ALL",
        trades=100,
        status="PORTFOLIO_ONLY",
        profit_factor=2.0,
        average=0.4,
    )
    unstable = _metric(
        trades=50,
        status="UNSTABLE",
        profit_factor=1.8,
        average=0.3,
    )

    assert _research_candidates((portfolio, unstable, candidate)) == [candidate]


def test_render_dashboard_contains_validation_and_escapes_labels() -> None:
    now = datetime(2026, 7, 15, 20, 0, tzinfo=timezone.utc)
    health = DataHealth(
        symbol="XAUUSD",
        status="OK",
        rows=500,
        latest_time=now,
        age_minutes=0.0,
        spread=2,
        tick_volume=1000,
    )
    journal = JournalHealth(
        status="OK",
        rows=100,
        schema_rows=50,
        duplicate_ids=0,
        latest_time=now,
        age_minutes=0.0,
        counts={"XAUUSD": 50},
    )
    metric = _metric(
        label="<BOS>",
        trades=12,
        status="INSUFFICIENT_SAMPLE",
        profit_factor=1.2,
        average=0.1,
    )
    snapshot = DashboardSnapshot(
        generated_at=now,
        overall_status="OK",
        journal=journal,
        symbols=(
            SymbolLine(
                symbol="XAUUSD",
                health=health,
                observations=50,
                evaluated={3: 12},
            ),
        ),
        metrics=(metric,),
        candidate_minimum=30,
        minimum_sample=300,
        schema_version="1.1",
        timeframe="M5",
    )

    output = render_dashboard(snapshot)

    assert "TradeMind AI v1.1" in output
    assert "Подтверждённых закономерностей пока нет" in output
    assert "CI95 avg" in output
    assert "&lt;BOS&gt;" in output
    assert "<BOS>" not in output
