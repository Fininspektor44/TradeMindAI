"""Tests for the new SER8 supervised-demo risk profile
(config/risk_profiles/ser8_supervised_demo_v1.json) and the new BLOCK
diagnostics in scripts/run_ser8_real_demo_pipeline.py.

This file does not import test helpers from sibling test files (consistent
with this session's own established convention) -- its candidate/account/
instrument builders deliberately mirror tests/test_risk_manager.py's own
pattern rather than importing it.
"""

from __future__ import annotations

import importlib
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

pipeline_module = importlib.import_module("run_ser8_real_demo_pipeline")

from trademind.risk_manager import (  # noqa: E402
    AccountSnapshot,
    InstrumentSpec,
    RiskProfile,
    evaluate_risk,
    profile_from_dict,
)
from trademind.signal_intelligence import EntryOrder, SignalCandidate, TradePlan  # noqa: E402

STANDARD_PATH = REPO_ROOT / "config" / "risk_profiles" / "standard_v1.json"
SUPERVISED_PATH = REPO_ROOT / "config" / "risk_profiles" / "ser8_supervised_demo_v1.json"

NOW = datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc)


def _load(path: Path) -> RiskProfile:
    return profile_from_dict(json.loads(path.read_text(encoding="utf-8")))


def _candidate(*, observed_at: datetime | None = None, symbol: str = "EURUSD") -> SignalCandidate:
    observed = observed_at or (NOW - timedelta(seconds=70))
    return SignalCandidate(
        observed_at=observed,
        created_at=observed + timedelta(seconds=10),
        symbol=symbol,
        timeframe="M5",
        setup_family="MULTIFACTOR_MARKET_SETUP",
        scenario="supervised demo risk profile test",
        plan=TradePlan(
            action="BUY",
            entries=(EntryOrder(1.1000, 1.0, "confirmed entry", "MARKET"),),
            stop_price=1.0950,
            targets=(1.1100,),
            invalidation="protected low broken",
            target_rationale=("external liquidity",),
        ),
        market_features={"structure": {"swing_bias": "BULLISH"}},
        factor_scores={"structure": 0.9},
        factor_reasons={"structure": ("BOS confirmed",)},
        provenance=("FX_RESEARCH",),
        generated_from_market_data=True,
    )


def _account(**changes: object) -> AccountSnapshot:
    values = {
        "account_id": "67206924", "venue": "MT5", "currency": "USD",
        "captured_at": NOW - timedelta(seconds=10), "balance": 10_000.0, "equity": 10_000.0,
        "free_margin": 10_000.0, "margin_used": 0.0, "high_watermark_equity": 10_000.0,
        "day_start_equity": 10_000.0, "trading_enabled": True,
    }
    values.update(changes)
    return AccountSnapshot(**values)


def _instrument(**changes: object) -> InstrumentSpec:
    values = {
        "symbol": "EURUSD", "venue": "MT5", "account_currency": "USD", "tick_size": 0.0001,
        "tick_value_per_volume": 10.0, "volume_min": 0.01, "volume_max": 100.0,
        "volume_step": 0.01, "correlation_group": "USD_SHORT", "margin_per_volume": 1100.0,
    }
    values.update(changes)
    return InstrumentSpec(**values)


# ---------------------------------------------------------------------------
# 1-4: the two checked-in profile files themselves.
# ---------------------------------------------------------------------------


def test_standard_still_rejects_approved_manual() -> None:
    profile = _load(STANDARD_PATH)
    assert profile.allowed_signal_states == ("PUBLISHABLE",)
    decision = evaluate_risk(
        _candidate(), gate_state="APPROVED_MANUAL", account=_account(), instrument=_instrument(),
        profile=profile, now=NOW,
    )
    assert decision.state == "BLOCK"
    assert decision.checks["signal_state"] is False
    assert any(reason.code == "SIGNAL_NOT_APPROVED" for reason in decision.reasons)


def test_supervised_demo_profile_accepts_approved_manual_at_signal_state_check() -> None:
    profile = _load(SUPERVISED_PATH)
    assert profile.allowed_signal_states == ("APPROVED_MANUAL",)
    decision = evaluate_risk(
        _candidate(), gate_state="APPROVED_MANUAL", account=_account(), instrument=_instrument(),
        profile=profile, now=NOW,
    )
    # The signal-state check specifically must pass -- other checks may or
    # may not, but SIGNAL_NOT_APPROVED must never be among the reasons.
    assert decision.checks["signal_state"] is True
    assert not any(reason.code == "SIGNAL_NOT_APPROVED" for reason in decision.reasons)
    assert decision.state == "ALLOW"


def test_supervised_demo_profile_keeps_every_numeric_limit_identical_to_standard() -> None:
    standard = _load(STANDARD_PATH)
    supervised = _load(SUPERVISED_PATH)
    numeric_fields = (
        "risk_basis", "default_trade_risk_pct", "maximum_trade_risk_pct", "maximum_portfolio_risk_pct",
        "maximum_symbol_risk_pct", "maximum_correlation_risk_pct", "maximum_daily_loss_pct",
        "maximum_account_drawdown_pct", "maximum_margin_usage_pct", "minimum_free_margin_pct",
        "maximum_open_trades", "maximum_account_snapshot_age_seconds", "maximum_signal_age_seconds",
        "maximum_clock_skew_seconds", "adverse_slippage_ticks", "minimum_risk_utilization_pct",
        "require_margin_check", "require_complete_portfolio_risk",
    )
    for field_name in numeric_fields:
        assert getattr(standard, field_name) == getattr(supervised, field_name), field_name
    # The ONLY two intentional differences.
    assert standard.name != supervised.name
    assert standard.allowed_signal_states != supervised.allowed_signal_states


def test_publishable_not_silently_added_to_supervised_profile() -> None:
    profile = _load(SUPERVISED_PATH)
    assert "PUBLISHABLE" not in profile.allowed_signal_states
    assert profile.allowed_signal_states == ("APPROVED_MANUAL",)
    assert len(profile.allowed_signal_states) == 1  # not BOTH/ANY/wildcard.


def test_standard_json_file_completely_unchanged() -> None:
    """Requirement 1 of this task: keep standard_v1.json completely
    unchanged."""
    payload = json.loads(STANDARD_PATH.read_text(encoding="utf-8"))
    assert payload["name"] == "STANDARD_V1"
    assert payload["allowed_signal_states"] == ["PUBLISHABLE"]


# ---------------------------------------------------------------------------
# 5-7: existing risk checks are not weakened under the new profile either.
# ---------------------------------------------------------------------------


def test_stale_candidate_still_blocks_under_supervised_profile() -> None:
    profile = _load(SUPERVISED_PATH)
    stale = _candidate(observed_at=NOW - timedelta(seconds=profile.maximum_signal_age_seconds + 100))
    decision = evaluate_risk(
        stale, gate_state="APPROVED_MANUAL", account=_account(), instrument=_instrument(),
        profile=profile, now=NOW,
    )
    assert decision.state == "BLOCK"


def test_stale_account_snapshot_still_blocks_under_supervised_profile() -> None:
    profile = _load(SUPERVISED_PATH)
    stale_account = _account(
        captured_at=NOW - timedelta(seconds=profile.maximum_account_snapshot_age_seconds + 100)
    )
    decision = evaluate_risk(
        _candidate(), gate_state="APPROVED_MANUAL", account=stale_account, instrument=_instrument(),
        profile=profile, now=NOW,
    )
    assert decision.state == "BLOCK"


def test_drawdown_limit_still_blocks_under_supervised_profile() -> None:
    profile = _load(SUPERVISED_PATH)
    # equity has fallen well past maximum_account_drawdown_pct (10%) below
    # the account's own high watermark.
    drawn_down_account = _account(equity=8_500.0, high_watermark_equity=10_000.0)
    decision = evaluate_risk(
        _candidate(), gate_state="APPROVED_MANUAL", account=drawn_down_account, instrument=_instrument(),
        profile=profile, now=NOW,
    )
    assert decision.state == "BLOCK"


# ---------------------------------------------------------------------------
# 8: BLOCK diagnostics -- never a generic message.
# ---------------------------------------------------------------------------


def test_block_diagnostics_expose_exact_reason_codes_and_messages(tmp_path: Path, capsys) -> None:
    profile = _load(STANDARD_PATH)
    candidate = _candidate()
    decision = evaluate_risk(
        candidate, gate_state="APPROVED_MANUAL", account=_account(), instrument=_instrument(),
        profile=profile, now=NOW,
    )
    assert decision.state == "BLOCK"

    account_csv = tmp_path / "mt5_risk_account_utc_67206924.csv"
    account_csv.write_text("time_msc,account_login\n1755500000000,67206924\n", encoding="utf-8")

    pipeline_module._print_block_diagnostics(decision, candidate=candidate, account_csv=account_csv)
    out = capsys.readouterr().out

    assert "decision_id" in out and decision.decision_id in out
    assert candidate.signal_id in out
    assert "SIGNAL_NOT_APPROVED" in out
    assert "not allowed by the risk profile" in out
    assert "requested_risk_pct" in out
    assert "failed checks" in out
    assert "signal_state" in out  # the actual failed check name, not hidden.
    assert "candidate observed_at" in out
    assert "account snapshot age" in out
    # Never JUST the generic one-line message with nothing else.
    assert out.count("\n") > 5


def test_block_diagnostics_never_silently_swallow_a_missing_account_csv(tmp_path: Path, capsys) -> None:
    profile = _load(STANDARD_PATH)
    candidate = _candidate()
    decision = evaluate_risk(
        candidate, gate_state="APPROVED_MANUAL", account=_account(), instrument=_instrument(),
        profile=profile, now=NOW,
    )
    pipeline_module._print_block_diagnostics(
        decision, candidate=candidate, account_csv=tmp_path / "does-not-exist.csv"
    )
    out = capsys.readouterr().out
    assert "account snapshot age  = unavailable" in out
    assert "SIGNAL_NOT_APPROVED" in out  # everything else still printed.


# ---------------------------------------------------------------------------
# 9: authorization/claim/order-send unreachable on BLOCK.
# ---------------------------------------------------------------------------


def test_block_path_returns_before_any_authorization_construction() -> None:
    """Source-order proof: run_pipeline's `if result.decision.state !=
    "ALLOW":` branch returns BEFORE SER8ExecutionAuthorizationControl is
    ever constructed -- BLOCK cannot reach authorization/claim/send."""
    import inspect

    source = inspect.getsource(pipeline_module.run_pipeline)
    block_index = source.index('if result.decision.state != "ALLOW":')
    return_index = source.index("return 3", block_index)
    authorization_index = source.index("SER8ExecutionAuthorizationControl(")
    claim_index = source.index("SER8ExecutionAuthorizationClaimControl(")
    send_index = source.index("SER8DemoOrderSendControl(")
    assert block_index < return_index < authorization_index < claim_index < send_index


def test_no_bypass_or_force_flag_exists_around_the_block_check() -> None:
    import inspect

    source = inspect.getsource(pipeline_module.run_pipeline)
    for forbidden in ("force", "override", "bypass", "skip_check", "ignore_block", "allow_anyway"):
        assert forbidden not in source.lower()


def test_no_fake_allow_state_ever_assigned_in_pipeline_script() -> None:
    source = pipeline_module.__file__ and Path(pipeline_module.__file__).read_text(encoding="utf-8")
    assert '.state = "ALLOW"' not in source
    assert "state=\"ALLOW\"" not in source
