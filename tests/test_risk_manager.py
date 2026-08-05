from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from trademind.risk_manager import (
    AccountSnapshot,
    InstrumentSpec,
    PortfolioSnapshot,
    PositionRisk,
    RiskProfile,
    evaluate_risk,
)
from trademind.signal_intelligence import EntryOrder, SignalCandidate, TradePlan


NOW = datetime(2026, 8, 5, 17, 0, tzinfo=timezone.utc)


def _candidate(
    *,
    entries: tuple[EntryOrder, ...] | None = None,
    generated_from_market_data: bool = True,
    symbol: str = "EURUSD",
) -> SignalCandidate:
    plan_entries = entries or (
        EntryOrder(1.1000, 1.0, "confirmed entry", "MARKET"),
    )
    return SignalCandidate(
        observed_at=NOW - timedelta(seconds=70),
        created_at=NOW - timedelta(seconds=60),
        symbol=symbol,
        timeframe="M5",
        setup_family="SMC_OTE_CONTINUATION",
        scenario="risk manager test",
        plan=TradePlan(
            action="BUY",
            entries=plan_entries,
            stop_price=1.0950,
            targets=(1.1100,),
            invalidation="protected low broken",
            target_rationale=("external liquidity",),
        ),
        market_features={
            "structure": {"swing_bias": "BULLISH"},
            "liquidity": {"ssl_sweep": True},
            "fibonacci": {"retracement": 0.705},
            "volume": {"rvol_20": 1.5},
        },
        factor_scores={
            "structure": 0.9,
            "liquidity": 0.9,
            "fibonacci": 0.8,
            "volume": 0.8,
        },
        factor_reasons={"structure": ("BOS confirmed",)},
        provenance=("FX_RESEARCH",),
        generated_from_market_data=generated_from_market_data,
    )


def _account(**changes: object) -> AccountSnapshot:
    values = {
        "account_id": "37365712",
        "venue": "MT5",
        "currency": "USD",
        "captured_at": NOW - timedelta(seconds=10),
        "balance": 10_000.0,
        "equity": 10_000.0,
        "free_margin": 10_000.0,
        "margin_used": 0.0,
        "high_watermark_equity": 10_000.0,
        "day_start_equity": 10_000.0,
        "trading_enabled": True,
    }
    values.update(changes)
    return AccountSnapshot(**values)


def _instrument(**changes: object) -> InstrumentSpec:
    values = {
        "symbol": "EURUSD",
        "venue": "MT5",
        "account_currency": "USD",
        "tick_size": 0.0001,
        "tick_value_per_volume": 10.0,
        "volume_min": 0.01,
        "volume_max": 100.0,
        "volume_step": 0.01,
        "correlation_group": "USD_SHORT",
        "margin_per_volume": 1100.0,
    }
    values.update(changes)
    return InstrumentSpec(**values)


def _profile(**changes: object) -> RiskProfile:
    values = {
        "adverse_slippage_ticks": 0.0,
        "minimum_risk_utilization_pct": 50.0,
    }
    values.update(changes)
    return RiskProfile(**values)


def _decision(
    *,
    candidate: SignalCandidate | None = None,
    account: AccountSnapshot | None = None,
    instrument: InstrumentSpec | None = None,
    profile: RiskProfile | None = None,
    portfolio: PortfolioSnapshot | None = None,
    gate_state: str = "PUBLISHABLE",
    requested_risk_pct: float | None = None,
):
    return evaluate_risk(
        candidate or _candidate(),
        gate_state=gate_state,
        account=account or _account(),
        instrument=instrument or _instrument(),
        profile=profile or _profile(),
        portfolio=portfolio or PortfolioSnapshot(),
        requested_risk_pct=requested_risk_pct,
        now=NOW,
    )


def _codes(decision) -> set[str]:
    return {item.code for item in decision.reasons}


def test_single_fx_entry_never_exceeds_budget_after_strict_floor() -> None:
    decision = _decision()

    assert decision.state == "ALLOW"
    assert decision.allowed is True
    assert decision.risk_budget_money == pytest.approx(50.0)
    assert decision.actual_risk_money == pytest.approx(45.0)
    assert decision.actual_risk_money <= decision.risk_budget_money
    assert decision.actual_risk_pct == pytest.approx(0.45)
    assert len(decision.orders) == 1
    assert decision.orders[0].volume == pytest.approx(0.09)
    assert decision.margin_required == pytest.approx(99.0)
    assert decision.free_margin_after == pytest.approx(9901.0)
    assert decision.as_dict()["safety"]["orders_enabled"] is False
    assert decision.as_dict()["safety"]["broker_api_called"] is False


def test_staged_ote_entries_share_one_conservative_risk_budget() -> None:
    candidate = _candidate(
        entries=(
            EntryOrder(1.1000, 0.50, "confirmation", "MARKET"),
            EntryOrder(1.0980, 0.30, "OTE 70.5%", "LIMIT"),
            EntryOrder(1.0960, 0.20, "OTE 79%", "LIMIT"),
        )
    )
    decision = _decision(candidate=candidate)

    assert decision.state == "ALLOW"
    assert [order.volume for order in decision.orders] == pytest.approx(
        [0.04, 0.04, 0.09]
    )
    assert [order.risk_money for order in decision.orders] == pytest.approx(
        [20.0, 12.0, 9.0]
    )
    assert decision.actual_risk_money == pytest.approx(41.0)
    assert decision.actual_risk_money <= decision.risk_budget_money


def test_minimum_broker_volume_blocks_oversized_risk() -> None:
    account = _account(
        balance=1000.0,
        equity=1000.0,
        free_margin=1000.0,
        high_watermark_equity=1000.0,
        day_start_equity=1000.0,
    )
    profile = _profile(
        default_trade_risk_pct=0.10,
        maximum_trade_risk_pct=0.50,
    )
    decision = _decision(account=account, profile=profile)

    assert decision.state == "BLOCK"
    assert "MIN_VOLUME_EXCEEDS_RISK" in _codes(decision)
    assert not decision.orders


def test_signal_must_pass_publication_gate_before_sizing() -> None:
    decision = _decision(gate_state="SHADOW_ONLY")

    assert decision.state == "BLOCK"
    assert "SIGNAL_NOT_APPROVED" in _codes(decision)


def test_non_market_candidate_is_blocked() -> None:
    decision = _decision(candidate=_candidate(generated_from_market_data=False))

    assert decision.state == "BLOCK"
    assert "NON_MARKET_SIGNAL" in _codes(decision)


def test_requested_risk_cannot_exceed_user_profile_cap() -> None:
    decision = _decision(requested_risk_pct=1.5)

    assert decision.state == "BLOCK"
    assert "TRADE_RISK_LIMIT" in _codes(decision)


def test_portfolio_risk_cap_blocks_new_trade() -> None:
    portfolio = PortfolioSnapshot(
        positions=(
            PositionRisk("p1", "GBPUSD", "GBP", 280.0),
        ),
        open_trades=1,
    )
    decision = _decision(portfolio=portfolio)

    assert decision.state == "BLOCK"
    assert decision.portfolio_risk_after_pct == pytest.approx(3.25)
    assert "PORTFOLIO_RISK_LIMIT" in _codes(decision)


def test_symbol_and_correlation_caps_are_checked_separately() -> None:
    symbol_portfolio = PortfolioSnapshot(
        positions=(PositionRisk("p1", "EURUSD", "USD_SHORT", 120.0),),
        open_trades=1,
    )
    symbol_decision = _decision(portfolio=symbol_portfolio)
    assert "SYMBOL_RISK_LIMIT" in _codes(symbol_decision)

    correlation_portfolio = PortfolioSnapshot(
        positions=(PositionRisk("p2", "GBPUSD", "USD_SHORT", 180.0),),
        open_trades=1,
    )
    correlation_decision = _decision(portfolio=correlation_portfolio)
    assert "CORRELATION_RISK_LIMIT" in _codes(correlation_decision)


def test_unknown_existing_position_risk_blocks_new_trade() -> None:
    portfolio = PortfolioSnapshot(
        positions=(PositionRisk("p1", "XAUUSD", "USD_SHORT", None),),
        open_trades=1,
    )
    decision = _decision(portfolio=portfolio)

    assert decision.state == "BLOCK"
    assert "OPEN_RISK_UNKNOWN" in _codes(decision)


def test_daily_loss_and_account_drawdown_locks() -> None:
    daily = _account(
        equity=9700.0,
        free_margin=9700.0,
        high_watermark_equity=10_000.0,
        day_start_equity=10_000.0,
    )
    daily_decision = _decision(account=daily)
    assert "DAILY_LOSS_LOCK" in _codes(daily_decision)

    drawdown = _account(
        equity=10_000.0,
        free_margin=10_000.0,
        high_watermark_equity=12_000.0,
        day_start_equity=10_000.0,
    )
    drawdown_decision = _decision(account=drawdown)
    assert "ACCOUNT_DRAWDOWN_LOCK" in _codes(drawdown_decision)


def test_margin_and_free_margin_can_block_an_otherwise_valid_signal() -> None:
    account = _account(free_margin=90.0)
    decision = _decision(account=account)

    assert decision.state == "BLOCK"
    assert "INSUFFICIENT_FREE_MARGIN" in _codes(decision)
    assert "FREE_MARGIN_BUFFER" in _codes(decision)


def test_missing_margin_model_blocks_in_strict_profile() -> None:
    instrument = _instrument(
        margin_per_volume=None,
        contract_size=None,
        leverage=None,
    )
    decision = _decision(instrument=instrument)

    assert decision.state == "BLOCK"
    assert "MARGIN_MODEL_MISSING" in _codes(decision)


def test_bybit_linear_contract_uses_notional_margin_model() -> None:
    candidate = SignalCandidate(
        observed_at=NOW - timedelta(seconds=70),
        created_at=NOW - timedelta(seconds=60),
        symbol="BTCUSDT",
        timeframe="M5",
        setup_family="BTC_SMC_CONTINUATION",
        scenario="BTC linear contract",
        plan=TradePlan(
            action="BUY",
            entries=(EntryOrder(60_000.0, 1.0, "market", "MARKET"),),
            stop_price=59_000.0,
            targets=(62_000.0,),
            invalidation="swing low broken",
        ),
        market_features={"structure": {"bias": "BULLISH"}},
        factor_scores={"structure": 0.9},
        factor_reasons={"structure": ("BOS",)},
        provenance=("BYBIT_RESEARCH",),
    )
    account = _account(
        account_id="BYBIT-DEMO",
        venue="BYBIT",
        currency="USDT",
        balance=20_000.0,
        equity=20_000.0,
        free_margin=20_000.0,
        high_watermark_equity=20_000.0,
        day_start_equity=20_000.0,
    )
    instrument = _instrument(
        symbol="BTCUSDT",
        venue="BYBIT",
        account_currency="USDT",
        tick_size=0.1,
        tick_value_per_volume=0.1,
        volume_min=0.001,
        volume_max=100.0,
        volume_step=0.001,
        correlation_group="CRYPTO_BETA",
        margin_per_volume=None,
        contract_size=1.0,
        leverage=10.0,
    )
    profile = _profile(default_trade_risk_pct=1.0, maximum_trade_risk_pct=1.0)
    decision = _decision(
        candidate=candidate,
        account=account,
        instrument=instrument,
        profile=profile,
    )

    assert decision.state == "ALLOW"
    assert decision.orders[0].volume == pytest.approx(0.2)
    assert decision.actual_risk_money == pytest.approx(200.0)
    assert decision.margin_required == pytest.approx(1200.0)


def test_min_balance_equity_is_default_risk_basis() -> None:
    account = _account(
        balance=10_000.0,
        equity=8_000.0,
        free_margin=8_000.0,
        high_watermark_equity=8_000.0,
        day_start_equity=8_000.0,
    )
    decision = _decision(account=account)

    assert decision.risk_basis_money == pytest.approx(8_000.0)
    assert decision.risk_budget_money == pytest.approx(40.0)
    assert decision.orders[0].volume == pytest.approx(0.07)
    assert decision.actual_risk_money == pytest.approx(35.0)


def test_stale_account_and_signal_are_blocked() -> None:
    stale_account = _account(captured_at=NOW - timedelta(minutes=5))
    account_decision = _decision(account=stale_account)
    assert "ACCOUNT_SNAPSHOT_STALE" in _codes(account_decision)

    stale_candidate = replace(
        _candidate(),
        observed_at=NOW - timedelta(hours=2, seconds=10),
        created_at=NOW - timedelta(hours=2),
    )
    signal_decision = _decision(candidate=stale_candidate)
    assert "SIGNAL_STALE" in _codes(signal_decision)


def test_open_trade_count_is_a_hard_limit() -> None:
    portfolio = PortfolioSnapshot(open_trades=8)
    decision = _decision(portfolio=portfolio)

    assert decision.state == "BLOCK"
    assert "OPEN_TRADE_LIMIT" in _codes(decision)


def test_decision_id_is_stable_for_identical_inputs() -> None:
    first = _decision()
    second = _decision()

    assert first.decision_id == second.decision_id
    assert first.decision_id.startswith(f"RM-{first.signal_id}-")
