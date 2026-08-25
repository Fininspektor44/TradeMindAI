from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from trademind.fx_research import (
    FX_MAJORS,
    build_fx_observations,
    session_for_time,
    validate_fx_observations,
)
from test_smc_ote import _synthetic_rows


def _volume_row(index: int, *, symbol: str = "EURUSD") -> dict[str, str]:
    start = datetime(2026, 7, 1, tzinfo=timezone.utc)
    opened = 1.10000 + index * 0.00010
    closed = opened + 0.00005
    return {
        "schema_version": "1.4",
        "time": str(int((start + timedelta(minutes=5 * index)).timestamp())),
        "symbol": symbol,
        "timeframe": "M5",
        "bar_seconds": "300",
        "point": "0.00001",
        "open": str(opened),
        "high": str(closed + 0.00002),
        "low": str(opened - 0.00002),
        "close": str(closed),
        "bar_tick_volume": str(300 + index),
        "tick_count": str(300 + index),
        "tick_rate_per_sec": str((300 + index) / 300),
        "bid_up": "150",
        "bid_down": "80",
        "ask_up": "150",
        "ask_down": "80",
        "mid_up": "150",
        "mid_down": "80",
        "buy_ticks": "0",
        "sell_ticks": "0",
        "trade_volume": "0",
        "trade_volume_real": "0",
        "spread_mean_points": "2",
        "spread_min_points": "1",
        "spread_max_points": "3",
        "spread_last_points": "2",
        "spread_expansion_points": "0",
        "realized_abs_move_points": "20",
        "direction_imbalance": "0.30",
        "delta_proxy": "70",
        "rvol_20": "1.5",
        "volume_percentile_100": "90",
        "range_per_tick_points": "0.003",
        "body_per_tick_points": "0.002",
        "tick_copy_status": "OK",
        "tick_copy_error": "0",
    }


def test_session_boundaries_are_stable_utc() -> None:
    day = datetime(2026, 7, 1, tzinfo=timezone.utc)

    assert session_for_time(day.replace(hour=1)) == "ASIA"
    assert session_for_time(day.replace(hour=7)) == "LONDON"
    assert session_for_time(day.replace(hour=12)) == "LONDON_NY_OVERLAP"
    assert session_for_time(day.replace(hour=17)) == "NEW_YORK"
    assert session_for_time(day.replace(hour=21)) == "OFF_HOURS"


def test_fx_observations_are_ote_only_and_charge_spread() -> None:
    rows = _synthetic_rows()

    observations = build_fx_observations(rows)

    assert observations
    assert {row["action"] for row in observations} == {"BUY", "SELL"}
    assert all(
        row["signal_source"] == "trademind.ote_engine.build_ote_signals"
        for row in observations
    )
    first = observations[0]
    assert first["symbol"] == "EURUSD"
    assert "ALL_SIGNALS" in first["labels"].split("|")
    assert first["outcome_3"] in {"WIN", "LOSS", "FLAT"}

    entry = float(first["entry_price"])
    future_close = float(first["exit_price_3"])
    spread_cost = float(first["spread_cost"])
    assert float(first["net_move_3"]) == pytest.approx(
        (1.0 if first["action"] == "BUY" else -1.0) * (future_close - entry)
        - spread_cost
    )


def _validation_row(day: int, hour: int, action: str, value: float) -> dict[str, str]:
    when = datetime(2026, 6, day, hour, tzinfo=timezone.utc)
    outcome = "WIN" if value > 0 else "LOSS"
    return {
        "signal_time": when.isoformat(),
        "symbol": "EURUSD",
        "timeframe": "M5",
        "session": session_for_time(when),
        "action": action,
        "labels": "ALL_SIGNALS|HIGH_RVOL",
        "outcome_3": outcome,
        "progress_atr_3": str(value),
        "outcome_6": outcome,
        "progress_atr_6": str(value),
        "outcome_12": outcome,
        "progress_atr_12": str(value),
    }


def test_validation_keeps_session_and_direction_separate() -> None:
    observations: list[dict[str, str]] = []
    for day in range(1, 25):
        value = 0.4 if day % 2 else -0.1
        observations.append(_validation_row(day, 1, "BUY", value))
        observations.append(_validation_row(day, 8, "SELL", value))

    validations = validate_fx_observations(
        observations,
        candidate_minimum=2,
        research_minimum=50,
        minimum_trading_days=1,
    )

    keys = {
        (item.symbol, item.label, session, item.action, item.horizon)
        for session, item in validations
    }
    assert ("EURUSD", "ASIA::ALL_SIGNALS", "ASIA", "BUY", 3) in keys
    assert ("EURUSD", "LONDON::ALL_SIGNALS", "LONDON", "SELL", 12) in keys
    assert all(item.result.status != "VALIDATED" for _, item in validations)


def test_fx_research_contract_is_read_only() -> None:
    source = Path("src/trademind/fx_research.py").read_text(encoding="utf-8")
    runners = "\n".join(
        Path(path).read_text(encoding="utf-8")
        for path in (
            "scripts/run_v142_fx_research.ps1",
            "scripts/install_v142_fx_research_task.ps1",
        )
    )
    text = source + runners
    forbidden = (
        "CTrade",
        "OrderSend(",
        ".Buy(",
        ".Sell(",
        "PositionClose(",
        "TRADE_ACTION_DEAL",
    )
    assert all(token not in text for token in forbidden)
    assert tuple(FX_MAJORS) == (
        "EURUSD",
        "GBPUSD",
        "USDJPY",
        "USDCHF",
        "USDCAD",
        "AUDUSD",
        "NZDUSD",
    )
