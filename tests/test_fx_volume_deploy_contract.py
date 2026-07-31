from __future__ import annotations

from pathlib import Path


FX_MAJORS = (
    "EURUSD",
    "GBPUSD",
    "USDJPY",
    "USDCHF",
    "USDCAD",
    "AUDUSD",
    "NZDUSD",
)


def test_fx_deployment_uses_exact_major_set_and_preserves_read_only_source() -> None:
    script = Path("scripts/deploy_v14_fx_exporter.ps1").read_text(encoding="utf-8")
    exporter = Path("mt5/TradeMindAI_VolumeExporter_v1_4.mq5").read_text(encoding="utf-8")

    for symbol in FX_MAJORS:
        assert symbol in script

    assert "TradeMindAI_VolumeExporter_FX_v1_4.mq5" in script
    assert "Existing market exporter was not changed." in script
    assert "CopyTicksRange" in exporter
    assert "FILE_COMMON" in exporter

    forbidden = (
        "CTrade",
        "OrderSend(",
        ".Buy(",
        ".Sell(",
        "PositionClose(",
        "TRADE_ACTION_DEAL",
    )
    assert all(token not in exporter for token in forbidden)


def test_fx_deployment_rejects_missing_or_duplicate_major_sets() -> None:
    script = Path("scripts/deploy_v14_fx_exporter.ps1").read_text(encoding="utf-8")

    assert "must contain exactly seven majors" in script
    assert "Missing required FX major" in script
    assert "contains duplicates" in script
