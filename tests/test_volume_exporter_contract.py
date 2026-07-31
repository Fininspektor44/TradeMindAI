from __future__ import annotations

from pathlib import Path

from trademind.volume import FIELDNAMES


def test_volume_exporter_is_read_only_and_matches_python_schema() -> None:
    source = Path("mt5/TradeMindAI_VolumeExporter_v1_4.mq5").read_text(encoding="utf-8")

    assert "CopyTicksRange" in source
    assert "COPY_TICKS_ALL" in source
    assert "FILE_COMMON" in source
    assert '"1.4"' in source
    for field in FIELDNAMES:
        assert f'"{field}"' in source

    forbidden = (
        "CTrade",
        "OrderSend(",
        ".Buy(",
        ".Sell(",
        "PositionClose(",
        "TRADE_ACTION_DEAL",
    )
    assert all(token not in source for token in forbidden)
