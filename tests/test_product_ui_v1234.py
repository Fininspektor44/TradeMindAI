from __future__ import annotations

from trademind.product_ui_v1234 import (
    _entries_html,
    safety_contract,
    translate_explanation,
)


def test_decimal_commas_survive_translation() -> None:
    translated = translate_explanation(
        "Fibonacci/OTE 70.5% retracement | "
        "Fibonacci/OTE 79.0% retracement | Prior/external low or 1.5R"
    )

    assert "70,5%" in translated
    assert "79,0%" in translated
    assert "1,5R" in translated
    assert "70 · 5%" not in translated
    assert "79 · 0%" not in translated
    assert "1 · 5R" not in translated


def test_existing_russian_decimal_comma_is_not_a_separator() -> None:
    translated = translate_explanation(
        "Коррекция Fibonacci/OTE 70,5% | цель 1,5R; фильтр пройден"
    )

    assert "70,5%" in translated
    assert "1,5R" in translated
    assert "70 · 5%" not in translated
    assert "1 · 5R" not in translated


def test_gate_word_is_replaced_with_human_label() -> None:
    translated = translate_explanation("Причины появятся после следующей проверки gate")

    assert "фильтр качества" in translated
    assert " gate" not in translated.lower()


def test_zero_placeholder_entry_weights_are_hidden() -> None:
    html = _entries_html(
        {
            "entries": [
                {
                    "price": 0.80762,
                    "weight": 0.0,
                    "rationale": "Fibonacci/OTE 70.5% retracement",
                },
                {
                    "price": 0.80748,
                    "weight": 0.0,
                    "rationale": "Fibonacci/OTE 79.0% retracement",
                },
            ]
        }
    )

    assert "0% ·" not in html
    assert "70,5%" in html
    assert "79,0%" in html


def test_real_normalized_entry_distribution_is_displayed() -> None:
    html = _entries_html(
        {
            "entries": [
                {"price": 1.1, "weight": 0.6, "rationale": "Основной вход"},
                {"price": 1.2, "weight": 0.4, "rationale": "Добор"},
            ]
        }
    )

    assert "60%" in html
    assert "40%" in html


def test_decimal_safe_ui_remains_read_only() -> None:
    assert safety_contract() == {
        "read_only": True,
        "orders_enabled": False,
        "publication_enabled": False,
        "broker_api_called": False,
    }
