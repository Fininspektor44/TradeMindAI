"""CORE_8 SMC/OTE MARKET_ONLY demo execution policy V1 tests.

Covers the five required areas: the CORE_8 allowlist, MARKET_ONLY entry
geometry, non-CORE_8 rejection, the still-mandatory demo account safety
gate, and the Risk Manager remaining the sole sizing authority.
"""

from __future__ import annotations

import ast
import inspect
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from trademind.fx_signal_adapter import build_candidate
from trademind.ser8_core8_market_only_policy import (
    CORE_8_SYMBOLS,
    MARKET_ONLY_ORDER_TYPE,
    SER8Core8PolicyError,
    is_core8_symbol,
    market_only_plan,
    verify_core8_market_only_execution,
    verify_core8_symbol,
    verify_market_only_order_types,
)
from trademind.signal_intelligence import EntryOrder, SignalCandidate, TradePlan

REPO_ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = REPO_ROOT / "src" / "trademind" / "ser8_core8_market_only_policy.py"
WORKER_PATH = REPO_ROOT / "scripts" / "run_ser8_autonomous_demo_execution.py"

EXPECTED_CORE_8 = {
    "CHFJPY",
    "EURJPY",
    "EURNZD",
    "GBPAUD",
    "GBPNZD",
    "NZDCAD",
    "NZDCHF",
    "USDJPY",
}


def _basket_plan(action: str = "BUY") -> TradePlan:
    """The existing MARKET+LIMIT+LIMIT basket geometry fx_signal_adapter
    builds today (0.50 MARKET / 0.30 LIMIT / 0.20 LIMIT)."""
    if action == "BUY":
        return TradePlan(
            action="BUY",
            entries=(
                EntryOrder(price=100.0, allocation=0.50, rationale="Market entry", order_type="MARKET"),
                EntryOrder(price=99.5, allocation=0.30, rationale="OTE 70.5%", order_type="LIMIT"),
                EntryOrder(price=99.0, allocation=0.20, rationale="OTE 79.0%", order_type="LIMIT"),
            ),
            stop_price=98.0,
            targets=(104.0, 106.0),
            invalidation="Protected swing low breaks",
            target_rationale=("Prior high or 1.5R", "External liquidity or 2R"),
        )
    return TradePlan(
        action="SELL",
        entries=(
            EntryOrder(price=100.0, allocation=0.50, rationale="Market entry", order_type="MARKET"),
            EntryOrder(price=100.5, allocation=0.30, rationale="OTE 70.5%", order_type="LIMIT"),
            EntryOrder(price=101.0, allocation=0.20, rationale="OTE 79.0%", order_type="LIMIT"),
        ),
        stop_price=102.0,
        targets=(96.0, 94.0),
        invalidation="Protected swing high breaks",
        target_rationale=("Prior low or 1.5R", "External liquidity or 2R"),
    )


def _candidate(symbol: str = "EURJPY", action: str = "BUY") -> SignalCandidate:
    observed = datetime(2026, 8, 25, 10, 0, tzinfo=timezone.utc)
    return SignalCandidate(
        observed_at=observed,
        created_at=observed + timedelta(seconds=2),
        symbol=symbol,
        timeframe="M15",
        setup_family="SMC_OTE",
        scenario="core8 market-only policy test",
        plan=_basket_plan(action),
        market_features={"structure": {"swing_bias": "BULLISH" if action == "BUY" else "BEARISH"}},
        factor_scores={"structure": 0.9},
        factor_reasons={"structure": ("BOS confirmed",)},
        provenance=("FX_RESEARCH",),
    )


def _observation_row(symbol: str = "USDJPY") -> dict[str, str]:
    """One authoritative OTE observation row, symbol-parameterized.

    Mirrors tests/test_fx_signal_adapter.py's own row fixture so the
    adapter's real, unmodified code path is exercised.
    """
    return {
        "observation_id": f"FX-{symbol}",
        "signal_time": "2026-08-05T13:30:00+00:00",
        "symbol": symbol,
        "timeframe": "M5",
        "session": "LONDON_NY_OVERLAP",
        "signal_source": "trademind.ote_engine.build_ote_signals",
        "ote_signal_id": f"{symbol}:M5:1:BUY:TOUCH_705:2",
        "action": "BUY",
        "variant": "TOUCH_705",
        "fib_ratio": "705",
        "score": "84",
        "entry_price": "1.1000",
        "bar_high": "1.1010",
        "bar_low": "1.0988",
        "atr": "0.0010",
        "anchor_price": "1.0940",
        "impulse_extreme": "1.1080",
        "impulse_atr": "2.1",
        "stop_price": "1.0938",
        "target_price": "1.1080",
        "h1_bias": "BULLISH",
        "h4_bias": "BULLISH",
        "setup_break": "BULLISH_BOS",
        "liquidity_sweep": "1",
        "fvg_aligned": "1",
        "signal_reasons": "bullish structure and volume impulse",
        "internal_bias": "BULLISH",
        "internal_reference_high": "1.1040",
        "internal_reference_low": "1.0960",
        "internal_break": "BULLISH",
        "swing_bias": "BULLISH",
        "swing_reference_high": "1.1080",
        "swing_reference_low": "1.0940",
        "swing_break": "BULLISH",
        "liquidity_reference_high": "1.1090",
        "liquidity_reference_low": "1.0950",
        "bsl_sweep": "0",
        "ssl_sweep": "1",
        "bsl_sweep_depth_atr": "0",
        "ssl_sweep_depth_atr": "0.35",
        "fvg_direction": "BULLISH",
        "fvg_size_atr": "0.42",
        "structure_event_count": "4",
        "bar_tick_volume": "1300",
        "rvol_20": "1.55",
        "volume_percentile_100": "87",
        "tick_rate_ratio_20": "1.35",
        "direction_imbalance": "0.18",
        "delta_proxy": "0.22",
        "spread_mean_points": "8",
        "spread_max_points": "11",
        "spread_ratio_20": "1.05",
        "point": "0.00001",
        "labels": "OTE|SWEEP",
    }


# --- 1. CORE_8 allowlist -------------------------------------------------


def test_core8_contains_exactly_the_eight_operationalized_symbols():
    assert set(CORE_8_SYMBOLS) == EXPECTED_CORE_8
    assert len(CORE_8_SYMBOLS) == 8


@pytest.mark.parametrize("symbol", sorted(EXPECTED_CORE_8))
def test_every_core8_symbol_is_allowed(symbol):
    assert is_core8_symbol(symbol) is True
    assert verify_core8_symbol(symbol) == symbol


def test_allowlist_is_immutable():
    assert isinstance(CORE_8_SYMBOLS, frozenset)
    with pytest.raises(AttributeError):
        CORE_8_SYMBOLS.add("EURUSD")  # type: ignore[attr-defined]


# --- 2. non-CORE_8 rejection (fail closed) -------------------------------


@pytest.mark.parametrize(
    "symbol",
    ["EURUSD", "XAUUSD", "GBPUSD", "BTCUSD", "AUDCAD", "USDCHF", "NZDUSD", "EURGBP"],
)
def test_non_core8_symbol_is_rejected(symbol):
    assert is_core8_symbol(symbol) is False
    with pytest.raises(SER8Core8PolicyError):
        verify_core8_symbol(symbol)


@pytest.mark.parametrize("symbol", ["eurjpy", "EurJpy", "EURJPY.raw", "EURJPY_M", " EURJPY", "EURJPYX"])
def test_case_and_suffix_variants_of_a_core8_symbol_are_rejected(symbol):
    # Exact match only -- no case-folding, no broker-suffix tolerance, no
    # prefix/substring matching, mirroring the demo account gate.
    assert is_core8_symbol(symbol) is False
    with pytest.raises(SER8Core8PolicyError):
        verify_core8_symbol(symbol)


@pytest.mark.parametrize("symbol", [None, "", 123, b"EURJPY", ["EURJPY"]])
def test_malformed_symbol_fails_closed(symbol):
    assert is_core8_symbol(symbol) is False
    with pytest.raises(SER8Core8PolicyError):
        verify_core8_symbol(symbol)


def test_policy_has_no_override_or_bypass_parameter():
    for function in (
        verify_core8_symbol,
        verify_market_only_order_types,
        verify_core8_market_only_execution,
        market_only_plan,
    ):
        params = set(inspect.signature(function).parameters)
        assert not (params & {"force", "override", "bypass", "allow_all", "skip_policy"})


# --- 3. MARKET_ONLY entry geometry ---------------------------------------


@pytest.mark.parametrize("action", ["BUY", "SELL"])
def test_market_only_plan_keeps_one_market_leg_at_full_allocation(action):
    result = market_only_plan(_basket_plan(action))
    assert len(result.entries) == 1
    entry = result.entries[0]
    assert entry.order_type == MARKET_ONLY_ORDER_TYPE
    assert entry.allocation == 1.0
    assert entry.price == 100.0


@pytest.mark.parametrize("action", ["BUY", "SELL"])
def test_market_only_plan_drops_every_limit_add_on(action):
    result = market_only_plan(_basket_plan(action))
    assert [item.order_type for item in result.entries] == ["MARKET"]
    assert not [item for item in result.entries if item.order_type == "LIMIT"]


@pytest.mark.parametrize("action", ["BUY", "SELL"])
def test_stop_and_primary_target_semantics_are_preserved_exactly(action):
    source = _basket_plan(action)
    result = market_only_plan(source)
    assert result.stop_price == source.stop_price
    assert result.targets == (source.targets[0],)
    assert result.action == source.action
    assert result.invalidation == source.invalidation


def test_market_only_plan_matches_the_researched_market_only_same_target_variant():
    # Binding equivalence: the executable geometry this policy produces must
    # be identical to the already-published MARKET_ONLY_SAME_TARGET research
    # variant, so the two can never silently diverge. Only human-readable
    # rationale prose differs (the research variant stamps "SCREENING ONLY",
    # which would be a false label on a real supervised demo order).
    from trademind.ser8_execution_geometry_experiment import (
        VARIANT_MARKET_SAME_TARGET,
        variant_trade_plan,
    )

    for action in ("BUY", "SELL"):
        source = _basket_plan(action)
        mine = market_only_plan(source)
        researched = variant_trade_plan(source, VARIANT_MARKET_SAME_TARGET)
        assert mine.action == researched.action
        assert mine.stop_price == researched.stop_price
        assert mine.targets == researched.targets
        assert len(mine.entries) == len(researched.entries) == 1
        assert mine.entries[0].price == researched.entries[0].price
        assert mine.entries[0].allocation == researched.entries[0].allocation
        assert mine.entries[0].order_type == researched.entries[0].order_type


def test_market_only_plan_never_mutates_the_source_plan():
    source = _basket_plan("BUY")
    before = tuple(item.as_dict() for item in source.entries)
    market_only_plan(source)
    assert tuple(item.as_dict() for item in source.entries) == before
    assert len(source.entries) == 3


def test_signal_id_is_derived_from_the_market_only_plan_not_reshaped_after():
    # The whole reason the policy is applied inside _build_plan: a
    # candidate's signal_id is a SHA-256 over its plan, so reshaping a
    # journaled candidate afterwards would mint an identity absent from
    # candidates.jsonl. A CORE_8 candidate must be BORN market-only, with
    # exactly one identity.
    candidate = build_candidate(_observation_row(symbol="USDJPY"))
    assert len(candidate.plan.entries) == 1
    rebuilt = build_candidate(_observation_row(symbol="USDJPY"))
    assert candidate.signal_id == rebuilt.signal_id  # deterministic
    # The id genuinely covers the market-only plan it was built from.
    assert candidate.as_dict()["plan"]["entries"] == [
        entry.as_dict() for entry in candidate.plan.entries
    ]


def test_plan_without_exactly_one_market_entry_fails_closed():
    limit_only = TradePlan(
        action="BUY",
        entries=(EntryOrder(price=99.0, allocation=1.0, rationale="OTE", order_type="LIMIT"),),
        stop_price=98.0,
        targets=(104.0,),
        invalidation="Protected swing low breaks",
    )
    with pytest.raises(SER8Core8PolicyError, match="exactly one MARKET entry"):
        market_only_plan(limit_only)


def test_market_only_geometry_that_would_be_invalid_fails_closed_rather_than_adjusting():
    # A BUY whose primary target sits above the basket average entry but
    # NOT above the (higher) market-only entry must raise, never silently
    # move the target to make it fit.
    plan = TradePlan(
        action="BUY",
        entries=(
            EntryOrder(price=100.0, allocation=0.50, rationale="Market entry", order_type="MARKET"),
            EntryOrder(price=90.0, allocation=0.50, rationale="Deep OTE", order_type="LIMIT"),
        ),
        stop_price=89.0,
        targets=(99.0,),
        invalidation="Protected swing low breaks",
    )
    with pytest.raises(SER8Core8PolicyError, match="MARKET_ONLY geometry is invalid"):
        market_only_plan(plan)


# --- 3b. the adapter builds CORE_8 candidates MARKET_ONLY at birth -------


@pytest.mark.parametrize("symbol", sorted(EXPECTED_CORE_8))
def test_adapter_builds_every_core8_candidate_market_only(symbol):
    candidate = build_candidate(_observation_row(symbol=symbol))
    assert candidate.symbol == symbol
    assert len(candidate.plan.entries) == 1
    entry = candidate.plan.entries[0]
    assert entry.order_type == "MARKET"
    assert entry.allocation == 1.0
    # The resulting candidate satisfies the execution policy as-journaled.
    verify_core8_market_only_execution(
        symbol=candidate.symbol,
        order_types=[item.order_type for item in candidate.plan.entries],
    )


@pytest.mark.parametrize("symbol", ["EURUSD", "XAUUSD", "GBPUSD"])
def test_adapter_leaves_non_core8_geometry_completely_unchanged(symbol):
    candidate = build_candidate(_observation_row(symbol=symbol))
    order_types = [item.order_type for item in candidate.plan.entries]
    assert order_types[0] == "MARKET"
    assert len(candidate.plan.entries) > 1, "non-CORE_8 keeps its LIMIT add-ons"
    assert "LIMIT" in order_types
    assert candidate.plan.entries[0].allocation == 0.50


def test_core8_and_non_core8_share_the_same_stop_and_primary_target():
    # The CORE_8 branch reduces the basket AFTER the researched stop/target
    # geometry has been computed, so both keep identical stop and primary
    # target numbers -- only the LIMIT legs and secondary target differ.
    core8 = build_candidate(_observation_row(symbol="USDJPY"))
    control = build_candidate(_observation_row(symbol="EURUSD"))
    assert core8.plan.stop_price == control.plan.stop_price
    assert core8.plan.targets[0] == control.plan.targets[0]
    assert len(core8.plan.targets) == 1
    assert len(control.plan.targets) == 2


def test_core8_candidate_carries_the_authoritative_ote_provenance():
    candidate = build_candidate(_observation_row(symbol="EURJPY"))
    assert "OTE_ENGINE_BUILD_OTE_SIGNALS" in candidate.provenance


# --- 4. MARKET_ONLY enforcement at the execution boundary ----------------


def test_single_market_leg_passes_execution_verification():
    verify_core8_market_only_execution(symbol="EURJPY", order_types=["MARKET"])


def test_market_plus_limit_basket_is_refused_at_the_execution_boundary():
    with pytest.raises(SER8Core8PolicyError, match="MARKET_ONLY"):
        verify_core8_market_only_execution(
            symbol="EURJPY", order_types=["MARKET", "LIMIT", "LIMIT"]
        )


@pytest.mark.parametrize("order_types", [["LIMIT"], ["STOP"], ["limit"], [None]])
def test_a_single_non_market_leg_is_refused(order_types):
    with pytest.raises(SER8Core8PolicyError):
        verify_market_only_order_types(order_types)


def test_empty_leg_list_is_refused():
    with pytest.raises(SER8Core8PolicyError, match="no legs"):
        verify_market_only_order_types([])


def test_execution_verification_checks_symbol_even_when_geometry_is_valid():
    with pytest.raises(SER8Core8PolicyError, match="not one of"):
        verify_core8_market_only_execution(symbol="EURUSD", order_types=["MARKET"])


# --- 5. sizing authority + no execution authority in this module ---------


def test_policy_module_never_sizes_anything():
    source = POLICY_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported_roots: set[str] = set()
    imported_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_roots.add(node.module.split(".")[0])
            imported_names.update(alias.name for alias in node.names)
    # Never imports or constructs the Risk Manager's sizing types, and
    # never imports risk_manager at all.
    assert "SizedOrder" not in imported_names
    assert "RiskDecision" not in imported_names
    assert "trademind.risk_manager" not in {
        node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module
    }
    # Never defines its own sizing routine or constructs a SizedOrder.
    assert "def size" not in source
    assert "SizedOrder(" not in source
    # Never performs the arithmetic that would constitute sizing.
    for banned_call in ("volume_step", "tick_value", "position_size", "lot_size"):
        assert f"{banned_call}(" not in source


def test_policy_module_has_no_execution_broker_or_network_authority():
    tree = ast.parse(POLICY_PATH.read_text(encoding="utf-8"))
    imported_roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_roots.add(node.module.split(".")[0])
    forbidden = {
        "requests", "httpx", "urllib", "socket", "subprocess", "os", "sqlite3",
        "MetaTrader5", "openai", "anthropic", "boto3",
    }
    assert imported_roots.isdisjoint(forbidden)
    source = POLICY_PATH.read_text(encoding="utf-8")
    assert "def send" not in source
    assert "open(" not in source


def test_policy_module_does_not_import_research_or_holdout_machinery():
    # The live execution path must not pull in historical dataset, replay,
    # shadow evaluation, or protected-holdout machinery.
    tree = ast.parse(POLICY_PATH.read_text(encoding="utf-8"))
    modules = {
        node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module
    }
    for forbidden in (
        "trademind.ser8_historical_data",
        "trademind.ser8_historical_replay",
        "trademind.ser8_execution_geometry_experiment",
        "trademind.signal_shadow",
        "trademind.final_holdout_evaluation",
        "trademind.final_holdout_decision_gate",
    ):
        assert forbidden not in modules


def test_policy_uses_no_ema_rsi_grid_martingale_or_averaging():
    # Word-boundary matching only -- a naive substring check would match
    # "ema" inside "schema"/"remains" and produce a meaningless failure.
    source = POLICY_PATH.read_text(encoding="utf-8").lower()
    for banned in ("ema", "rsi", "martingale", "grid", "averaging", "pyramiding"):
        assert re.search(rf"\b{banned}\b", source) is None, f"banned technique referenced: {banned}"


# --- 6. worker wiring: demo gate + policy both mandatory -----------------


def test_worker_applies_the_policy_before_risk_authorization_claim_or_send():
    source = WORKER_PATH.read_text(encoding="utf-8")
    policy_at = source.index("order_types=[entry.order_type for entry in candidate.plan.entries]")
    for later_call in (
        "evaluate_ser8_research_risk_gate(",
        "authorization_control.authorize(",
        "real_send_control.send(",
    ):
        assert policy_at < source.index(later_call), f"policy must precede {later_call}"


def test_worker_never_reshapes_a_journaled_candidate():
    # Reshaping would mint a signal_id absent from candidates.jsonl.
    source = WORKER_PATH.read_text(encoding="utf-8")
    assert "core8_market_only_candidate" not in source
    assert "replace(candidate" not in source
    assert "candidate.plan =" not in source


def test_worker_still_requires_the_demo_account_safety_gate():
    # The policy is ADDITIVE -- it never replaces the demo allowlist gate.
    source = WORKER_PATH.read_text(encoding="utf-8")
    assert "DemoAccountAllowlistV1" in source
    assert "demo_account_allowlist" in source
    assert "allowlist=allowlist" in source


def test_worker_verifies_market_only_against_the_actual_sized_orders():
    source = WORKER_PATH.read_text(encoding="utf-8")
    assert "order_types=[order.order_type for order in result.decision.orders]" in source
    send_at = source.index("real_send_control.send(")
    verify_at = source.index("order_types=[order.order_type for order in result.decision.orders]")
    assert verify_at < send_at


def test_worker_blocks_resuming_a_legacy_non_conforming_persisted_plan():
    source = WORKER_PATH.read_text(encoding="utf-8")
    assert "order_types=[leg.order_type for leg in plan.legs]" in source
    verify_at = source.index("order_types=[leg.order_type for leg in plan.legs]")
    resume_at = source.index("real_send_control.resume_plan(")
    assert verify_at < resume_at


def test_worker_reuses_the_existing_unified_executor_and_adds_no_second_one():
    source = WORKER_PATH.read_text(encoding="utf-8")
    assert "SER8DemoOrderSendControl" in source
    # No competing executor/transport class is defined in the worker itself.
    tree = ast.parse(source)
    defined_classes = {
        node.name for node in ast.walk(tree) if isinstance(node, ast.ClassDef)
    }
    for name in defined_classes:
        assert "OrderSendControl" not in name


def test_risk_manager_remains_the_sole_sizing_authority_in_the_worker():
    # The worker never constructs a SizedOrder or computes a volume itself;
    # it passes result.decision (the Risk Manager's own output) straight to
    # the executor.
    source = WORKER_PATH.read_text(encoding="utf-8")
    assert "SizedOrder(" not in source
    assert "real_send_control.send(\n            claim, result.decision, candidate" in source
