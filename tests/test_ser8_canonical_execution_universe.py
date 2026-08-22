"""Proofs for stable semantic SER8 execution-universe identity."""

from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from trademind.ser8_historical_data import (
    EXECUTION_UNIVERSE_AUDIT_VOLATILE_FIELDS,
    EXECUTION_UNIVERSE_CANONICAL_SCHEMA_VERSION,
    EXECUTION_UNIVERSE_IDENTITY_RELEVANT_FIELDS,
    EXECUTION_UNIVERSE_SOURCE_FIELDS,
    READ_ONLY_MT5_OPERATIONS,
    HistoricalBarV1,
    HistoricalDataError,
    build_dataset_manifest,
    load_canonical_execution_universe,
    publish_canonical_execution_universe_snapshot,
    publish_dataset,
    verify_canonical_execution_universe_artifact,
    verify_canonical_execution_universe_snapshot,
    verify_dataset,
)

ACCOUNT = "67206924"
MARKET_DATA_ACCOUNT = "77053345"
REPO_ROOT = Path(__file__).resolve().parent.parent
UTC = timezone.utc
START = datetime(2026, 7, 20, tzinfo=UTC)
END = datetime(2026, 8, 21, tzinfo=UTC)


def _row(
    symbol: str,
    *,
    time_msc: str = "1787313600000",
    account_login: str = ACCOUNT,
    server: str = "RoboForex-Demo",
    digits: str = "5",
    trade_mode: str = "FULL",
    bid: str = "1.10000",
    ask: str = "1.10010",
    tick_size: str = "0.00001",
    margin_maintenance: str = "0",
    expiration_mode_flags: str = "15",
) -> dict[str, str]:
    return {
        "time_msc": time_msc,
        "account_login": account_login,
        "server": server,
        "currency": "USD",
        "symbol": symbol,
        "digits": digits,
        "trade_mode": trade_mode,
        "bid": bid,
        "ask": ask,
        "tick_size": tick_size,
        "tick_value": "1",
        "tick_value_profit": "1.0",
        "tick_value_loss": "1.000",
        "volume_min": "0.01",
        "volume_max": "100.0",
        "volume_step": "0.010",
        "contract_size": "100000",
        "margin_initial": "0",
        "margin_maintenance": margin_maintenance,
        "margin_buy_per_volume": "1",
        "margin_sell_per_volume": "1.0",
        "leverage": "100",
        "expiration_mode_flags": expiration_mode_flags,
    }


def _csv_bytes(
    rows: list[dict[str, str]],
    *,
    line_ending: str = "\n",
    fieldnames: tuple[str, ...] = EXECUTION_UNIVERSE_SOURCE_FIELDS,
) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=fieldnames, lineterminator=line_ending)
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue().encode("utf-8")


def _load(tmp_path: Path, name: str, content: bytes):
    path = tmp_path / name
    path.write_bytes(content)
    return load_canonical_execution_universe(path, account_login=ACCOUNT)


def _proof() -> dict[str, object]:
    return {
        "schema_version": "ser8-mt5-history-source-proof-v1",
        "source_type": "MT5_PYTHON_COPY_RATES_RANGE",
        "market_data_account_login": MARKET_DATA_ACCOUNT,
        "market_data_account_server": "RoboForex-ECN",
        "market_data_account_company": "RoboForex",
        "market_data_account_currency": "USD",
        "authenticated_market_data_account_verified": True,
        "read_only_operations": list(READ_ONLY_MT5_OPERATIONS),
    }


def _bars() -> tuple[HistoricalBarV1, ...]:
    return tuple(
        HistoricalBarV1(
            time_utc=START + timedelta(minutes=5 * index),
            symbol="EURUSD",
            timeframe="M5",
            open=1.1,
            high=1.101,
            low=1.099,
            close=1.1002,
            tick_volume=100,
            spread=10,
            real_volume=0,
        )
        for index in range(2)
    )


def _manifest(universe, *, captured_at: datetime = END):
    broker = next(item for item in universe.symbols if item.symbol == "EURUSD")
    return build_dataset_manifest(
        bars=_bars(),
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
        requested_from_utc=START,
        requested_to_utc=END,
        expected_interval_seconds=300,
        source_capture_utc=captured_at,
        collector_code_sha256="sha256:" + "c" * 64,
    )


def test_actual_authoritative_export_schema_is_explicitly_classified_and_accepted(
    tmp_path: Path,
) -> None:
    universe = _load(tmp_path, "actual-schema.csv", _csv_bytes([_row("EURUSD")]))
    assert EXECUTION_UNIVERSE_SOURCE_FIELDS == (
        "time_msc", "account_login", "server", "currency", "symbol", "digits",
        "trade_mode", "bid", "ask", "tick_size", "tick_value", "tick_value_profit",
        "tick_value_loss", "volume_min", "volume_max", "volume_step", "contract_size",
        "margin_initial", "margin_maintenance", "margin_buy_per_volume",
        "margin_sell_per_volume", "leverage", "expiration_mode_flags",
    )
    assert EXECUTION_UNIVERSE_AUDIT_VOLATILE_FIELDS == (
        "time_msc",
        "bid",
        "ask",
        "margin_buy_per_volume",
        "margin_sell_per_volume",
    )
    assert {
        "server", "digits", "margin_maintenance", "expiration_mode_flags",
    } <= set(EXECUTION_UNIVERSE_IDENTITY_RELEVANT_FIELDS)
    assert {
        "margin_buy_per_volume",
        "margin_sell_per_volume",
    }.isdisjoint(EXECUTION_UNIVERSE_IDENTITY_RELEVANT_FIELDS)
    canonical_row = universe.canonical_snapshot["rows"][0]
    assert set(EXECUTION_UNIVERSE_AUDIT_VOLATILE_FIELDS).isdisjoint(canonical_row)
    assert canonical_row["server"] == "RoboForex-Demo"
    assert canonical_row["digits"] == "5"
    assert canonical_row["margin_maintenance"] == "0"
    assert canonical_row["expiration_mode_flags"] == "15"


@pytest.mark.parametrize(
    "producer",
    [
        "mt5/TradeMind_MT5_Risk_Snapshot_Exporter.mq5",
        "mt5/TradeMind_Demo_Order_Executor_v1.mq5",
    ],
)
def test_margin_snapshot_producers_calculate_one_lot_at_current_ask_and_bid(
    producer: str,
) -> None:
    source = (REPO_ROOT / producer).read_text(encoding="utf-8")
    header = '"margin_buy_per_volume","margin_sell_per_volume"'
    buy_calculation = "OrderCalcMargin(ORDER_TYPE_BUY,symbol,1.0,tick.ask,buy_margin)"
    sell_calculation = "OrderCalcMargin(ORDER_TYPE_SELL,symbol,1.0,tick.bid,sell_margin)"
    assert "MqlTick tick;" in source
    assert "SymbolInfoTick(symbol,tick)" in source
    assert header in source
    assert buy_calculation in source
    assert sell_calculation in source
    assert "DoubleToString(buy_margin,8)" in source
    assert "DoubleToString(sell_margin,8)" in source
    assert source.index(header) < source.index(buy_calculation)
    assert source.index(header) < source.index(sell_calculation)
    assert source.index(buy_calculation) < source.index("DoubleToString(buy_margin,8)")
    assert source.index(sell_calculation) < source.index("DoubleToString(sell_margin,8)")


@pytest.mark.parametrize(("field", "changed"), [("bid", "1.20000"), ("ask", "1.20010")])
def test_bid_and_ask_changes_are_raw_only_and_do_not_change_canonical_sha(
    tmp_path: Path,
    field: str,
    changed: str,
) -> None:
    original = _row("EURUSD")
    first = _load(tmp_path, f"{field}-first.csv", _csv_bytes([original]))
    second = _load(
        tmp_path,
        f"{field}-second.csv",
        _csv_bytes([{**original, field: changed}]),
    )
    assert first.raw_sha256 != second.raw_sha256
    assert first.canonical_sha256 == second.canonical_sha256
    assert first.canonical_snapshot == second.canonical_snapshot


@pytest.mark.parametrize(
    ("field", "changed"),
    [
        ("margin_buy_per_volume", "154.78"),
        ("margin_sell_per_volume", "154.77"),
    ],
)
def test_calculated_margin_snapshot_changes_raw_but_not_canonical_sha(
    tmp_path: Path,
    field: str,
    changed: str,
) -> None:
    original = _row("BTCUSD", bid="154620", ask="154780")
    first = _load(tmp_path, f"{field}-first.csv", _csv_bytes([original]))
    second = _load(
        tmp_path,
        f"{field}-second.csv",
        _csv_bytes([{**original, field: changed}]),
    )
    assert first.raw_sha256 != second.raw_sha256
    assert first.canonical_sha256 == second.canonical_sha256
    assert first.canonical_snapshot == second.canonical_snapshot


@pytest.mark.parametrize(
    ("field", "changed"),
    [
        ("digits", "4"),
        ("margin_maintenance", "250"),
        ("expiration_mode_flags", "7"),
    ],
)
def test_new_execution_semantic_fields_change_canonical_sha(
    tmp_path: Path,
    field: str,
    changed: str,
) -> None:
    original = _row("EURUSD")
    first = _load(tmp_path, f"{field}-first.csv", _csv_bytes([original]))
    second = _load(
        tmp_path,
        f"{field}-second.csv",
        _csv_bytes([{**original, field: changed}]),
    )
    assert first.canonical_sha256 != second.canonical_sha256


@pytest.mark.parametrize(
    ("field", "changed"),
    [
        ("contract_size", "100001"),
        ("leverage", "101"),
        ("margin_initial", "250"),
        ("margin_maintenance", "125"),
    ],
)
def test_stable_margin_semantic_fields_remain_in_canonical_identity(
    tmp_path: Path,
    field: str,
    changed: str,
) -> None:
    original = _row("EURUSD")
    first = _load(tmp_path, f"stable-{field}-first.csv", _csv_bytes([original]))
    second = _load(
        tmp_path,
        f"stable-{field}-second.csv",
        _csv_bytes([{**original, field: changed}]),
    )
    assert first.canonical_sha256 != second.canonical_sha256


def test_server_is_trimmed_case_preserved_execution_source_identity(tmp_path: Path) -> None:
    original = _row("EURUSD", server="RoboForex-Demo")
    first = _load(tmp_path, "server-first.csv", _csv_bytes([original]))
    whitespace = _load(
        tmp_path,
        "server-whitespace.csv",
        _csv_bytes([{**original, "server": "  RoboForex-Demo  "}]),
    )
    changed = _load(
        tmp_path,
        "server-changed.csv",
        _csv_bytes([{**original, "server": "RoboForex-Pro"}]),
    )
    assert first.canonical_sha256 == whitespace.canonical_sha256
    assert first.canonical_sha256 != changed.canonical_sha256
    assert first.canonical_snapshot["rows"][0]["server"] == "RoboForex-Demo"


def test_raw_order_line_endings_and_capture_timestamp_do_not_change_canonical_sha(
    tmp_path: Path,
) -> None:
    rows = [_row("EURUSD"), _row("GBPUSD")]
    first = _load(tmp_path, "first.csv", _csv_bytes(rows, line_ending="\n"))
    changed = [
        _row("GBPUSD", time_msc="1787319999999"),
        _row("EURUSD", time_msc="1787319999998"),
    ]
    second = _load(tmp_path, "second.csv", _csv_bytes(changed, line_ending="\r\n"))
    assert first.raw_sha256 != second.raw_sha256
    assert first.canonical_sha256 == second.canonical_sha256
    assert first.canonical_snapshot == second.canonical_snapshot
    assert [item.symbol for item in first.symbols] == ["EURUSD", "GBPUSD"]


@pytest.mark.parametrize(
    "mutator",
    [
        lambda rows: [rows[0], {**rows[1], "tick_size": "0.00002"}],
        lambda rows: [*rows, _row("USDJPY", tick_size="0.001")],
        lambda rows: [rows[0]],
    ],
)
def test_execution_semantic_change_addition_and_removal_change_canonical_sha(
    tmp_path: Path,
    mutator,
) -> None:
    rows = [_row("EURUSD"), _row("GBPUSD")]
    first = _load(tmp_path, "first.csv", _csv_bytes(rows))
    changed_rows = mutator(rows)
    second = _load(tmp_path, "second.csv", _csv_bytes(changed_rows))
    assert first.canonical_sha256 != second.canonical_sha256


def test_identical_normalized_duplicate_is_deduplicated(tmp_path: Path) -> None:
    original = _row("EURUSD")
    equivalent = {
        **original,
        "time_msc": "9999999999999",
        "symbol": " eurusd ",
        "tick_value_profit": "1.000000",
        "volume_max": "100",
    }
    universe = _load(tmp_path, "duplicates.csv", _csv_bytes([original, equivalent]))
    assert len(universe.symbols) == 1
    assert universe.canonical_snapshot["row_count"] == 1


def test_conflicting_duplicate_symbol_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "conflict.csv"
    path.write_bytes(_csv_bytes([_row("EURUSD"), _row("EURUSD", trade_mode="CLOSEONLY")]))
    with pytest.raises(HistoricalDataError) as caught:
        load_canonical_execution_universe(path, account_login=ACCOUNT)
    assert caught.value.code == "BROKER_SYMBOL_CONFLICT"


@pytest.mark.parametrize(
    ("row", "expected_code"),
    [
        (_row("EURUSD", account_login="99999999"), "BROKER_UNIVERSE_ACCOUNT_MISMATCH"),
        (_row("EURUSD", tick_size="not-a-number"), "BROKER_UNIVERSE_FIELD_MALFORMED"),
    ],
)
def test_wrong_account_and_malformed_execution_field_fail_closed(
    tmp_path: Path,
    row: dict[str, str],
    expected_code: str,
) -> None:
    path = tmp_path / f"{expected_code}.csv"
    path.write_bytes(_csv_bytes([row]))
    with pytest.raises(HistoricalDataError) as caught:
        load_canonical_execution_universe(path, account_login=ACCOUNT)
    assert caught.value.code == expected_code


def test_unclassified_export_column_fails_closed(tmp_path: Path) -> None:
    row = {**_row("EURUSD"), "new_unknown_field": "value"}
    path = tmp_path / "unknown.csv"
    path.write_bytes(
        _csv_bytes([row], fieldnames=(*EXECUTION_UNIVERSE_SOURCE_FIELDS, "new_unknown_field"))
    )
    with pytest.raises(HistoricalDataError) as caught:
        load_canonical_execution_universe(path, account_login=ACCOUNT)
    assert caught.value.code == "BROKER_UNIVERSE_COLUMNS_UNCLASSIFIED"


def test_manifest_preserves_raw_and_canonical_provenance_but_identity_uses_canonical(
    tmp_path: Path,
) -> None:
    rows = [_row("EURUSD"), _row("GBPUSD")]
    first_universe = _load(tmp_path, "first.csv", _csv_bytes(rows))
    second_universe = _load(
        tmp_path,
        "second.csv",
        _csv_bytes(
            [
                _row("GBPUSD", time_msc="2"),
                _row("EURUSD", time_msc="1"),
            ],
            line_ending="\r\n",
        ),
    )
    first, first_bars = _manifest(first_universe)
    second, second_bars = _manifest(second_universe, captured_at=END + timedelta(hours=1))
    assert first_bars == second_bars
    assert first["execution_universe_raw_sha256"] != second["execution_universe_raw_sha256"]
    assert first["execution_universe_canonical_sha256"] == second[
        "execution_universe_canonical_sha256"
    ]
    assert first["execution_universe_sha256"] == first[
        "execution_universe_canonical_sha256"
    ]
    assert first["execution_universe_canonical_schema_version"] == (
        EXECUTION_UNIVERSE_CANONICAL_SCHEMA_VERSION
    )
    assert first["dataset_sha256"] == second["dataset_sha256"]


def test_btcusd_ethusd_calculated_margin_drift_preserves_dataset_idempotency(
    tmp_path: Path,
) -> None:
    first_rows = [
        _row("EURUSD"),
        {**_row("BTCUSD"), "margin_buy_per_volume": "154.62", "margin_sell_per_volume": "154.62"},
        {**_row("ETHUSD"), "margin_buy_per_volume": "48.68", "margin_sell_per_volume": "48.68"},
    ]
    second_rows = [
        _row("EURUSD", time_msc="1787313601000"),
        {
            **_row("BTCUSD", time_msc="1787313601000"),
            "margin_buy_per_volume": "154.78",
            "margin_sell_per_volume": "154.77",
        },
        {
            **_row("ETHUSD", time_msc="1787313601000"),
            "margin_buy_per_volume": "48.72",
            "margin_sell_per_volume": "48.72",
        },
    ]
    first_universe = _load(tmp_path, "c1.csv", _csv_bytes(first_rows))
    second_universe = _load(tmp_path, "c2.csv", _csv_bytes(second_rows))
    first_manifest, first_bars = _manifest(first_universe, captured_at=END)
    second_manifest, second_bars = _manifest(
        second_universe,
        captured_at=END + timedelta(minutes=1),
    )
    assert first_universe.raw_sha256 != second_universe.raw_sha256
    assert first_universe.canonical_sha256 == second_universe.canonical_sha256
    assert first_bars == second_bars
    assert first_manifest["bars_sha256"] == second_manifest["bars_sha256"]
    assert first_manifest["row_count"] == second_manifest["row_count"]
    assert first_manifest["dataset_sha256"] == second_manifest["dataset_sha256"]


def test_changed_other_symbol_semantics_changes_dataset_sha(tmp_path: Path) -> None:
    first_universe = _load(
        tmp_path,
        "first.csv",
        _csv_bytes([_row("EURUSD"), _row("GBPUSD")]),
    )
    second_universe = _load(
        tmp_path,
        "second.csv",
        _csv_bytes([_row("EURUSD"), _row("GBPUSD", tick_size="0.00002")]),
    )
    first, _ = _manifest(first_universe)
    second, _ = _manifest(second_universe)
    assert first["dataset_sha256"] != second["dataset_sha256"]


def test_snapshot_artifact_and_dataset_verify_without_live_csv(tmp_path: Path) -> None:
    csv_path = tmp_path / f"mt5_risk_symbols_utc_{ACCOUNT}.csv"
    csv_path.write_bytes(_csv_bytes([_row("EURUSD"), _row("GBPUSD")]))
    universe = load_canonical_execution_universe(csv_path, account_login=ACCOUNT)
    artifact = publish_canonical_execution_universe_snapshot(tmp_path / "datasets", universe)
    verified_snapshot = verify_canonical_execution_universe_artifact(
        artifact,
        expected_sha256=universe.canonical_sha256,
    )
    assert verified_snapshot == universe.canonical_snapshot
    assert verify_canonical_execution_universe_snapshot(
        verified_snapshot,
        expected_sha256=universe.canonical_sha256,
    ) == universe.canonical_sha256
    manifest, bars_bytes = _manifest(universe)
    dataset_dir, _, _ = publish_dataset(tmp_path / "datasets", manifest, bars_bytes)
    csv_path.write_bytes(_csv_bytes([_row("USDJPY", tick_size="0.001")]))
    assert verify_dataset(dataset_dir)["dataset_sha256"] == manifest["dataset_sha256"]
