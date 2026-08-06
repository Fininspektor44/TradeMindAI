"""TradeMind Product UI v1.23.2 with human Russian explanations.

This module is a read-only presentation layer. It translates research phrases,
internal reason codes and metric diagnostics without changing signal, gate or
risk calculations.
"""

from __future__ import annotations

import argparse
import copy
import csv
import html
import json
import math
import os
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

from trademind import product_ui_v1231 as base

VERSION = "1.23.2"

PHRASE_REPLACEMENTS = {
    "EMA fast is below EMA slow": "Быстрая EMA ниже медленной",
    "EMA fast is above EMA slow": "Быстрая EMA выше медленной",
    "Price is below EMA slow": "Цена находится ниже медленной EMA",
    "Price is above EMA slow": "Цена находится выше медленной EMA",
    "RSI confirms bearish momentum": "RSI подтверждает медвежий импульс",
    "RSI confirms bullish momentum": "RSI подтверждает бычий импульс",
    "Latest candle closed higher": "Последняя свеча закрылась ростом",
    "Latest candle closed lower": "Последняя свеча закрылась снижением",
    "Latest candle closed bullish": "Последняя свеча закрылась ростом",
    "Latest candle closed bearish": "Последняя свеча закрылась снижением",
    "Market confirmation entry from the research signal close": (
        "Вход после подтверждения рынка на закрытии исследовательского сигнала"
    ),
    "Fibonacci/OTE 61.8% retracement": "Коррекция Fibonacci/OTE 61,8%",
    "Fibonacci/OTE 70.5% retracement": "Коррекция Fibonacci/OTE 70,5%",
    "Fibonacci/OTE 79.0% retracement": "Коррекция Fibonacci/OTE 79,0%",
    "Protected swing high breaks after the liquidity/OTE setup": (
        "Сценарий отменяется при пробое защищённого свингового максимума"
    ),
    "Protected swing low breaks after the liquidity/OTE setup": (
        "Сценарий отменяется при пробое защищённого свингового минимума"
    ),
    "Prior/external low or 1.5R": "Предыдущий или внешний минимум, либо 1,5R",
    "Prior/external high or 1.5R": "Предыдущий или внешний максимум, либо 1,5R",
    "External liquidity or 2R": "Внешняя ликвидность либо 2R",
    "portfolio correlation feed not connected yet": (
        "Данные о корреляции портфеля пока не подключены"
    ),
    "insufficient historical sample": "Недостаточная историческая выборка",
    "insufficient sample": "Недостаточная выборка",
    "negative expected value": "Отрицательное математическое ожидание",
    "profit factor below threshold": "Profit Factor ниже минимального порога",
    "quality score below threshold": "Оценка качества ниже минимального порога",
    "conservative probability below threshold": (
        "Консервативная вероятность ниже минимального порога"
    ),
    "maximum drawdown above threshold": "Просадка выше допустимого порога",
    "recent performance drift": "Недавняя статистика отклонилась от базовой",
    "candidate is stale": "Сетап уже устарел",
    "evidence is stale": "Статистические данные устарели",
}

TOKEN_TRANSLATIONS = {
    "ALL_SIGNALS": "основные сигнальные условия соблюдены",
    "LOW_SPREAD": "низкий спред",
    "HIGH_SPREAD": "повышенный спред",
    "NORMAL_VOLUME": "обычный объём",
    "LOW_VOLUME": "пониженный объём",
    "HIGH_VOLUME": "повышенный объём",
    "STRUCTURE_CONFLICT": "конфликт структуры",
    "STRUCTURE_ALIGNED": "структура согласована",
    "NO_STRUCTURE_BREAK": "нет подтверждённого слома структуры",
    "NO_LIQUIDITY_SWEEP": "нет подтверждённого съёма ликвидности",
    "NO_FVG": "нет подтверждённого FVG",
    "BULLISH": "бычий",
    "BEARISH": "медвежий",
    "NEUTRAL": "нейтральный",
    "NONE": "нет",
    "UNKNOWN": "не определено",
    "LONDON": "Лондонская сессия",
    "NEW_YORK": "Нью-Йоркская сессия",
    "ASIA": "Азиатская сессия",
    "LONDON_NY_OVERLAP": "Пересечение Лондона и Нью-Йорка",
    "PUBLISHABLE": "допущен к публикации",
    "SHADOW_ONLY": "теневое наблюдение",
    "PENDING_GATE": "ожидает проверки качества",
    "REJECTED": "отклонён",
}

CODE_WORDS = {
    "ALL": "все",
    "SIGNAL": "сигнал",
    "SIGNALS": "сигнальные условия",
    "LOW": "низкий",
    "HIGH": "высокий",
    "NORMAL": "нормальный",
    "SPREAD": "спред",
    "VOLUME": "объём",
    "STRUCTURE": "структура",
    "CONFLICT": "конфликт",
    "ALIGNED": "согласована",
    "BREAK": "слом",
    "LIQUIDITY": "ликвидность",
    "SWEEP": "съём",
    "CONFIRMATION": "подтверждение",
    "MOMENTUM": "импульс",
    "SESSION": "сессия",
    "PORTFOLIO": "портфель",
    "EXECUTION": "исполнение",
    "VOLATILITY": "волатильность",
    "CANDIDATE": "сетап",
    "EVIDENCE": "статистика",
    "FRESH": "свежий",
    "STALE": "устаревший",
    "READY": "готов",
}

METRIC_LABELS = {
    "swing_bias": "Старший уклон",
    "internal_bias": "Внутренняя структура",
    "aligned_sweep": "Подтверждённый съём ликвидности",
    "sweep_depth_atr": "Глубина съёма в ATR",
    "current_retracement": "Текущая коррекция",
    "rvol": "Относительный объём",
    "rvol_20": "Относительный объём за 20 свечей",
    "volume_percentile": "Процентиль объёма",
    "ema_aligned": "EMA согласованы",
    "body_efficiency": "Эффективность тела свечи",
    "fvg": "FVG",
    "break_confirmed": "Слом структуры подтверждён",
    "spread_ratio": "Отношение текущего спреда к обычному",
    "spread_cost_atr": "Стоимость спреда в ATR",
    "atr": "ATR",
    "correlation_load": "Корреляционная нагрузка",
    "tick_rate_ratio": "Отношение темпа тиков",
}

BOOLEAN_METRICS = {
    "aligned_sweep",
    "ema_aligned",
    "break_confirmed",
    "spread_ok",
}

CHECK_LABELS = {
    "minimum_sample": "Достаточная историческая выборка",
    "sample_size": "Достаточная историческая выборка",
    "quality_score": "Минимальная оценка качества",
    "conservative_probability": "Нижняя граница вероятности 95%",
    "profit_factor": "Минимальный Profit Factor",
    "expected_value": "Положительное математическое ожидание",
    "expected_value_r": "Положительное математическое ожидание",
    "max_drawdown": "Допустимая историческая просадка",
    "drawdown": "Допустимая историческая просадка",
    "recent_drift": "Стабильность недавней статистики",
    "drift": "Стабильность недавней статистики",
    "risk_reward": "Минимальное соотношение риск/прибыль",
    "candidate_fresh": "Сетап остаётся свежим",
    "candidate_age": "Допустимый возраст сетапа",
    "evidence_fresh": "Статистика остаётся актуальной",
    "evidence_age": "Допустимый возраст статистики",
    "setup_match": "Статистика относится к этому сетапу",
    "signal_integrity": "Целостность паспорта сигнала",
    "generated_from_market_data": "Сетап сформирован рыночными данными",
}

STATIC_REPLACEMENTS = {
    "LIVE MARKET INTELLIGENCE": "АНАЛИЗ РЫНКА В РЕАЛЬНОМ ВРЕМЕНИ",
    "Signal Intelligence": "Аналитика сигналов",
    "READ-ONLY": "ТОЛЬКО ЧТЕНИЕ",
    "Factory": "Фабрика паспортов",
    "Publishable": "Допущенные",
    "Quality": "Качество",
    "95% low": "Нижняя граница 95%",
    "Wilson 95%": "Нижняя граница 95%",
    "Publication gate": "Фильтр публикации",
    "publication gate": "фильтр публикации",
    "Broker API": "API брокера",
    "ALLOW/BLOCK": "РАЗРЕШЕНО / ЗАБЛОКИРОВАНО",
    "PUBLISHABLE-сетапа": "сетапа, прошедшего фильтр качества",
    "Smart Money": "Smart Money",
    "Order Flow": "Поток заявок",
    "Market State": "Состояние рынка",
}

UPPER_CODE_RE = re.compile(r"\b[A-Z][A-Z0-9_]{2,}\b")
METRIC_RE = re.compile(r"^\s*([A-Za-z][A-Za-z0-9_]*)\s*=\s*(.+?)\s*$")


def _humanize_unknown_code(token: str) -> str:
    words = token.split("_")
    translated = [CODE_WORDS.get(word, word.lower()) for word in words]
    return " ".join(translated)


def _translate_metric_part(part: str) -> str:
    match = METRIC_RE.match(part)
    if not match:
        return part.strip()
    key, raw_value = match.groups()
    label = METRIC_LABELS.get(key, key.replace("_", " ").capitalize())
    value = raw_value.strip()
    if key in BOOLEAN_METRICS and value in {"0", "1", "0.0", "1.0"}:
        value = "да" if float(value) == 1.0 else "нет"
    else:
        value = TOKEN_TRANSLATIONS.get(value.upper(), value)
    return f"{label}: {value}"


def translate_explanation(value: Any) -> str:
    result = str(value or "").strip()
    if not result:
        return ""
    for source, target in PHRASE_REPLACEMENTS.items():
        result = result.replace(source, target)
    parts = re.split(r"(\s*[|·;,]\s*)", result)
    translated_parts: list[str] = []
    for part in parts:
        if not part:
            continue
        if re.fullmatch(r"\s*[|·;,]\s*", part):
            translated_parts.append(" · ")
        else:
            translated_parts.append(_translate_metric_part(part))
    result = "".join(translated_parts)
    result = UPPER_CODE_RE.sub(
        lambda match: TOKEN_TRANSLATIONS.get(
            match.group(0), _humanize_unknown_code(match.group(0))
        ),
        result,
    )
    result = re.sub(r"(?:\s*·\s*){2,}", " · ", result)
    return result.strip(" ·")


def _translate_sequence(value: Any) -> list[Any]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    return [translate_explanation(item) if isinstance(item, str) else item for item in value]


def _localize_candidate(candidate: Mapping[str, Any]) -> dict[str, Any]:
    item = copy.deepcopy(dict(candidate))
    item["scenario"] = translate_explanation(item.get("scenario"))
    item["setup_family_label"] = translate_explanation(item.get("setup_family"))
    item["reasons"] = _translate_sequence(item.get("reasons"))

    factor_reasons = item.get("factor_reasons")
    if isinstance(factor_reasons, Mapping):
        item["factor_reasons"] = {
            str(key): _translate_sequence(values)
            for key, values in factor_reasons.items()
        }

    plan = item.get("plan")
    if isinstance(plan, Mapping):
        localized_plan = copy.deepcopy(dict(plan))
        localized_plan["invalidation"] = translate_explanation(
            localized_plan.get("invalidation")
        )
        localized_plan["target_rationale"] = _translate_sequence(
            localized_plan.get("target_rationale")
        )
        entries = localized_plan.get("entries")
        if isinstance(entries, Sequence) and not isinstance(entries, (str, bytes)):
            localized_entries = []
            for raw in entries:
                if not isinstance(raw, Mapping):
                    localized_entries.append(raw)
                    continue
                entry = copy.deepcopy(dict(raw))
                entry["rationale"] = translate_explanation(entry.get("rationale"))
                localized_entries.append(entry)
            localized_plan["entries"] = localized_entries
        item["plan"] = localized_plan

    market = item.get("market")
    if isinstance(market, Mapping):
        localized_market = copy.deepcopy(dict(market))
        for section_name, section in list(localized_market.items()):
            if isinstance(section, Mapping):
                localized_market[section_name] = {
                    str(key): translate_explanation(raw) if isinstance(raw, str) else raw
                    for key, raw in section.items()
                }
            elif isinstance(section, str):
                localized_market[section_name] = translate_explanation(section)
        item["market"] = localized_market

    checks = item.get("checks")
    if isinstance(checks, Mapping):
        item["checks"] = {
            CHECK_LABELS.get(str(key), str(key)): bool(raw)
            for key, raw in checks.items()
        }
    return item


def build_payload(
    data: Mapping[str, Any],
    canonical: Path | None,
    limit: int,
    candle_limit: int,
) -> dict[str, Any]:
    payload = base.build_payload(data, canonical, limit, candle_limit)
    payload["schema_version"] = VERSION
    payload["candidates"] = [
        _localize_candidate(candidate)
        for candidate in payload.get("candidates", [])
        if isinstance(candidate, Mapping)
    ]
    return payload


def _market_value(value: Any, digits: int = 2, suffix: str = "") -> str:
    if isinstance(value, bool):
        return "Да" if value else "Нет"
    if value is None or base.text(value) == "":
        return "—"
    if isinstance(value, (int, float)):
        return base.fmt(value, digits, suffix)
    return html.escape(translate_explanation(value))


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
    <p><span>Старший уклон</span><b>{_market_value(structure.get('swing_bias'))}</b></p>
    <p><span>Слом старшей структуры</span><b>{_market_value(structure.get('swing_break'))}</b></p>
    <p><span>Слом внутренней структуры</span><b>{_market_value(structure.get('internal_break'))}</b></p>
    <p><span>Съём ликвидности снизу</span><b>{_market_value(liquidity.get('ssl_sweep'))}</b></p>
    <p><span>Съём ликвидности сверху</span><b>{_market_value(liquidity.get('bsl_sweep'))}</b></p>
    <p><span>Ценовой разрыв FVG</span><b>{_market_value(confirmation.get('fvg'))}</b></p>
  </section>
  <section><small>Fibonacci</small><h4>Коррекция и зона OTE</h4>
    <p><span>Текущая коррекция</span><b>{_market_value(fibonacci.get('retracement'), 3)}</b></p>
    <p><span>Нижняя граница OTE</span><b>{_market_value(fibonacci.get('ote_low'), 5)}</b></p>
    <p><span>Средняя граница OTE</span><b>{_market_value(fibonacci.get('ote_mid'), 5)}</b></p>
    <p><span>Верхняя граница OTE</span><b>{_market_value(fibonacci.get('ote_high'), 5)}</b></p>
  </section>
  <section><small>Поток заявок</small><h4>Объёмы</h4>
    <p><span>Относительный объём за 20 свечей</span><b>{_market_value(volume.get('rvol_20'), 2)}</b></p>
    <p><span>Процентиль объёма</span><b>{_market_value(volume.get('percentile'), 1, '%')}</b></p>
    <p><span>Дисбаланс потока</span><b>{_market_value(volume.get('imbalance'), 2)}</b></p>
    <p><span>Отношение темпа тиков</span><b>{_market_value(volume.get('tick_rate_ratio'), 2)}</b></p>
  </section>
  <section><small>Состояние рынка</small><h4>Импульс и ATR</h4>
    <p><span>RSI</span><b>{_market_value(momentum.get('rsi'), 1)}</b></p>
    <p><span>Быстрая EMA</span><b>{_market_value(momentum.get('ema_fast'), 5)}</b></p>
    <p><span>Медленная EMA</span><b>{_market_value(momentum.get('ema_slow'), 5)}</b></p>
    <p><span>ATR</span><b>{_market_value(volatility.get('atr'), 5)}</b></p>
    <p><span>Стоимость спреда в ATR</span><b>{_market_value(volatility.get('spread_cost_atr'), 3)}</b></p>
  </section>
</div>
"""


def _factor_html(candidate: Mapping[str, Any]) -> str:
    scores = base._mapping(candidate.get("factor_scores"))
    reasons = base._mapping(candidate.get("factor_reasons"))
    if not scores:
        return "<div class='muted-box'>Факторные оценки ещё не сформированы.</div>"
    rows = []
    ordered = list(base.FACTOR_LABELS) + [
        key for key in scores if key not in base.FACTOR_LABELS
    ]
    for key in ordered:
        if key not in scores:
            continue
        pct = base._score_pct(scores.get(key))
        explanation = " · ".join(
            translate_explanation(item)
            for item in base._sequence(reasons.get(key))[:2]
            if base.text(item)
        )
        rows.append(
            "<div class='factor'>"
            f"<div><span>{html.escape(base.FACTOR_LABELS.get(key, key.title()))}</span>"
            f"<b>{pct:.0f}</b></div>"
            f"<i><em style='width:{pct:.1f}%'></em></i>"
            f"<small>{html.escape(explanation or 'Фактор учтён в общей модели')}</small>"
            "</div>"
        )
    return "".join(rows)


def _check_label(key: Any) -> str:
    raw = base.text(key)
    if raw in CHECK_LABELS.values():
        return raw
    if raw in CHECK_LABELS:
        return CHECK_LABELS[raw]
    return translate_explanation(raw.replace("_", " "))


def _checks_html(candidate: Mapping[str, Any]) -> str:
    checks = base._mapping(candidate.get("checks"))
    if not checks:
        return "<div class='muted-box'>Фильтр публикации ещё не сформировал полный набор проверок.</div>"
    rows = []
    for key, value in checks.items():
        passed = bool(value)
        rows.append(
            f"<div class='check {'pass' if passed else 'fail'}'>"
            f"<span>{'✓' if passed else '×'}</span>"
            f"<b>{html.escape(_check_label(key))}</b>"
            f"<small>{'Пройдено' if passed else 'Не пройдено'}</small></div>"
        )
    return "".join(rows)


def _risk_candidate_html(
    candidate: Mapping[str, Any], decision: Mapping[str, Any]
) -> str:
    state = base.text(candidate.get("state")).upper()
    summary = base._mapping(decision.get("trader_summary"))
    decision_signal_id = base.text(
        decision.get("signal_id")
        or base._mapping(decision.get("passport")).get("signal_id")
        or summary.get("signal_id")
    )
    candidate_id = base.text(candidate.get("signal_id"))
    matched = bool(summary) and (not decision_signal_id or decision_signal_id == candidate_id)
    if matched:
        return f"""
<div class='risk-strip'>
  <div><small>Решение</small><b>{html.escape(base.human_state(summary.get('decision')))}</b></div>
  <div><small>Риск</small><b>{base.fmt(summary.get('actual_risk_pct'), 3, '%')}</b></div>
  <div><small>Лот</small><b>{base.fmt(summary.get('total_volume'), 2)}</b></div>
  <div><small>Маржа</small><b>{base.fmt(summary.get('margin_required'), 2)}</b></div>
  <div><small>Свободно после</small><b>{base.fmt(summary.get('free_margin_after'), 2)}</b></div>
</div>
"""
    if state in {"PUBLISHABLE", "PASSPORTS_READY", "DECISION_READY", "ALLOW", "BLOCK"}:
        message = "Паспорт готов. Выполняется или уточняется расчёт для живого счёта."
    else:
        message = "Риск будет рассчитан только после прохождения фильтра публикации."
    return f"<div class='muted-box'>{html.escape(message)}</div>"


def _risk_page(decision: Mapping[str, Any]) -> str:
    summary = base._mapping(decision.get("trader_summary"))
    if not summary:
        return """
<div class='empty-state'><i>◇</i><h2>Risk Manager ждёт проверенный паспорт</h2>
<p>Лотность, риск деньгами и маржа появятся только после прохождения фильтра качества.</p></div>
"""
    return f"""
<div class='risk-hero'><span class='status {base.tone(summary.get("decision"))}'>{html.escape(base.human_state(summary.get("decision")))}</span>
<h2>{html.escape(base.text(summary.get("symbol")))} {html.escape(base.text(summary.get("action")))}</h2>
<p>Расчёт выполнен для подключённого счёта.</p></div>
<div class='metric-grid'>
  <div><small>Риск, %</small><b>{base.fmt(summary.get('actual_risk_pct'), 3, '%')}</b></div>
  <div><small>Риск деньгами</small><b>{base.fmt(summary.get('actual_risk_money'), 2)}</b></div>
  <div><small>Объём</small><b>{base.fmt(summary.get('total_volume'), 2)}</b></div>
  <div><small>Маржа</small><b>{base.fmt(summary.get('margin_required'), 2)}</b></div>
  <div><small>Свободно после</small><b>{base.fmt(summary.get('free_margin_after'), 2)}</b></div>
</div>
"""


def render(data: Mapping[str, Any]) -> str:
    original_market = base._market_html
    original_factor = base._factor_html
    original_checks = base._checks_html
    original_risk_candidate = base._risk_candidate_html
    original_risk_page = base._risk_page
    try:
        base._market_html = _market_html
        base._factor_html = _factor_html
        base._checks_html = _checks_html
        base._risk_candidate_html = _risk_candidate_html
        base._risk_page = _risk_page
        page = base.render(data)
    finally:
        base._market_html = original_market
        base._factor_html = original_factor
        base._checks_html = original_checks
        base._risk_candidate_html = original_risk_candidate
        base._risk_page = original_risk_page
    for source, target in STATIC_REPLACEMENTS.items():
        page = page.replace(source, target)
    return page


def run_product_ui(
    runtime_root: Path,
    limit: int = 24,
    candle_limit: int = 48,
) -> tuple[Path, Mapping[str, Any]]:
    root = runtime_root.expanduser().resolve()
    source = base.read_json(root / "dashboard" / "data.json")
    runtime = base._mapping(source.get("runtime"))
    paths = base._mapping(runtime.get("paths"))
    canonical = (
        Path(base.text(paths.get("canonical_volume"))).expanduser().resolve()
        if base.text(paths.get("canonical_volume"))
        else None
    )
    payload = build_payload(source, canonical, limit, candle_limit)
    output = root / "product"
    index = output / "index.html"
    base.atomic_write(index, render(payload))
    base.atomic_write(
        output / "data.json",
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
    )
    base.atomic_write(
        output / "status.json",
        json.dumps(
            {
                "schema_version": VERSION,
                "state": "OK",
                "index": str(index),
                "signals": len(payload.get("candidates", [])),
                "read_only": True,
            },
            ensure_ascii=False,
            indent=2,
        ),
    )
    return index, payload


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="TradeMind Product UI v1.23.2")
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
        print(f"TradeMind Product UI v1.23.2 failed: {exc}")
        return 1
    print("TradeMind Product UI v1.23.2")
    print("Russian human explanations. Read-only. Orders OFF. Publication OFF.")
    print(f"Signals displayed: {len(payload.get('candidates', []))}")
    print(f"Product UI: {index}")
    if args.open and hasattr(os, "startfile"):
        os.startfile(index)  # type: ignore[attr-defined]
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
