from __future__ import annotations

from trademind.product_ui_v1235 import (
    _factor_html,
    _market_html,
    safety_contract,
    translate_explanation,
)


def test_fraction_metrics_are_presented_as_percentages() -> None:
    assert translate_explanation("current_retracement=0.037") == "Текущая коррекция: 3,7%"
    assert translate_explanation("spread_cost_atr=0.1643") == "Спред: 16,4% от ATR"


def test_ratio_metrics_are_human_readable() -> None:
    assert translate_explanation("rvol=0.836") == "RVOL: 0,84"
    assert translate_explanation("volume_percentile=75.0") == "Процентиль объёма: 75,0%"
    assert translate_explanation("body_efficiency=2.373") == (
        "Эффективность тела свечи: 2,37×"
    )
    assert translate_explanation("spread_ratio=1.011") == (
        "Текущий спред: 1,01× от обычного"
    )


def test_zero_sweep_depth_is_hidden() -> None:
    assert translate_explanation("sweep_depth_atr=0.000") == ""


def test_factor_html_hides_placeholder_depth_and_uses_russian_numbers() -> None:
    rendered = _factor_html(
        {
            "factor_scores": {"liquidity": 0.0, "volume": 0.49},
            "factor_reasons": {
                "liquidity": ["aligned_sweep=0", "sweep_depth_atr=0.000"],
                "volume": ["rvol=0.836", "volume_percentile=75.0"],
            },
        }
    )

    assert "Глубина съёма" not in rendered
    assert "RVOL: 0,84" in rendered
    assert "Процентиль объёма: 75,0%" in rendered
    assert "0.836" not in rendered


def test_market_context_uses_trader_friendly_units() -> None:
    rendered = _market_html(
        {
            "market": {
                "structure": {"swing_bias": "BULLISH"},
                "fibonacci": {
                    "retracement": 0.274,
                    "ote_low": 0.618,
                    "ote_mid": 0.705,
                    "ote_high": 0.79,
                },
                "volume": {
                    "rvol_20": 0.836,
                    "percentile": 75.0,
                    "tick_rate_ratio": 1.12,
                },
                "volatility": {"atr": 0.00024714, "spread_cost_atr": 0.1643},
            }
        }
    )

    assert "27,4%" in rendered
    assert "61,8%" in rendered
    assert "0,84" in rendered
    assert "75,0%" in rendered
    assert "1,12×" in rendered
    assert "0,00024714" in rendered
    assert "16,4% ATR" in rendered


def test_human_metrics_ui_remains_read_only() -> None:
    assert safety_contract() == {
        "read_only": True,
        "orders_enabled": False,
        "publication_enabled": False,
        "broker_api_called": False,
    }
