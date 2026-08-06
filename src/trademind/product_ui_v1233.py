"""TradeMind Product UI v1.23.3 market-language normalization.

This read-only presentation layer removes mixed Russian and English market
phrases while preserving professional abbreviations such as BOS and CHoCH.
Signal, gate and risk calculations remain unchanged.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

from trademind import product_ui_v1232 as ui
from trademind import product_ui_v1232_entry as entry

VERSION = "1.23.3"
_BASE_TRANSLATE = entry.translate_explanation
_BASE_BUILD_PAYLOAD = ui.build_payload

MARKET_CODE_TRANSLATIONS = {
    "ANY_SMC_EVENT": "есть событие Smart Money",
    "SMC_EVENT": "событие Smart Money",
    "BULLISH_BOS": "бычий BOS",
    "BEARISH_BOS": "медвежий BOS",
    "BULLISH_CHOCH": "бычий CHoCH",
    "BEARISH_CHOCH": "медвежий CHoCH",
    "BULLISH_INTERNAL_BOS": "бычий внутренний BOS",
    "BEARISH_INTERNAL_BOS": "медвежий внутренний BOS",
    "BULLISH_EXTERNAL_BOS": "бычий внешний BOS",
    "BEARISH_EXTERNAL_BOS": "медвежий внешний BOS",
    "BULLISH_INTERNAL_CHOCH": "бычий внутренний CHoCH",
    "BEARISH_INTERNAL_CHOCH": "медвежий внутренний CHoCH",
    "BULLISH_EXTERNAL_CHOCH": "бычий внешний CHoCH",
    "BEARISH_EXTERNAL_CHOCH": "медвежий внешний CHoCH",
    "INTERNAL_BOS": "внутренний BOS",
    "EXTERNAL_BOS": "внешний BOS",
    "INTERNAL_CHOCH": "внутренний CHoCH",
    "EXTERNAL_CHOCH": "внешний CHoCH",
    "POSITIVE_QUOTE_IMBALANCE": "положительный дисбаланс котировок",
    "NEGATIVE_QUOTE_IMBALANCE": "отрицательный дисбаланс котировок",
    "NEUTRAL_QUOTE_IMBALANCE": "нейтральный дисбаланс котировок",
    "QUOTE_PRESSURE_ALIGNED": "давление котировок согласовано с направлением",
    "QUOTE_PRESSURE_OPPOSED": "давление котировок направлено против сетапа",
    "QUOTE_PRESSURE_NEUTRAL": "давление котировок нейтральное",
}

MARKET_PHRASE_PATTERNS = (
    (r"\bbullish[_\s]+internal[_\s]+choch\b", "бычий внутренний CHoCH"),
    (r"\bbearish[_\s]+internal[_\s]+choch\b", "медвежий внутренний CHoCH"),
    (r"\bbullish[_\s]+external[_\s]+choch\b", "бычий внешний CHoCH"),
    (r"\bbearish[_\s]+external[_\s]+choch\b", "медвежий внешний CHoCH"),
    (r"\bbullish[_\s]+internal[_\s]+bos\b", "бычий внутренний BOS"),
    (r"\bbearish[_\s]+internal[_\s]+bos\b", "медвежий внутренний BOS"),
    (r"\bbullish[_\s]+external[_\s]+bos\b", "бычий внешний BOS"),
    (r"\bbearish[_\s]+external[_\s]+bos\b", "медвежий внешний BOS"),
    (r"\bpositive[_\s]+quote[_\s]+imbalance\b", "положительный дисбаланс котировок"),
    (r"\bnegative[_\s]+quote[_\s]+imbalance\b", "отрицательный дисбаланс котировок"),
    (r"\bneutral[_\s]+quote[_\s]+imbalance\b", "нейтральный дисбаланс котировок"),
    (
        r"\bquote[_\s]+pressure[_\s]+(?:aligned|согласована|согласовано)\b",
        "давление котировок согласовано с направлением",
    ),
    (
        r"\bquote[_\s]+pressure[_\s]+(?:opposed|opposite|противоположно)\b",
        "давление котировок направлено против сетапа",
    ),
    (r"\bquote[_\s]+pressure[_\s]+neutral\b", "давление котировок нейтральное"),
    (r"\bany[_\s]+smc[_\s]+event\b", "есть событие Smart Money"),
    (r"\bsmc[_\s]+event\b", "событие Smart Money"),
    (r"\bbullish[_\s]+choch\b", "бычий CHoCH"),
    (r"\bbearish[_\s]+choch\b", "медвежий CHoCH"),
    (r"\binternal[_\s]+choch\b", "внутренний CHoCH"),
    (r"\bexternal[_\s]+choch\b", "внешний CHoCH"),
    (r"\bbullish[_\s]+bos\b", "бычий BOS"),
    (r"\bbearish[_\s]+bos\b", "медвежий BOS"),
    (r"\binternal[_\s]+bos\b", "внутренний BOS"),
    (r"\bexternal[_\s]+bos\b", "внешний BOS"),
    (r"\bquote[_\s]+imbalance\b", "дисбаланс котировок"),
    (r"\bquote[_\s]+pressure\b", "давление котировок"),
)

_COMPILED_PATTERNS = tuple(
    (re.compile(pattern, flags=re.IGNORECASE), replacement)
    for pattern, replacement in MARKET_PHRASE_PATTERNS
)

ui.TOKEN_TRANSLATIONS.update(
    {
        **MARKET_CODE_TRANSLATIONS,
        "BOS": "BOS",
        "CHOCH": "CHoCH",
        "SMC": "Smart Money",
    }
)
ui.STATIC_REPLACEMENTS.update(
    {
        "TradeMind Product UI v1.23.1": "TradeMind Product UI v1.23.3",
        "TradeMind Product UI v1.23.2": "TradeMind Product UI v1.23.3",
    }
)


def normalize_market_language(value: Any) -> str:
    result = str(value or "").strip()
    for pattern, replacement in _COMPILED_PATTERNS:
        result = pattern.sub(replacement, result)
    return result


def translate_explanation(value: Any) -> str:
    prepared = normalize_market_language(value)
    translated = _BASE_TRANSLATE(prepared)
    return normalize_market_language(translated)


def build_payload(
    data: Mapping[str, Any],
    canonical: Path | None,
    limit: int,
    candle_limit: int,
) -> dict[str, Any]:
    payload = _BASE_BUILD_PAYLOAD(data, canonical, limit, candle_limit)
    payload["schema_version"] = VERSION
    return payload


ui.VERSION = VERSION
ui.translate_explanation = translate_explanation
ui.build_payload = build_payload

_localize_candidate = ui._localize_candidate
render = ui.render
run_product_ui = ui.run_product_ui


def safety_contract() -> Mapping[str, Any]:
    return {
        "read_only": True,
        "orders_enabled": False,
        "publication_enabled": False,
        "broker_api_called": False,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="TradeMind Product UI v1.23.3")
    parser.add_argument(
        "--runtime-root",
        type=Path,
        default=Path("data/live_signal_runtime_v1"),
    )
    parser.add_argument("--limit", type=int, default=24)
    parser.add_argument("--candle-limit", type=int, default=48)
    parser.add_argument("--open", action="store_true")
    args = parser.parse_args(argv)
    try:
        index, payload = run_product_ui(
            args.runtime_root,
            args.limit,
            args.candle_limit,
        )
    except (OSError, ValueError, TypeError, csv.Error, json.JSONDecodeError) as exc:
        print(f"TradeMind Product UI v1.23.3 failed: {exc}")
        return 1
    print("TradeMind Product UI v1.23.3")
    print("Normalized Russian market language. Read-only. Orders OFF. Publication OFF.")
    print(f"Signals displayed: {len(payload.get('candidates', []))}")
    print(f"Product UI: {index}")
    if args.open and hasattr(os, "startfile"):
        os.startfile(index)  # type: ignore[attr-defined]
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
