from __future__ import annotations

import csv
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from trademind.crypto_watch import inspect_crypto_streams
from trademind.smc_ote import CRYPTO_SYMBOLS, MONITORED_SYMBOLS

ROOT = Path(__file__).resolve().parents[1]
EXPORTER = ROOT / "mt5" / "TradeMindAI_CryptoVolumeExporter_v1_7.mq5"
DEPLOYER = ROOT / "scripts" / "deploy_v17_monitoring_pack.ps1"


def _write_manifest(path: Path, rows: list[tuple[str, str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            ["schema_version", "canonical_symbol", "broker_symbol", "status", "timeframe"]
        )
        for canonical, broker, status in rows:
            writer.writerow(["1.7", canonical, broker, status, "M5"])


def _write_volume(path: Path, symbol: str, *, ticks: int = 100, status: str = "OK") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["time", "symbol", "tick_count", "tick_copy_status"])
        writer.writerow(["1785585600", symbol, str(ticks), status])


def test_crypto_exporter_is_auto_discovering_and_read_only() -> None:
    text = EXPORTER.read_text(encoding="utf-8")
    assert "SymbolsTotal(false)" in text
    assert "ResolveBrokerSymbol" in text
    assert "crypto_manifest.csv" in text
    assert "FILE_COMMON" in text
    assert "BTCUSD,ETHUSD,SOLUSD,XRPUSD,LTCUSD,BCHUSD,ADAUSD,DOGEUSD" in text
    assert "canonical" in text and "actual" in text
    forbidden = ("CTrade", "OrderSend", "PositionClose", "TRADE_ACTION_DEAL")
    assert not any(token in text for token in forbidden)


def test_monitoring_pack_deploys_market_fx_and_crypto() -> None:
    text = DEPLOYER.read_text(encoding="utf-8")
    assert "TradeMindAI_VolumeExporter_v1_4_FIXED.mq5" in text
    assert "TradeMindAI_VolumeExporter_FX_v1_4.mq5" in text
    assert "TradeMindAI_CryptoVolumeExporter_v1_7.mq5" in text
    assert "MQL5\\Experts" in text
    assert "No order" not in text or "read-only" in text


def test_smc_ote_defaults_include_crypto_without_removing_existing_symbols() -> None:
    assert CRYPTO_SYMBOLS == (
        "BTCUSD",
        "ETHUSD",
        "SOLUSD",
        "XRPUSD",
        "LTCUSD",
        "BCHUSD",
        "ADAUSD",
        "DOGEUSD",
    )
    assert "XAUUSD" in MONITORED_SYMBOLS
    assert "EURUSD" in MONITORED_SYMBOLS
    assert set(CRYPTO_SYMBOLS).issubset(MONITORED_SYMBOLS)


def test_missing_crypto_manifest_is_only_warning(tmp_path: Path) -> None:
    check = inspect_crypto_streams(
        tmp_path / "crypto_manifest.csv",
        tmp_path,
        maximum_age=20,
        now=datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc),
    )
    assert check.status == "WARN"
    assert "not installed" in check.message


def test_resolved_crypto_stream_is_healthy_and_unavailable_symbols_are_informational(
    tmp_path: Path,
) -> None:
    manifest = tmp_path / "crypto_manifest.csv"
    _write_manifest(
        manifest,
        [("BTCUSD", "BTCUSD.r", "RESOLVED"), ("DOGEUSD", "", "MISSING")],
    )
    volume = tmp_path / "volume_BTCUSD_M5.csv"
    _write_volume(volume, "BTCUSD")
    now = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
    timestamp = now.timestamp()
    os.utime(volume, (timestamp, timestamp))

    check = inspect_crypto_streams(manifest, tmp_path, maximum_age=20, now=now)
    assert check.status == "OK"
    assert "resolved 1" in check.message
    assert "DOGEUSD" in check.message


def test_stale_crypto_stream_is_error_even_on_weekend(tmp_path: Path) -> None:
    manifest = tmp_path / "crypto_manifest.csv"
    _write_manifest(manifest, [("BTCUSD", "BTCUSD", "RESOLVED")])
    volume = tmp_path / "volume_BTCUSD_M5.csv"
    _write_volume(volume, "BTCUSD")
    now = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
    old = (now - timedelta(minutes=60)).timestamp()
    os.utime(volume, (old, old))

    check = inspect_crypto_streams(manifest, tmp_path, maximum_age=20, now=now)
    assert check.status == "ERROR"
    assert "24/7" in check.message
