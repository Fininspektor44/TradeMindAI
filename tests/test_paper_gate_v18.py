from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from trademind.paper_gate_v18 import (
    asset_class,
    build_decisions,
    build_paper_journal,
    state_gate_status,
)

NOW = datetime(2026, 8, 3, 6, 0, tzinfo=timezone.utc)


def _signal(
    *,
    source_id: str = "EURUSD:M5:1",
    signal_time: datetime = NOW,
    scenario: str = "BOS_PLUS_SWEEP",
    horizon: str = "M30",
    minutes: str = "30",
    score: str = "78",
    components: str = "INTERNAL_BOS|LIQUIDITY_SWEEP|FVG|LOW_SPREAD",
) -> dict[str, str]:
    return {
        "event_id": f"FX_RESEARCH:{source_id}:{scenario}:{horizon}",
        "source": "FX_RESEARCH",
        "source_id": source_id,
        "signal_time": signal_time.isoformat(),
        "symbol": "EURUSD",
        "timeframe": "M5",
        "session": "LONDON",
        "action": "BUY",
        "scenario": scenario,
        "scenario_family": "COMBINATION",
        "components": components,
        "quality_score": score,
        "entry_price": "1.1000",
        "stop_price": "",
        "target_price": "",
        "rr": "",
        "horizon": horizon,
        "horizon_minutes": minutes,
        "metric_unit": "ATR",
        "outcome": "WIN",
        "result": "0.4",
        "mfe": "0.8",
        "mae": "0.2",
        "completed": "1",
    }


def _state(
    *,
    completed: str = "80",
    days: str = "8",
    pf: str = "1.30",
    average: str = "0.08",
    early: str = "0.06",
    late: str = "0.10",
    drawdown: str = "8",
    streak: str = "4",
    scenario: str = "BOS_PLUS_SWEEP",
    horizon: str = "M30",
    score_filter: str = "SCORE_70",
) -> dict[str, str]:
    return {
        "source": "FX_RESEARCH",
        "symbol": "EURUSD",
        "action": "BUY",
        "scenario": scenario,
        "score_filter": score_filter,
        "horizon": horizon,
        "metric_unit": "ATR",
        "completed": completed,
        "trading_days": days,
        "status": "CANDIDATE",
        "win_rate": "0.58",
        "profit_factor": pf,
        "avg_result": average,
        "early_avg_result": early,
        "late_avg_result": late,
        "max_drawdown": drawdown,
        "max_loss_streak": streak,
    }


def test_asset_classes_are_kept_separate() -> None:
    assert asset_class("BTCUSD") == "CRYPTO"
    assert asset_class("EURUSD") == "FX"
    assert asset_class("XAUUSD") == "METALS"
    assert asset_class(".US30Cash") == "INDICES"
    assert asset_class("BRENT") == "OIL"


def test_gate_thresholds_follow_watch_candidate_validated_contract() -> None:
    assert state_gate_status(_state(completed="49", days="20"))[0] == "WATCH"
    assert state_gate_status(_state())[0] == "CANDIDATE"
    validated = _state(completed="170", days="15", pf="1.4", average="0.09")
    assert state_gate_status(validated)[0] == "VALIDATED"
    unstable = _state(completed="80", days="8", pf="0.9", average="-0.02", late="-0.01")
    assert state_gate_status(unstable)[0] == "REJECTED"


def test_decisions_use_preferred_horizon_and_reject_conflicting_cost() -> None:
    m15 = _signal(horizon="M15", minutes="15")
    m30 = _signal(horizon="M30", minutes="30")
    states = [_state()]
    decisions = build_decisions([m15, m30], states, NOW)
    assert len(decisions) == 1
    assert decisions[0]["horizon"] == "M30"
    assert decisions[0]["gate_status"] == "CANDIDATE"

    conflict = _signal(
        source_id="EURUSD:M5:2",
        components="INTERNAL_BOS|LIQUIDITY_SWEEP|HIGH_SPREAD",
    )
    conflict_state = _state()
    rejected = build_decisions([conflict], [conflict_state], NOW)[0]
    assert rejected["gate_status"] == "REJECTED"
    assert "HIGH_SPREAD" in rejected["reasons"]


def test_duplicate_market_wave_keeps_only_strongest_signal() -> None:
    first = _signal(source_id="EURUSD:M5:1", signal_time=NOW, score="72")
    second = _signal(
        source_id="EURUSD:M5:2",
        signal_time=NOW + timedelta(minutes=10),
        score="82",
    )
    states = [_state(score_filter="SCORE_70"), _state(score_filter="SCORE_80")]
    decisions = build_decisions([first, second], states, NOW)
    eligible = [row for row in decisions if row["eligible"] == "1"]
    duplicate = [row for row in decisions if row["duplicate_wave"] == "1"]
    assert len(eligible) == 1
    assert eligible[0]["quality_score"] == "82"
    assert len(duplicate) == 1


def test_forward_journal_never_backfills_and_is_idempotent() -> None:
    before = _signal(source_id="EURUSD:M5:old", signal_time=NOW - timedelta(minutes=1))
    after = _signal(source_id="EURUSD:M5:new", signal_time=NOW + timedelta(minutes=1))
    states = [_state()]
    decisions = build_decisions([before, after], states, NOW)
    journal = build_paper_journal(decisions, [], NOW, NOW + timedelta(minutes=2))
    assert len(journal) == 1
    assert journal[0]["source_id"] == "EURUSD:M5:new"

    activated_at = journal[0]["activated_at"]
    rerun = build_paper_journal(
        decisions,
        journal,
        NOW,
        NOW + timedelta(minutes=7),
    )
    assert len(rerun) == 1
    assert rerun[0]["activated_at"] == activated_at
    assert rerun[0]["updated_at"] != activated_at


def test_v18_files_are_read_only() -> None:
    root = Path(__file__).resolve().parents[1]
    paths = [
        root / "src" / "trademind" / "paper_gate_v18.py",
        root / "src" / "trademind" / "watchdog_paper.py",
        root / "scripts" / "run_v180_paper_gate.ps1",
        root / "scripts" / "install_v180_paper_gate_task.ps1",
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
