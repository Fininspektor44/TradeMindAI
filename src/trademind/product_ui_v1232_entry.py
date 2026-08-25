"""Runtime entry for Product UI v1.23.2.

Keeps established market abbreviations intact while the explanation engine
translates internal codes and English research phrases.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from trademind import product_ui_v1232 as ui

PRESERVED_MARKET_TERMS = {
    "ATR": "ATR",
    "FVG": "FVG",
    "OTE": "OTE",
    "SSL": "SSL",
    "BSL": "BSL",
    "RVOL": "RVOL",
    "SMC": "SMC",
    "TP": "TP",
}

ui.TOKEN_TRANSLATIONS.update(PRESERVED_MARKET_TERMS)
ui.STATIC_REPLACEMENTS = {
    "Состояние Factory": "Состояние фабрики паспортов",
    "Passport Factory": "Фабрика паспортов",
    **ui.STATIC_REPLACEMENTS,
}

VERSION = ui.VERSION
translate_explanation = ui.translate_explanation
_localize_candidate = ui._localize_candidate
build_payload = ui.build_payload
render = ui.render
run_product_ui = ui.run_product_ui


def main(argv: Sequence[str] | None = None) -> int:
    return ui.main(argv)


def safety_contract() -> Mapping[str, Any]:
    return {
        "read_only": True,
        "orders_enabled": False,
        "publication_enabled": False,
        "broker_api_called": False,
    }


if __name__ == "__main__":
    raise SystemExit(main())
