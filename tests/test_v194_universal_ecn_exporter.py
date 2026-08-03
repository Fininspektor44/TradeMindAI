from __future__ import annotations

import re
from pathlib import Path

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
    assert '"volume_"+SafeSymbolName(canonical)' in text
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


def test_deployer_installs_only_the_universal_ecn_source() -> None:
    text = DEPLOYER.read_text(encoding="utf-8")
    assert "TradeMindAI_ECN_UniversalVolumeExporter_v1_9_4.mq5" in text
    assert "Cent is not included" in text
    assert "ROBO_CENT" in text
    assert "crypto_manifest_cent" in text
    assert "MQL5\\Experts" in text
    assert "Trading function detected" in text
    for symbol in EXPECTED_SYMBOLS:
        assert symbol in text
