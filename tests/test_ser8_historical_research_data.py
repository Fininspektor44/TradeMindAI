"""Synthetic/isolated proofs for SER8 historical data and replay.

No test imports or reaches a real MetaTrader terminal. Every broker source is
an in-memory read-only fake and every artifact is written below ``tmp_path``.
"""

from __future__ import annotations

import ast
import csv
import importlib
import json
import math
import sys
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from trademind.ser8_historical_data import (
    DATASET_SCHEMA_VERSION,
    READ_ONLY_MT5_OPERATIONS,
    HistoricalBarV1,
    HistoricalDataError,
    MetaTrader5HistorySource,
    BrokerSymbolV1,
    assert_historical_artifact_isolation,
    build_dataset_manifest,
    canonical_bars_csv,
    load_broker_universe,
    load_inventory,
    publish_dataset,
    validate_historical_bars,
    verify_cross_account_symbol_compatibility,
    verify_dataset,
    write_inventory_artifacts,
)
from trademind.ser8_historical_replay import (
    build_replay_payloads,
    build_research_readiness_inventory,
    assert_replay_artifact_isolation,
    load_research_policy,
    load_verified_research_readiness,
)
from trademind.ser8_symbol_universe import (
    RESEARCH_STATUS_RESEARCH_READY,
    discover_symbol_universe,
)
from trademind.signal_statistics_provenance import sha256_bytes

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
inventory_cli = importlib.import_module("build_ser8_historical_data_inventory")
discovery_cli = importlib.import_module("discover_ser8_symbol_universe")
replay_cli = importlib.import_module("replay_ser8_historical_data")

ACCOUNT = "67206924"
MARKET_DATA_ACCOUNT = "77053345"
RETIRED_MARKET_DATA_ACCOUNT = "37365712"
NOW = datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc)
POLICY_PATH = REPO_ROOT / "config" / "research" / "ser8_historical_research_policy_v1.json"


def _symbol_row(symbol: str, *, trade_mode: str = "FULL") -> dict[str, str]:
    return {
        "time_msc": "1787313600000",
        "account_login": ACCOUNT,
        "currency": "USD",
        "symbol": symbol,
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
        "margin_buy_per_volume": "1",
        "margin_sell_per_volume": "1",
        "leverage": "100",
    }


def _write_universe(path: Path, rows: list[dict[str, str]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    return path


def _bars(
    count: int,
    *,
    symbol: str = "EURUSD",
    start: datetime = NOW - timedelta(days=10),
) -> tuple[HistoricalBarV1, ...]:
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


def _inventory_identity() -> dict[str, object]:
    return {
        "execution_account_login": ACCOUNT,
        "market_data_account_login": MARKET_DATA_ACCOUNT,
        "execution_universe_source": f"mt5_risk_symbols_utc_{ACCOUNT}.csv",
        "market_data_source_type": "MT5_PYTHON_COPY_RATES_RANGE",
        "market_data_account_server": "Broker-ECN",
        "market_data_account_company": "Broker",
        "market_data_account_currency": "USD",
        "source_proof": _proof(),
    }


def _broker(symbol: str = "EURUSD", *, trade_mode: str = "FULL") -> BrokerSymbolV1:
    return BrokerSymbolV1(
        symbol=symbol,
        trade_mode=trade_mode,
        source_row=_symbol_row(symbol, trade_mode=trade_mode),
        asset_class="FX" if symbol == "EURUSD" else "UNKNOWN",
        risk_model_supported=True,
        risk_model_reason="",
    )


def _manifest(
    bars: tuple[HistoricalBarV1, ...],
    *,
    captured_at: datetime = NOW,
    broker: BrokerSymbolV1 | None = None,
) -> tuple[dict[str, object], bytes]:
    active = broker or _broker(bars[0].symbol if bars else "EURUSD")
    return build_dataset_manifest(
        bars=bars,
        source_proof=_proof(),
        symbol_metadata={
            "name": active.symbol,
            "point": 0.00001,
            "digits": 5,
            "visible": True,
            "trade_tick_size": 0.00001,
        },
        broker_symbol=active,
        execution_account_login=ACCOUNT,
        execution_universe_source=f"mt5_risk_symbols_utc_{ACCOUNT}.csv",
        execution_universe_sha256=sha256_bytes(b"execution universe"),
        timeframe="M5",
        requested_from_utc=NOW - timedelta(days=10),
        requested_to_utc=NOW,
        expected_interval_seconds=300,
        source_capture_utc=captured_at,
        collector_code_sha256="sha256:" + "c" * 64,
    )


class _FakeSource:
    bars_by_symbol: dict[str, tuple[HistoricalBarV1, ...]] = {}
    initialization: dict[str, object] = {}

    def __init__(self, **kwargs: object) -> None:
        self.closed = False
        type(self).initialization = kwargs

    def source_proof(self) -> dict[str, object]:
        return _proof()

    def symbol_metadata(self, symbol: str) -> dict[str, object]:
        return {
            "name": symbol,
            "point": 0.00001,
            "digits": 5,
            "visible": True,
            "trade_tick_size": 0.00001,
            "trade_mode_name": "FULL",
        }

    def copy_rates(self, symbol: str, *_: object) -> tuple[HistoricalBarV1, ...]:
        return self.bars_by_symbol.get(symbol, ())

    def close(self) -> None:
        self.closed = True


def _run_collection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    symbol_rows: list[dict[str, str]],
    bars_by_symbol: dict[str, tuple[HistoricalBarV1, ...]],
) -> tuple[int, Path, Path]:
    common = tmp_path / "MT5 Common Files With Spaces" / "TradeMindAI"
    _write_universe(common / f"mt5_risk_symbols_utc_{ACCOUNT}.csv", symbol_rows)
    _FakeSource.bars_by_symbol = bars_by_symbol
    monkeypatch.setattr(inventory_cli, "MetaTrader5HistorySource", _FakeSource)
    dataset_root = tmp_path / "historical data"
    inventory = dataset_root / "historical_inventory.json"
    compatibility = dataset_root / "historical_rows.csv"
    exit_code = inventory_cli.main(
        [
            "--mode", "collect",
            "--execution-account", ACCOUNT,
            "--market-data-account", MARKET_DATA_ACCOUNT,
            "--mt5-export-dir", str(common),
            "--from-utc", "2026-08-01T00:00:00Z",
            "--to-utc", "2026-08-21T00:00:00Z",
            "--dataset-root", str(dataset_root),
            "--inventory", str(inventory),
            "--compatibility-csv", str(compatibility),
        ]
    )
    return exit_code, inventory, compatibility


def test_full_universe_is_loaded_from_broker_export_not_handwritten(tmp_path: Path) -> None:
    path = _write_universe(
        tmp_path / f"mt5_risk_symbols_utc_{ACCOUNT}.csv",
        [_symbol_row("EURUSD"), _symbol_row("GBPUSD"), _symbol_row("US500")],
    )
    universe = load_broker_universe(path, account_login=ACCOUNT)
    assert [item.symbol for item in universe] == ["EURUSD", "GBPUSD", "US500"]
    source = (REPO_ROOT / "scripts" / "build_ser8_historical_data_inventory.py").read_text()
    assert "mt5_risk_symbols_utc_" in source
    assert "FX_MAJORS" not in source


def test_explicit_execution_and_market_data_accounts_may_differ(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    common = tmp_path / "Common Files" / "TradeMindAI"
    _write_universe(
        common / f"mt5_risk_symbols_utc_{ACCOUNT}.csv",
        [_symbol_row("EURUSD")],
    )
    monkeypatch.setattr(inventory_cli, "MetaTrader5HistorySource", _FakeSource)
    exit_code = inventory_cli.main(
        [
            "--mode",
            "verify-source",
            "--execution-account",
            ACCOUNT,
            "--market-data-account",
            MARKET_DATA_ACCOUNT,
            "--mt5-export-dir",
            str(common),
        ]
    )
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["execution_account_login"] == ACCOUNT
    assert payload["market_data_account_login"] == MARKET_DATA_ACCOUNT
    assert payload["execution_universe_source"] == f"mt5_risk_symbols_utc_{ACCOUNT}.csv"
    assert _FakeSource.initialization["market_data_account_login"] == MARKET_DATA_ACCOUNT


def test_execution_universe_wrong_account_fails_closed(tmp_path: Path) -> None:
    wrong_row = _symbol_row("EURUSD")
    wrong_row["account_login"] = MARKET_DATA_ACCOUNT
    path = _write_universe(
        tmp_path / f"mt5_risk_symbols_utc_{ACCOUNT}.csv",
        [wrong_row],
    )
    with pytest.raises(HistoricalDataError) as caught:
        load_broker_universe(path, account_login=ACCOUNT)
    assert caught.value.code == "BROKER_UNIVERSE_ACCOUNT_MISMATCH"


def test_ambiguous_historical_account_cli_is_rejected() -> None:
    with pytest.raises(SystemExit):
        inventory_cli.build_arg_parser().parse_args(
            ["--mode", "verify-source", "--account", ACCOUNT]
        )


def test_missing_history_is_unavailable_and_never_fabricated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    code, inventory_path, compatibility = _run_collection(
        tmp_path,
        monkeypatch,
        symbol_rows=[_symbol_row("EURUSD"), _symbol_row("GBPUSD")],
        bars_by_symbol={"EURUSD": _bars(120), "GBPUSD": ()},
    )
    capsys.readouterr()
    assert code == 0
    payload = load_inventory(inventory_path)
    by_symbol = {entry["symbol"]: entry for entry in payload["entries"]}
    assert by_symbol["GBPUSD"]["status"] == "BROKER_HISTORY_UNAVAILABLE"
    assert by_symbol["GBPUSD"]["row_count"] == 0
    assert compatibility.read_text() == "symbol,rows\nEURUSD,120\n"


def test_inventory_binds_both_accounts_and_excludes_market_data_only_symbol(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, inventory_path, _ = _run_collection(
        tmp_path,
        monkeypatch,
        symbol_rows=[_symbol_row("EURUSD")],
        bars_by_symbol={
            "EURUSD": _bars(120),
            "USDJPY": _bars(120, symbol="USDJPY"),
        },
    )
    capsys.readouterr()
    assert code == 0
    payload = load_inventory(inventory_path)
    assert payload["execution_account_login"] == ACCOUNT
    assert payload["market_data_account_login"] == MARKET_DATA_ACCOUNT
    assert payload["execution_universe_source"] == f"mt5_risk_symbols_utc_{ACCOUNT}.csv"
    assert payload["market_data_source_type"] == "MT5_PYTHON_COPY_RATES_RANGE"
    assert [entry["symbol"] for entry in payload["entries"]] == ["EURUSD"]


def test_one_row_never_becomes_research_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    code, inventory_path, _ = _run_collection(
        tmp_path,
        monkeypatch,
        symbol_rows=[_symbol_row("EURUSD")],
        bars_by_symbol={"EURUSD": _bars(1)},
    )
    capsys.readouterr()
    assert code == 0
    entry = load_inventory(inventory_path)["entries"][0]
    assert entry["status"] == "HISTORICAL_DATA_INSUFFICIENT"
    policy = load_research_policy(POLICY_PATH)
    readiness_path = tmp_path / "replay" / "research_readiness.json"
    result = build_research_readiness_inventory(
        historical_inventory_path=inventory_path,
        replay_root=tmp_path / "replay",
        policy=policy,
        output_path=readiness_path,
        captured_at=NOW,
    )
    assert result["research_ready_count"] == 0
    rows, evidence = load_verified_research_readiness(readiness_path)
    discovered = discover_symbol_universe(
        symbols_csv=tmp_path / "MT5 Common Files With Spaces" / "TradeMindAI" / f"mt5_risk_symbols_utc_{ACCOUNT}.csv",
        historical_rows_by_symbol=rows,
        verified_research_by_symbol=evidence,
        now=NOW,
    )[0]
    assert discovered.research_status != RESEARCH_STATUS_RESEARCH_READY


def test_duplicate_and_out_of_order_timestamps_fail_integrity() -> None:
    bars = _bars(3)
    duplicate = (bars[0], bars[1], replace(bars[2], time_utc=bars[1].time_utc))
    quality = validate_historical_bars(
        duplicate, symbol="EURUSD", timeframe="M5", expected_interval_seconds=300
    )
    assert quality["duplicate_timestamp_count"] == 1
    assert quality["monotonic_timestamp_pass"] is False
    assert quality["data_integrity_pass"] is False
    out_of_order = (bars[1], bars[0], bars[2])
    quality = validate_historical_bars(
        out_of_order, symbol="EURUSD", timeframe="M5", expected_interval_seconds=300
    )
    assert quality["duplicate_timestamp_count"] == 0
    assert quality["monotonic_timestamp_pass"] is False


@pytest.mark.parametrize(
    "changed",
    [
        {"high": 0.9, "low": 1.0},
        {"open": 1.2, "high": 1.1},
        {"close": 0.8, "low": 0.9},
        {"open": 0.0},
    ],
)
def test_impossible_ohlc_values_fail(changed: dict[str, float]) -> None:
    bar = replace(_bars(1)[0], **changed)
    quality = validate_historical_bars(
        (bar,), symbol="EURUSD", timeframe="M5", expected_interval_seconds=300
    )
    assert quality["ohlc_integrity_pass"] is False
    assert quality["data_integrity_pass"] is False


def test_interrupted_publication_never_leaves_trusted_dataset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest, bars_bytes = _manifest(_bars(3))
    destination = tmp_path / str(manifest["dataset_sha256"])
    original = Path.replace

    def fail_final_replace(self: Path, target: Path) -> Path:
        if Path(target) == destination:
            raise OSError("simulated interruption")
        return original(self, target)

    monkeypatch.setattr(Path, "replace", fail_final_replace)
    with pytest.raises(OSError, match="simulated interruption"):
        publish_dataset(tmp_path, manifest, bars_bytes)
    assert not destination.exists()
    assert not list(tmp_path.glob(".ser8-history-*"))


def test_dataset_rerun_is_idempotent_and_hash_is_content_sensitive(tmp_path: Path) -> None:
    bars = _bars(10)
    first_manifest, first_bytes = _manifest(bars, captured_at=NOW)
    first_dir, persisted, created = publish_dataset(tmp_path, first_manifest, first_bytes)
    assert created is True
    second_manifest, second_bytes = _manifest(bars, captured_at=NOW + timedelta(hours=1))
    second_dir, second_persisted, created = publish_dataset(tmp_path, second_manifest, second_bytes)
    assert created is False
    assert first_dir == second_dir
    assert persisted == second_persisted
    assert first_manifest["dataset_sha256"] == second_manifest["dataset_sha256"]
    changed = tuple([*bars[:-1], replace(bars[-1], close=bars[-1].close + 0.00001)])
    changed_manifest, _ = _manifest(changed)
    assert changed_manifest["dataset_sha256"] != first_manifest["dataset_sha256"]


def test_manifest_contains_required_quality_and_no_gap_repair() -> None:
    bars = (_bars(1)[0], replace(_bars(1)[0], time_utc=_bars(1)[0].time_utc + timedelta(minutes=15)))
    manifest, bars_bytes = _manifest(bars)
    required = {
        "schema_version", "dataset_id", "dataset_sha256", "source_type",
        "execution_account_login", "market_data_account_login", "execution_universe_source",
        "market_data_source_type", "market_data_account_server", "symbol", "timeframe",
        "cross_account_provenance", "symbol_compatibility",
        "requested_from_utc", "requested_to_utc",
        "actual_first_bar_utc", "actual_last_bar_utc", "row_count", "unique_timestamp_count",
        "duplicate_timestamp_count", "monotonic_timestamp_pass", "ohlc_integrity_pass",
        "numeric_integrity_pass", "gap_count", "largest_gap_seconds", "expected_interval_seconds",
        "unexplained_gap_count", "zero_or_negative_price_count", "high_low_violation_count",
        "open_outside_high_low_count", "close_outside_high_low_count", "source_capture_utc",
        "collector_version",
    }
    assert required <= set(manifest)
    assert manifest["execution_account_login"] == ACCOUNT
    assert manifest["market_data_account_login"] == MARKET_DATA_ACCOUNT
    assert manifest["execution_universe_source"] == f"mt5_risk_symbols_utc_{ACCOUNT}.csv"
    assert manifest["market_data_account_server"] == "Broker-ECN"
    assert manifest["cross_account_provenance"]["accounts_claimed_equivalent"] is False
    assert manifest["cross_account_provenance"]["price_feeds_claimed_byte_identical"] is False
    assert manifest["symbol_compatibility"]["exact_symbol_identity_verified"] is True
    assert manifest["gap_count"] == 1
    assert manifest["unexplained_gap_count"] == 1
    assert manifest["gap_repair_performed"] is False
    assert manifest["synthetic_bars_added"] == 0
    assert len(canonical_bars_csv(bars).splitlines()) == len(bars_bytes.splitlines()) == 3


def test_cross_account_exact_symbol_and_trade_tick_size_mismatches_fail_closed() -> None:
    with pytest.raises(HistoricalDataError) as symbol_error:
        verify_cross_account_symbol_compatibility(
            broker_symbol=_broker("EURUSD"),
            market_data_symbol_metadata={
                "name": "EURUSD.r",
                "trade_tick_size": 0.00001,
            },
        )
    assert symbol_error.value.code == "BROKER_SYMBOL_IDENTITY_MISMATCH"

    with pytest.raises(HistoricalDataError) as semantics_error:
        verify_cross_account_symbol_compatibility(
            broker_symbol=_broker("EURUSD"),
            market_data_symbol_metadata={
                "name": "EURUSD",
                "trade_tick_size": 0.0001,
            },
        )
    assert semantics_error.value.code == "BROKER_SYMBOL_SEMANTICS_MISMATCH"


def test_unsupported_and_disabled_symbols_stay_gated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    code, inventory_path, _ = _run_collection(
        tmp_path,
        monkeypatch,
        symbol_rows=[_symbol_row("US500"), _symbol_row("EURUSD", trade_mode="CLOSEONLY")],
        bars_by_symbol={"US500": _bars(120, symbol="US500"), "EURUSD": _bars(120)},
    )
    capsys.readouterr()
    assert code == 0
    entries = {entry["symbol"]: entry for entry in load_inventory(inventory_path)["entries"]}
    assert entries["US500"]["status"] == "RISK_MODEL_UNSUPPORTED"
    assert entries["EURUSD"]["status"] == "BROKER_SYMBOL_DISABLED"
    assert entries["EURUSD"]["dataset_sha256"] is None


def test_mt5_source_verifies_terminal_account_symbol_and_utc() -> None:
    class FakeMT5:
        TIMEFRAME_M5 = 5

        def initialize(self, *_: object) -> bool:
            return True

        def terminal_info(self) -> SimpleNamespace:
            return SimpleNamespace(connected=True, company="Broker", name="Terminal", path="C:/MT5", trade_allowed=True)

        def account_info(self) -> SimpleNamespace:
            return SimpleNamespace(
                login=int(MARKET_DATA_ACCOUNT),
                server="Broker-ECN",
                company="Broker",
                currency="USD",
            )

        def version(self) -> tuple[int, int, str]:
            return (5, 5000, "21 Aug 2026")

        def symbol_info(self, symbol: str) -> SimpleNamespace:
            return SimpleNamespace(
                name=symbol,
                visible=True,
                select=True,
                trade_mode=4,
                digits=5,
                point=0.00001,
                trade_tick_size=0.00001,
            )

        def copy_rates_range(self, *_: object) -> list[dict[str, object]]:
            bar = _bars(1)[0]
            return [{
                "time": int(bar.time_utc.timestamp()), "open": bar.open, "high": bar.high,
                "low": bar.low, "close": bar.close, "tick_volume": 100, "spread": 10,
                "real_volume": 0,
            }]

        def last_error(self) -> tuple[int, str]:
            return (0, "ok")

        def shutdown(self) -> None:
            pass

    source = MetaTrader5HistorySource(
        market_data_account_login=MARKET_DATA_ACCOUNT,
        module=FakeMT5(),
    )
    proof = source.initialize()
    assert proof["authenticated_market_data_account_verified"] is True
    assert proof["market_data_account_login"] == MARKET_DATA_ACCOUNT
    assert proof["market_data_account_server"] == "Broker-ECN"
    assert "execution_account_login" not in proof
    assert "account_login" not in proof
    assert proof["terminal_name"] == "Terminal"
    assert proof["terminal_path"] == "C:/MT5"
    assert proof["read_only_operations"] == list(READ_ONLY_MT5_OPERATIONS)
    bars = source.copy_rates("EURUSD", "M5", NOW - timedelta(days=1), NOW)
    assert bars[0].symbol == "EURUSD"
    assert bars[0].time_utc.utcoffset() == timedelta(0)


@pytest.mark.parametrize("active_login", [RETIRED_MARKET_DATA_ACCOUNT, "1"])
def test_mt5_source_fails_on_wrong_account(active_login: str) -> None:
    module = SimpleNamespace(
        initialize=lambda *_: True,
        terminal_info=lambda: SimpleNamespace(connected=True, company="Broker"),
        account_info=lambda: SimpleNamespace(
            login=int(active_login),
            server="Broker-Demo",
            company="Broker",
        ),
        shutdown=lambda: None,
    )
    with pytest.raises(HistoricalDataError) as caught:
        MetaTrader5HistorySource(
            market_data_account_login=MARKET_DATA_ACCOUNT,
            module=module,
        ).initialize()
    assert caught.value.code == "MT5_ACCOUNT_MISMATCH"


def test_retired_market_data_account_cannot_be_configured_as_fallback() -> None:
    with pytest.raises(HistoricalDataError) as caught:
        MetaTrader5HistorySource(
            market_data_account_login=RETIRED_MARKET_DATA_ACCOUNT,
            module=SimpleNamespace(),
        )
    assert caught.value.code == "SER8_MARKET_DATA_ACCOUNT_NOT_ACTIVE"


def test_explicit_terminal_path_is_forwarded_and_verified() -> None:
    expected_path = Path("C:/Operator-Proven/MT5-77053345/terminal64.exe")

    class PathAwareMT5:
        def __init__(self) -> None:
            self.initialize_args: tuple[str, ...] | None = None

        def initialize(self, *args: str) -> bool:
            self.initialize_args = args
            return True

        def terminal_info(self) -> SimpleNamespace:
            return SimpleNamespace(
                connected=True,
                company="Broker",
                name="Active ECN Terminal",
                path=str(expected_path.parent),
                trade_allowed=False,
            )

        def account_info(self) -> SimpleNamespace:
            return SimpleNamespace(
                login=int(MARKET_DATA_ACCOUNT),
                server="Broker-ECN",
                company="Broker",
                currency="USD",
            )

        def version(self) -> tuple[int, int, str]:
            return (5, 5000, "22 Aug 2026")

        def shutdown(self) -> None:
            pass

    module = PathAwareMT5()
    source = MetaTrader5HistorySource(
        market_data_account_login=MARKET_DATA_ACCOUNT,
        terminal_path=expected_path,
        module=module,
    )
    proof = source.initialize()
    assert module.initialize_args == (str(expected_path),)
    assert proof["market_data_account_login"] == MARKET_DATA_ACCOUNT
    assert proof["terminal_name"] == "Active ECN Terminal"
    assert proof["terminal_path"] == str(expected_path.parent)


def test_wrong_explicit_terminal_path_or_attached_account_fails_closed() -> None:
    unavailable = SimpleNamespace(
        initialize=lambda *_: False,
        last_error=lambda: (1, "terminal executable unavailable"),
        shutdown=lambda: None,
    )
    with pytest.raises(HistoricalDataError) as path_error:
        MetaTrader5HistorySource(
            market_data_account_login=MARKET_DATA_ACCOUNT,
            terminal_path=Path("C:/Wrong/terminal64.exe"),
            module=unavailable,
        ).initialize()
    assert path_error.value.code == "MT5_INITIALIZE_FAILED"

    retired = SimpleNamespace(
        initialize=lambda *_: True,
        terminal_info=lambda: SimpleNamespace(connected=True, company="Broker"),
        account_info=lambda: SimpleNamespace(
            login=int(RETIRED_MARKET_DATA_ACCOUNT),
            server="Broker-Old",
            company="Broker",
        ),
        shutdown=lambda: None,
    )
    with pytest.raises(HistoricalDataError) as account_error:
        MetaTrader5HistorySource(
            market_data_account_login=MARKET_DATA_ACCOUNT,
            terminal_path=Path("C:/Retired/terminal64.exe"),
            module=retired,
        ).initialize()
    assert account_error.value.code == "MT5_ACCOUNT_MISMATCH"


def test_replay_reuses_production_semantics_and_binds_every_outcome() -> None:
    bars = _bars(430)
    manifest, _ = _manifest(bars)
    candidates, outcomes, _ = build_replay_payloads(
        bars=bars,
        dataset_manifest=manifest,
        max_bars=72,
        cost_r=0.04,
    )
    assert len(candidates) >= 300
    assert len(outcomes) >= 300
    candidate_ids = {row["signal_id"] for row in candidates}
    assert {row["signal_id"] for row in outcomes} <= candidate_ids
    assert all(str(row["replay_outcome_id"]).startswith("sha256:") for row in outcomes)
    assert all(row["orders_enabled"] is False for row in candidates)
    assert all(row["replay"]["future_fields_available_to_candidate"] is False for row in candidates)
    assert candidates[0]["market_features"]["volume"]["direction_imbalance"] is None
    assert candidates[0]["market_features"]["custom"]["source_capability"] == "MT5_HISTORICAL_RATES_ONLY"


def test_future_bar_changes_cannot_change_earlier_candidate_identity() -> None:
    bars = _bars(180)
    manifest, _ = _manifest(bars)
    original, _, _ = build_replay_payloads(
        bars=bars, dataset_manifest=manifest, max_bars=72, cost_r=0.04
    )
    changed_at = bars[120].time_utc
    changed_bar = replace(
        bars[120],
        high=bars[120].high + 0.001,
        close=bars[120].close + 0.0005,
    )
    changed = tuple([*bars[:120], changed_bar, *bars[121:]])
    changed_manifest, _ = _manifest(changed)
    replayed, _, _ = build_replay_payloads(
        bars=changed, dataset_manifest=changed_manifest, max_bars=72, cost_r=0.04
    )
    before_original = {
        row["observed_at"]: row["signal_id"] for row in original
        if datetime.fromisoformat(str(row["observed_at"])) < changed_at
    }
    before_changed = {
        row["observed_at"]: row["signal_id"] for row in replayed
        if datetime.fromisoformat(str(row["observed_at"])) < changed_at
    }
    assert before_original
    assert before_changed == before_original


def test_final_incomplete_horizon_is_excluded() -> None:
    bars = _bars(40)
    manifest, _ = _manifest(bars)
    candidates, outcomes, _ = build_replay_payloads(
        bars=bars, dataset_manifest=manifest, max_bars=72, cost_r=0.04
    )
    assert candidates
    assert len(outcomes) < len(candidates)


def test_replay_is_content_addressed_idempotent_and_never_touches_live_runtime(
    tmp_path: Path,
) -> None:
    bars = _bars(430)
    manifest, bars_bytes = _manifest(bars)
    dataset_dir, _, _ = publish_dataset(tmp_path / "historical", manifest, bars_bytes)
    inventory_path = tmp_path / "historical" / "historical_inventory.json"
    compatibility = tmp_path / "historical" / "historical_rows.csv"
    write_inventory_artifacts(
        inventory_path=inventory_path,
        compatibility_path=compatibility,
        payload={
            **_inventory_identity(),
            "schema_version": "ser8-historical-data-inventory-v1",
            "captured_at_utc": NOW.isoformat(),
            "total_broker_symbols": 1,
            "accepted_dataset_count": 1,
            "entries": [{
                "symbol": "EURUSD", "asset_class": "FX", "broker_trade_mode": "FULL",
                "risk_model_supported": True, "row_count": len(bars),
                "accepted_historical_data": True, "dataset_sha256": manifest["dataset_sha256"],
                "dataset_dir": str(dataset_dir), "status": "HISTORICAL_DATA_READY",
                "status_reason": "replay required",
            }],
        },
    )
    policy = load_research_policy(POLICY_PATH)
    replay_root = tmp_path / "ser8_historical_replay"
    readiness_path = replay_root / "research_readiness.json"
    first = build_research_readiness_inventory(
        historical_inventory_path=inventory_path,
        replay_root=replay_root,
        policy=policy,
        output_path=readiness_path,
        captured_at=NOW,
    )
    second = build_research_readiness_inventory(
        historical_inventory_path=inventory_path,
        replay_root=replay_root,
        policy=policy,
        output_path=readiness_path,
        captured_at=NOW,
    )
    assert first["entries"][0]["replay_sha256"] == second["entries"][0]["replay_sha256"]
    assert first["research_ready_count"] == 1
    assert first["execution_account_login"] == ACCOUNT
    assert first["market_data_account_login"] == MARKET_DATA_ACCOUNT
    assert not (tmp_path / "live_signal_runtime_v1").exists()
    replay_dir = replay_root / str(first["entries"][0]["replay_sha256"])
    replay_manifest = json.loads((replay_dir / "manifest.json").read_text())
    assert replay_manifest["live_runtime_artifacts_written"] is False
    assert replay_manifest["execution_account_login"] == ACCOUNT
    assert replay_manifest["market_data_account_login"] == MARKET_DATA_ACCOUNT
    common_args = [
        "--execution-account",
        ACCOUNT,
        "--historical-inventory",
        str(inventory_path),
        "--replay-root",
        str(replay_root),
        "--output",
        str(readiness_path),
        "--policy",
        str(POLICY_PATH),
    ]
    assert replay_cli.main(
        [*common_args, "--market-data-account", MARKET_DATA_ACCOUNT]
    ) == 0
    assert replay_cli.main(
        [*common_args, "--market-data-account", RETIRED_MARKET_DATA_ACCOUNT]
    ) == 1


def test_discovery_consumes_hash_verified_replay_inventory(tmp_path: Path) -> None:
    bars = _bars(430)
    manifest, bars_bytes = _manifest(bars)
    dataset_dir, _, _ = publish_dataset(tmp_path / "historical", manifest, bars_bytes)
    historical_inventory = tmp_path / "historical" / "historical_inventory.json"
    write_inventory_artifacts(
        inventory_path=historical_inventory,
        compatibility_path=tmp_path / "historical" / "historical_rows.csv",
        payload={
            **_inventory_identity(),
            "schema_version": "ser8-historical-data-inventory-v1",
            "captured_at_utc": NOW.isoformat(),
            "total_broker_symbols": 1,
            "accepted_dataset_count": 1,
            "entries": [{
                "symbol": "EURUSD", "asset_class": "FX", "broker_trade_mode": "FULL",
                "risk_model_supported": True, "row_count": len(bars),
                "accepted_historical_data": True, "dataset_sha256": manifest["dataset_sha256"],
                "dataset_dir": str(dataset_dir), "status": "HISTORICAL_DATA_READY",
                "status_reason": "replay required",
            }],
        },
    )
    replay_root = tmp_path / "replay with spaces"
    readiness_path = replay_root / "research_readiness.json"
    build_research_readiness_inventory(
        historical_inventory_path=historical_inventory,
        replay_root=replay_root,
        policy=load_research_policy(POLICY_PATH),
        output_path=readiness_path,
        captured_at=NOW,
    )
    rows, evidence = load_verified_research_readiness(
        readiness_path,
        execution_account_login=ACCOUNT,
        market_data_account_login=MARKET_DATA_ACCOUNT,
    )
    symbols = _write_universe(
        tmp_path / f"mt5_risk_symbols_utc_{ACCOUNT}.csv",
        [_symbol_row("EURUSD")],
    )
    entry = discover_symbol_universe(
        symbols_csv=symbols,
        historical_rows_by_symbol=rows,
        verified_research_by_symbol=evidence,
        now=NOW,
    )[0]
    assert entry.research_status == RESEARCH_STATUS_RESEARCH_READY
    exit_code = discovery_cli.main(
        [
            "--mt5-export-dir",
            str(symbols.parent),
            "--execution-account",
            ACCOUNT,
            "--market-data-account",
            MARKET_DATA_ACCOUNT,
            "--data-root",
            str(tmp_path / "data"),
            "--historical-inventory",
            str(readiness_path),
        ]
    )
    assert exit_code == 0
    wrong_identity_exit = discovery_cli.main(
        [
            "--mt5-export-dir",
            str(symbols.parent),
            "--execution-account",
            ACCOUNT,
            "--market-data-account",
            RETIRED_MARKET_DATA_ACCOUNT,
            "--data-root",
            str(tmp_path / "data"),
            "--historical-inventory",
            str(readiness_path),
        ]
    )
    assert wrong_identity_exit == 2
    tampered = json.loads(readiness_path.read_text())
    tampered["entries"][0]["completed_outcome_count"] = 1
    readiness_path.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(HistoricalDataError) as caught:
        load_verified_research_readiness(readiness_path)
    assert caught.value.code == "READINESS_INVENTORY_HASH_MISMATCH"


def test_failed_dataset_is_excluded_from_compatibility_csv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    invalid = (_bars(2)[0], replace(_bars(2)[1], time_utc=_bars(2)[0].time_utc))
    code, inventory_path, compatibility = _run_collection(
        tmp_path,
        monkeypatch,
        symbol_rows=[_symbol_row("EURUSD")],
        bars_by_symbol={"EURUSD": invalid},
    )
    capsys.readouterr()
    assert code == 0
    assert load_inventory(inventory_path)["entries"][0]["status"] == "DATA_INTEGRITY_FAILED"
    assert compatibility.read_text() == "symbol,rows\n"


def test_new_pipeline_has_no_internet_order_or_lifecycle_mutation_path() -> None:
    paths = [
        REPO_ROOT / "src" / "trademind" / "ser8_historical_data.py",
        REPO_ROOT / "src" / "trademind" / "ser8_historical_replay.py",
        REPO_ROOT / "scripts" / "build_ser8_historical_data_inventory.py",
        REPO_ROOT / "scripts" / "replay_ser8_historical_data.py",
    ]
    forbidden_imports = {"requests", "urllib", "socket", "yfinance", "pandas_datareader"}
    forbidden_calls = {
        "order_send", "OrderSend", "OrderSendAsync", "PositionClose", "PositionModify",
        "symbol_select", "login",
    }
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
        assert not imported & forbidden_imports
        called = {
            node.func.attr if isinstance(node.func, ast.Attribute) else node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, (ast.Attribute, ast.Name))
        }
        assert not called & forbidden_calls
        assert "HypothesisRegistry" not in source
        assert "HoldoutSealStore" not in source
        assert "rpi-v1:sha256:205b5260711f7578a59cef2feea59550b777b3df0956ffd192076b37c4e5866d:0" not in source


def test_active_historical_layer_has_no_retired_account_default_or_fallback() -> None:
    active_paths = [
        REPO_ROOT / "config" / "research" / "ser8_historical_research_policy_v1.json",
        REPO_ROOT / "docs" / "SER8_MULTISYMBOL_HISTORICAL_RESEARCH_DATA_V1.md",
        REPO_ROOT / "scripts" / "build_ser8_historical_data_inventory.py",
        REPO_ROOT / "scripts" / "replay_ser8_historical_data.py",
        REPO_ROOT / "scripts" / "discover_ser8_symbol_universe.py",
        REPO_ROOT / "src" / "trademind" / "fx_research.py",
        REPO_ROOT / "src" / "trademind" / "fx_signal_adapter.py",
        REPO_ROOT / "src" / "trademind" / "ser8_historical_data.py",
        REPO_ROOT / "src" / "trademind" / "ser8_historical_replay.py",
        REPO_ROOT / "src" / "trademind" / "ser8_symbol_universe.py",
    ]
    for path in active_paths:
        assert RETIRED_MARKET_DATA_ACCOUNT not in path.read_text(encoding="utf-8"), path


def test_dataset_verifier_detects_tampered_bar(tmp_path: Path) -> None:
    manifest, bars_bytes = _manifest(_bars(4))
    dataset_dir, _, _ = publish_dataset(tmp_path, manifest, bars_bytes)
    (dataset_dir / "bars.csv").write_bytes(bars_bytes.replace(b"1.100", b"1.101", 1))
    with pytest.raises(HistoricalDataError) as caught:
        verify_dataset(dataset_dir)
    assert caught.value.code == "DATASET_BARS_HASH_MISMATCH"


def test_policy_uses_existing_research_minimum_and_m5_contract() -> None:
    policy = load_research_policy(POLICY_PATH)
    assert policy["initial_timeframe"] == "M5"
    assert policy["expected_interval_seconds"] == 300
    assert policy["research_minimum_completed_outcomes"] == 300
    assert policy["minimum_rows_for_replay_attempt"] == 103
    assert sha256_bytes(POLICY_PATH.read_bytes()).startswith("sha256:")


def test_manifest_schema_is_versioned() -> None:
    manifest, _ = _manifest(_bars(2))
    assert manifest["schema_version"] == DATASET_SCHEMA_VERSION


def test_configurable_artifact_paths_cannot_target_live_journals(tmp_path: Path) -> None:
    live_root = tmp_path / "data" / "live_signal_runtime_v1"
    with pytest.raises(HistoricalDataError) as historical:
        assert_historical_artifact_isolation(
            dataset_root=live_root,
            inventory_path=live_root / "historical_inventory.json",
            compatibility_path=live_root / "historical_rows.csv",
        )
    assert historical.value.code == "LIVE_ARTIFACT_PATH_FORBIDDEN"
    with pytest.raises(HistoricalDataError) as replay:
        assert_replay_artifact_isolation(
            live_root,
            live_root / "research_readiness.json",
        )
    assert replay.value.code == "LIVE_REPLAY_PATH_FORBIDDEN"
    assert not live_root.exists()


def test_windows_runbook_uses_real_paths_and_stepwise_commands() -> None:
    text = (REPO_ROOT / "docs" / "SER8_MULTISYMBOL_HISTORICAL_RESEARCH_DATA_V1.md").read_text(
        encoding="utf-8"
    )
    assert "C:\\Users\\meff4\\Documents\\TradeMindAI" in text
    assert "C:\\Users\\meff4\\AppData\\Roaming\\MetaQuotes\\Terminal\\Common\\Files\\TradeMindAI" in text
    for step in ("### A1.", "### A2.", "### B.", "### C.", "### D.", "### E.", "### F."):
        assert step in text
    assert "proof-symbol-limit 1" in text
    assert "--mode verify-inventory" in text
    assert "--historical-inventory" in text
    assert text.count("--execution-account 67206924") == 7
    assert text.count("--market-data-account 77053345") == 7
    assert text.count("--terminal-path \"<OPERATOR-PROVEN-77053345-TERMINAL64.EXE>\"") == 3
    assert RETIRED_MARKET_DATA_ACCOUNT not in text
    assert "--mode verify-source --account" not in text
    assert "do not run them yet" in text
    assert ".\\data\\mt5" not in text
    assert "Stop and" in text
