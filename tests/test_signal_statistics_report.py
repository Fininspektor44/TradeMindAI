from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from trademind.signal_statistics_report import REPORT_SCHEMA_VERSION, build_report


def _rows(values: list[float], horizon: int = 3) -> list[dict[str, str]]:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows: list[dict[str, str]] = []
    for index, value in enumerate(values):
        rows.append(
            {
                "schema_version": "1.1",
                "signal_time": (start + timedelta(minutes=15 * index)).isoformat(),
                "symbol": "XAUUSD",
                "timeframe": "M5",
                "action": "BUY",
                "internal_break": "BULLISH_BOS",
                f"outcome_{horizon}": "WIN" if value > 0 else "LOSS" if value < 0 else "FLAT",
                f"progress_atr_{horizon}": str(value),
                "atr": "1.0",
            }
        )
    return rows


def test_report_is_json_ready_and_read_only() -> None:
    generated_at = datetime(2026, 8, 10, 18, 0, tzinfo=timezone.utc)
    report = build_report(
        _rows([0.5, -0.1] * 30),
        horizons=[3],
        candidate_minimum=30,
        research_minimum=300,
        generated_at=generated_at,
    )

    assert report["schema_version"] == REPORT_SCHEMA_VERSION
    assert report["read_only"] is True
    assert report["orders_enabled"] is False
    assert report["symbols"] == ["XAUUSD"]
    assert report["horizons"] == [3]
    assert report["journal_rows"] == 60
    json.dumps(report)


def test_report_exposes_agent_useful_candidate_metrics_and_reasons() -> None:
    report = build_report(
        _rows([0.5, -0.1] * 30),
        horizons=[3],
        candidate_minimum=30,
        research_minimum=300,
    )

    candidates = [
        item for item in report["patterns"] if item["status"] == "RESEARCH_CANDIDATE"
    ]

    assert candidates
    candidate = candidates[0]
    assert candidate["symbol"] == "XAUUSD"
    assert candidate["horizon"] == 3
    assert candidate["trades"] == 60
    assert candidate["profit_factor_atr"] > 1.0
    assert candidate["avg_net_atr"] > 0.0
    assert candidate["early"]["avg_net_atr"] > 0.0
    assert candidate["late"]["avg_net_atr"] > 0.0
    assert len(candidate["mean_ci95"]) == 2
    assert "research threshold is 300 trades" in candidate["reasons"]


def test_report_exposes_validated_patterns() -> None:
    report = build_report(
        _rows([0.4, -0.1] * 150),
        horizons=[3],
        candidate_minimum=30,
        research_minimum=300,
    )

    assert report["status_counts"].get("VALIDATED", 0) >= 1
    validated = [item for item in report["patterns"] if item["status"] == "VALIDATED"]
    assert validated
    assert validated[0]["mean_ci95"][0] > 0.0
