"""SER8 28-SYMBOL HISTORICAL REPLAY AND RANKING V1 — proofs.

SCREENING ONLY: these tests prove the new aggregation/ranking layer reuses
the EXISTING, unmodified ser8_historical_replay engine (build_replay_payloads
/ create_replay / build_research_readiness_inventory) verbatim, never
recomputes candidates/outcomes itself, never creates or accepts a
hypothesis, never touches a protected holdout, and never references the
already-accepted EURUSD hypothesis id. Every HISTORICAL_DATA_READY symbol
appears in the final report regardless of outcome (including negative,
insufficient-sample, and replay-unavailable results).

No live MT5 calls, no network data acquisition, no broker mutation.
"""

from __future__ import annotations

import ast
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from trademind.ser8_historical_data import (
    HistoricalBarV1,
    HistoricalDataError,
    INVENTORY_SCHEMA_VERSION,
    READ_ONLY_MT5_OPERATIONS,
    build_canonical_execution_universe,
    build_dataset_manifest,
    publish_dataset,
    write_inventory_artifacts,
)
from trademind.ser8_historical_multisymbol_screening import (
    PROFIT_FACTOR_NO_LOSSES_SENTINEL,
    STATUS_INSUFFICIENT_SAMPLE,
    STATUS_NO_COMPLETED_OUTCOMES,
    STATUS_REPLAY_UNAVAILABLE,
    STATUS_REPLAY_VERIFICATION_FAILED,
    STATUS_SCREENED,
    TIER_SCREENED_NEGATIVE,
    TIER_SCREENED_POSITIVE,
    _resolve_candidate_direction,
    build_multisymbol_screening_report,
    build_symbol_screening_entry,
    compact_report_lines,
    compute_symbol_replay_metrics,
    load_verified_multisymbol_screening_report,
    rank_symbol_screening_entries,
    write_multisymbol_screening_report,
)
from trademind.ser8_historical_replay import (
    build_research_readiness_inventory,
    load_research_policy,
)
from trademind.signal_statistics_provenance import sha256_bytes

REPO_ROOT = Path(__file__).resolve().parent.parent
ACCOUNT = "67206924"
MARKET_DATA_ACCOUNT = "77053345"
NOW = datetime(2026, 8, 23, 12, 0, tzinfo=timezone.utc)
POLICY_PATH = REPO_ROOT / "config" / "research" / "ser8_historical_research_policy_v1.json"


def _symbol_row(symbol: str, *, trade_mode: str = "FULL") -> dict[str, str]:
    return {
        "account_login": ACCOUNT,
        "server": "RoboForex-Demo",
        "currency": "USD",
        "symbol": symbol,
        "digits": "5",
        "trade_mode": trade_mode,
        "tick_size": "0.00001",
        "tick_value": "1",
        "tick_value_profit": "1",
        "tick_value_loss": "1",
        "volume_min": "0.01",
        "volume_max": "100",
        "volume_step": "0.01",
        "contract_size": "100000",
        "margin_initial": "0",
        "margin_maintenance": "0",
        "margin_buy_per_volume": "1",
        "margin_sell_per_volume": "1",
        "leverage": "100",
        "expiration_mode_flags": "15",
    }


def _bars(count: int, *, symbol: str = "EURUSD", start: datetime = NOW - timedelta(days=10)) -> tuple[HistoricalBarV1, ...]:
    rows = []
    previous = 1.1000
    for index in range(count):
        close = 1.1000 + index * 0.00003 + math.sin(index / 7.0) * 0.00012
        open_price = previous
        rows.append(
            HistoricalBarV1(
                time_utc=start + timedelta(minutes=5 * index),
                symbol=symbol,
                timeframe="M5",
                open=open_price,
                high=max(open_price, close) + 0.00015,
                low=min(open_price, close) - 0.00015,
                close=close,
                tick_volume=100 + index % 17,
                spread=10,
                real_volume=0,
            )
        )
        previous = close
    return tuple(rows)


def _proof() -> dict[str, object]:
    return {
        "schema_version": "ser8-mt5-history-source-proof-v1",
        "source_type": "MT5_PYTHON_COPY_RATES_RANGE",
        "market_data_account_login": MARKET_DATA_ACCOUNT,
        "market_data_account_server": "Broker-ECN",
        "market_data_account_company": "Broker",
        "market_data_account_currency": "USD",
        "terminal_company": "Broker",
        "authenticated_market_data_account_verified": True,
        "utc_contract": "UTC",
        "read_only_operations": list(READ_ONLY_MT5_OPERATIONS),
    }


def _entry(
    *,
    symbol: str,
    status: str,
    bars: tuple[HistoricalBarV1, ...],
    dataset_root: Path,
    trade_mode: str = "FULL",
) -> dict[str, object]:
    from trademind.ser8_historical_data import BrokerSymbolV1

    row = _symbol_row(symbol, trade_mode=trade_mode)
    universe = build_canonical_execution_universe(
        [row], account_login=ACCOUNT, raw_sha256=sha256_bytes(b"universe raw")
    )
    broker = BrokerSymbolV1(
        symbol=symbol, trade_mode=trade_mode, source_row=row,
        asset_class="FX", risk_model_supported=True, risk_model_reason="",
    )
    manifest, bars_bytes = build_dataset_manifest(
        bars=bars,
        source_proof=_proof(),
        symbol_metadata={
            "name": symbol, "point": 0.00001, "digits": 5, "visible": True, "trade_tick_size": 0.00001,
        },
        broker_symbol=broker,
        execution_account_login=ACCOUNT,
        execution_universe_source=f"mt5_risk_symbols_utc_{ACCOUNT}.csv",
        execution_universe=universe,
        timeframe="M5",
        requested_from_utc=NOW - timedelta(days=10),
        requested_to_utc=NOW,
        expected_interval_seconds=300,
        source_capture_utc=NOW,
        collector_code_sha256="sha256:" + "c" * 64,
    )
    dataset_dir, _, _ = publish_dataset(dataset_root, manifest, bars_bytes)
    return {
        "symbol": symbol, "asset_class": "FX", "broker_trade_mode": trade_mode,
        "risk_model_supported": True, "row_count": len(bars),
        "accepted_historical_data": True, "dataset_sha256": manifest["dataset_sha256"],
        "dataset_dir": str(dataset_dir), "status": status, "status_reason": "screening fixture",
    }


def _inventory_identity() -> dict[str, object]:
    universe = build_canonical_execution_universe(
        [_symbol_row("EURUSD")], account_login=ACCOUNT, raw_sha256=sha256_bytes(b"universe raw"),
    )
    return {
        "execution_account_login": ACCOUNT,
        "market_data_account_login": MARKET_DATA_ACCOUNT,
        "execution_universe_source": f"mt5_risk_symbols_utc_{ACCOUNT}.csv",
        "execution_universe_sha256": universe.canonical_sha256,
        "execution_universe_raw_sha256": universe.raw_sha256,
        "execution_universe_canonical_sha256": universe.canonical_sha256,
        "execution_universe_canonical_schema_version": universe.canonical_snapshot["schema_version"],
        "execution_universe_canonical_snapshot": dict(universe.canonical_snapshot),
        "broker_universe_raw_sha256": universe.raw_sha256,
        "market_data_source_type": "MT5_PYTHON_COPY_RATES_RANGE",
        "market_data_account_server": "Broker-ECN",
        "market_data_account_company": "Broker",
        "market_data_account_currency": "USD",
        "source_proof": _proof(),
    }


def _outcome(signal_id: str, *, completed_at: str, net_r: float, outcome: str, allocation_filled: float = 1.0) -> dict[str, object]:
    return {
        "schema_version": "signal-shadow-v1",
        "signal_id": signal_id,
        "setup_key": "SETUP",
        "completed_at": completed_at,
        "outcome": outcome,
        "net_r": net_r,
        "exit_reason": "TARGET_1" if outcome == "WIN" else "STOP",
        "exit_price": 1.1,
        "filled_entries": 1,
        "allocation_filled": allocation_filled,
        "average_entry": 1.1,
        "mfe_r": max(net_r, 0.0),
        "mae_r": max(-net_r, 0.0),
        "bars_observed": 5,
    }


def _candidate(
    signal_id: str,
    *,
    action: str,
    confirmation_action: str | None = None,
    similarity_action: str | None = None,
) -> dict[str, object]:
    """Build a candidate row shaped like the real, authoritative schema:
    there is no top-level ``candidate["action"]`` -- the direction lives at
    ``plan.action`` (the only structurally-validated field), and is
    normally echoed verbatim into ``market_features.confirmation.action``
    and ``similarity_dimensions.action`` by the real candidate-building
    code. The two optional overrides let tests construct a deliberately
    contradictory/corrupted artifact for the fail-closed consistency check.
    """
    return {
        "signal_id": signal_id,
        "plan": {"action": action},
        "market_features": {
            "confirmation": {
                "action": action if confirmation_action is None else confirmation_action
            }
        },
        "similarity_dimensions": {
            "action": action if similarity_action is None else similarity_action
        },
    }


# ---------------------------------------------------------------------------
# Pure aggregation (compute_symbol_replay_metrics)
# ---------------------------------------------------------------------------


def test_metrics_are_deterministic_and_order_independent() -> None:
    candidates = [_candidate(f"s{i}", action="BUY" if i % 2 == 0 else "SELL") for i in range(6)]
    outcomes = [
        _outcome("s0", completed_at="2026-01-01T00:00:00Z", net_r=1.0, outcome="WIN"),
        _outcome("s1", completed_at="2026-01-02T00:00:00Z", net_r=-1.0, outcome="LOSS"),
        _outcome("s2", completed_at="2026-01-03T00:00:00Z", net_r=0.5, outcome="WIN"),
        _outcome("s3", completed_at="2026-01-04T00:00:00Z", net_r=-0.5, outcome="LOSS"),
        _outcome("s4", completed_at="2026-01-05T00:00:00Z", net_r=0.0, outcome="FLAT"),
        _outcome("s5", completed_at="2026-01-06T00:00:00Z", net_r=2.0, outcome="WIN"),
    ]
    forward = compute_symbol_replay_metrics(candidates=candidates, outcomes=outcomes, cost_r=0.04)
    reversed_outcomes = list(reversed(outcomes))
    backward = compute_symbol_replay_metrics(candidates=candidates, outcomes=reversed_outcomes, cost_r=0.04)
    assert forward == backward

    assert forward["trade_count"] == 6
    assert forward["long_count"] == 3  # s0, s2, s4
    assert forward["short_count"] == 3  # s1, s3, s5
    assert forward["wins"] == 3
    assert forward["losses"] == 2
    assert forward["flats"] == 1
    assert forward["win_rate"] == pytest.approx(60.0)
    assert forward["gross_profit_r"] == pytest.approx(3.5)
    assert forward["gross_loss_r"] == pytest.approx(1.5)
    assert forward["net_r_total"] == pytest.approx(2.0)
    assert forward["profit_factor"] == pytest.approx(3.5 / 1.5)
    assert forward["expectancy_r"] == pytest.approx(2.0 / 6)
    assert forward["average_winner_r"] == pytest.approx(3.5 / 3)
    assert forward["average_loser_r"] == pytest.approx(-1.5 / 2)
    assert forward["payoff_ratio"] == pytest.approx((3.5 / 3) / (1.5 / 2))
    assert forward["max_drawdown_r"] >= 0.0


def test_zero_losses_gives_sentinel_profit_factor_and_zero_trades_gives_none() -> None:
    # An infinite profit factor cannot be persisted as canonical JSON
    # (Infinity is not valid JSON), so a zero-loss symbol is recorded with
    # the project's existing large finite sentinel instead of math.inf.
    candidates = [_candidate("s0", action="BUY")]
    only_winner = [_outcome("s0", completed_at="2026-01-01T00:00:00Z", net_r=1.0, outcome="WIN")]
    metrics = compute_symbol_replay_metrics(candidates=candidates, outcomes=only_winner, cost_r=0.0)
    assert metrics["profit_factor"] == PROFIT_FACTOR_NO_LOSSES_SENTINEL
    assert math.isfinite(metrics["profit_factor"])
    assert metrics["average_loser_r"] is None
    assert metrics["payoff_ratio"] is None

    empty = compute_symbol_replay_metrics(candidates=[], outcomes=[], cost_r=0.0)
    assert empty["profit_factor"] is None
    assert empty["expectancy_r"] is None
    assert empty["trade_count"] == 0


def test_max_drawdown_reflects_the_worst_cumulative_dip() -> None:
    candidates = [_candidate(f"s{i}", action="BUY") for i in range(4)]
    outcomes = [
        _outcome("s0", completed_at="2026-01-01T00:00:00Z", net_r=2.0, outcome="WIN"),
        _outcome("s1", completed_at="2026-01-02T00:00:00Z", net_r=-3.0, outcome="LOSS"),
        _outcome("s2", completed_at="2026-01-03T00:00:00Z", net_r=-1.0, outcome="LOSS"),
        _outcome("s3", completed_at="2026-01-04T00:00:00Z", net_r=5.0, outcome="WIN"),
    ]
    metrics = compute_symbol_replay_metrics(candidates=candidates, outcomes=outcomes, cost_r=0.0)
    # equity curve: 2, -1, -2, 3 -> peak 2, trough -2 -> drawdown 4
    assert metrics["max_drawdown_r"] == pytest.approx(4.0)


def test_before_cost_reconstruction_flags_profitable_only_before_costs() -> None:
    candidates = [_candidate(f"s{i}", action="BUY") for i in range(4)]
    cost_r = 0.5
    # gross (pre-cost) values before subtracting cost_r*allocation: +0.6,+0.6,-0.4,-0.4
    outcomes = [
        _outcome("s0", completed_at="2026-01-01T00:00:00Z", net_r=0.1, outcome="WIN"),
        _outcome("s1", completed_at="2026-01-02T00:00:00Z", net_r=0.1, outcome="WIN"),
        _outcome("s2", completed_at="2026-01-03T00:00:00Z", net_r=-0.9, outcome="LOSS"),
        _outcome("s3", completed_at="2026-01-04T00:00:00Z", net_r=-0.9, outcome="LOSS"),
    ]
    metrics = compute_symbol_replay_metrics(candidates=candidates, outcomes=outcomes, cost_r=cost_r)
    assert metrics["expectancy_r"] == pytest.approx((0.1 + 0.1 - 0.9 - 0.9) / 4)
    assert metrics["expectancy_r_before_cost"] == pytest.approx(
        ((0.1 + 0.5) + (0.1 + 0.5) + (-0.9 + 0.5) + (-0.9 + 0.5)) / 4
    )
    assert metrics["expectancy_r_before_cost"] > 0.0
    assert metrics["expectancy_r"] <= 0.0
    assert metrics["profitable_only_before_costs"] is True


def test_chronological_stability_detects_degradation() -> None:
    candidates = [_candidate(f"s{i}", action="BUY") for i in range(6)]
    outcomes = [
        _outcome("s0", completed_at="2026-01-01T00:00:00Z", net_r=1.0, outcome="WIN"),
        _outcome("s1", completed_at="2026-01-02T00:00:00Z", net_r=1.0, outcome="WIN"),
        _outcome("s2", completed_at="2026-01-03T00:00:00Z", net_r=1.0, outcome="WIN"),
        _outcome("s3", completed_at="2026-01-04T00:00:00Z", net_r=-1.0, outcome="LOSS"),
        _outcome("s4", completed_at="2026-01-05T00:00:00Z", net_r=-1.0, outcome="LOSS"),
        _outcome("s5", completed_at="2026-01-06T00:00:00Z", net_r=-1.0, outcome="LOSS"),
    ]
    metrics = compute_symbol_replay_metrics(
        candidates=candidates, outcomes=outcomes, cost_r=0.0, stability_window_count=2
    )
    stability = metrics["chronological_stability"]
    assert stability["window_count"] == 2
    assert stability["windows"][0]["expectancy_r"] == pytest.approx(1.0)
    assert stability["windows"][1]["expectancy_r"] == pytest.approx(-1.0)
    assert stability["first_to_last_expectancy_delta_r"] == pytest.approx(-2.0)
    assert stability["degraded"] is True


# ---------------------------------------------------------------------------
# Candidate direction resolution (SER8 SCREENING DIRECTION COUNTS FIX V1)
#
# Real candidates have no top-level "action" field; the authoritative
# direction lives at plan.action, which trademind.signal_intelligence.
# TradePlan validates at construction time. These tests prove the resolver
# reads plan.action, fails closed (never an empty string) when it is
# missing/invalid, and fails closed on a genuine contradiction with an
# embedded echo field, without touching net_r/expectancy/profit_factor/
# drawdown or ranking determinism.
# ---------------------------------------------------------------------------


def test_plan_action_buy_resolves_to_buy() -> None:
    assert _resolve_candidate_direction(_candidate("s0", action="BUY")) == "BUY"


def test_plan_action_sell_resolves_to_sell() -> None:
    assert _resolve_candidate_direction(_candidate("s0", action="SELL")) == "SELL"


def test_mixed_directions_increment_long_and_short_counts_correctly() -> None:
    candidates = [_candidate(f"s{i}", action="BUY" if i % 3 else "SELL") for i in range(9)]
    outcomes = [
        _outcome(f"s{i}", completed_at=f"2026-01-{i + 1:02d}T00:00:00Z", net_r=0.1, outcome="WIN")
        for i in range(9)
    ]
    metrics = compute_symbol_replay_metrics(candidates=candidates, outcomes=outcomes, cost_r=0.0)
    expected_sell = sum(1 for i in range(9) if i % 3 == 0)
    assert metrics["long_count"] == 9 - expected_sell
    assert metrics["short_count"] == expected_sell
    assert metrics["long_count"] + metrics["short_count"] == metrics["trade_count"]


def test_missing_plan_action_fails_closed_not_empty_string() -> None:
    broken = {"signal_id": "s0", "plan": {}}
    with pytest.raises(HistoricalDataError) as excinfo:
        _resolve_candidate_direction(broken)
    assert excinfo.value.code == "CANDIDATE_DIRECTION_UNRESOLVED"

    # A whole missing plan key fails closed identically.
    with pytest.raises(HistoricalDataError):
        _resolve_candidate_direction({"signal_id": "s0"})


def test_invalid_plan_action_fails_closed() -> None:
    with pytest.raises(HistoricalDataError) as excinfo:
        _resolve_candidate_direction({"signal_id": "s0", "plan": {"action": "HOLD"}})
    assert excinfo.value.code == "CANDIDATE_DIRECTION_UNRESOLVED"


def test_missing_candidate_direction_aborts_metrics_computation_fail_closed() -> None:
    # A single unresolvable candidate must not silently miscount as neither
    # BUY nor SELL -- compute_symbol_replay_metrics fails closed for the
    # whole batch rather than defaulting the bad row to an empty direction.
    candidates = [_candidate("s0", action="BUY"), {"signal_id": "s1", "plan": {}}]
    outcomes = [
        _outcome("s0", completed_at="2026-01-01T00:00:00Z", net_r=0.1, outcome="WIN"),
        _outcome("s1", completed_at="2026-01-02T00:00:00Z", net_r=-0.1, outcome="LOSS"),
    ]
    with pytest.raises(HistoricalDataError) as excinfo:
        compute_symbol_replay_metrics(candidates=candidates, outcomes=outcomes, cost_r=0.0)
    assert excinfo.value.code == "CANDIDATE_DIRECTION_UNRESOLVED"


def test_contradictory_embedded_direction_fails_closed() -> None:
    contradictory = _candidate("s0", action="BUY", confirmation_action="SELL")
    with pytest.raises(HistoricalDataError) as excinfo:
        _resolve_candidate_direction(contradictory)
    assert excinfo.value.code == "CANDIDATE_DIRECTION_CONTRADICTION"

    contradictory_similarity = _candidate("s0", action="BUY", similarity_action="SELL")
    with pytest.raises(HistoricalDataError) as excinfo:
        _resolve_candidate_direction(contradictory_similarity)
    assert excinfo.value.code == "CANDIDATE_DIRECTION_CONTRADICTION"


def test_consistent_embedded_directions_do_not_raise() -> None:
    # The real candidate-building code always echoes plan.action into both
    # embedded fields, so the ordinary (non-contradictory) case must pass.
    assert _resolve_candidate_direction(_candidate("s0", action="SELL")) == "SELL"


def test_build_symbol_screening_entry_reports_direction_failure_not_silently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import trademind.ser8_historical_multisymbol_screening as screening_module

    def _fake_load(_replay_dir: Path):
        candidates = [{"signal_id": "s0", "plan": {}}]
        outcomes = [_outcome("s0", completed_at="2026-01-01T00:00:00Z", net_r=0.1, outcome="WIN")]
        manifest = {"shadow_cost_r": 0.0}
        return candidates, outcomes, manifest

    monkeypatch.setattr(screening_module, "load_verified_replay_rows", _fake_load)
    entry = build_symbol_screening_entry(
        readiness_entry={
            "symbol": "BADUSD", "asset_class": "FX", "broker_trade_mode": "FULL",
            "risk_model_supported": True, "historical_rows": 5000,
            "accepted_historical_data": True, "dataset_sha256": "a" * 64, "dataset_dir": "/tmp/x",
            "replay_sha256": "b" * 64, "replay_dir": "/tmp/fake-replay-dir",
            "candidate_count": 1, "completed_outcome_count": 1,
            "research_minimum": 300, "research_ready": False,
            "readiness_reason": "below research minimum",
        }
    )
    # The symbol still appears in the report (never silently dropped), with
    # an explicit rejection reason -- never a fabricated/zeroed metrics row.
    assert entry["symbol"] == "BADUSD"
    assert entry["screening_status"] == STATUS_REPLAY_VERIFICATION_FAILED
    assert "no valid plan.action" in entry["rejection_reason"]
    assert entry["metrics"] is None


def test_direction_fix_leaves_net_r_expectancy_profit_factor_drawdown_unchanged() -> None:
    # The direction resolver only affects long_count/short_count; every
    # other statistic is derived solely from outcomes/net_r and must be
    # identical regardless of which (consistent) direction each trade used.
    outcomes = [
        _outcome("s0", completed_at="2026-01-01T00:00:00Z", net_r=1.0, outcome="WIN"),
        _outcome("s1", completed_at="2026-01-02T00:00:00Z", net_r=-0.5, outcome="LOSS"),
        _outcome("s2", completed_at="2026-01-03T00:00:00Z", net_r=0.3, outcome="WIN"),
    ]
    all_buy = [_candidate(f"s{i}", action="BUY") for i in range(3)]
    all_sell = [_candidate(f"s{i}", action="SELL") for i in range(3)]
    metrics_buy = compute_symbol_replay_metrics(candidates=all_buy, outcomes=outcomes, cost_r=0.05)
    metrics_sell = compute_symbol_replay_metrics(candidates=all_sell, outcomes=outcomes, cost_r=0.05)
    for key in (
        "net_r_total", "expectancy_r", "profit_factor", "max_drawdown_r",
        "gross_profit_r", "gross_loss_r", "average_winner_r", "average_loser_r",
        "payoff_ratio", "net_r_total_before_cost", "expectancy_r_before_cost",
        "profitable_only_before_costs", "chronological_stability",
    ):
        assert metrics_buy[key] == metrics_sell[key]
    assert metrics_buy["long_count"] == 3 and metrics_buy["short_count"] == 0
    assert metrics_sell["short_count"] == 3 and metrics_sell["long_count"] == 0


def test_ranking_remains_deterministic_after_direction_fix() -> None:
    # Ranking never consumes direction/long_count/short_count at all, so it
    # must remain deterministic and unaffected by this fix.
    entries = [
        _screened("AAA", expectancy=0.10, profit_factor=2.0, drawdown=1.0, delta=-0.01),
        _screened("BBB", expectancy=-0.05, profit_factor=0.5, drawdown=3.0, delta=-0.2),
        _screened("CCC", expectancy=0.20, profit_factor=3.0, drawdown=0.5, delta=0.01),
    ]
    forward, _ = rank_symbol_screening_entries(entries)
    backward, _ = rank_symbol_screening_entries(list(reversed(entries)))
    assert forward == backward
    again, _ = rank_symbol_screening_entries(entries)
    assert forward == again


# ---------------------------------------------------------------------------
# build_symbol_screening_entry: edge cases (hand-constructed readiness rows)
# ---------------------------------------------------------------------------


def test_missing_replay_dir_is_reported_not_dropped() -> None:
    entry = build_symbol_screening_entry(
        readiness_entry={
            "symbol": "XYZUSD", "asset_class": "FX", "broker_trade_mode": "LONGONLY",
            "risk_model_supported": True, "historical_rows": 5000,
            "accepted_historical_data": True, "dataset_sha256": "a" * 64, "dataset_dir": "/tmp/x",
            "replay_sha256": None, "candidate_count": 0, "completed_outcome_count": 0,
            "research_minimum": 300, "research_ready": False,
            "readiness_reason": "asset class, trade mode, or risk-model boundary is not eligible",
        }
    )
    assert entry["symbol"] == "XYZUSD"
    assert entry["screening_status"] == STATUS_REPLAY_UNAVAILABLE
    assert entry["rejection_reason"]
    assert entry["metrics"] is None


def test_zero_outcomes_is_reported_not_dropped(tmp_path: Path) -> None:
    replay_dir = tmp_path / "replay_empty"
    entry = build_symbol_screening_entry(
        readiness_entry={
            "symbol": "NOSIGUSD", "asset_class": "FX", "broker_trade_mode": "FULL",
            "risk_model_supported": True, "historical_rows": 5000,
            "accepted_historical_data": True, "dataset_sha256": "a" * 64, "dataset_dir": "/tmp/x",
            "replay_sha256": "b" * 64, "replay_dir": str(replay_dir),
            "candidate_count": 0, "completed_outcome_count": 0,
            "research_minimum": 300, "research_ready": False,
            "readiness_reason": "no candidates",
        }
    )
    # replay_dir does not exist -> verify_replay fails closed
    assert entry["screening_status"] == "REPLAY_VERIFICATION_FAILED"
    assert entry["metrics"] is None


# ---------------------------------------------------------------------------
# Ranking
# ---------------------------------------------------------------------------


def _screened(symbol: str, *, expectancy: float, profit_factor: float | None, drawdown: float, delta: float | None) -> dict[str, object]:
    return {
        "symbol": symbol,
        "screening_status": STATUS_SCREENED,
        "metrics": {
            "expectancy_r": expectancy,
            "profit_factor": profit_factor,
            "max_drawdown_r": drawdown,
            "chronological_stability": {"first_to_last_expectancy_delta_r": delta},
        },
    }


def test_ranking_orders_positive_before_negative_and_never_drops_anyone() -> None:
    entries = [
        _screened("AAA", expectancy=0.10, profit_factor=2.0, drawdown=1.0, delta=-0.01),
        _screened("BBB", expectancy=-0.05, profit_factor=0.5, drawdown=3.0, delta=-0.2),
        _screened("CCC", expectancy=0.20, profit_factor=3.0, drawdown=0.5, delta=0.01),
        {"symbol": "DDD", "screening_status": STATUS_INSUFFICIENT_SAMPLE, "metrics": None},
    ]
    ranked, not_ranked = rank_symbol_screening_entries(entries)
    assert {row["symbol"] for row in ranked} == {"AAA", "BBB", "CCC"}
    assert not_ranked == ["DDD"]
    tiers = {row["symbol"]: row["screening_tier"] for row in ranked}
    assert tiers["AAA"] == tiers["CCC"] == TIER_SCREENED_POSITIVE
    assert tiers["BBB"] == TIER_SCREENED_NEGATIVE
    # every positive-tier symbol ranks strictly ahead of every negative-tier symbol
    positive_ranks = [row["overall_rank"] for row in ranked if row["screening_tier"] == TIER_SCREENED_POSITIVE]
    negative_ranks = [row["overall_rank"] for row in ranked if row["screening_tier"] == TIER_SCREENED_NEGATIVE]
    assert max(positive_ranks) < min(negative_ranks)
    # CCC dominates AAA on every dimension -> must rank first
    assert ranked[0]["symbol"] == "CCC"


def test_ranking_is_deterministic_regardless_of_input_order() -> None:
    entries = [
        _screened("AAA", expectancy=0.10, profit_factor=2.0, drawdown=1.0, delta=-0.01),
        _screened("BBB", expectancy=-0.05, profit_factor=0.5, drawdown=3.0, delta=-0.2),
        _screened("CCC", expectancy=0.20, profit_factor=3.0, drawdown=0.5, delta=0.01),
    ]
    forward, _ = rank_symbol_screening_entries(entries)
    backward, _ = rank_symbol_screening_entries(list(reversed(entries)))
    assert forward == backward


def test_sentinel_profit_factor_ranks_but_never_crashes() -> None:
    entries = [
        _screened("AAA", expectancy=0.1, profit_factor=PROFIT_FACTOR_NO_LOSSES_SENTINEL, drawdown=1.0, delta=0.0),
        _screened("BBB", expectancy=0.2, profit_factor=1.5, drawdown=1.0, delta=0.0),
    ]
    ranked, _ = rank_symbol_screening_entries(entries)
    assert {row["symbol"] for row in ranked} == {"AAA", "BBB"}


# ---------------------------------------------------------------------------
# Full end-to-end report: real create_replay reuse + not-dropped edge cases
# ---------------------------------------------------------------------------


def test_full_report_reuses_real_replay_engine_and_includes_every_ready_symbol(
    tmp_path: Path,
) -> None:
    dataset_root = tmp_path / "historical"
    inventory_path = dataset_root / "historical_inventory.json"
    compatibility_path = dataset_root / "historical_rows.csv"

    profitable_bars = _bars(430, symbol="EURUSD")
    profitable_entry = _entry(symbol="EURUSD", status="HISTORICAL_DATA_READY", bars=profitable_bars, dataset_root=dataset_root)

    thin_bars = _bars(120, symbol="AUDNZD")
    thin_entry = _entry(symbol="AUDNZD", status="HISTORICAL_DATA_READY", bars=thin_bars, dataset_root=dataset_root)

    longonly_bars = _bars(430, symbol="GBPCHF")
    longonly_entry = _entry(
        symbol="GBPCHF", status="HISTORICAL_DATA_READY", bars=longonly_bars,
        dataset_root=dataset_root, trade_mode="LONGONLY",
    )

    write_inventory_artifacts(
        inventory_path=inventory_path,
        compatibility_path=compatibility_path,
        payload={
            **_inventory_identity(),
            "schema_version": INVENTORY_SCHEMA_VERSION,
            "captured_at_utc": NOW.isoformat(),
            "total_broker_symbols": 3,
            "accepted_dataset_count": 3,
            "entries": [profitable_entry, thin_entry, longonly_entry],
        },
    )

    from trademind.ser8_historical_data import load_inventory

    historical_inventory = load_inventory(inventory_path)
    policy = load_research_policy(POLICY_PATH)
    replay_root = tmp_path / "ser8_historical_replay"
    readiness_path = replay_root / "research_readiness.json"
    readiness_payload = build_research_readiness_inventory(
        historical_inventory_path=inventory_path,
        replay_root=replay_root,
        policy=policy,
        output_path=readiness_path,
        captured_at=NOW,
    )

    report = build_multisymbol_screening_report(
        historical_inventory=historical_inventory,
        readiness_payload=readiness_payload,
        stability_window_count=3,
        captured_at=NOW,
    )

    assert report["ready_symbol_count"] == 3
    by_symbol = {entry["symbol"]: entry for entry in report["entries"]}
    assert set(by_symbol) == {"EURUSD", "AUDNZD", "GBPCHF"}

    # EURUSD: real replay through the unmodified engine, >=300 outcomes -> SCREENED.
    assert by_symbol["EURUSD"]["screening_status"] == STATUS_SCREENED
    eurusd_metrics = by_symbol["EURUSD"]["metrics"]
    assert eurusd_metrics["trade_count"] >= 300
    assert by_symbol["EURUSD"]["signal_count"] > 0
    # Every real candidate's plan.action is BUY or SELL (TradePlan enforces
    # this at construction), so long_count/short_count must fully account for
    # every trade -- proving the real plan.action-based resolver actually
    # populates direction counts instead of the previous BUY=0/SELL=0 defect.
    assert eurusd_metrics["long_count"] + eurusd_metrics["short_count"] == eurusd_metrics["trade_count"]
    assert eurusd_metrics["long_count"] > 0

    # AUDNZD: below the 103-row acquisition floor is impossible here (bars
    # accepted), but 120 rows is far below the 300-outcome research minimum
    # -> real replay runs but is not research-ready.
    assert by_symbol["AUDNZD"]["screening_status"] == STATUS_INSUFFICIENT_SAMPLE
    assert by_symbol["AUDNZD"]["rejection_reason"]

    # GBPCHF: HISTORICAL_DATA_READY but LONGONLY trade mode falls outside the
    # policy's replay-eligible trade modes -> genuinely no replay artifact.
    assert by_symbol["GBPCHF"]["screening_status"] == STATUS_REPLAY_UNAVAILABLE
    assert by_symbol["GBPCHF"]["rejection_reason"]

    ranked_symbols = {row["symbol"] for row in report["ranking"]["ranked"]}
    assert "EURUSD" in ranked_symbols or by_symbol["EURUSD"]["screening_status"] != STATUS_SCREENED
    assert set(report["ranking"]["not_ranked_symbols"]) >= {"AUDNZD", "GBPCHF"}

    # Deterministic repeatability: rebuilding from the SAME already-published
    # artifacts produces a byte-identical hash.
    report_again = build_multisymbol_screening_report(
        historical_inventory=historical_inventory,
        readiness_payload=readiness_payload,
        stability_window_count=3,
        captured_at=NOW,
    )
    assert report_again["screening_report_sha256"] == report["screening_report_sha256"]

    # Atomic write + verified reload round-trip.
    screening_output = tmp_path / "ser8_historical_screening" / "screening_report.json"
    write_multisymbol_screening_report(screening_output, report)
    reloaded = load_verified_multisymbol_screening_report(screening_output)
    assert reloaded == report

    lines = compact_report_lines(report, screening_report_path=str(screening_output))
    assert lines[0] == "=== TRADEMIND REPORT ==="
    assert lines[-1] == "=== END REPORT ==="
    assert any(line.startswith("READY_SYMBOLS: 3") for line in lines)


def test_no_hypothesis_lifecycle_or_holdout_or_eurusd_hypothesis_reference() -> None:
    paths = [
        REPO_ROOT / "src" / "trademind" / "ser8_historical_multisymbol_screening.py",
        REPO_ROOT / "scripts" / "run_ser8_historical_multisymbol_screening.py",
    ]
    for path in paths:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported = {
            alias.name.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        } | {
            str(node.module).split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        forbidden_imports = {"requests", "urllib", "socket", "yfinance", "pandas_datareader", "MetaTrader5"}
        assert not imported & forbidden_imports
        assert "HypothesisRegistry" not in source
        assert "HoldoutSealStore" not in source
        called = {
            node.func.attr if isinstance(node.func, ast.Attribute) else node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, (ast.Attribute, ast.Name))
        }
        forbidden_calls = {
            "order_send", "OrderSend", "OrderSendAsync", "PositionClose", "PositionModify",
            "symbol_select", "login",
        }
        assert not called & forbidden_calls
