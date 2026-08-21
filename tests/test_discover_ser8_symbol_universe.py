"""Tests for scripts/discover_ser8_symbol_universe.py -- the read-only
CLI inventory/ranking tool for SER8 FULL SYMBOL UNIVERSE + RESEARCH
RANKING V1. Proves it is genuinely read-only (zero broker interaction,
zero research-lifecycle mutation) and produces the exact FIRST
DELIVERABLE inventory fields the task's own spec requires."""

from __future__ import annotations

import csv
import importlib
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "src"))

cli_module = importlib.import_module("discover_ser8_symbol_universe")

from trademind.discovery.hypothesis_registry import HypothesisRegistry  # noqa: E402
from trademind.ser8_symbol_universe import SER8SymbolUniverseControl  # noqa: E402

_ACCOUNT = "77053345"


def _write_symbols_csv(path: Path, symbols: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "time_msc", "account_login", "currency", "symbol", "trade_mode", "tick_size", "tick_value",
        "tick_value_profit", "tick_value_loss", "volume_min", "volume_max", "volume_step", "contract_size",
        "margin_initial", "margin_buy_per_volume", "margin_sell_per_volume", "leverage",
    ]
    rows = [
        {
            "time_msc": "1700000000000", "account_login": _ACCOUNT, "currency": "USD", "symbol": symbol,
            "trade_mode": "FULL", "tick_size": "0.0001", "tick_value": "1.0", "tick_value_profit": "1.0",
            "tick_value_loss": "1.0", "volume_min": "0.01", "volume_max": "100.0", "volume_step": "0.01",
            "contract_size": "100000", "margin_initial": "0", "margin_buy_per_volume": "20",
            "margin_sell_per_volume": "20", "leverage": "100",
        }
        for symbol in symbols
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def test_cli_prints_required_inventory_fields(tmp_path: Path, capsys) -> None:
    mt5_dir = tmp_path / "mt5"
    _write_symbols_csv(mt5_dir / f"mt5_risk_symbols_utc_{_ACCOUNT}.csv", ["EURUSD", "GBPUSD"])
    exit_code = cli_module.main(["--mt5-export-dir", str(mt5_dir), "--execution-account", _ACCOUNT, "--data-root", str(tmp_path / "data")])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "TOTAL BROKER SYMBOLS: 2" in out
    assert "LIVE-RUNTIME SUPPORTED: 0" in out  # no candidate journal written.
    assert "RESEARCH-DATA AVAILABLE: 0" in out  # no --historical-data-csv supplied.
    assert "CURRENTLY ACCEPTED: 0" in out
    assert "CURRENTLY EXECUTABLE: 0" in out
    assert "RESEARCH QUEUE" in out


def test_cli_legacy_historical_rows_are_availability_only(tmp_path: Path, capsys) -> None:
    mt5_dir = tmp_path / "mt5"
    _write_symbols_csv(mt5_dir / f"mt5_risk_symbols_utc_{_ACCOUNT}.csv", ["EURUSD"])
    hist_csv = tmp_path / "historical.csv"
    hist_csv.write_text("symbol,rows\nEURUSD,5000\n", encoding="utf-8")

    exit_code = cli_module.main([
        "--mt5-export-dir", str(mt5_dir), "--execution-account", _ACCOUNT, "--data-root", str(tmp_path / "data"),
        "--historical-data-csv", str(hist_csv),
    ])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "RESEARCH-DATA AVAILABLE: 1" in out
    assert "RESEARCH-READY: 0" in out


def test_cli_missing_symbols_csv_fails_closed(tmp_path: Path) -> None:
    exit_code = cli_module.main([
        "--mt5-export-dir", str(tmp_path / "nonexistent"), "--execution-account", _ACCOUNT, "--data-root", str(tmp_path / "data"),
    ])
    assert exit_code == 2


def test_cli_json_output_is_valid(tmp_path: Path, capsys) -> None:
    mt5_dir = tmp_path / "mt5"
    _write_symbols_csv(mt5_dir / f"mt5_risk_symbols_utc_{_ACCOUNT}.csv", ["EURUSD"])
    exit_code = cli_module.main([
        "--mt5-export-dir", str(mt5_dir), "--execution-account", _ACCOUNT, "--data-root", str(tmp_path / "data"), "--json",
    ])
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert isinstance(payload, list)
    assert payload[0]["symbol"] == "EURUSD"


def test_cli_persist_requires_db(tmp_path: Path) -> None:
    mt5_dir = tmp_path / "mt5"
    _write_symbols_csv(mt5_dir / f"mt5_risk_symbols_utc_{_ACCOUNT}.csv", ["EURUSD"])
    exit_code = cli_module.main([
        "--mt5-export-dir", str(mt5_dir), "--execution-account", _ACCOUNT, "--data-root", str(tmp_path / "data"), "--persist",
    ])
    assert exit_code == 2


def test_cli_persist_writes_universe_table(tmp_path: Path) -> None:
    mt5_dir = tmp_path / "mt5"
    _write_symbols_csv(mt5_dir / f"mt5_risk_symbols_utc_{_ACCOUNT}.csv", ["EURUSD", "GBPUSD"])
    db_path = tmp_path / "registry.db"
    exit_code = cli_module.main([
        "--mt5-export-dir", str(mt5_dir), "--execution-account", _ACCOUNT, "--data-root", str(tmp_path / "data"),
        "--db", str(db_path), "--persist",
    ])
    assert exit_code == 0
    registry = HypothesisRegistry(db_path)
    control = SER8SymbolUniverseControl(registry=registry)
    entries = control.list_entries()
    assert {e.symbol for e in entries} == {"EURUSD", "GBPUSD"}


def test_cli_never_imports_a_broker_or_execution_module() -> None:
    """Structural proof this CLI is genuinely read-only -- it must never
    import anything that could send a broker order or advance the
    research lifecycle."""
    import ast

    source = Path(cli_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name)
    forbidden_substrings = ("mt5_demo_order_send", "execution_authorization", "MetaTrader5", "risk_manager")
    for module_name in imported:
        for forbidden in forbidden_substrings:
            assert forbidden not in module_name, module_name
