"""SER8 EXECUTION GEOMETRY A/B EXPERIMENT V1 -- proofs.

RESEARCH/SCREENING ONLY: these tests prove the four-variant geometry
experiment reuses the existing, unmodified trademind.signal_shadow.
evaluate_shadow_candidate verbatim (never a modified evaluator), reuses
trademind.ser8_historical_multisymbol_screening.compute_symbol_replay_
metrics verbatim, never regenerates signals, never touches historical
acquisition, never creates or accepts a hypothesis, never consumes a
protected holdout, and that CONTROL_BASKET exactly reproduces the
already-published replay outcomes before any variant is interpreted.

No live MT5 calls, no network data acquisition, no broker mutation.
"""

from __future__ import annotations

import ast
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from trademind.ser8_execution_geometry_experiment import (
    ALL_VARIANTS,
    STATUS_CONTROL_REPRODUCTION_FAILED,
    STATUS_EVIDENCE_UNAVAILABLE,
    VARIANT_CONTROL,
    VARIANT_MARKET_1_5R,
    VARIANT_MARKET_2_0R,
    VARIANT_MARKET_SAME_TARGET,
    build_multisymbol_geometry_experiment_report,
    build_symbol_geometry_experiment,
    compact_report_lines,
    evaluate_variant_for_symbol,
    load_verified_multisymbol_geometry_experiment_report,
    variant_trade_plan,
    verify_control_reproduces_published_outcomes,
    write_multisymbol_geometry_experiment_report,
)
from trademind.ser8_historical_data import (
    HistoricalBarV1,
    HistoricalDataError,
    INVENTORY_SCHEMA_VERSION,
    READ_ONLY_MT5_OPERATIONS,
    BrokerSymbolV1,
    build_canonical_execution_universe,
    build_dataset_manifest,
    load_inventory,
    publish_dataset,
    write_inventory_artifacts,
)
from trademind.ser8_historical_multisymbol_screening import compute_symbol_replay_metrics
from trademind.ser8_historical_replay import (
    build_research_readiness_inventory,
    load_research_policy,
)
from trademind.signal_intelligence import EntryOrder, SignalCandidate, TradePlan
from trademind.signal_shadow import Bar, evaluate_shadow_candidate
from trademind.signal_statistics_provenance import sha256_bytes

REPO_ROOT = Path(__file__).resolve().parent.parent
ACCOUNT = "67206924"
MARKET_DATA_ACCOUNT = "77053345"
NOW = datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc)
POLICY_PATH = REPO_ROOT / "config" / "research" / "ser8_historical_research_policy_v1.json"


# ---------------------------------------------------------------------------
# Direct TradePlan/EntryOrder fixtures for pure variant-geometry unit tests
# ---------------------------------------------------------------------------


def _basket_plan(*, action: str) -> TradePlan:
    """A realistic MARKET(0.50) + LIMIT(0.30) + LIMIT(0.20) basket, matching
    trademind.fx_signal_adapter._build_plan's staged-entry shape."""
    if action == "BUY":
        entries = (
            EntryOrder(price=1.1000, allocation=0.50, rationale="market", order_type="MARKET"),
            EntryOrder(price=1.0990, allocation=0.30, rationale="ote1", order_type="LIMIT"),
            EntryOrder(price=1.0985, allocation=0.20, rationale="ote2", order_type="LIMIT"),
        )
        stop_price = 1.0950
        targets = (1.1080, 1.1120)
    else:
        entries = (
            EntryOrder(price=1.1000, allocation=0.50, rationale="market", order_type="MARKET"),
            EntryOrder(price=1.1010, allocation=0.30, rationale="ote1", order_type="LIMIT"),
            EntryOrder(price=1.1015, allocation=0.20, rationale="ote2", order_type="LIMIT"),
        )
        stop_price = 1.1050
        targets = (1.0920, 1.0880)
    return TradePlan(
        action=action,
        entries=entries,
        stop_price=stop_price,
        targets=targets,
        invalidation="structure invalidation",
        target_rationale=("1.5R", "2.0R"),
    )


def _candidate(*, action: str, plan: TradePlan | None = None, observed_at: datetime = NOW) -> SignalCandidate:
    return SignalCandidate(
        observed_at=observed_at,
        created_at=observed_at + timedelta(seconds=1),
        symbol="EURUSD",
        timeframe="M5",
        setup_family="LIQUIDITY_SWEEP_OTE",
        scenario="TEST",
        plan=plan or _basket_plan(action=action),
        market_features={},
        factor_scores={},
        factor_reasons={},
        provenance=("TEST",),
    )


# ---------------------------------------------------------------------------
# variant_trade_plan: geometry proofs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("action", ["BUY", "SELL"])
def test_market_only_same_target_has_one_market_entry_full_allocation_no_limits(action: str) -> None:
    control = _basket_plan(action=action)
    variant = variant_trade_plan(control, VARIANT_MARKET_SAME_TARGET)
    assert len(variant.entries) == 1
    assert variant.entries[0].order_type == "MARKET"
    assert variant.entries[0].allocation == 1.0
    assert variant.entries[0].price == control.entries[0].price
    assert not any(item.order_type == "LIMIT" for item in variant.entries)
    assert variant.stop_price == control.stop_price
    assert variant.targets[0] == control.targets[0]


@pytest.mark.parametrize("action", ["BUY", "SELL"])
def test_1_5r_and_2_0r_target_formulas_buy_and_sell(action: str) -> None:
    control = _basket_plan(action=action)
    market_price = control.entries[0].price
    stop = control.stop_price
    risk = abs(market_price - stop)

    variant_15 = variant_trade_plan(control, VARIANT_MARKET_1_5R)
    variant_20 = variant_trade_plan(control, VARIANT_MARKET_2_0R)
    expected_15 = market_price + 1.5 * risk if action == "BUY" else market_price - 1.5 * risk
    expected_20 = market_price + 2.0 * risk if action == "BUY" else market_price - 2.0 * risk
    assert variant_15.targets[0] == pytest.approx(expected_15)
    assert variant_20.targets[0] == pytest.approx(expected_20)
    assert len(variant_15.entries) == 1 and variant_15.entries[0].allocation == 1.0
    assert len(variant_20.entries) == 1 and variant_20.entries[0].allocation == 1.0
    assert variant_15.stop_price == control.stop_price
    assert variant_20.stop_price == control.stop_price


def test_control_variant_returns_the_control_plan_unchanged() -> None:
    control = _basket_plan(action="BUY")
    assert variant_trade_plan(control, VARIANT_CONTROL) is control


def test_variant_fails_closed_when_no_market_entry_exists() -> None:
    limit_only = TradePlan(
        action="BUY",
        entries=(
            EntryOrder(price=1.0990, allocation=0.50, rationale="ote1", order_type="LIMIT"),
            EntryOrder(price=1.0985, allocation=0.50, rationale="ote2", order_type="LIMIT"),
        ),
        stop_price=1.0950,
        targets=(1.1080,),
        invalidation="x",
    )
    with pytest.raises(HistoricalDataError) as excinfo:
        variant_trade_plan(limit_only, VARIANT_MARKET_SAME_TARGET)
    assert excinfo.value.code == "EXPERIMENT_MARKET_ENTRY_MISSING"


def test_variant_fails_closed_on_structurally_invalid_geometry() -> None:
    # A VALID control plan (blended average_entry pulled well below the
    # MARKET price by a heavily-weighted LIMIT entry) whose primary target
    # sits above the blended average but NOT above the market-only entry
    # price. SAME_TARGET's market-only average_entry (100) would then be
    # >= the target (56) -- structurally invalid -- and must raise rather
    # than silently coerce.
    control = TradePlan(
        action="BUY",
        entries=(
            EntryOrder(price=100.0, allocation=0.1, rationale="m", order_type="MARKET"),
            EntryOrder(price=50.0, allocation=0.9, rationale="ote", order_type="LIMIT"),
        ),
        stop_price=40.0,
        targets=(56.0,),  # > blended average (55.0), but < market entry (100.0)
        invalidation="x",
    )
    with pytest.raises(HistoricalDataError) as excinfo:
        variant_trade_plan(control, VARIANT_MARKET_SAME_TARGET)
    assert excinfo.value.code == "EXPERIMENT_INVALID_VARIANT_GEOMETRY"


# ---------------------------------------------------------------------------
# Existing evaluator semantics preserved (same-bar stop+target, cost model)
# ---------------------------------------------------------------------------


def test_same_bar_stop_and_target_still_resolves_stop_for_variant_candidates() -> None:
    control = _candidate(action="BUY")
    variant_plan = variant_trade_plan(control.plan, VARIANT_MARKET_SAME_TARGET)
    import dataclasses

    variant_candidate = dataclasses.replace(control, plan=variant_plan)
    # A single bar that touches BOTH stop and target must resolve to STOP,
    # per the existing, unmodified conservative intrabar rule.
    bar = Bar(
        time=NOW + timedelta(minutes=5),
        symbol="EURUSD",
        timeframe="M5",
        open=1.1000,
        high=variant_plan.targets[0] + 0.0005,
        low=variant_plan.stop_price - 0.0005,
        close=1.1000,
    )
    outcome = evaluate_shadow_candidate(variant_candidate, [bar], max_bars=10, target_index=0, cost_r=0.0)
    assert outcome is not None
    assert outcome.outcome == "LOSS"
    assert outcome.exit_reason == "STOP_FIRST_CONSERVATIVE"


def test_existing_cost_model_is_used_unchanged_for_variant_candidates() -> None:
    control = _candidate(action="BUY")
    variant_plan = variant_trade_plan(control.plan, VARIANT_MARKET_SAME_TARGET)
    import dataclasses

    variant_candidate = dataclasses.replace(control, plan=variant_plan)
    bar = Bar(
        time=NOW + timedelta(minutes=5),
        symbol="EURUSD",
        timeframe="M5",
        open=1.1000,
        high=variant_plan.targets[0] + 0.0005,
        low=1.1000,
        close=1.1000,
    )
    no_cost = evaluate_shadow_candidate(variant_candidate, [bar], max_bars=10, target_index=0, cost_r=0.0)
    with_cost = evaluate_shadow_candidate(variant_candidate, [bar], max_bars=10, target_index=0, cost_r=0.04)
    assert no_cost is not None and with_cost is not None
    assert no_cost.outcome == "WIN" and with_cost.outcome == "WIN"
    # cost_r subtracts max(0, cost_r) * allocation (allocation=1.0 here) from
    # the gross R -- the existing, unmodified signal_shadow._net_r formula.
    assert with_cost.net_r == pytest.approx(no_cost.net_r - 0.04, abs=1e-6)


# ---------------------------------------------------------------------------
# No forbidden imports/calls; existing evaluator module not globally mutated
# ---------------------------------------------------------------------------


def test_no_forbidden_imports_or_calls_and_no_monkeypatching() -> None:
    paths = [
        REPO_ROOT / "src" / "trademind" / "ser8_execution_geometry_experiment.py",
        REPO_ROOT / "scripts" / "run_ser8_execution_geometry_experiment.py",
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
        assert "discovery" not in imported
        assert "setattr" not in source  # no monkeypatching of signal_shadow
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
        assert "37365712" not in source  # retired account never referenced


# ---------------------------------------------------------------------------
# Real end-to-end fixtures: CONTROL must exactly reproduce the published
# replay/screening outcome for the SAME real create_replay artifact.
# ---------------------------------------------------------------------------


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


def _entry(*, symbol: str, bars: tuple[HistoricalBarV1, ...], dataset_root: Path) -> dict[str, object]:
    row = _symbol_row(symbol)
    universe = build_canonical_execution_universe(
        [row], account_login=ACCOUNT, raw_sha256=sha256_bytes(b"universe raw")
    )
    broker = BrokerSymbolV1(
        symbol=symbol, trade_mode="FULL", source_row=row,
        asset_class="FX", risk_model_supported=True, risk_model_reason="",
    )
    manifest, bars_bytes = build_dataset_manifest(
        bars=bars,
        source_proof=_proof(),
        symbol_metadata={"name": symbol, "point": 0.00001, "digits": 5, "visible": True, "trade_tick_size": 0.00001},
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
        "symbol": symbol, "asset_class": "FX", "broker_trade_mode": "FULL",
        "risk_model_supported": True, "row_count": len(bars),
        "accepted_historical_data": True, "dataset_sha256": manifest["dataset_sha256"],
        "dataset_dir": str(dataset_dir), "status": "HISTORICAL_DATA_READY",
        "status_reason": "geometry experiment fixture",
    }


def _inventory_identity() -> dict[str, object]:
    universe = build_canonical_execution_universe(
        [_symbol_row("EURUSD")], account_login=ACCOUNT, raw_sha256=sha256_bytes(b"universe raw")
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


def _build_real_report(tmp_path: Path, *, symbols: dict[str, tuple[HistoricalBarV1, ...]]):
    dataset_root = tmp_path / "historical"
    inventory_path = dataset_root / "historical_inventory.json"
    compatibility_path = dataset_root / "historical_rows.csv"
    entries = [_entry(symbol=symbol, bars=bars, dataset_root=dataset_root) for symbol, bars in symbols.items()]
    write_inventory_artifacts(
        inventory_path=inventory_path,
        compatibility_path=compatibility_path,
        payload={
            **_inventory_identity(),
            "schema_version": INVENTORY_SCHEMA_VERSION,
            "captured_at_utc": NOW.isoformat(),
            "total_broker_symbols": len(entries),
            "accepted_dataset_count": len(entries),
            "entries": entries,
        },
    )
    historical_inventory = load_inventory(inventory_path)
    policy = load_research_policy(POLICY_PATH)
    replay_root = tmp_path / "ser8_historical_replay"
    readiness_payload = build_research_readiness_inventory(
        historical_inventory_path=inventory_path,
        replay_root=replay_root,
        policy=policy,
        output_path=replay_root / "research_readiness.json",
        captured_at=NOW,
    )
    return historical_inventory, readiness_payload


def test_control_exactly_reproduces_published_replay_outcomes_end_to_end(tmp_path: Path) -> None:
    historical_inventory, readiness_payload = _build_real_report(
        tmp_path, symbols={"EURUSD": _bars(430, symbol="EURUSD")}
    )
    report = build_multisymbol_geometry_experiment_report(
        historical_inventory=historical_inventory,
        readiness_payload=readiness_payload,
        stability_window_count=3,
        captured_at=NOW,
    )
    assert report["experiment_valid"] is True
    eurusd = next(item for item in report["symbols"] if item["symbol"] == "EURUSD")
    assert eurusd["control_reproduction_verified"] is True
    control_metrics = eurusd["variants"][VARIANT_CONTROL]["metrics"]
    assert control_metrics["trade_count"] >= 300

    # Independently cross-check CONTROL's metrics against the already-
    # published screening aggregation over the SAME candidates/outcomes --
    # proving no divergence from the authoritative screening report.
    readiness_entry = next(e for e in readiness_payload["entries"] if e["symbol"] == "EURUSD")
    from trademind.ser8_historical_multisymbol_screening import load_verified_replay_rows

    candidates_raw, outcomes_raw, manifest = load_verified_replay_rows(Path(readiness_entry["replay_dir"]))
    independent_metrics = compute_symbol_replay_metrics(
        candidates=candidates_raw, outcomes=outcomes_raw, cost_r=float(manifest["shadow_cost_r"]),
        stability_window_count=3,
    )
    assert control_metrics == independent_metrics

    # All four variants used the identical candidate population size.
    candidate_counts = {variant: eurusd["variants"][variant]["candidate_count"] for variant in ALL_VARIANTS}
    assert len(set(candidate_counts.values())) == 1

    for variant in ALL_VARIANTS[1:]:
        assert eurusd["variants"][variant]["metrics"] is not None
        assert eurusd["variants"][variant]["comparative"] is not None


def test_report_is_deterministic_across_repeated_builds(tmp_path: Path) -> None:
    historical_inventory, readiness_payload = _build_real_report(
        tmp_path, symbols={"EURUSD": _bars(430, symbol="EURUSD")}
    )
    first = build_multisymbol_geometry_experiment_report(
        historical_inventory=historical_inventory, readiness_payload=readiness_payload,
        stability_window_count=3, captured_at=NOW,
    )
    second = build_multisymbol_geometry_experiment_report(
        historical_inventory=historical_inventory, readiness_payload=readiness_payload,
        stability_window_count=3, captured_at=NOW,
    )
    assert first["experiment_report_sha256"] == second["experiment_report_sha256"]


def test_atomic_write_and_hash_verified_reload(tmp_path: Path) -> None:
    historical_inventory, readiness_payload = _build_real_report(
        tmp_path, symbols={"EURUSD": _bars(430, symbol="EURUSD")}
    )
    report = build_multisymbol_geometry_experiment_report(
        historical_inventory=historical_inventory, readiness_payload=readiness_payload,
        stability_window_count=3, captured_at=NOW,
    )
    output = tmp_path / "ser8_execution_geometry_experiment" / "experiment_report.json"
    write_multisymbol_geometry_experiment_report(output, report)
    reloaded = load_verified_multisymbol_geometry_experiment_report(output)
    assert reloaded == report
    lines = compact_report_lines(report, experiment_report_path=str(output))
    assert lines[0] == "=== TRADEMIND REPORT ==="
    assert lines[-1] == "=== END REPORT ==="
    assert any(line.startswith("STATUS: PASS") for line in lines)


def test_evidence_unavailable_symbol_is_reported_not_dropped() -> None:
    readiness_entry = {
        "symbol": "NOSYMUSD", "asset_class": "FX", "broker_trade_mode": "FULL",
        "risk_model_supported": True, "historical_rows": 5000,
        "accepted_historical_data": True, "dataset_sha256": "a" * 64, "dataset_dir": "",
        "replay_sha256": None, "replay_dir": None,
        "candidate_count": 0, "completed_outcome_count": 0,
        "research_minimum": 300, "research_ready": False, "readiness_reason": "no replay",
    }
    historical_inventory = {
        "execution_account_login": ACCOUNT,
        "market_data_account_login": MARKET_DATA_ACCOUNT,
        "inventory_sha256": "x" * 64,
        "entries": [{"symbol": "NOSYMUSD", "status": "HISTORICAL_DATA_READY"}],
    }
    readiness_payload = {"readiness_inventory_sha256": "y" * 64, "entries": [readiness_entry]}
    report = build_multisymbol_geometry_experiment_report(
        historical_inventory=historical_inventory, readiness_payload=readiness_payload,
        stability_window_count=3, captured_at=NOW,
    )
    assert report["ready_symbol_count"] == 1
    entry = report["symbols"][0]
    assert entry["symbol"] == "NOSYMUSD"
    assert entry["control_reproduction_verified"] is False
    for variant in ALL_VARIANTS:
        assert entry["variants"][variant]["status"] == STATUS_EVIDENCE_UNAVAILABLE
        assert entry["variants"][variant]["metrics"] is None
    assert report["experiment_valid"] is False


# ---------------------------------------------------------------------------
# CONTROL reproduction gate: a genuine divergence must fail closed and stop
# variant interpretation for that symbol.
# ---------------------------------------------------------------------------


def test_control_reproduction_mismatch_blocks_variant_interpretation() -> None:
    fresh = [{"signal_id": "s0", "schema_version": "signal-shadow-v1", "outcome": "WIN", "net_r": 1.0,
              "completed_at": "2026-01-01T00:00:00Z", "setup_key": "K", "exit_reason": "TARGET_1",
              "exit_price": 1.1, "filled_entries": 1, "allocation_filled": 1.0, "average_entry": 1.1,
              "mfe_r": 1.0, "mae_r": 0.0, "bars_observed": 3}]
    published = [dict(fresh[0], net_r=0.5)]  # divergent net_r
    ok, detail = verify_control_reproduces_published_outcomes(
        control_outcome_rows=fresh, published_outcome_rows=published
    )
    assert ok is False
    assert "net_r" in detail


def test_control_reproduction_matches_when_fields_identical() -> None:
    row = {"signal_id": "s0", "schema_version": "signal-shadow-v1", "outcome": "WIN", "net_r": 1.0,
           "completed_at": "2026-01-01T00:00:00Z", "setup_key": "K", "exit_reason": "TARGET_1",
           "exit_price": 1.1, "filled_entries": 1, "allocation_filled": 1.0, "average_entry": 1.1,
           "mfe_r": 1.0, "mae_r": 0.0, "bars_observed": 3}
    ok, _ = verify_control_reproduces_published_outcomes(
        control_outcome_rows=[dict(row)], published_outcome_rows=[dict(row)]
    )
    assert ok is True


def test_evaluate_variant_for_symbol_directly_on_real_candidates(tmp_path: Path) -> None:
    from trademind.ser8_historical_data import load_canonical_bars, verify_dataset
    from trademind.ser8_historical_multisymbol_screening import load_verified_replay_rows
    from trademind.signal_shadow import load_candidates

    historical_inventory, readiness_payload = _build_real_report(
        tmp_path, symbols={"EURUSD": _bars(430, symbol="EURUSD")}
    )
    readiness_entry = next(e for e in readiness_payload["entries"] if e["symbol"] == "EURUSD")
    replay_dir = Path(readiness_entry["replay_dir"])
    _raw_candidates, published_outcomes, manifest = load_verified_replay_rows(replay_dir)
    candidates = load_candidates(replay_dir / "candidates.jsonl")
    verify_dataset(Path(readiness_entry["dataset_dir"]))
    bars = load_canonical_bars(Path(readiness_entry["dataset_dir"]) / "bars.csv")
    max_bars = int(manifest["shadow_max_bars"])
    cost_r = float(manifest["shadow_cost_r"])

    control_candidates, control_outcomes, control_skipped = evaluate_variant_for_symbol(
        candidates=candidates, bars=bars, variant=VARIANT_CONTROL, max_bars=max_bars, cost_r=cost_r
    )
    assert control_skipped == []
    ok, _ = verify_control_reproduces_published_outcomes(
        control_outcome_rows=control_outcomes, published_outcome_rows=published_outcomes
    )
    assert ok is True

    market_candidates, market_outcomes, market_skipped = evaluate_variant_for_symbol(
        candidates=candidates, bars=bars, variant=VARIANT_MARKET_1_5R, max_bars=max_bars, cost_r=cost_r
    )
    # Every real candidate's MARKET-only geometry is structurally valid
    # (the adapter's target is always beyond the market entry) -- nothing
    # should be skipped for real data.
    assert market_skipped == []
    assert len(market_candidates) == len(control_candidates)


def test_build_symbol_geometry_experiment_reports_control_reproduction_failed(tmp_path: Path) -> None:
    from trademind.ser8_historical_data import load_canonical_bars, verify_dataset
    from trademind.ser8_historical_multisymbol_screening import load_verified_replay_rows
    from trademind.signal_shadow import load_candidates

    historical_inventory, readiness_payload = _build_real_report(
        tmp_path, symbols={"EURUSD": _bars(430, symbol="EURUSD")}
    )
    readiness_entry = next(e for e in readiness_payload["entries"] if e["symbol"] == "EURUSD")
    replay_dir = Path(readiness_entry["replay_dir"])
    _raw_candidates, published_outcomes, manifest = load_verified_replay_rows(replay_dir)
    candidates = load_candidates(replay_dir / "candidates.jsonl")
    verify_dataset(Path(readiness_entry["dataset_dir"]))
    bars = load_canonical_bars(Path(readiness_entry["dataset_dir"]) / "bars.csv")

    # Deliberately tamper with one published outcome's net_r so CONTROL can
    # no longer reproduce it -- this must fail closed and block every other
    # variant from being interpreted for this symbol.
    tampered = [dict(row) for row in published_outcomes]
    tampered[0]["net_r"] = tampered[0]["net_r"] + 999.0

    result = build_symbol_geometry_experiment(
        symbol="EURUSD",
        candidates=candidates,
        published_outcome_rows=tampered,
        bars=bars,
        max_bars=int(manifest["shadow_max_bars"]),
        cost_r=float(manifest["shadow_cost_r"]),
    )
    assert result["control_reproduction_verified"] is False
    assert "net_r" in result["control_reproduction_detail"]
    for variant in ALL_VARIANTS[1:]:
        assert result["variants"][variant]["status"] == STATUS_CONTROL_REPRODUCTION_FAILED
        assert result["variants"][variant]["metrics"] is None
