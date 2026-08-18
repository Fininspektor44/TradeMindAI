"""Tests for the pure Hypothesis-Tradeable-Scope <-> live-SignalCandidate
matcher: the one module in the codebase that reads both the Discovery
Engine research lineage and the live signal/risk lineage.

IMPORTANT: every candidate in this file is synthetic fixture data. None of
it constitutes a live trade instruction, a risk decision, or execution
authorization -- this file only proves the matcher's exact, deterministic
identity comparison and its structural safety boundaries.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from trademind.discovery.hypothesis_tradeable_scope import HypothesisTradeableScopeV1
from trademind.hypothesis_live_candidate_matching import verify_live_candidate_matches_scope
from trademind.signal_intelligence import EntryOrder, SignalCandidate, TradePlan

NOW = datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc)


def _scope(
    *,
    symbol: str = "XAUUSD",
    timeframe: str = "M5",
    setup_family: str = "spread_pressure",
    allowed_action_scope: str = "BOTH",
) -> HypothesisTradeableScopeV1:
    return HypothesisTradeableScopeV1(
        hypothesis_id="rpi-v1:" + "a" * 64 + ":0",
        hypothesis_family_id="hf_" + "b" * 64,
        bound_hypothesis_content_hash="c" * 64,
        manifest_semantic_hash=f"sha256:{'d' * 64}",
        manifest_artifact_hash_ref=f"sha256:{'e' * 64}",
        symbol=symbol,
        timeframe=timeframe,
        setup_family=setup_family,
        allowed_action_scope=allowed_action_scope,
        source_action_scope="BUY_SELL_DIRECTIONAL",
        source_candidate_id="ssc-v2-" + "f" * 64,
        source_candidate_content_hash=f"sha256:{'0' * 64}",
        source_packet_artifact_hash_ref=f"sha256:{'1' * 64}",
        source_packet_semantic_hash=f"sha256:{'2' * 64}",
        source_report_artifact_hash_ref=f"sha256:{'3' * 64}",
        bound_at="2026-08-18T00:00:00+00:00",
    )


def _candidate(
    *,
    symbol: str = "XAUUSD",
    timeframe: str = "M5",
    setup_family: str = "spread_pressure",
    action: str = "BUY",
) -> SignalCandidate:
    return SignalCandidate(
        observed_at=NOW - timedelta(seconds=70),
        created_at=NOW - timedelta(seconds=60),
        symbol=symbol,
        timeframe=timeframe,
        setup_family=setup_family,
        scenario="hypothesis tradeable scope matcher test",
        plan=TradePlan(
            action=action,
            entries=(EntryOrder(2000.0, 1.0, "confirmed entry", "MARKET"),),
            stop_price=1990.0 if action == "BUY" else 2010.0,
            targets=(2020.0,) if action == "BUY" else (1980.0,),
            invalidation="protected level broken",
            target_rationale=("external liquidity",),
        ),
        market_features={"structure": {"swing_bias": "BULLISH" if action == "BUY" else "BEARISH"}},
        factor_scores={"structure": 0.9},
        factor_reasons={"structure": ("BOS confirmed",)},
        provenance=("FX_RESEARCH",),
    )


# ---------------------------------------------------------------------------
# 10-14: exact identity matching only
# ---------------------------------------------------------------------------


def test_exact_match_passes() -> None:
    scope = _scope()
    candidate = _candidate()
    assert verify_live_candidate_matches_scope(scope, candidate) is True


def test_wrong_symbol_fails() -> None:
    scope = _scope(symbol="XAUUSD")
    candidate = _candidate(symbol="EURUSD")
    assert verify_live_candidate_matches_scope(scope, candidate) is False


def test_wrong_timeframe_fails() -> None:
    scope = _scope(timeframe="M5")
    candidate = _candidate(timeframe="H1")
    assert verify_live_candidate_matches_scope(scope, candidate) is False


def test_wrong_setup_family_fails() -> None:
    scope = _scope(setup_family="spread_pressure")
    candidate = _candidate(setup_family="liquidity_sweep")
    assert verify_live_candidate_matches_scope(scope, candidate) is False


def test_wrong_action_fails_for_a_single_sided_scope() -> None:
    scope = _scope(allowed_action_scope="SELL")
    buy_candidate = _candidate(action="BUY")
    sell_candidate = _candidate(action="SELL")
    assert verify_live_candidate_matches_scope(scope, buy_candidate) is False
    assert verify_live_candidate_matches_scope(scope, sell_candidate) is True


def test_both_action_scope_accepts_either_direction() -> None:
    scope = _scope(allowed_action_scope="BOTH")
    assert verify_live_candidate_matches_scope(scope, _candidate(action="BUY")) is True
    assert verify_live_candidate_matches_scope(scope, _candidate(action="SELL")) is True


def test_unrelated_accepted_hypothesis_scope_cannot_claim_an_unrelated_candidate() -> None:
    # Two distinct scopes (as if from two distinct ACCEPTED hypotheses) --
    # neither can claim a candidate that matches only the other.
    eurusd_scope = _scope(symbol="EURUSD", timeframe="H1", setup_family="liquidity_sweep")
    xauusd_scope = _scope(symbol="XAUUSD", timeframe="M5", setup_family="spread_pressure")
    xauusd_candidate = _candidate(symbol="XAUUSD", timeframe="M5", setup_family="spread_pressure")
    assert verify_live_candidate_matches_scope(xauusd_scope, xauusd_candidate) is True
    assert verify_live_candidate_matches_scope(eurusd_scope, xauusd_candidate) is False


def test_matcher_rejects_wrong_types() -> None:
    scope = _scope()
    candidate = _candidate()
    with pytest.raises(TypeError):
        verify_live_candidate_matches_scope(scope.to_payload(), candidate)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        verify_live_candidate_matches_scope(scope, candidate.immutable_payload)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 16-19: no TradePlan invented, no risk decision, no execution
# authorization, no MT5/broker/network path anywhere in this module
# ---------------------------------------------------------------------------


def test_matcher_never_invents_a_trade_plan_or_risk_decision() -> None:
    import inspect

    import trademind.hypothesis_live_candidate_matching as module

    source = inspect.getsource(module)
    for forbidden_call in (
        "TradePlan(",
        "EntryOrder(",
        "evaluate_risk(",
        "RiskDecision(",
        "SizedOrder(",
        "order_send(",
        "OrderSend(",
    ):
        assert forbidden_call not in source, forbidden_call


def test_matcher_module_never_imports_broker_network_or_mt5() -> None:
    import ast
    import inspect

    import trademind.hypothesis_live_candidate_matching as module

    source = inspect.getsource(module)
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    forbidden = {
        "requests",
        "urllib",
        "socket",
        "http",
        "MetaTrader5",
        "trademind.risk_manager",
        "trademind.mt5_risk_adapter",
        "trademind.signal_to_risk_bridge",
    }
    assert not (imported & forbidden), imported & forbidden


def test_matcher_result_is_a_plain_bool_not_a_decision_object() -> None:
    scope = _scope()
    candidate = _candidate()
    result = verify_live_candidate_matches_scope(scope, candidate)
    assert type(result) is bool
