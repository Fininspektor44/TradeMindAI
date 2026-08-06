from __future__ import annotations

import re

from trademind import product_ui_v1262 as ui


SVG = """
<svg class='price-scale-chart'>
<line class='trade-level target' x1='10.0' x2='488.0' y1='40.00' y2='40.00'/>
<rect class='trade-tag target' x='495.00' y='30.00' width='118.00' height='20' rx='5'/>
<text class='trade-tag-text target' x='501.00' y='44.00'>TP 0.38920</text>
<line class='trade-level entry' x1='10.0' x2='488.0' y1='100.00' y2='100.00'/>
<rect class='trade-tag entry' x='495.00' y='90.00' width='118.00' height='20' rx='5'/>
<text class='trade-tag-text entry' x='501.00' y='104.00'>ВХОД 0.37670</text>
<line class='trade-level stop' x1='10.0' x2='488.0' y1='108.00' y2='108.00'/>
<rect class='trade-tag stop' x='495.00' y='98.00' width='118.00' height='20' rx='5'/>
<text class='trade-tag-text stop' x='501.00' y='112.00'>СТОП 0.37330</text>
</svg>
""".strip()


def _tag_center(rendered: str, css: str) -> float:
    match = re.search(rf"<rect class='trade-tag {css}'[^>]* y='([0-9.]+)'", rendered)
    assert match is not None
    return float(match.group(1)) + 10.0


def test_spread_centers_keeps_order_and_minimum_gap() -> None:
    result = ui._spread_centers(
        [("target", 40.0), ("entry", 100.0), ("stop", 108.0)]
    )

    assert result["target"] < result["entry"] < result["stop"]
    assert result["stop"] - result["entry"] >= ui.MIN_TAG_GAP


def test_price_labels_move_but_price_lines_stay_exact(monkeypatch) -> None:
    monkeypatch.setattr(ui.previous, "_price_scale_svg", lambda candidate: SVG)

    rendered = ui._price_scale_svg({})

    assert "y1='100.00' y2='100.00'" in rendered
    assert "y1='108.00' y2='108.00'" in rendered
    assert _tag_center(rendered, "stop") - _tag_center(rendered, "entry") >= 24.0
    assert "trade-tag-connector entry" in rendered
    assert "trade-tag-connector stop" in rendered


def test_v1262_safety_contract_is_unchanged() -> None:
    assert ui.safety_contract() == {
        "read_only": True,
        "orders_enabled": False,
        "publication_enabled": False,
        "broker_api_called": False,
        "future_bars_used": False,
        "crypto_position_sizing_available": False,
    }
