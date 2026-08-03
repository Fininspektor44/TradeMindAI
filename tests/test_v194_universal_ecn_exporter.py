from __future__ import annotations

import csv
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from trademind.crypto_watch import inspect_crypto_streams

ROOT = Path(__file__).resolve().parents[1]
EXPORTER = ROOT / "mt5" / "TradeMindAI_ECN_UniversalVolumeExporter_v1_9_4.mq5"
DEPLOYER = ROOT / "scripts" / "deploy_v194_ecn_universal_exporter.ps1"

EXPECTED_SYMBOLS = (
    "EURUSD",
    "GBPUSD",
    "USDJPY",
    "USDCHF",
    "USDCAD",
    "AUDUSD",
    "NZDUSD",
    "XAUUSD",
    "XAGUSD",
    ".USTECHCash",
    ".US500Cash",
    ".US30Cash",
    "WTI",
    "BRENT",
    "BTCUSD",
    "ETHUSD",
)


def _configured_symbols(text: str) -> tuple[str, ...]:
    match = re.search(r'InpCanonicalSymbols\s*=\s*"([^"]+)";', text)
    assert match is not None
    return tuple(item.strip() for item in match.group(1).split(","))


def _write_volume(path: Path, symbol: str) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["time", "symbol", "tick_count", "tick_copy_status"])
        writer.writerow(["1785585600", symbol, "100", "OK"])


def test_universal_exporter_keeps_complete_ecn_universe_in_order() -> None:
    text = EXPORTER.read_text(encoding="utf-8")
    assert _configured_symbols(text) == EXPECTED_SYMBOLS
    assert "ecn_manifest.csv" in text
    assert "ROBO_ECN" in text
    assert "ROBO_CENT" not in text
    assert "crypto_manifest_cent" not in text


def test_universal_exporter_preserves_existing_file_and_schema_contracts() -> None:
    text = EXPORTER.read_text(encoding="utf-8")
    assert 'InpOutputFolder     = "TradeMindAI_Volume_v1_4"' in text
    assert '"\\\\volume_"+SafeSymbolName(canonical)' in text
    assert 'normalized=="BTCUSD" || normalized=="ETHUSD"' in text
    assert 'return (IsCryptoCanonical(canonical) ? "1.7" : "1.4")' in text
    assert "FILE_COMMON" in text
    assert "FILE_READ|FILE_WRITE" in text
    assert "FileSeek(handle,0,SEEK_END)" in text


def test_universal_exporter_is_read_only_and_mql_arrays_are_by_reference() -> None:
    text = EXPORTER.read_text(encoding="utf-8")
    forbidden = (
        "#include <Trade/Trade.mqh>",
        "CTrade",
        "OrderSend(",
        "OrderSendAsync(",
        "TRADE_ACTION_DEAL",
        "PositionClose(",
    )
    assert not any(token in text for token in forbidden)
    assert "void WriteManifest(string &requested[])" in text
    assert "int ParseSymbols(const string list,string &symbols[])" in text
    assert "void OnTick()" in text
    assert "Intentionally empty" in text


def test_deployer_targets_existing_ecn_advisors_and_installs_compiled_file() -> None:
    text = DEPLOYER.read_text(encoding="utf-8")
    assert "TradeMindAI_ECN_UniversalVolumeExporter_v1_9_4.mq5" in text
    assert "Cent is not included" in text
    assert "ROBO_CENT" in text
    assert "crypto_manifest_cent" in text
    assert "MQL5\\Experts\\Advisors" in text
    assert "TradeMind.*ECN.*Exporter" in text
    assert "UniversalVolumeExporter" in text
    assert "ChangeExtension($TargetName, \".ex5\")" in text
    assert "Installed compiled ECN exporter" in text
    assert "Trading function detected" in text
    for symbol in EXPECTED_SYMBOLS:
        assert symbol in text


def test_crypto_watch_prefers_ecn_manifest_and_ignores_non_crypto_rows(tmp_path: Path) -> None:
    manifest = tmp_path / "ecn_manifest.csv"
    with manifest.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "schema_version",
                "canonical_symbol",
                "broker_symbol",
                "status",
                "timeframe",
                "source_id",
            ]
        )
        writer.writerow(["1.4", "XAUUSD", "XAUUSD", "RESOLVED", "M5", "ROBO_ECN"])
        writer.writerow(["1.4", "EURUSD", "EURUSD", "RESOLVED", "M5", "ROBO_ECN"])
        writer.writerow(["1.7", "BTCUSD", "BTCUSD", "RESOLVED", "M5", "ROBO_ECN"])
        writer.writerow(["1.7", "ETHUSD", "ETHUSD", "RESOLVED", "M5", "ROBO_ECN"])

    _write_volume(tmp_path / "volume_BTCUSD_M5.csv", "BTCUSD")
    _write_volume(tmp_path / "volume_ETHUSD_M5.csv", "ETHUSD")
    now = datetime(2026, 8, 3, 15, 0, tzinfo=timezone.utc)
    timestamp = now.timestamp()
    os.utime(tmp_path / "volume_BTCUSD_M5.csv", (timestamp, timestamp))
    os.utime(tmp_path / "volume_ETHUSD_M5.csv", (timestamp, timestamp))

    check = inspect_crypto_streams(
        tmp_path / "crypto_manifest.csv",
        tmp_path,
        maximum_age=20,
        now=now,
    )

    assert check.status == "OK"
    assert "resolved 2 crypto symbols" in check.message
    assert check.details["manifest"] == str(manifest)
    assert check.details["mapping"] == {"BTCUSD": "BTCUSD", "ETHUSD": "ETHUSD"}


def test_crypto_watch_default_never_selects_cent_manifest(tmp_path: Path) -> None:
    cent = tmp_path / "crypto_manifest_cent.csv"
    cent.write_text(
        "schema_version,canonical_symbol,broker_symbol,status,timeframe\n"
        "1.7,SOLUSD,SOLUSD,RESOLVED,M5\n",
        encoding="utf-8",
    )

    check = inspect_crypto_streams(
        tmp_path / "crypto_manifest.csv",
        tmp_path,
        maximum_age=20,
        now=datetime(2026, 8, 3, 15, 0, tzinfo=timezone.utc),
    )

    assert check.status == "WARN"
    assert "not installed" in check.message
    assert "cent" not in str(check.details).lower()
