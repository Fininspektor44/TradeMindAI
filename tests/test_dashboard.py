from __future__ import annotations

from datetime import datetime, timezone

from trademind.dashboard import (
    DashboardSnapshot,
    MetricLine,
    SymbolLine,
    _confirmed_patterns,
    _research_candidates,
    render_dashboard,
)
from trademind.health import DataHealth, JournalHealth


def _metric(
    *,
    scope: str = "XAUUSD",
    label: str = "INTERNAL_BOS",
    trades: int,
    status: str,
    profit_factor: float,
    average: float,
) -> MetricLine:
    return MetricLine(
        scope=scope,
        label=label,
        horizon=3,
        observations=trades + 4,
        trades=trades,
        status=status,
        win_rate=60.0,
        profit_factor_atr=profit_factor,
        avg_net_atr=average,
    )


def test_confirmed_patterns_require_research_sample_and_positive_edge() -> None:
    confirmed = _metric(
        trades=300,
        status="RESEARCH_SAMPLE",
        profit_factor=1.4,
        average=0.2,
    )
    too_small = _metric(
        trades=299,
        status="INSUFFICIENT_SAMPLE",
        profit_factor=3.0,
        average=0.8,
    )
    negative = _metric(
        label="SSL_SWEEP",
        trades=320,
        status="RESEARCH_SAMPLE",
        profit_factor=0.8,
        average=-0.1,
    )

    assert _confirmed_patterns((too_small, negative, confirmed)) == [confirmed]


def test_candidates_are_limited_to_positive_early_groups() -> None:
    candidate = _metric(
        trades=20,
        status="INSUFFICIENT_SAMPLE",
        profit_factor=1.5,
        average=0.2,
    )
    tiny = _metric(
        trades=9,
        status="INSUFFICIENT_SAMPLE",
        profit_factor=2.0,
        average=0.4,
    )

    assert _research_candidates((tiny, candidate)) == [candidate]


def test_render_dashboard_contains_health_and_escapes_labels() -> None:
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
        minimum_sample=300,
        schema_version="1.1",
        timeframe="M5",
    )

    output = render_dashboard(snapshot)

    assert "TradeMind AI v1.0" in output
    assert "Подтверждённых закономерностей пока нет" in output
    assert "&lt;BOS&gt;" in output
    assert "<BOS>" not in output
