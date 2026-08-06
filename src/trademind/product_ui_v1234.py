"""TradeMind Product UI v1.23.4 with decimal-safe Russian text.

This read-only presentation layer preserves decimal commas, hides placeholder
zero-percent entry allocations and replaces the remaining gate jargon with
human wording. Signal, gate and risk calculations remain unchanged.
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import os
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

from trademind import product_ui_v1231 as base
from trademind import product_ui_v1232 as ui
from trademind import product_ui_v1233 as previous

VERSION = "1.23.4"
_DECIMAL_COMMA = "\ue000"
_SEPARATOR_RE = re.compile(r"(\s*[|·;,]\s*)")
_SEPARATOR_ONLY_RE = re.compile(r"\s*[|·;,]\s*")
_GATE_RE = re.compile(r"\bgate\b", flags=re.IGNORECASE)


def _protect_decimal_commas(value: str) -> str:
    return re.sub(r"(?<=\d),(?=\d)", _DECIMAL_COMMA, value)


def _restore_decimal_commas(value: str) -> str:
    return value.replace(_DECIMAL_COMMA, ",")


def translate_explanation(value: Any) -> str:
    """Translate text without treating decimal commas as list separators."""

    result = previous.normalize_market_language(value)
    if not result:
        return ""

    for source, target in ui.PHRASE_REPLACEMENTS.items():
        result = result.replace(source, target)

    result = _protect_decimal_commas(result)
    parts = _SEPARATOR_RE.split(result)
    translated_parts: list[str] = []
    for part in parts:
        if not part:
            continue
        if _SEPARATOR_ONLY_RE.fullmatch(part):
            translated_parts.append(" · ")
        else:
            translated_parts.append(ui._translate_metric_part(part))

    result = "".join(translated_parts)
    result = ui.UPPER_CODE_RE.sub(
        lambda match: ui.TOKEN_TRANSLATIONS.get(
            match.group(0), ui._humanize_unknown_code(match.group(0))
        ),
        result,
    )
    result = re.sub(r"(?:\s*·\s*){2,}", " · ", result)
    result = _restore_decimal_commas(result).strip(" ·")
    result = previous.normalize_market_language(result)
    return _GATE_RE.sub("фильтр качества", result)


def _entries_html(plan: Mapping[str, Any]) -> str:
    """Render entry allocation only when weights form a real 100% distribution."""

    entries = base._sequence(plan.get("entries"))
    if not entries:
        return (
            "<div class='entry-row'><span>Средний вход</span>"
            f"<b>{base.fmt(plan.get('average_entry'), 5)}</b>"
            "<small>Основная точка</small></div>"
        )

    mapped = [base._mapping(raw) for raw in entries]
    weights = [max(0.0, base.number(item.get("weight"), 0.0)) for item in mapped]
    total_weight = sum(weights)
    allocation_ready = bool(weights) and abs(total_weight - 1.0) <= 0.001

    rows: list[str] = []
    for index, (item, weight) in enumerate(zip(mapped, weights, strict=True), start=1):
        rationale = translate_explanation(item.get("rationale")) or "Лесенка входов"
        details: list[str] = []
        if allocation_ready:
            details.append(base.fmt(100.0 * weight, 0, "%"))
        details.append(rationale)
        rows.append(
            "<div class='entry-row'>"
            f"<span>Вход {index}</span>"
            f"<b>{base.fmt(item.get('price'), 5)}</b>"
            f"<small>{html.escape(' · '.join(details))}</small>"
            "</div>"
        )
    return "".join(rows)


def build_payload(
    data: Mapping[str, Any],
    canonical: Path | None,
    limit: int,
    candle_limit: int,
) -> dict[str, Any]:
    payload = previous.build_payload(data, canonical, limit, candle_limit)
    payload["schema_version"] = VERSION
    return payload


def render(data: Mapping[str, Any]) -> str:
    original_entries = base._entries_html
    try:
        base._entries_html = _entries_html
        page = previous.render(data)
    finally:
        base._entries_html = original_entries

    replacements = {
        "TradeMind Product UI v1.23.3": "TradeMind Product UI v1.23.4",
        "Причины будут добавлены после следующей проверки gate.": (
            "Причины будут добавлены после следующей проверки фильтра качества."
        ),
        "Publication gate": "Фильтр качества",
        "publication gate": "фильтр качества",
        "Фильтр публикации": "Фильтр качества",
        "фильтр публикации": "фильтр качества",
    }
    for source, target in replacements.items():
        page = page.replace(source, target)
    return page


ui.VERSION = VERSION
ui.translate_explanation = translate_explanation
ui.build_payload = build_payload
ui.render = render
ui.STATIC_REPLACEMENTS.update(
    {
        "TradeMind Product UI v1.23.3": "TradeMind Product UI v1.23.4",
        "Publication gate": "Фильтр качества",
        "publication gate": "фильтр качества",
    }
)

_localize_candidate = ui._localize_candidate
run_product_ui = ui.run_product_ui


def safety_contract() -> Mapping[str, Any]:
    return {
        "read_only": True,
        "orders_enabled": False,
        "publication_enabled": False,
        "broker_api_called": False,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="TradeMind Product UI v1.23.4")
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
        print(f"TradeMind Product UI v1.23.4 failed: {exc}")
        return 1
    print("TradeMind Product UI v1.23.4")
    print("Decimal-safe Russian UI. Read-only. Orders OFF. Publication OFF.")
    print(f"Signals displayed: {len(payload.get('candidates', []))}")
    print(f"Product UI: {index}")
    if args.open and hasattr(os, "startfile"):
        os.startfile(index)  # type: ignore[attr-defined]
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
