from pathlib import Path

from trademind import product_ui_v126 as ui


def crypto_candidate():
    return {
        "asset_class": "CRYPTO",
        "setup_family": ui.SETUP_FAMILY,
        "signal_id": "ONDOUSDT:test",
        "symbol": "ONDOUSDT",
        "timeframe": "M5",
        "action": "BUY",
        "state": "SHADOW_ONLY",
        "quality_score": 91.0,
        "scenario": "H1 swing continuation after M5 volume breakout",
        "plan": {
            "action": "BUY",
            "average_entry": 0.37670,
            "entries": [
                {
                    "price": 0.37670,
                    "weight": 1.0,
                    "rationale": "M5 breakout confirmed by volume",
                }
            ],
            "stop_price": 0.37330,
            "targets": [0.38920],
            "first_target_rr": 3.68,
            "invalidation": "Return below the opposite M5 pivot",
        },
        "candles": [
            {"open": 0.3740, "high": 0.3750, "low": 0.3735, "close": 0.3748},
            {"open": 0.3748, "high": 0.3760, "low": 0.3742, "close": 0.3757},
            {"open": 0.3757, "high": 0.3772, "low": 0.3751, "close": 0.3767},
        ],
        "factor_reasons": {
            "structure": ["H1 swing направлен в сторону сделки"],
            "confirmation": [
                "M5 закрылась за последним подтверждённым локальным экстремумом"
            ],
            "volume": ["Объём M5 к медиане 20: 3.26x", "Delta M5: 37537"],
            "volatility": ["Цель H1: 3.68R"],
        },
        "market": {
            "structure": {
                "swing_bias": "BULLISH",
                "swing_break": "NONE",
                "internal_bias": "NEUTRAL",
                "internal_break": "NONE",
            },
            "volume": {
                "m5_volume_ratio_20": 3.26,
                "m5_volume": 3260,
                "m5_median_volume_20": 1000,
                "m5_delta_turnover": 37537,
            },
            "volatility": {
                "target_distance_atr_h1": 2.81,
            },
            "confirmation": {
                "breakout_level": 0.37560,
                "future_bars_used": False,
            },
            "custom": {
                "target_rr": 3.68,
                "h1_target": 0.38920,
            },
        },
    }


def test_base_formatter_route_is_valid() -> None:
    assert ui.base is ui.previous.previous.base
    assert ui.base.integer("2") == 2


def test_price_scale_svg_draws_visible_entry_stop_take_lines() -> None:
    rendered = ui._price_scale_svg(crypto_candidate())

    assert "price-scale-chart" in rendered
    assert "axis-tick" in rendered
    assert "trade-level target" in rendered
    assert "trade-level entry" in rendered
    assert "trade-level stop" in rendered
    assert "TP 0.38920" in rendered
    assert "ВХОД 0.37670" in rendered
    assert "СТОП 0.37330" in rendered
    assert "RR 3.68R" in rendered


def test_signal_card_marks_v126_price_scale() -> None:
    rendered = ui._signal_card(crypto_candidate(), 1)

    assert "swing-price-card" in rendered
    assert "data-price-scale='true'" in rendered


def test_crypto_market_panel_keeps_core_and_collapses_secondary_context() -> None:
    rendered = ui._crypto_market_html(crypto_candidate())

    assert "H1 Swing v1.26" in rendered
    assert "Последний M5 экстремум" in rendered
    assert "Объём к медиане 20" in rendered
    assert "Delta M5" in rendered
    assert "RR до H1-цели" in rendered
    assert "1,80R" in rendered
    assert "0,70 ATR H1" in rendered
    assert "<details class='extra-context'>" in rendered
    assert "Дополнительный контекст" in rendered
    assert "FVG, OTE, funding, OI и стакан" in rendered


def test_dialog_uses_single_entry_title_and_real_reasons() -> None:
    rendered = ui._signal_dialog(crypto_candidate(), {}, 1)

    assert "Точка входа" in rendered
    assert "Лесенка входов" not in rendered
    assert "Причины будут добавлены" not in rendered
    assert "H1 swing направлен в сторону сделки" in rendered


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

    assert result["schema_version"] == "1.26.1"
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
