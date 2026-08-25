"""TradeMind Product UI v1.23.5 with human-readable market metrics.

This read-only presentation layer formats research diagnostics as trader-facing
Russian values. It does not change signal, publication-gate or risk decisions.
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
from trademind import product_ui_v1234 as previous

VERSION = "1.23.5"
_BASE_TRANSLATE = previous.translate_explanation
_BASE_BUILD_PAYLOAD = previous.build_payload
_BASE_RENDER = previous.render
_NUMBER_RE = re.compile(r"^[-+]?\d+(?:[.,]\d+)?$")


def _number(value: Any) -> float | None:
    raw = str(value or "").strip().replace(",", ".")
    if not _NUMBER_RE.fullmatch(raw):
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _ru_number(value: Any, digits: int = 2, *, trim: bool = False) -> str:
    number = _number(value)
    if number is None:
        return "—"
    rendered = f"{number:.{digits}f}"
    if trim and "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered.replace(".", ",")


def _fraction_percent(value: Any, digits: int = 1) -> str:
    number = _number(value)
    if number is None:
        return "—"
    scaled = number * 100.0 if abs(number) <= 2.0 else number
    return f"{_ru_number(scaled, digits)}%"


def _humanize_metric_segment(segment: str) -> str:
    value = segment.strip()
    if not value or ":" not in value:
        return re.sub(r"(?<=\d)\.(?=\d)", ",", value)

    label, raw = (part.strip() for part in value.split(":", 1))
    numeric = _number(raw)
    if numeric is None:
        return re.sub(r"(?<=\d)\.(?=\d)", ",", value)

    if label == "Глубина съёма в ATR":
        if abs(numeric) < 1e-12:
            return ""
        return f"Глубина съёма: {_ru_number(numeric, 2, trim=True)} ATR"
    if label == "Текущая коррекция":
        return f"Текущая коррекция: {_fraction_percent(numeric)}"
    if label in {"Относительный объём", "Относительный объём за 20 свечей"}:
        return f"RVOL: {_ru_number(numeric, 2)}"
    if label == "Процентиль объёма":
        return f"Процентиль объёма: {_ru_number(numeric, 1)}%"
    if label == "Эффективность тела свечи":
        return f"Эффективность тела свечи: {_ru_number(numeric, 2)}×"
    if label == "Стоимость спреда в ATR":
        return f"Спред: {_fraction_percent(numeric)} от ATR"
    if label == "Отношение текущего спреда к обычному":
        return f"Текущий спред: {_ru_number(numeric, 2)}× от обычного"
    if label == "Отношение темпа тиков":
        return f"Темп тиков: {_ru_number(numeric, 2)}× от обычного"
    if label == "Корреляционная нагрузка":
        return f"Корреляционная нагрузка: {_fraction_percent(numeric)}"
    if label == "ATR":
        return f"ATR: {_ru_number(numeric, 8, trim=True)}"
    if label in {"Дисбаланс потока", "Дисбаланс котировок"}:
        return f"{label}: {_ru_number(numeric, 2)}"
    return f"{label}: {_ru_number(numeric, 3, trim=True)}"


def translate_explanation(value: Any) -> str:
    """Translate and format technical metric diagnostics for a trader."""

    translated = _BASE_TRANSLATE(value)
    if not translated:
        return ""
    parts = [_humanize_metric_segment(part) for part in translated.split(" · ")]
    return " · ".join(part for part in parts if part).strip(" ·")


def _text_value(value: Any) -> str:
    if isinstance(value, bool):
        return "Да" if value else "Нет"
    if value is None or base.text(value) == "":
        return "—"
    if isinstance(value, (int, float)):
        return _ru_number(value, 2, trim=True)
    return html.escape(translate_explanation(value))


def _metric_value(value: Any, digits: int = 2, suffix: str = "", *, trim: bool = False) -> str:
    if value is None or base.text(value) == "":
        return "—"
    return f"{_ru_number(value, digits, trim=trim)}{suffix}"


def _market_html(candidate: Mapping[str, Any]) -> str:
    market = base._mapping(candidate.get("market"))
    structure = base._mapping(market.get("structure"))
    liquidity = base._mapping(market.get("liquidity"))
    fibonacci = base._mapping(market.get("fibonacci"))
    volume = base._mapping(market.get("volume"))
    momentum = base._mapping(market.get("momentum"))
    volatility = base._mapping(market.get("volatility"))
    confirmation = base._mapping(market.get("confirmation"))
    return f"""
<div class='market-grid'>
  <section><small>Smart Money</small><h4>Структура и ликвидность</h4>
    <p><span>Старший уклон</span><b>{_text_value(structure.get('swing_bias'))}</b></p>
    <p><span>Слом старшей структуры</span><b>{_text_value(structure.get('swing_break'))}</b></p>
    <p><span>Слом внутренней структуры</span><b>{_text_value(structure.get('internal_break'))}</b></p>
    <p><span>Съём ликвидности снизу</span><b>{_text_value(liquidity.get('ssl_sweep'))}</b></p>
    <p><span>Съём ликвидности сверху</span><b>{_text_value(liquidity.get('bsl_sweep'))}</b></p>
    <p><span>Ценовой разрыв FVG</span><b>{_text_value(confirmation.get('fvg'))}</b></p>
  </section>
  <section><small>Fibonacci</small><h4>Коррекция и зона OTE</h4>
    <p><span>Текущая коррекция</span><b>{_fraction_percent(fibonacci.get('retracement'))}</b></p>
    <p><span>Нижняя граница OTE</span><b>{_fraction_percent(fibonacci.get('ote_low'))}</b></p>
    <p><span>Средняя граница OTE</span><b>{_fraction_percent(fibonacci.get('ote_mid'))}</b></p>
    <p><span>Верхняя граница OTE</span><b>{_fraction_percent(fibonacci.get('ote_high'))}</b></p>
  </section>
  <section><small>Поток заявок</small><h4>Объёмы</h4>
    <p><span>RVOL за 20 свечей</span><b>{_metric_value(volume.get('rvol_20'), 2)}</b></p>
    <p><span>Процентиль объёма</span><b>{_metric_value(volume.get('percentile'), 1, '%')}</b></p>
    <p><span>Дисбаланс потока</span><b>{_metric_value(volume.get('imbalance'), 2)}</b></p>
    <p><span>Темп тиков к обычному</span><b>{_metric_value(volume.get('tick_rate_ratio'), 2, '×')}</b></p>
  </section>
  <section><small>Состояние рынка</small><h4>Импульс и ATR</h4>
    <p><span>Импульс / ATR</span><b>{_metric_value(momentum.get('impulse_atr'), 2)}</b></p>
    <p><span>Эффективность тела</span><b>{_metric_value(momentum.get('body_efficiency_ratio_20'), 2)}</b></p>
    <p><span>ATR</span><b>{_metric_value(volatility.get('atr'), 8, trim=True)}</b></p>
    <p><span>Стоимость спреда</span><b>{_fraction_percent(volatility.get('spread_cost_atr'))} ATR</b></p>
  </section>
</div>
"""


def _factor_html(candidate: Mapping[str, Any]) -> str:
    scores = base._mapping(candidate.get("factor_scores"))
    reasons = base._mapping(candidate.get("factor_reasons"))
    if not scores:
        return "<div class='muted-box'>Факторные оценки ещё не сформированы.</div>"

    rows: list[str] = []
    ordered = list(base.FACTOR_LABELS) + [
        key for key in scores if key not in base.FACTOR_LABELS
    ]
    for key in ordered:
        if key not in scores:
            continue
        pct = base._score_pct(scores.get(key))
        translated_reasons = [
            translate_explanation(item)
            for item in base._sequence(reasons.get(key))[:2]
            if base.text(item)
        ]
        explanation = " · ".join(item for item in translated_reasons if item)
        rows.append(
            "<div class='factor'>"
            f"<div><span>{html.escape(base.FACTOR_LABELS.get(key, key.title()))}</span>"
            f"<b>{pct:.0f}</b></div>"
            f"<i><em style='width:{pct:.1f}%'></em></i>"
            f"<small>{html.escape(explanation or 'Фактор учтён в общей модели')}</small>"
            "</div>"
        )
    return "".join(rows)


def build_payload(
    data: Mapping[str, Any],
    canonical: Path | None,
    limit: int,
    candle_limit: int,
) -> dict[str, Any]:
    payload = _BASE_BUILD_PAYLOAD(data, canonical, limit, candle_limit)
    payload["schema_version"] = VERSION
    return payload


def render(data: Mapping[str, Any]) -> str:
    original_market = ui._market_html
    original_factor = ui._factor_html
    try:
        ui._market_html = _market_html
        ui._factor_html = _factor_html
        page = _BASE_RENDER(data)
    finally:
        ui._market_html = original_market
        ui._factor_html = original_factor

    return page.replace(
        "TradeMind Product UI v1.23.4",
        "TradeMind Product UI v1.23.5",
    )


ui.VERSION = VERSION
ui.translate_explanation = translate_explanation
ui.build_payload = build_payload
ui.render = render
ui.STATIC_REPLACEMENTS.update(
    {
        "TradeMind Product UI v1.23.4": "TradeMind Product UI v1.23.5",
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
    parser = argparse.ArgumentParser(description="TradeMind Product UI v1.23.5")
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
        print(f"TradeMind Product UI v1.23.5 failed: {exc}")
        return 1
    print("TradeMind Product UI v1.23.5")
    print("Human-readable market metrics. Read-only. Orders OFF. Publication OFF.")
    print(f"Signals displayed: {len(payload.get('candidates', []))}")
    print(f"Product UI: {index}")
    if args.open and hasattr(os, "startfile"):
        os.startfile(index)  # type: ignore[attr-defined]
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
