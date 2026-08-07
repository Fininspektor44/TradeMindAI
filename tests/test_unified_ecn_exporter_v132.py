from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
WRAPPER = ROOT / "mt5" / "exporters" / "TradeMindAI_ECN_UnifiedExporter_v1_32.mq5"
SOURCE_DIR = ROOT / "mt5" / "exporters" / "source"


def text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_unified_wrapper_includes_all_three_collectors() -> None:
    body = text(WRAPPER)
    assert "TradeMindAI_ECN_UniversalVolumeExporter_v1_9_4.mq5" in body
    assert "TradeMind_Grid_Deal_Exporter.mq5" in body
    assert "TradeMind_MT5_Risk_Snapshot_Exporter.mq5" in body
    assert "Volume_OnTimer" in body
    assert "Deal_OnTimer" in body
    assert "Risk_OnTimer" in body


def test_legacy_output_contracts_are_preserved() -> None:
    volume = text(SOURCE_DIR / "TradeMindAI_ECN_UniversalVolumeExporter_v1_9_4.mq5")
    deals = text(SOURCE_DIR / "TradeMind_Grid_Deal_Exporter.mq5")
    risk = text(SOURCE_DIR / "TradeMind_MT5_Risk_Snapshot_Exporter.mq5")

    assert 'InpOutputFolder     = "TradeMindAI_Volume_v1_4"' in volume
    assert 'InpManifestFile     = "ecn_manifest.csv"' in volume
    assert '"\\\\grid_deals_"+LoginText()+".csv"' in deals
    assert '"\\\\grid_positions_"+LoginText()+".csv"' in deals
    assert '"\\\\grid_account_"+LoginText()+".csv"' in deals
    assert '"\\\\mt5_risk_account_utc_"+LoginText()+".csv"' in risk
    assert '"\\\\mt5_risk_positions_utc_"+LoginText()+".csv"' in risk
    assert '"\\\\mt5_risk_symbols_utc_"+LoginText()+".csv"' in risk


def test_unified_exporter_is_read_only() -> None:
    files = [WRAPPER, *SOURCE_DIR.glob("*.mq5")]
    body = "\n".join(text(path) for path in files)

    forbidden = [
        r"\bOrderSend\s*\(",
        r"\bOrderSendAsync\s*\(",
        r"\bPositionModify\s*\(",
        r"\bPositionClose\s*\(",
        r"\bCTrade\b",
        r"\.Buy\s*\(",
        r"\.Sell\s*\(",
    ]
    for pattern in forbidden:
        assert re.search(pattern, body) is None, pattern

    # OrderCalcMargin is an informational calculation and is explicitly allowed.
    assert "OrderCalcMargin" in body


def test_one_master_timer_schedules_three_cadences() -> None:
    body = text(WRAPPER)
    assert "UnifiedBaseTimerSeconds" in body
    assert "Volume_InpTimerSeconds" in body
    assert "Risk_InpRefreshSeconds" in body
    assert "Deal_InpRefreshSeconds" in body
    assert "now-g_unified_last_volume" in body
    assert "now-g_unified_last_risk" in body
    assert "now-g_unified_last_deals" in body
    assert "READ-ONLY. No orders. No position modification." in body
