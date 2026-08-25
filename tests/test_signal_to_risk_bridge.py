from __future__ import annotations

import csv
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from trademind.signal_evidence import similarity_key
from trademind.signal_intelligence import (
    EntryOrder,
    HistoricalEvidence,
    SignalCandidate,
    TradePlan,
    build_passport,
)
from trademind.signal_to_risk_bridge import (
    DECISION_STATE,
    WAITING_STATE,
    run_bridge,
    validate_publishable_passport,
)

NOW = datetime(2026, 8, 5, 18, 30, tzinfo=timezone.utc)
LOGIN = "67206924"


def _msc(value: datetime) -> int:
    return int(value.timestamp() * 1000)


def _write_csv(path: Path, fields: list[str], rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _candidate(*, created_at: datetime | None = None) -> SignalCandidate:
    observed = (created_at or NOW - timedelta(seconds=10)) - timedelta(seconds=2)
    created = created_at or NOW - timedelta(seconds=10)
    return SignalCandidate(
        observed_at=observed,
        created_at=created,
        symbol="EURUSD",
        timeframe="M5",
        setup_family="SMC_OTE_CONTINUATION",
        scenario="SSL sweep, bullish displacement, OTE retracement",
        plan=TradePlan(
            action="BUY",
            entries=(
                EntryOrder(1.1000, 0.50, "Fibonacci 61.8% retracement"),
                EntryOrder(1.0980, 0.30, "OTE zone"),
                EntryOrder(1.0960, 0.20, "Fibonacci 79% and demand edge"),
            ),
            stop_price=1.0920,
            targets=(1.1070, 1.1120),
            invalidation="Close below protected swing low",
            target_rationale=("Prior high", "External buy-side liquidity"),
        ),
        market_features={
            "structure": {"swing_bias": "BULLISH", "internal_bias": "BULLISH"},
            "liquidity": {"ssl_sweep": True},
            "fibonacci": {"entry_1": 0.618, "entry_2": 0.705, "entry_3": 0.79},
            "volume": {"rvol_20": 1.7},
            "momentum": {"impulse_atr": 1.4},
            "volatility": {"atr_percentile": 60.0, "spread_cost_atr": 0.02},
            "confirmation": {"fvg": "BULLISH"},
            "session": {"name": "LONDON_NY_OVERLAP"},
            "execution": {"spread_ok": True},
            "portfolio": {"correlation_load": 0.10},
        },
        factor_scores={
            "structure": 0.92,
            "liquidity": 0.90,
            "fibonacci": 0.88,
            "volume": 0.85,
            "momentum": 0.84,
            "volatility": 0.82,
            "confirmation": 0.90,
            "session": 0.85,
            "execution": 0.88,
            "portfolio": 0.80,
        },
        factor_reasons={
            "structure": ("bullish BOS",),
            "liquidity": ("sell-side liquidity swept",),
            "fibonacci": ("OTE retracement",),
            "volume": ("relative volume expansion",),
        },
        provenance=("FX_RESEARCH", "SMC_ENGINE", "VOLUME_COLLECTOR"),
        generated_from_market_data=True,
        robot_context_only={"note": "monitoring only"},
    )


def _evidence(
    candidate: SignalCandidate,
    *,
    wins: int = 36,
    losses: int = 7,
) -> HistoricalEvidence:
    return HistoricalEvidence(
        setup_key=similarity_key(candidate),
        captured_at=NOW - timedelta(minutes=5),
        wins=wins,
        losses=losses,
        flats=0,
        gross_win_r=59.4,
        gross_loss_r=6.44,
        average_win_r=1.65,
        average_loss_r=-0.92,
        max_drawdown_r=3.2,
        recent_win_rate=0.80,
        baseline_win_rate=0.77,
    )


def _write_passport(
    path: Path,
    *,
    candidate: SignalCandidate | None = None,
    wins: int = 36,
    losses: int = 7,
) -> dict[str, object]:
    setup = candidate or _candidate()
    passport = build_passport(
        setup,
        _evidence(setup, wins=wins, losses=losses),
        cost_r=0.04,
        now=NOW,
    ).as_dict()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(passport, ensure_ascii=False, indent=2), encoding="utf-8")
    return passport


def _profile(path: Path) -> Path:
    path.write_text(
        json.dumps(
            {
                "name": "STANDARD",
                "risk_basis": "MIN_BALANCE_EQUITY",
                "default_trade_risk_pct": 0.5,
                "maximum_trade_risk_pct": 1.0,
                "maximum_portfolio_risk_pct": 3.0,
                "maximum_symbol_risk_pct": 1.5,
                "maximum_correlation_risk_pct": 2.0,
                "maximum_daily_loss_pct": 2.0,
                "maximum_account_drawdown_pct": 10.0,
                "maximum_margin_usage_pct": 50.0,
                "minimum_free_margin_pct": 25.0,
                "maximum_open_trades": 8,
                "maximum_account_snapshot_age_seconds": 120,
                "maximum_signal_age_seconds": 900,
                "maximum_clock_skew_seconds": 30,
                "adverse_slippage_ticks": 2.0,
                "minimum_risk_utilization_pct": 50.0,
                "require_margin_check": True,
                "require_complete_portfolio_risk": True,
                "allowed_signal_states": ["PUBLISHABLE"],
            }
        ),
        encoding="utf-8",
    )
    return path


def _account_fields() -> list[str]:
    return [
        "time_msc",
        "account_login",
        "server",
        "currency",
        "balance",
        "equity",
        "margin",
        "free_margin",
        "margin_level",
        "leverage",
        "open_positions",
        "trade_allowed",
        "terminal_connected",
    ]


def _position_fields() -> list[str]:
    return [
        "time_msc",
        "account_login",
        "server",
        "currency",
        "position_ticket",
        "position_id",
        "position_time_msc",
        "symbol",
        "magic",
        "side",
        "volume",
        "open_price",
        "current_price",
        "sl",
        "tp",
        "profit",
        "swap",
        "comment",
    ]


def _symbol_fields() -> list[str]:
    return [
        "time_msc",
        "account_login",
        "server",
        "currency",
        "symbol",
        "digits",
        "trade_mode",
        "bid",
        "ask",
        "tick_size",
        "tick_value",
        "tick_value_profit",
        "tick_value_loss",
        "volume_min",
        "volume_max",
        "volume_step",
        "contract_size",
        "margin_initial",
        "margin_maintenance",
        "margin_buy_per_volume",
        "margin_sell_per_volume",
        "leverage",
    ]


def _mt5_files(
    tmp_path: Path,
    *,
    position_stop: float | None = None,
) -> tuple[Path, Path, Path]:
    captured = NOW - timedelta(seconds=5)
    account = tmp_path / f"mt5_risk_account_utc_{LOGIN}.csv"
    positions = tmp_path / f"mt5_risk_positions_utc_{LOGIN}.csv"
    symbols = tmp_path / f"mt5_risk_symbols_utc_{LOGIN}.csv"
    open_positions = 1 if position_stop is not None else 0
    _write_csv(
        account,
        _account_fields(),
        [
            {
                "time_msc": _msc(captured),
                "account_login": LOGIN,
                "server": "RoboForex-Pro",
                "currency": "USD",
                "balance": 10_000.0,
                "equity": 9_950.0,
                "margin": 200.0,
                "free_margin": 9_750.0,
                "margin_level": 4975.0,
                "leverage": 500,
                "open_positions": open_positions,
                "trade_allowed": 1,
                "terminal_connected": 1,
            }
        ],
    )
    position_rows: list[dict[str, object]] = []
    if position_stop is not None:
        position_rows.append(
            {
                "time_msc": _msc(captured),
                "account_login": LOGIN,
                "server": "RoboForex-Pro",
                "currency": "USD",
                "position_ticket": "1001",
                "position_id": "9001",
                "position_time_msc": _msc(captured - timedelta(hours=1)),
                "symbol": "EURUSD",
                "magic": 0,
                "side": "BUY",
                "volume": 0.10,
                "open_price": 1.0970,
                "current_price": 1.1000,
                "sl": position_stop,
                "tp": 1.1100,
                "profit": 20.0,
                "swap": 0.0,
                "comment": "manual",
            }
        )
    _write_csv(positions, _position_fields(), position_rows)
    if not position_rows:
        os.utime(positions, (captured.timestamp(), captured.timestamp()))
    _write_csv(
        symbols,
        _symbol_fields(),
        [
            {
                "time_msc": _msc(captured),
                "account_login": LOGIN,
                "server": "RoboForex-Pro",
                "currency": "USD",
                "symbol": "EURUSD",
                "digits": 5,
                "trade_mode": "FULL",
                "bid": 1.0999,
                "ask": 1.1000,
                "tick_size": 0.0001,
                "tick_value": 10.0,
                "tick_value_profit": 10.0,
                "tick_value_loss": 10.0,
                "volume_min": 0.01,
                "volume_max": 100.0,
                "volume_step": 0.01,
                "contract_size": 100_000.0,
                "margin_initial": 0.0,
                "margin_maintenance": 0.0,
                "margin_buy_per_volume": 220.0,
                "margin_sell_per_volume": 219.8,
                "leverage": 500,
            }
        ],
    )
    return account, positions, symbols


def test_bridge_creates_allow_decision_from_validated_passport_and_live_mt5(
    tmp_path: Path,
) -> None:
    passport = tmp_path / "passport.json"
    _write_passport(passport)
    account, positions, symbols = _mt5_files(tmp_path)
    output = tmp_path / "output"
    journal = tmp_path / "events.jsonl"

    result = run_bridge(
        login=LOGIN,
        passport_path=passport,
        account_csv=account,
        positions_csv=positions,
        symbols_csv=symbols,
        profile_path=_profile(tmp_path / "profile.json"),
        output_dir=output,
        journal=journal,
        now=NOW,
    )

    assert result.state == DECISION_STATE
    assert result.package is not None
    decision = result.package["risk_decision"]
    assert decision["state"] == "ALLOW"
    assert decision["actual_risk_money"] <= decision["risk_budget_money"]
    assert len(decision["orders"]) == 3
    assert result.package["passport"]["completed_sample"] == 43
    assert result.package["safety"]["orders_enabled"] is False
    assert (output / "latest_decision.json").is_file()
    assert (output / "status.json").is_file()
    assert journal.is_file()


def test_directory_mode_waits_when_no_passport_is_publishable(tmp_path: Path) -> None:
    passports = tmp_path / "passports"
    _write_passport(passports / "small_sample.json", wins=9, losses=2)
    account, positions, symbols = _mt5_files(tmp_path)

    result = run_bridge(
        login=LOGIN,
        passports_dir=passports,
        account_csv=account,
        positions_csv=positions,
        symbols_csv=symbols,
        profile_path=_profile(tmp_path / "profile.json"),
        output_dir=tmp_path / "output",
        now=NOW,
    )

    assert result.state == WAITING_STATE
    assert result.package is None
    assert result.status["passports_scanned"] == 1
    assert result.status["passports_rejected"] == 1


def test_passport_candidate_tampering_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "passport.json"
    payload = _write_passport(path)
    payload["candidate"]["scenario"] = "tampered scenario"  # type: ignore[index]
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="signal_id"):
        validate_publishable_passport(path, now=NOW, cost_r=0.04)


def test_evidence_for_another_setup_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "passport.json"
    payload = _write_passport(path)
    payload["historical_evidence"]["setup_key"] = "WRONG"  # type: ignore[index]
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="setup_key"):
        validate_publishable_passport(path, now=NOW, cost_r=0.04)


def test_bridge_blocks_when_existing_position_has_no_stop(tmp_path: Path) -> None:
    passport = tmp_path / "passport.json"
    _write_passport(passport)
    account, positions, symbols = _mt5_files(tmp_path, position_stop=0.0)

    result = run_bridge(
        login=LOGIN,
        passport_path=passport,
        account_csv=account,
        positions_csv=positions,
        symbols_csv=symbols,
        profile_path=_profile(tmp_path / "profile.json"),
        output_dir=tmp_path / "output",
        now=NOW,
    )

    assert result.package is not None
    decision = result.package["risk_decision"]
    assert decision["state"] == "BLOCK"
    assert any(item["code"] == "OPEN_RISK_UNKNOWN" for item in decision["reasons"])


def test_explicit_shadow_passport_is_rejected_before_account_calculation(
    tmp_path: Path,
) -> None:
    path = tmp_path / "passport.json"
    _write_passport(path, wins=9, losses=2)

    with pytest.raises(ValueError, match="not PUBLISHABLE"):
        validate_publishable_passport(path, now=NOW, cost_r=0.04)


def test_bridge_source_contains_no_execution_or_broker_client_calls() -> None:
    source = Path("src/trademind/signal_to_risk_bridge.py").read_text(encoding="utf-8")
    forbidden = (
        "OrderSend(",
        "OrderSendAsync(",
        "PositionClose(",
        "PositionModify(",
        "import MetaTrader5",
        "import requests",
    )
    assert not any(token in source for token in forbidden)
