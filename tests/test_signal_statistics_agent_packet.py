from __future__ import annotations

from trademind.signal_statistics_agent_packet import (
    AGENT_PACKET_SCHEMA_VERSION,
    build_agent_packet,
)
from trademind.signal_statistics_report import REPORT_SCHEMA_VERSION


def _pattern(
    *,
    status: str,
    ci_low: float,
    early: float,
    late: float,
    profit_factor: float,
    trades: int,
) -> dict[str, object]:
    return {
        "symbol": "US30CASH",
        "pattern": "BULLISH_FVG",
        "horizon": 12,
        "status": status,
        "trades": trades,
        "profit_factor_atr": profit_factor,
        "avg_net_atr": 0.4,
        "early": {"avg_net_atr": early},
        "late": {"avg_net_atr": late},
        "mean_ci95": [ci_low, 0.8],
        "reasons": [],
    }


def _report(patterns: list[dict[str, object]]) -> dict[str, object]:
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "generated_at": "2026-08-11T00:00:00+00:00",
        "read_only": True,
        "orders_enabled": False,
        "patterns": patterns,
    }


def test_packet_selects_only_positive_stable_research_candidates() -> None:
    report = _report(
        [
            _pattern(
                status="RESEARCH_CANDIDATE",
                ci_low=0.05,
                early=0.2,
                late=0.5,
                profit_factor=1.7,
                trades=99,
            ),
            _pattern(
                status="RESEARCH_CANDIDATE",
                ci_low=-0.01,
                early=0.2,
                late=0.5,
                profit_factor=2.0,
                trades=150,
            ),
            _pattern(
                status="RESEARCH_CANDIDATE",
                ci_low=0.02,
                early=0.2,
                late=-0.1,
                profit_factor=1.5,
                trades=200,
            ),
        ]
    )

    packet = build_agent_packet(report)

    assert packet["schema_version"] == AGENT_PACKET_SCHEMA_VERSION
    assert packet["read_only"] is True
    assert packet["orders_enabled"] is False
    assert packet["decision_scope"] == "research_hypotheses_only"
    assert packet["candidate_count"] == 1
    assert packet["candidates"][0]["trades"] == 99
    assert "change_signal_weights" in packet["prohibited_actions"]
    assert "publish_or_sell_signals" in packet["prohibited_actions"]


def test_validated_candidate_ranks_before_research_candidate() -> None:
    report = _report(
        [
            _pattern(
                status="RESEARCH_CANDIDATE",
                ci_low=0.20,
                early=0.3,
                late=0.3,
                profit_factor=2.0,
                trades=250,
            ),
            _pattern(
                status="VALIDATED",
                ci_low=0.05,
                early=0.2,
                late=0.2,
                profit_factor=1.4,
                trades=400,
            ),
        ]
    )

    packet = build_agent_packet(report)

    assert packet["candidate_count"] == 2
    assert packet["candidates"][0]["status"] == "VALIDATED"
    assert packet["candidates"][1]["status"] == "RESEARCH_CANDIDATE"


def test_packet_rejects_non_read_only_report() -> None:
    report = _report([])
    report["orders_enabled"] = True

    try:
        build_agent_packet(report)
    except ValueError as exc:
        assert "read-only" in str(exc)
    else:
        raise AssertionError("expected ValueError")
