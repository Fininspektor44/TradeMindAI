"""Tests for src/trademind/ser8_symbol_universe.py -- SER8 FULL SYMBOL
UNIVERSE + RESEARCH RANKING V1.

Central invariant proven throughout: FULL UNIVERSE != FULL EXECUTION.
Discovery/classification/ranking never grants, widens, or infers
execution authority for any symbol -- only a genuine, already-ACCEPTED
HypothesisRegistry record (observed, never created here) can ever cause
execution_status to become anything but NOT_EXECUTABLE.
"""

from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from trademind.discovery.hypothesis_registry import HypothesisRegistry, HypothesisState
from trademind.discovery.holdout_store import HoldoutSealStore
from trademind.signal_statistics_provenance import sha256_bytes
from trademind.ser8_symbol_universe import (
    ASSET_CLASS_FX,
    ASSET_CLASS_METALS,
    ASSET_CLASS_UNKNOWN,
    EXECUTION_STATUS_DEMO_ACTIVE,
    EXECUTION_STATUS_NOT_EXECUTABLE,
    RESEARCH_STATUS_ACCEPTED,
    RESEARCH_STATUS_DATA_INSUFFICIENT,
    RESEARCH_STATUS_DISCOVERED,
    RESEARCH_STATUS_REJECTED,
    RESEARCH_STATUS_RESEARCHING,
    RESEARCH_STATUS_RESEARCH_READY,
    RESEARCH_STATUS_RISK_MODEL_UNSUPPORTED,
    SCHEMA_VERSION,
    SER8SymbolUniverseControl,
    SER8SymbolUniverseError,
    SymbolUniverseEntryV1,
    VerifiedHistoricalResearchEvidenceV1,
    aggregate_forward_demo_performance,
    apply_research_lifecycle_state,
    classify_asset_class,
    discover_symbol_universe,
    rank_research_readiness,
    scan_live_signal_symbols,
)

_ACCOUNT = "77053345"
_NOW = datetime(2026, 8, 20, 12, 0, 0, tzinfo=timezone.utc)


def _symbol_row(
    symbol: str, *, trade_mode: str = "FULL", tick_size: float = 0.0001, tick_value: float = 1.0,
    volume_min: float = 0.01, volume_max: float = 100.0, volume_step: float = 0.01,
) -> dict[str, str]:
    return {
        "time_msc": "1700000000000", "account_login": _ACCOUNT, "currency": "USD", "symbol": symbol,
        "trade_mode": trade_mode, "tick_size": str(tick_size), "tick_value": str(tick_value),
        "tick_value_profit": str(tick_value), "tick_value_loss": str(tick_value), "volume_min": str(volume_min),
        "volume_max": str(volume_max), "volume_step": str(volume_step), "contract_size": "100000",
        "margin_initial": "0", "margin_buy_per_volume": "20", "margin_sell_per_volume": "20", "leverage": "100",
    }


def _write_symbols_csv(path: Path, rows: list[dict[str, str]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(rows[0].keys()) if rows else [
        "time_msc", "account_login", "currency", "symbol", "trade_mode", "tick_size", "tick_value",
        "tick_value_profit", "tick_value_loss", "volume_min", "volume_max", "volume_step", "contract_size",
        "margin_initial", "margin_buy_per_volume", "margin_sell_per_volume", "leverage",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    return path


def _write_candidate_line(path: Path, *, symbol: str, signal_id: str = "sig-1") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    now = _NOW.isoformat()
    payload = {
        "signal_id": signal_id, "observed_at": now, "created_at": now, "symbol": symbol, "timeframe": "M5",
        "setup_family": "spread_pressure", "scenario": "continuation",
        "plan": {
            "action": "BUY", "entries": [{"price": 1.1, "allocation": 1.0, "rationale": "t", "order_type": "MARKET"}],
            "stop_price": 1.09, "targets": [1.12], "invalidation": "close below stop", "target_rationale": ["r1"],
        },
        "market_features": {}, "factor_scores": {}, "factor_reasons": {}, "provenance": ["test"],
        "generated_from_market_data": True, "robot_context_only": {},
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")


# ---------------------------------------------------------------------------
# classify_asset_class
# ---------------------------------------------------------------------------


def test_classify_fx_pair() -> None:
    assert classify_asset_class("EURUSD") == ASSET_CLASS_FX
    assert classify_asset_class("usdjpy") == ASSET_CLASS_FX


def test_classify_metal() -> None:
    assert classify_asset_class("XAUUSD") == ASSET_CLASS_METALS


def test_classify_unknown_for_non_fx_shape() -> None:
    assert classify_asset_class("BTCUSD") == ASSET_CLASS_UNKNOWN
    assert classify_asset_class("US500") == ASSET_CLASS_UNKNOWN
    assert classify_asset_class("EURUSDX") == ASSET_CLASS_UNKNOWN


def test_classify_rejects_same_currency_both_sides() -> None:
    assert classify_asset_class("USDUSD") == ASSET_CLASS_UNKNOWN


# ---------------------------------------------------------------------------
# discover_symbol_universe: 1 symbol / 10 symbols / large universe
# ---------------------------------------------------------------------------


def test_historical_rows_alone_never_make_symbol_research_ready(tmp_path: Path) -> None:
    csv_path = _write_symbols_csv(tmp_path / "symbols.csv", [_symbol_row("EURUSD")])
    entries = discover_symbol_universe(
        symbols_csv=csv_path, historical_rows_by_symbol={"EURUSD": 5000}, now=_NOW,
    )
    assert len(entries) == 1
    entry = entries[0]
    assert entry.symbol == "EURUSD"
    assert entry.asset_class == ASSET_CLASS_FX
    assert entry.risk_model_supported is True
    assert entry.data_available is True
    assert entry.historical_rows == 5000
    assert entry.research_status == RESEARCH_STATUS_DATA_INSUFFICIENT
    assert entry.execution_status == EXECUTION_STATUS_NOT_EXECUTABLE
    assert "replay evidence" in entry.rejection_reason
    assert entry.schema_version == SCHEMA_VERSION
    assert entry.entry_hash.startswith("sha256:")


def test_ten_symbols_discovered(tmp_path: Path) -> None:
    fx_symbols = ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "NZDUSD", "USDCAD", "USDCHF", "EURGBP", "EURJPY", "GBPJPY"]
    rows = [_symbol_row(sym) for sym in fx_symbols]
    csv_path = _write_symbols_csv(tmp_path / "symbols.csv", rows)
    entries = discover_symbol_universe(
        symbols_csv=csv_path, historical_rows_by_symbol={sym: 1000 for sym in fx_symbols}, now=_NOW,
    )
    assert {e.symbol for e in entries} == set(fx_symbols)
    assert all(e.asset_class == ASSET_CLASS_FX for e in entries)
    assert all(e.research_status == RESEARCH_STATUS_DATA_INSUFFICIENT for e in entries)


def test_verified_replay_evidence_can_make_symbol_research_ready(tmp_path: Path) -> None:
    csv_path = _write_symbols_csv(tmp_path / "symbols.csv", [_symbol_row("EURUSD")])
    evidence = VerifiedHistoricalResearchEvidenceV1(
        dataset_sha256="a" * 64,
        replay_sha256="b" * 64,
        historical_rows=5000,
        completed_outcomes=300,
        research_minimum=300,
        research_ready=True,
        readiness_reason="verified replay outcomes meet the authoritative research minimum",
    )
    entry = discover_symbol_universe(
        symbols_csv=csv_path,
        verified_research_by_symbol={"EURUSD": evidence},
        now=_NOW,
    )[0]
    assert entry.data_available is True
    assert entry.historical_rows == 5000
    assert entry.research_status == RESEARCH_STATUS_RESEARCH_READY
    assert entry.rejection_reason is None


def test_large_symbol_universe(tmp_path: Path) -> None:
    # A large, heterogeneous universe: proper FX pairs, metals, unknown
    # asset classes, and duplicate rows (which must collapse to one entry).
    fx = [f"{a}{b}" for a in ("EUR", "GBP", "AUD", "NZD", "CAD") for b in ("USD", "JPY", "CHF") if a != b]
    metals = ["XAUUSD", "XAGUSD"]
    unknown = ["BTCUSD", "US500", "USOIL"]
    all_symbols = fx + metals + unknown
    rows = [_symbol_row(sym) for sym in all_symbols] + [_symbol_row(all_symbols[0])]  # duplicate
    csv_path = _write_symbols_csv(tmp_path / "symbols.csv", rows)
    entries = discover_symbol_universe(symbols_csv=csv_path, now=_NOW)
    assert len(entries) == len(set(all_symbols))
    by_symbol = {e.symbol: e for e in entries}
    assert all(by_symbol[s].asset_class == ASSET_CLASS_FX for s in fx)
    assert all(by_symbol[s].asset_class == ASSET_CLASS_METALS for s in metals)
    assert all(by_symbol[s].asset_class == ASSET_CLASS_UNKNOWN for s in unknown)
    # Metals/unknown are RISK_MODEL_UNSUPPORTED (asset-class boundary) even
    # though their instrument metadata is otherwise structurally valid.
    for sym in metals + unknown:
        assert by_symbol[sym].research_status == RESEARCH_STATUS_RISK_MODEL_UNSUPPORTED
        assert by_symbol[sym].rejection_reason is not None


# ---------------------------------------------------------------------------
# unsupported symbol / missing historical data / missing risk metadata
# ---------------------------------------------------------------------------


def test_disabled_trade_mode_is_risk_model_unsupported(tmp_path: Path) -> None:
    csv_path = _write_symbols_csv(tmp_path / "symbols.csv", [_symbol_row("EURUSD", trade_mode="DISABLED")])
    entries = discover_symbol_universe(symbols_csv=csv_path, historical_rows_by_symbol={"EURUSD": 1000}, now=_NOW)
    assert entries[0].research_status == RESEARCH_STATUS_RISK_MODEL_UNSUPPORTED
    assert "trade_mode=DISABLED" in entries[0].rejection_reason


def test_closeonly_trade_mode_is_risk_model_unsupported(tmp_path: Path) -> None:
    csv_path = _write_symbols_csv(tmp_path / "symbols.csv", [_symbol_row("EURUSD", trade_mode="CLOSEONLY")])
    entries = discover_symbol_universe(symbols_csv=csv_path, historical_rows_by_symbol={"EURUSD": 1000}, now=_NOW)
    assert entries[0].research_status == RESEARCH_STATUS_RISK_MODEL_UNSUPPORTED


def test_missing_historical_data_is_data_insufficient(tmp_path: Path) -> None:
    csv_path = _write_symbols_csv(tmp_path / "symbols.csv", [_symbol_row("EURUSD")])
    entries = discover_symbol_universe(symbols_csv=csv_path, historical_rows_by_symbol={}, now=_NOW)
    assert entries[0].research_status == RESEARCH_STATUS_DATA_INSUFFICIENT
    assert entries[0].data_available is False
    assert entries[0].historical_rows is None


def test_missing_risk_metadata_zero_tick_size_is_unsupported(tmp_path: Path) -> None:
    csv_path = _write_symbols_csv(tmp_path / "symbols.csv", [_symbol_row("EURUSD", tick_size=0.0)])
    entries = discover_symbol_universe(symbols_csv=csv_path, historical_rows_by_symbol={"EURUSD": 1000}, now=_NOW)
    assert entries[0].research_status == RESEARCH_STATUS_RISK_MODEL_UNSUPPORTED
    assert "tick_size" in entries[0].rejection_reason


def test_missing_risk_metadata_zero_volume_min_is_unsupported(tmp_path: Path) -> None:
    csv_path = _write_symbols_csv(tmp_path / "symbols.csv", [_symbol_row("EURUSD", volume_min=0.0)])
    entries = discover_symbol_universe(symbols_csv=csv_path, historical_rows_by_symbol={"EURUSD": 1000}, now=_NOW)
    assert entries[0].research_status == RESEARCH_STATUS_RISK_MODEL_UNSUPPORTED
    assert "volume" in entries[0].rejection_reason


def test_missing_symbols_csv_raises() -> None:
    with pytest.raises(SER8SymbolUniverseError):
        discover_symbol_universe(symbols_csv=Path("/nonexistent/symbols.csv"))


def test_missing_required_column_raises(tmp_path: Path) -> None:
    path = tmp_path / "symbols.csv"
    path.write_text("symbol,trade_mode\nEURUSD,FULL\n", encoding="utf-8")
    with pytest.raises(SER8SymbolUniverseError):
        discover_symbol_universe(symbols_csv=path)


# ---------------------------------------------------------------------------
# live-runtime signal scanning
# ---------------------------------------------------------------------------


def test_scan_live_signal_symbols_counts_by_symbol(tmp_path: Path) -> None:
    journal = tmp_path / "candidates.jsonl"
    _write_candidate_line(journal, symbol="EURUSD", signal_id="sig-1")
    _write_candidate_line(journal, symbol="EURUSD", signal_id="sig-2")
    _write_candidate_line(journal, symbol="GBPUSD", signal_id="sig-3")
    counts = scan_live_signal_symbols([journal])
    assert counts == {"EURUSD": 2, "GBPUSD": 1}


def test_scan_live_signal_symbols_skips_malformed_lines(tmp_path: Path) -> None:
    journal = tmp_path / "candidates.jsonl"
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.write_text("not json\n{}\n", encoding="utf-8")
    _write_candidate_line(journal, symbol="EURUSD")
    counts = scan_live_signal_symbols([journal])
    assert counts == {"EURUSD": 1}


def test_scan_live_signal_symbols_missing_file_is_empty(tmp_path: Path) -> None:
    assert scan_live_signal_symbols([tmp_path / "missing.jsonl"]) == {}


def test_live_runtime_supported_reflects_journal_presence(tmp_path: Path) -> None:
    csv_path = _write_symbols_csv(tmp_path / "symbols.csv", [_symbol_row("EURUSD"), _symbol_row("GBPUSD")])
    journal = tmp_path / "candidates.jsonl"
    _write_candidate_line(journal, symbol="EURUSD")
    entries = {e.symbol: e for e in discover_symbol_universe(
        symbols_csv=csv_path, candidates_paths=[journal], historical_rows_by_symbol={"EURUSD": 1, "GBPUSD": 1}, now=_NOW,
    )}
    assert entries["EURUSD"].live_runtime_supported is True
    assert entries["EURUSD"].live_signal_sample_count == 1
    assert entries["GBPUSD"].live_runtime_supported is False
    assert entries["GBPUSD"].live_signal_sample_count == 0


# ---------------------------------------------------------------------------
# correlation fallback -- structurally always supported
# ---------------------------------------------------------------------------


def test_correlation_model_supported_true_via_fallback(tmp_path: Path) -> None:
    csv_path = _write_symbols_csv(tmp_path / "symbols.csv", [_symbol_row("EURUSD")])
    entries = discover_symbol_universe(symbols_csv=csv_path, correlation_config={"symbols": {}}, now=_NOW)
    assert entries[0].correlation_model_supported is True
    assert entries[0].correlation_group == "SYMBOL:EURUSD"


def test_correlation_model_uses_explicit_group_when_configured(tmp_path: Path) -> None:
    csv_path = _write_symbols_csv(tmp_path / "symbols.csv", [_symbol_row("EURUSD")])
    entries = discover_symbol_universe(
        symbols_csv=csv_path, correlation_config={"symbols": {"EURUSD": "USD_MAJORS"}}, now=_NOW,
    )
    assert entries[0].correlation_group == "USD_MAJORS"
    assert entries[0].correlation_model_supported is True


# ---------------------------------------------------------------------------
# entry hashing / determinism
# ---------------------------------------------------------------------------


def test_entry_hash_deterministic_across_identical_semantic_state(tmp_path: Path) -> None:
    csv_path = _write_symbols_csv(tmp_path / "symbols.csv", [_symbol_row("EURUSD")])
    e1 = discover_symbol_universe(symbols_csv=csv_path, historical_rows_by_symbol={"EURUSD": 1}, now=_NOW)[0]
    e2 = discover_symbol_universe(
        symbols_csv=csv_path, historical_rows_by_symbol={"EURUSD": 1},
        now=datetime(2026, 8, 21, 0, 0, 0, tzinfo=timezone.utc),
    )[0]
    assert e1.entry_hash == e2.entry_hash  # captured_at excluded from the hash domain
    assert e1.captured_at != e2.captured_at


def test_entry_rejects_naive_captured_at() -> None:
    with pytest.raises(SER8SymbolUniverseError):
        SymbolUniverseEntryV1(
            schema_version=SCHEMA_VERSION, symbol="EURUSD", asset_class=ASSET_CLASS_FX, broker_trade_mode="FULL",
            data_available=True, historical_rows=1, live_runtime_supported=False, live_signal_sample_count=0,
            risk_model_supported=True, correlation_model_supported=True, correlation_group="SYMBOL:EURUSD",
            research_status=RESEARCH_STATUS_DISCOVERED, execution_status=EXECUTION_STATUS_NOT_EXECUTABLE,
            rejection_reason=None, captured_at="2026-08-20T00:00:00",
        )


# ---------------------------------------------------------------------------
# apply_research_lifecycle_state: EURUSD accepted / USDJPY unaccepted /
# multiple independently accepted / rejected
# ---------------------------------------------------------------------------


def _registry(tmp_path: Path) -> HypothesisRegistry:
    return HypothesisRegistry(tmp_path / "registry.db")


def _register(registry: HypothesisRegistry, *, hypothesis_id: str, family_tag: str) -> None:
    """Uses ONLY the real, public HypothesisRegistry.register() API --
    family_id/content_hash are derived from the supplied definitions,
    never hand-picked, exactly as the authoritative research pipeline
    itself does."""
    registry.register(
        hypothesis_id=hypothesis_id,
        family_definition={"family_tag": family_tag},
        content_definition={"hypothesis_id": hypothesis_id, "family_tag": family_tag},
    )


def _accept(registry: HypothesisRegistry, hypothesis_id: str, family_tag: str) -> None:
    """Advances a hypothesis through the REAL HypothesisRegistry/
    HoldoutSealStore public APIs to ACCEPTED -- the same minimal,
    authoritative sequence the full research pipeline drives, without
    invoking the heavier proposal/train-test/validation control-plane
    machinery this module's own tests do not need to re-prove."""
    _register(registry, hypothesis_id=hypothesis_id, family_tag=family_tag)
    registry.freeze(hypothesis_id, manifest_hash="b" * 64)
    seals = HoldoutSealStore(registry)
    unique_hash = sha256_bytes(hypothesis_id.encode("utf-8"))[len("sha256:"):]
    seals.register(
        hypothesis_id=hypothesis_id, envelope_hash=unique_hash, key_id="test-key",
        evaluator_id="test-evaluator", evaluator_hash=unique_hash,
    )
    seals.mark_isolated(
        hypothesis_id, isolation_receipt_hash="c" * 64, public_max_time="2026-01-02T00:00:00+00:00",
        holdout_start_time="2026-01-03T00:00:00+00:00", holdout_end_time="2026-01-04T00:00:00+00:00",
        public_row_count=10, holdout_row_count=10,
    )
    registry.transition(hypothesis_id, HypothesisState.TRAIN_TESTED)
    registry.transition(hypothesis_id, HypothesisState.VALIDATION_PASSED)
    registry.transition(hypothesis_id, HypothesisState.HOLDOUT_CONSUMED)
    registry.transition(hypothesis_id, HypothesisState.ACCEPTED)


def _reject(registry: HypothesisRegistry, hypothesis_id: str, family_tag: str) -> None:
    """Advances to VALIDATION_REJECTED -- a genuinely TERMINAL rejection
    state reached directly from TRAIN_TESTED (see
    _TERMINAL_FAMILY_STATES in hypothesis_registry.py; there is no legal
    VALIDATION_REJECTED -> REJECTED_FINAL transition, so this is the
    real, authoritative rejection path, not a shortcut)."""
    _register(registry, hypothesis_id=hypothesis_id, family_tag=family_tag)
    registry.freeze(hypothesis_id, manifest_hash="d" * 64)
    seals = HoldoutSealStore(registry)
    unique_hash = sha256_bytes(("reject:" + hypothesis_id).encode("utf-8"))[len("sha256:"):]
    seals.register(
        hypothesis_id=hypothesis_id, envelope_hash=unique_hash, key_id="test-key",
        evaluator_id="test-evaluator", evaluator_hash=unique_hash,
    )
    seals.mark_isolated(
        hypothesis_id, isolation_receipt_hash="e" * 64, public_max_time="2026-01-02T00:00:00+00:00",
        holdout_start_time="2026-01-03T00:00:00+00:00", holdout_end_time="2026-01-04T00:00:00+00:00",
        public_row_count=10, holdout_row_count=10,
    )
    registry.transition(hypothesis_id, HypothesisState.TRAIN_TESTED)
    registry.transition(hypothesis_id, HypothesisState.VALIDATION_REJECTED)


def _entry(symbol: str) -> SymbolUniverseEntryV1:
    return SymbolUniverseEntryV1(
        schema_version=SCHEMA_VERSION, symbol=symbol, asset_class=ASSET_CLASS_FX, broker_trade_mode="FULL",
        data_available=True, historical_rows=100, live_runtime_supported=True, live_signal_sample_count=5,
        risk_model_supported=True, correlation_model_supported=True, correlation_group=f"SYMBOL:{symbol}",
        research_status=RESEARCH_STATUS_RESEARCH_READY, execution_status=EXECUTION_STATUS_NOT_EXECUTABLE,
        rejection_reason=None, captured_at=_NOW.isoformat(),
    )


def test_accepted_hypothesis_marks_symbol_accepted(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    _accept(registry, "hyp-eurusd", "fam-eurusd")
    entries = apply_research_lifecycle_state(
        [_entry("EURUSD")], registry=registry, symbol_to_hypothesis_ids={"EURUSD": ["hyp-eurusd"]},
    )
    assert entries[0].research_status == RESEARCH_STATUS_ACCEPTED
    assert entries[0].execution_status == EXECUTION_STATUS_NOT_EXECUTABLE  # not configured for demo


def test_accepted_and_configured_symbol_is_demo_active(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    _accept(registry, "hyp-eurusd", "fam-eurusd")
    entries = apply_research_lifecycle_state(
        [_entry("EURUSD")], registry=registry, symbol_to_hypothesis_ids={"EURUSD": ["hyp-eurusd"]},
        demo_active_symbols=["EURUSD"],
    )
    assert entries[0].execution_status == EXECUTION_STATUS_DEMO_ACTIVE


def test_unaccepted_symbol_stays_not_executable(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    _register(registry, hypothesis_id="hyp-usdjpy", family_tag="fam-usdjpy")
    entries = apply_research_lifecycle_state(
        [_entry("USDJPY")], registry=registry, symbol_to_hypothesis_ids={"USDJPY": ["hyp-usdjpy"]},
        demo_active_symbols=["USDJPY"],
    )
    assert entries[0].research_status != RESEARCH_STATUS_ACCEPTED
    assert entries[0].execution_status == EXECUTION_STATUS_NOT_EXECUTABLE


def test_rejected_hypothesis_marks_symbol_rejected(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    _reject(registry, "hyp-usdjpy", "fam-usdjpy")
    entries = apply_research_lifecycle_state(
        [_entry("USDJPY")], registry=registry, symbol_to_hypothesis_ids={"USDJPY": ["hyp-usdjpy"]},
    )
    assert entries[0].research_status == RESEARCH_STATUS_REJECTED
    assert entries[0].execution_status == EXECUTION_STATUS_NOT_EXECUTABLE


def test_multiple_independently_accepted_symbols(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    _accept(registry, "hyp-eurusd", "fam-eurusd")
    _accept(registry, "hyp-gbpusd", "fam-gbpusd")
    _register(registry, hypothesis_id="hyp-usdjpy", family_tag="fam-usdjpy")
    entries = apply_research_lifecycle_state(
        [_entry("EURUSD"), _entry("GBPUSD"), _entry("USDJPY")], registry=registry,
        symbol_to_hypothesis_ids={"EURUSD": ["hyp-eurusd"], "GBPUSD": ["hyp-gbpusd"], "USDJPY": ["hyp-usdjpy"]},
        demo_active_symbols=["EURUSD", "GBPUSD"],
    )
    by_symbol = {e.symbol: e for e in entries}
    assert by_symbol["EURUSD"].execution_status == EXECUTION_STATUS_DEMO_ACTIVE
    assert by_symbol["GBPUSD"].execution_status == EXECUTION_STATUS_DEMO_ACTIVE
    assert by_symbol["USDJPY"].execution_status == EXECUTION_STATUS_NOT_EXECUTABLE
    assert by_symbol["USDJPY"].research_status != RESEARCH_STATUS_ACCEPTED


def test_no_hypothesis_mapping_leaves_entry_unchanged(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    entries = apply_research_lifecycle_state([_entry("EURUSD")], registry=registry, symbol_to_hypothesis_ids={})
    assert entries[0].research_status == RESEARCH_STATUS_RESEARCH_READY
    assert entries[0].execution_status == EXECUTION_STATUS_NOT_EXECUTABLE


def test_mid_lifecycle_hypothesis_marks_symbol_researching(tmp_path: Path) -> None:
    """A hypothesis that has been FROZEN (past PROPOSED, not yet at any
    terminal state) marks its symbol RESEARCHING -- distinct from both
    DISCOVERED (nothing started) and ACCEPTED/REJECTED (a verdict
    reached)."""
    registry = _registry(tmp_path)
    _register(registry, hypothesis_id="hyp-audusd", family_tag="fam-audusd")
    registry.freeze("hyp-audusd", manifest_hash="f" * 64)
    entries = apply_research_lifecycle_state(
        [_entry("AUDUSD")], registry=registry, symbol_to_hypothesis_ids={"AUDUSD": ["hyp-audusd"]},
    )
    assert entries[0].research_status == RESEARCH_STATUS_RESEARCHING
    assert entries[0].execution_status == EXECUTION_STATUS_NOT_EXECUTABLE


# ---------------------------------------------------------------------------
# rank_research_readiness: deterministic, no fabricated scores
# ---------------------------------------------------------------------------


def test_ranking_prefers_research_ready_and_larger_samples() -> None:
    ready_small = SymbolUniverseEntryV1(
        schema_version=SCHEMA_VERSION, symbol="AUDUSD", asset_class=ASSET_CLASS_FX, broker_trade_mode="FULL",
        data_available=True, historical_rows=100, live_runtime_supported=True, live_signal_sample_count=1,
        risk_model_supported=True, correlation_model_supported=True, correlation_group="SYMBOL:AUDUSD",
        research_status=RESEARCH_STATUS_RESEARCH_READY, execution_status=EXECUTION_STATUS_NOT_EXECUTABLE,
        rejection_reason=None, captured_at=_NOW.isoformat(),
    )
    ready_large = SymbolUniverseEntryV1(
        schema_version=SCHEMA_VERSION, symbol="GBPUSD", asset_class=ASSET_CLASS_FX, broker_trade_mode="FULL",
        data_available=True, historical_rows=9000, live_runtime_supported=True, live_signal_sample_count=50,
        risk_model_supported=True, correlation_model_supported=True, correlation_group="SYMBOL:GBPUSD",
        research_status=RESEARCH_STATUS_RESEARCH_READY, execution_status=EXECUTION_STATUS_NOT_EXECUTABLE,
        rejection_reason=None, captured_at=_NOW.isoformat(),
    )
    unsupported = SymbolUniverseEntryV1(
        schema_version=SCHEMA_VERSION, symbol="AAAUSD", asset_class=ASSET_CLASS_UNKNOWN, broker_trade_mode="FULL",
        data_available=False, historical_rows=None, live_runtime_supported=False, live_signal_sample_count=0,
        risk_model_supported=False, correlation_model_supported=True, correlation_group="SYMBOL:AAAUSD",
        research_status=RESEARCH_STATUS_RISK_MODEL_UNSUPPORTED, execution_status=EXECUTION_STATUS_NOT_EXECUTABLE,
        rejection_reason="asset class unsupported", captured_at=_NOW.isoformat(),
    )
    ranked = rank_research_readiness([unsupported, ready_small, ready_large])
    assert [e.symbol for e in ranked] == ["GBPUSD", "AUDUSD", "AAAUSD"]


def test_ranking_is_deterministic_and_pure() -> None:
    entries = [_entry("GBPUSD"), _entry("AUDUSD"), _entry("EURUSD")]
    ranked_once = [e.symbol for e in rank_research_readiness(entries)]
    ranked_twice = [e.symbol for e in rank_research_readiness(entries)]
    assert ranked_once == ranked_twice


# ---------------------------------------------------------------------------
# aggregate_forward_demo_performance: minimum-sample safeguard
# ---------------------------------------------------------------------------


def test_forward_performance_insufficient_sample_yields_no_scores() -> None:
    outcomes = [{"symbol": "EURUSD", "realized_pl": 5.0} for _ in range(3)]
    summary = aggregate_forward_demo_performance(outcomes, symbol="EURUSD", minimum_sample_size=20)
    assert summary.sample_size == 3
    assert summary.sufficient_sample is False
    assert summary.total_realized_pl is None
    assert summary.win_rate is None
    assert summary.average_realized_pl is None


def test_forward_performance_sufficient_sample_computes_scores() -> None:
    outcomes = [{"symbol": "EURUSD", "realized_pl": 5.0} for _ in range(15)] + [
        {"symbol": "EURUSD", "realized_pl": -3.0} for _ in range(5)
    ]
    summary = aggregate_forward_demo_performance(outcomes, symbol="EURUSD", minimum_sample_size=20)
    assert summary.sample_size == 20
    assert summary.sufficient_sample is True
    assert summary.total_realized_pl == pytest.approx(15 * 5.0 - 5 * 3.0)
    assert summary.win_rate == pytest.approx(15 / 20)


def test_forward_performance_filters_by_symbol() -> None:
    outcomes = [{"symbol": "EURUSD", "realized_pl": 1.0}] * 25 + [{"symbol": "GBPUSD", "realized_pl": 9.0}] * 25
    summary = aggregate_forward_demo_performance(outcomes, symbol="GBPUSD", minimum_sample_size=20)
    assert summary.sample_size == 25
    assert summary.total_realized_pl == pytest.approx(25 * 9.0)


# ---------------------------------------------------------------------------
# SER8SymbolUniverseControl: persistence, idempotent upsert, no schema clash
# ---------------------------------------------------------------------------


def test_persist_and_reload_round_trip(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    control = SER8SymbolUniverseControl(registry=registry)
    entries = [_entry("EURUSD"), _entry("GBPUSD")]
    written = control.persist_universe(entries)
    assert written == 2
    reloaded = control.list_entries()
    assert {e.symbol for e in reloaded} == {"EURUSD", "GBPUSD"}
    reloaded_eurusd = control.get_entry("EURUSD")
    assert reloaded_eurusd is not None
    assert reloaded_eurusd.entry_hash == _entry("EURUSD").entry_hash


def test_persist_is_idempotent_upsert(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    control = SER8SymbolUniverseControl(registry=registry)
    control.persist_universe([_entry("EURUSD")])
    control.persist_universe([_entry("EURUSD")])
    assert len(control.list_entries()) == 1


def test_persist_does_not_touch_hypothesis_registry_schema(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    _register(registry, hypothesis_id="hyp-1", family_tag="fam-1")
    control = SER8SymbolUniverseControl(registry=registry)
    control.persist_universe([_entry("EURUSD")])
    # The pre-existing hypothesis record is completely untouched.
    record = registry.get("hyp-1")
    assert record.hypothesis_id == "hyp-1"


def test_get_entry_missing_symbol_returns_none(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    control = SER8SymbolUniverseControl(registry=registry)
    assert control.get_entry("EURUSD") is None
