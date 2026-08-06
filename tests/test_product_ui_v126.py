from pathlib import Path

from trademind import product_ui_v126 as ui


def crypto_candidate():
    return {
        "asset_class": "CRYPTO",
        "setup_family": ui.SETUP_FAMILY,
        "plan": {"targets": [130.0]},
        "market": {
            "structure": {
                "swing_bias": "BULLISH",
                "swing_break": "BULLISH_BOS",
                "internal_bias": "NEUTRAL",
                "internal_break": "NONE",
            },
            "volume": {
                "m5_volume_ratio_20": 1.35,
                "m5_volume": 1350,
                "m5_median_volume_20": 1000,
                "m5_delta_turnover": 50000,
            },
            "volatility": {
                "target_distance_atr_h1": 0.85,
            },
            "confirmation": {
                "breakout_level": 108.5,
                "future_bars_used": False,
            },
            "custom": {
                "target_rr": 2.1,
                "h1_target": 130.0,
            },
        },
    }


def test_base_formatter_route_is_valid() -> None:
    assert ui.base is ui.previous.previous.base
    assert ui.base.integer("2") == 2


def test_crypto_market_panel_shows_only_core_opportunity_metrics() -> None:
    rendered = ui._crypto_market_html(crypto_candidate())

    assert "H1 Swing v1.26" in rendered
    assert "Последний M5 экстремум" in rendered
    assert "Объём к медиане 20" in rendered
    assert "Delta M5" in rendered
    assert "RR до H1-цели" in rendered
    assert "Минимум RR" in rendered
    assert "1,80R" in rendered
    assert "0,70 ATR H1" in rendered


def test_build_payload_labels_v126_crypto_family(monkeypatch) -> None:
    payload = {
        "schema_version": "1.25.2",
        "candidates": [crypto_candidate(), {"asset_class": "FOREX"}],
    }
    monkeypatch.setattr(ui, "_BASE_BUILD_PAYLOAD", lambda *args, **kwargs: payload)

    result = ui.build_payload(
        {},
        None,
        Path("crypto"),
        Path("bars.csv"),
        fx_limit=1,
        crypto_limit=1,
        candle_limit=1,
    )

    assert result["schema_version"] == "1.26.0"
    assert result["candidates"][0]["setup_family_label"] == (
        "H1 Swing + M5 объёмный пробой"
    )


def test_v126_ui_safety_contract() -> None:
    assert ui.safety_contract() == {
        "read_only": True,
        "orders_enabled": False,
        "publication_enabled": False,
        "broker_api_called": False,
        "future_bars_used": False,
        "crypto_position_sizing_available": False,
    }
