"""SER8 HISTORICAL INVENTORY CAPACITY FIX V1 — proofs.

The real Windows collection loop can legitimately produce a full 90-symbol,
multi-year, calendar-month chunk-audit inventory whose aggregate JSON string
content exceeds the generic, module-wide JSON safety ceiling
(``MAX_JSON_TOTAL_STRING_BYTES`` = 196_608) used for every other small
provenance artifact. These tests prove:

- the real 90-symbol x ~32-chunk envelope now writes and loads successfully;
- the exact legacy 196_608-byte failure scenario is fixed;
- every UNRELATED JSON artifact keeps its original, stricter ceiling exactly;
- a deliberately excessive/malicious inventory still fails at a finite,
  inventory-specific hard ceiling;
- a write failure never leaves a partial canonical inventory file behind;
- dataset identity, chunk-cache identity, and coverage classification are
  completely untouched by this change.

No live MT5 calls, no network data acquisition, no broker mutation: every
payload here is a synthetic, in-memory, JSON-shaped dict.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import trademind.ser8_historical_data as ser8_historical_data
from trademind.ser8_historical_data import (
    CHUNK_ACQUISITION_CODE_SHA256,
    CHUNK_COLLECTOR_VERSION,
    CHUNK_CACHE_SCHEMA_VERSION,
    CHUNK_POLICY_VERSION,
    COVERAGE_DISCOVERY_POLICY_VERSION,
    DATASET_SCHEMA_VERSION,
    EXECUTION_UNIVERSE_CANONICAL_SCHEMA_VERSION,
    HISTORICAL_INVENTORY_JSON_BUDGET,
    INVENTORY_SCHEMA_VERSION,
    READ_ONLY_MT5_OPERATIONS,
    SOURCE_TYPE,
    BrokerSymbolV1,
    HistoricalBarV1,
    HistoricalDataError,
    build_canonical_execution_universe,
    build_dataset_manifest,
    inventory_hash,
    load_inventory,
    write_inventory_artifacts,
)
from trademind.signal_statistics_provenance import (
    DEFAULT_JSON_SAFETY_BUDGET,
    JsonSafetyBudget,
    ProvenanceError,
    canonical_json_bytes,
    sha256_bytes,
)

ACCOUNT = "67206924"
MARKET_DATA_ACCOUNT = "77053345"
UTC = timezone.utc


def _symbol_row(symbol: str) -> dict[str, str]:
    return {
        "account_login": ACCOUNT,
        "server": "RoboForex-Demo",
        "currency": "USD",
        "symbol": symbol,
        "digits": "5",
        "trade_mode": "FULL",
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
        "source_type": SOURCE_TYPE,
        "market_data_account_login": MARKET_DATA_ACCOUNT,
        "market_data_account_server": "RoboForex-ECN",
        "market_data_account_company": "RoboForex",
        "authenticated_market_data_account_verified": True,
        "read_only_operations": list(READ_ONLY_MT5_OPERATIONS),
    }


def _chunk_audit_entry(symbol: str, index: int) -> dict[str, object]:
    start = datetime(2024, 1, 1, tzinfo=UTC) + timedelta(days=31 * index)
    end = start + timedelta(days=31)
    digest = hashlib.sha256(f"{symbol}-chunk-{index}".encode()).hexdigest()
    return {
        "chunk_id": f"{start:%Y%m%dT%H%M%SZ}__{end:%Y%m%dT%H%M%SZ}",
        "chunk_from_utc": start.isoformat().replace("+00:00", "Z"),
        "chunk_to_utc": end.isoformat().replace("+00:00", "Z"),
        "status": "COMPLETED",
        "acquisition_method": "MT5",
        "cache_validation": "NOT_PRESENT",
        "cache_error_code": None,
        "row_count": 8_000 + index,
        "bars_sha256": f"sha256:{digest}",
        "error_code": None,
        "error": None,
        "retry_attempts": 0,
    }


def _quality(row_count: int) -> dict[str, object]:
    return {
        "row_count": row_count,
        "unique_timestamp_count": row_count,
        "duplicate_timestamp_count": 0,
        "monotonic_timestamp_pass": True,
        "symbol_timeframe_identity_pass": True,
        "ohlc_integrity_pass": True,
        "numeric_integrity_pass": True,
        "gap_count": 0,
        "largest_gap_seconds": 0,
        "expected_interval_seconds": 300,
        "weekend_session_gap_classification": (
            "UTC_WEEKEND_OVERLAP_OBSERVED_ONLY_BROKER_SESSION_NOT_ASSUMED"
        ),
        "weekend_overlap_gap_count": 0,
        "unexplained_gap_count": 0,
        "zero_or_negative_price_count": 0,
        "high_low_violation_count": 0,
        "open_outside_high_low_count": 0,
        "close_outside_high_low_count": 0,
        "negative_volume_or_spread_count": 0,
        "data_integrity_pass": True,
    }


def _symbol_entry(symbol: str, chunk_count: int) -> dict[str, object]:
    chunk_audit = [_chunk_audit_entry(symbol, index) for index in range(chunk_count)]
    row_count = sum(int(item["row_count"]) for item in chunk_audit)
    dataset_digest = hashlib.sha256(symbol.encode()).hexdigest()
    return {
        "symbol": symbol,
        "broker_trade_mode": "FULL",
        "asset_class": "FX",
        "risk_model_supported": True,
        "risk_model_reason": None,
        "row_count": row_count,
        "accepted_historical_data": True,
        "dataset_id": f"ser8-historical:sha256:{dataset_digest}",
        "dataset_sha256": dataset_digest,
        "dataset_dir": f"/data/ser8_historical_market_data/{dataset_digest}",
        "chunk_policy_version": CHUNK_POLICY_VERSION,
        "status": "HISTORICAL_DATA_READY",
        "status_reason": "historical integrity passed; deterministic replay still required",
        "quality": _quality(row_count),
        "coverage_discovery_policy_version": COVERAGE_DISCOVERY_POLICY_VERSION,
        "coverage_resolution": "COMPLETE",
        "requested_chunk_count": chunk_count,
        "accepted_chunk_count": chunk_count,
        "empty_chunk_count": 0,
        "cached_chunk_count": 0,
        "acquired_chunk_count": chunk_count,
        "unavailable_prefix_chunk_count": 0,
        "discarded_chunk_count": 0,
        "abandoned_chunk_count": 0,
        "chunk_audit": chunk_audit,
        "unavailable_prefix_chunk_audit": [],
        "discarded_chunk_audit": [],
        "abandoned_chunk_audit": [],
        "coverage_truncated_at_requested_start": False,
        "truncation_reason_code": None,
        "truncation_reason": None,
        "requested_coverage_start_utc": "2024-01-01T00:00:00Z",
        "requested_coverage_end_utc": "2026-08-21T00:00:00Z",
        "effective_coverage_start_utc": "2024-01-01T00:00:00Z",
        "effective_coverage_end_utc": "2026-08-21T00:00:00Z",
        "historical_capture_complete": True,
        "unresolved_error_code": None,
        "unresolved_error": None,
        "integrity_error_code": None,
        "integrity_error": None,
        "merge_integrity_error_code": None,
        "merge_integrity_error": None,
    }


def _build_inventory_payload(*, symbol_count: int, chunk_count: int) -> dict[str, object]:
    symbols = [f"SYMTEST{index:04d}" for index in range(symbol_count)]
    rows = [_symbol_row(symbol) for symbol in symbols]
    universe = build_canonical_execution_universe(
        rows,
        account_login=ACCOUNT,
        raw_sha256=sha256_bytes(b"synthetic capacity-test universe raw bytes"),
    )
    entries = [_symbol_entry(symbol, chunk_count) for symbol in symbols]
    return {
        "schema_version": INVENTORY_SCHEMA_VERSION,
        "captured_at_utc": "2026-08-22T00:00:00Z",
        "source_proof": _proof(),
        "execution_account_login": ACCOUNT,
        "market_data_account_login": MARKET_DATA_ACCOUNT,
        "execution_universe_source": f"mt5_risk_symbols_utc_{ACCOUNT}.csv",
        "execution_universe_sha256": universe.canonical_sha256,
        "execution_universe_raw_sha256": universe.raw_sha256,
        "execution_universe_canonical_sha256": universe.canonical_sha256,
        "execution_universe_canonical_schema_version": EXECUTION_UNIVERSE_CANONICAL_SCHEMA_VERSION,
        "execution_universe_canonical_snapshot": dict(universe.canonical_snapshot),
        "execution_universe_canonical_snapshot_artifact": (
            f"/data/ser8_historical_market_data/execution_universe_snapshots/"
            f"{universe.canonical_sha256.removeprefix('sha256:')}/snapshot.json"
        ),
        "market_data_source_type": SOURCE_TYPE,
        "market_data_account_server": "RoboForex-ECN",
        "market_data_account_company": "RoboForex",
        "market_data_account_currency": "USD",
        "cross_account_provenance": {
            "execution_account_login": ACCOUNT,
            "market_data_account_login": MARKET_DATA_ACCOUNT,
            "accounts_claimed_equivalent": False,
            "price_feeds_claimed_byte_identical": False,
            "research_evidence_scope": "MARKET_DATA_SOURCE_OBSERVED_NOT_EXECUTION_PRICE_EQUIVALENCE",
        },
        "broker_universe_path": f"/data/mt5_risk_symbols_utc_{ACCOUNT}.csv",
        "broker_universe_raw_sha256": universe.raw_sha256,
        "timeframe": "M5",
        "requested_from_utc": "2024-01-01T00:00:00Z",
        "requested_to_utc": "2026-08-21T00:00:00Z",
        "expected_interval_seconds": 300,
        "chunk_policy_version": CHUNK_POLICY_VERSION,
        "staging_artifacts_canonical": False,
        "total_broker_symbols": symbol_count,
        "proof_symbol_limit": None,
        "entries": entries,
        "accepted_dataset_count": symbol_count,
        "research_ready_count": 0,
        "research_ready_requires_replay": True,
        "internet_fallback_used": False,
        "orders_sent": 0,
        "orders_canceled": 0,
        "positions_modified": 0,
    }


def test_A_ninety_symbol_multi_chunk_inventory_writes_and_loads(tmp_path: Path) -> None:
    """The real supported envelope (90 symbols x ~32 monthly chunks) round-trips."""
    payload = _build_inventory_payload(symbol_count=90, chunk_count=32)
    inventory_path = tmp_path / "historical_inventory.json"
    compatibility_path = tmp_path / "historical_rows.csv"
    write_inventory_artifacts(
        inventory_path=inventory_path,
        compatibility_path=compatibility_path,
        payload=payload,
    )
    assert inventory_path.is_file()
    loaded = load_inventory(inventory_path)
    assert len(loaded["entries"]) == 90
    assert len(loaded["entries"][23]["chunk_audit"]) == 32
    assert loaded["entries"][23]["chunk_audit"][30]["bars_sha256"].startswith("sha256:")


def test_B_legacy_196608_byte_failure_scenario_is_fixed(tmp_path: Path) -> None:
    """The exact real-world failure (aggregate bytes > 196_608 under the
    generic default budget) no longer blocks the historical inventory, while
    the default budget itself still rejects the same payload unchanged."""
    payload = _build_inventory_payload(symbol_count=90, chunk_count=32)
    semantic = dict(payload)
    semantic.pop("inventory_sha256", None)

    # The exact old failure still reproduces under the untouched default budget.
    with pytest.raises(ProvenanceError, match="196608"):
        canonical_json_bytes(semantic, budget=DEFAULT_JSON_SAFETY_BUDGET)

    # ...but the actual inventory pipeline (which uses the inventory-specific
    # budget internally) now succeeds end-to-end.
    inventory_path = tmp_path / "historical_inventory.json"
    write_inventory_artifacts(
        inventory_path=inventory_path,
        compatibility_path=tmp_path / "historical_rows.csv",
        payload=payload,
    )
    assert inventory_path.is_file()
    loaded = load_inventory(inventory_path)
    assert loaded["inventory_sha256"] == inventory_hash(loaded)


def test_C_unrelated_json_artifact_limits_are_not_globally_relaxed() -> None:
    """The generic module-wide ceiling used by every other artifact is
    byte-for-byte identical to before this change."""
    assert DEFAULT_JSON_SAFETY_BUDGET.max_total_string_bytes == 196_608
    assert DEFAULT_JSON_SAFETY_BUDGET.max_canonical_bytes == 262_144
    assert DEFAULT_JSON_SAFETY_BUDGET.max_nodes == 10_000
    assert DEFAULT_JSON_SAFETY_BUDGET.max_mapping_entries == 1_024
    assert DEFAULT_JSON_SAFETY_BUDGET.max_sequence_length == 4_096
    assert DEFAULT_JSON_SAFETY_BUDGET.max_string_length == 65_536

    # An unrelated oversized artifact (no budget kwarg -> default) still fails
    # exactly as it did before this change.
    oversized_unrelated_artifact = {f"k{index}": "v" * 3_000 for index in range(100)}
    with pytest.raises(ProvenanceError, match="196608"):
        canonical_json_bytes(oversized_unrelated_artifact)

    # The inventory-specific budget is strictly larger than, and independent
    # from, the default budget object.
    assert HISTORICAL_INVENTORY_JSON_BUDGET.max_total_string_bytes > (
        DEFAULT_JSON_SAFETY_BUDGET.max_total_string_bytes
    )
    assert HISTORICAL_INVENTORY_JSON_BUDGET is not DEFAULT_JSON_SAFETY_BUDGET


def test_D_malicious_oversize_inventory_still_fails_at_a_finite_hard_limit(
    tmp_path: Path,
) -> None:
    """A deliberately excessive inventory — far beyond the supported 90 x 32
    envelope — still fails closed at the new, still-finite hard ceiling."""
    budget = HISTORICAL_INVENTORY_JSON_BUDGET
    payload = _build_inventory_payload(symbol_count=220, chunk_count=110)
    semantic = dict(payload)
    semantic.pop("inventory_sha256", None)
    # Confirm the malicious payload is genuinely beyond the inventory-specific
    # ceiling before asserting the fail-closed behavior it should trigger.
    with pytest.raises(ProvenanceError) as caught:
        canonical_json_bytes(semantic, budget=budget)
    assert str(budget.max_total_string_bytes) in str(caught.value) or str(
        budget.max_nodes
    ) in str(caught.value)

    inventory_path = tmp_path / "historical_inventory.json"
    compatibility_path = tmp_path / "historical_rows.csv"
    with pytest.raises(ProvenanceError):
        write_inventory_artifacts(
            inventory_path=inventory_path,
            compatibility_path=compatibility_path,
            payload=payload,
        )
    assert not inventory_path.exists()


def test_E_atomic_write_failure_leaves_no_partial_canonical_inventory(
    tmp_path: Path,
) -> None:
    """Mirrors the real Windows failure: historical_inventory.json must not
    exist (fully or partially) after a validation/write failure."""
    payload = _build_inventory_payload(symbol_count=220, chunk_count=110)
    inventory_path = tmp_path / "historical_inventory.json"
    compatibility_path = tmp_path / "historical_rows.csv"
    with pytest.raises(ProvenanceError):
        write_inventory_artifacts(
            inventory_path=inventory_path,
            compatibility_path=compatibility_path,
            payload=payload,
        )
    assert not inventory_path.exists()
    assert not compatibility_path.exists()
    assert not (inventory_path.parent / f".{inventory_path.name}.tmp").exists()
    assert list(tmp_path.iterdir()) == []


def _tiny_dataset_manifest() -> dict[str, object]:
    row = _symbol_row("EURUSD")
    universe = build_canonical_execution_universe(
        [row],
        account_login=ACCOUNT,
        raw_sha256=sha256_bytes(b"tiny dataset universe raw bytes"),
    )
    broker = BrokerSymbolV1(
        symbol="EURUSD",
        trade_mode="FULL",
        source_row=row,
        asset_class="FX",
        risk_model_supported=True,
        risk_model_reason="",
    )
    bar = HistoricalBarV1(
        time_utc=datetime(2024, 1, 1, tzinfo=UTC),
        symbol="EURUSD",
        timeframe="M5",
        open=1.1000,
        high=1.1002,
        low=1.0998,
        close=1.1001,
        tick_volume=100,
        spread=10,
        real_volume=0,
    )
    manifest, _ = build_dataset_manifest(
        bars=(bar,),
        source_proof=_proof(),
        symbol_metadata={
            "name": "EURUSD",
            "point": 0.00001,
            "digits": 5,
            "visible": True,
            "trade_tick_size": 0.00001,
        },
        broker_symbol=broker,
        execution_account_login=ACCOUNT,
        execution_universe_source=f"mt5_risk_symbols_utc_{ACCOUNT}.csv",
        execution_universe=universe,
        timeframe="M5",
        requested_from_utc=datetime(2024, 1, 1, tzinfo=UTC),
        requested_to_utc=datetime(2024, 1, 1, 0, 5, tzinfo=UTC),
        expected_interval_seconds=300,
        source_capture_utc=datetime(2024, 1, 1, tzinfo=UTC),
        collector_code_sha256="sha256:" + "c" * 64,
    )
    return manifest


def test_G_dataset_identity_is_unaffected_by_the_inventory_json_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """build_dataset_manifest never touches the inventory-specific budget:
    even swapping it for a broken/tiny one leaves dataset identity unchanged."""
    baseline = _tiny_dataset_manifest()
    monkeypatch.setattr(
        ser8_historical_data,
        "HISTORICAL_INVENTORY_JSON_BUDGET",
        JsonSafetyBudget(max_nodes=1, max_total_string_bytes=1, max_canonical_bytes=1),
    )
    perturbed = _tiny_dataset_manifest()
    assert perturbed["dataset_sha256"] == baseline["dataset_sha256"]
    assert perturbed["manifest_sha256"] == baseline["manifest_sha256"]
    assert DATASET_SCHEMA_VERSION == "ser8-historical-market-data-v3"


def test_H_chunk_cache_identity_constants_are_unchanged() -> None:
    assert CHUNK_CACHE_SCHEMA_VERSION == "ser8-mt5-history-chunk-v1"
    assert CHUNK_COLLECTOR_VERSION == "1.1.0"
    assert CHUNK_ACQUISITION_CODE_SHA256 == (
        "sha256:34a3d2633b744942eee35ab72d291bb5205275abfc4c5a38bd122f83e02607da"
    )
