from __future__ import annotations

from trademind.product_ui_v1233 import (
    _localize_candidate,
    normalize_market_language,
    safety_contract,
    translate_explanation,
)


def test_normalizes_mixed_market_sentence_from_live_ui() -> None:
    source = (
        "any smc event | bullish internal bos | internal bos | "
        "positive quote imbalance | quote pressure aligned | STRUCTURE_CONFLICT"
    )

    translated = translate_explanation(source)

    assert "есть событие Smart Money" in translated
    assert "бычий внутренний BOS" in translated
    assert "внутренний BOS" in translated
    assert "положительный дисбаланс котировок" in translated
    assert "давление котировок согласовано с направлением" in translated
    assert "конфликт структуры" in translated
    assert "any smc event" not in translated.lower()
    assert "quote pressure" not in translated.lower()


def test_normalizes_bos_and_choch_variants_without_losing_abbreviations() -> None:
    source = (
        "BEARISH_INTERNAL_CHOCH | bullish external bos | "
        "bearish bos | INTERNAL_CHOCH"
    )

    translated = translate_explanation(source)

    assert "медвежий внутренний CHoCH" in translated
    assert "бычий внешний BOS" in translated
    assert "медвежий BOS" in translated
    assert "внутренний CHoCH" in translated
    assert "BOS" in translated
    assert "CHoCH" in translated


def test_localizes_structure_values_inside_candidate() -> None:
    candidate = {
        "scenario": "any smc event | positive quote imbalance",
        "market": {
            "structure": {
                "swing_break": "bullish bos",
                "internal_break": "bearish internal choch",
            }
        },
    }

    localized = _localize_candidate(candidate)
    structure = localized["market"]["structure"]

    assert localized["scenario"] == (
        "есть событие Smart Money · положительный дисбаланс котировок"
    )
    assert structure["swing_break"] == "бычий BOS"
    assert structure["internal_break"] == "медвежий внутренний CHoCH"


def test_normalizer_handles_partially_translated_quote_pressure() -> None:
    assert normalize_market_language("quote pressure согласована") == (
        "давление котировок согласовано с направлением"
    )


def test_market_normalizer_remains_read_only() -> None:
    assert safety_contract() == {
        "read_only": True,
        "orders_enabled": False,
        "publication_enabled": False,
        "broker_api_called": False,
    }
