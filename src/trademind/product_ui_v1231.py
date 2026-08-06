"""TradeMind Product UI v1.23.1, polished read-only interface."""

from __future__ import annotations

import argparse
import csv
import html
import json
import math
import os
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

from trademind.product_ui import (
    atomic_write,
    build_payload as build_legacy_payload,
    candle_svg,
    display_time,
    fmt,
    human_state,
    integer,
    number,
    read_json,
    text,
    tone,
)

VERSION = "1.23.1"

FACTOR_LABELS = {
    "structure": "Структура",
    "liquidity": "Ликвидность",
    "fibonacci": "Fibonacci",
    "volume": "Объёмы",
    "momentum": "Импульс",
    "volatility": "Волатильность",
    "confirmation": "Подтверждение",
    "session": "Сессия",
    "execution": "Исполнение",
    "portfolio": "Портфель",
}

STATE_PRIORITY = {
    "ALLOW": 0,
    "DECISION_READY": 0,
    "PASSPORTS_READY": 0,
    "PUBLISHABLE": 0,
    "PENDING_GATE": 1,
    "SHADOW_ONLY": 1,
    "REJECTED": 2,
    "BLOCK": 2,
    "OUTCOME_WIN": 3,
    "OUTCOME_LOSS": 3,
    "OUTCOME_FLAT": 3,
}


def _safe_id(value: Any, fallback: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_-]+", "-", text(value)).strip("-")
    return cleaned[:80] or fallback


def _sequence(value: Any) -> list[Any]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return list(value)
    return []


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _score_pct(value: Any) -> float:
    score = number(value, math.nan)
    if not math.isfinite(score):
        return 0.0
    if score <= 1.5:
        score *= 100.0
    return max(0.0, min(100.0, score))


def _created_sort_value(candidate: Mapping[str, Any]) -> str:
    return text(candidate.get("created_at"))


def _state_rank(candidate: Mapping[str, Any]) -> int:
    return STATE_PRIORITY.get(text(candidate.get("state")).upper(), 2)


def sort_candidates(candidates: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    rows = [dict(item) for item in candidates]
    rows.sort(
        key=lambda item: (
            _state_rank(item),
            -number(item.get("quality_score"), -1.0),
            _created_sort_value(item),
        )
    )
    grouped: list[dict[str, Any]] = []
    for rank in sorted({_state_rank(item) for item in rows}):
        band = [item for item in rows if _state_rank(item) == rank]
        band.sort(
            key=lambda item: (
                number(item.get("quality_score"), -1.0),
                _created_sort_value(item),
            ),
            reverse=True,
        )
        grouped.extend(band)
    return grouped


def build_payload(
    data: Mapping[str, Any],
    canonical: Path | None,
    limit: int,
    candle_limit: int,
) -> dict[str, Any]:
    payload = build_legacy_payload(data, canonical, limit, candle_limit)
    candidates = sort_candidates(
        [item for item in payload.get("candidates", []) if isinstance(item, Mapping)]
    )
    payload["schema_version"] = VERSION
    payload["candidates"] = candidates
    summary = dict(payload.get("summary", {}))
    archive_candidates = integer(summary.get("total_candidates"))
    summary.update(
        {
            "archive_candidates": archive_candidates,
            "displayed_candidates": len(candidates),
            "active_candidates": sum(
                not text(item.get("state")).upper().startswith("OUTCOME_")
                for item in candidates
            ),
            "buy": sum(text(item.get("action")).upper() == "BUY" for item in candidates),
            "sell": sum(text(item.get("action")).upper() == "SELL" for item in candidates),
        }
    )
    summary.pop("total_candidates", None)
    payload["summary"] = summary
    return payload


def _plan(candidate: Mapping[str, Any]) -> Mapping[str, Any]:
    return _mapping(candidate.get("plan"))


def _first_target(candidate: Mapping[str, Any]) -> Any:
    targets = _sequence(_plan(candidate).get("targets"))
    return targets[0] if targets else None


def _status_group(candidate: Mapping[str, Any]) -> str:
    state = text(candidate.get("state")).upper()
    if state in {"ALLOW", "DECISION_READY", "PASSPORTS_READY", "PUBLISHABLE"}:
        return "READY"
    if state in {"PENDING_GATE", "SHADOW_ONLY"}:
        return "REVIEW"
    if state.startswith("OUTCOME_"):
        return "OUTCOME"
    return "REJECTED"


def _entries_html(plan: Mapping[str, Any]) -> str:
    entries = _sequence(plan.get("entries"))
    if not entries:
        return (
            "<div class='entry-row'><span>Средний вход</span>"
            f"<b>{fmt(plan.get('average_entry'), 5)}</b><small>Основная точка</small></div>"
        )
    rows: list[str] = []
    for index, raw in enumerate(entries, start=1):
        item = _mapping(raw)
        rows.append(
            "<div class='entry-row'>"
            f"<span>Вход {index}</span>"
            f"<b>{fmt(item.get('price'), 5)}</b>"
            f"<small>{fmt(100 * number(item.get('weight')), 0, '%')} · "
            f"{html.escape(text(item.get('rationale')) or 'Лесенка входов')}</small>"
            "</div>"
        )
    return "".join(rows)


def _targets_html(plan: Mapping[str, Any]) -> str:
    targets = _sequence(plan.get("targets"))
    rationales = _sequence(plan.get("target_rationale"))
    if not targets:
        return "<div class='entry-row'><span>Цели</span><b>—</b><small>Не рассчитаны</small></div>"
    rows = []
    for index, target in enumerate(targets, start=1):
        rationale = text(rationales[index - 1]) if index - 1 < len(rationales) else ""
        rows.append(
            "<div class='entry-row'>"
            f"<span>TP{index}</span><b>{fmt(target, 5)}</b>"
            f"<small>{html.escape(rationale or 'Цель торгового плана')}</small>"
            "</div>"
        )
    return "".join(rows)


def _factor_html(candidate: Mapping[str, Any]) -> str:
    scores = _mapping(candidate.get("factor_scores"))
    reasons = _mapping(candidate.get("factor_reasons"))
    if not scores:
        return "<div class='muted-box'>Факторные оценки ещё не сформированы.</div>"
    rows = []
    ordered = list(FACTOR_LABELS) + [key for key in scores if key not in FACTOR_LABELS]
    for key in ordered:
        if key not in scores:
            continue
        pct = _score_pct(scores.get(key))
        raw_reasons = _sequence(reasons.get(key))
        explanation = " · ".join(text(item) for item in raw_reasons[:2] if text(item))
        rows.append(
            "<div class='factor'>"
            f"<div><span>{html.escape(FACTOR_LABELS.get(key, key.title()))}</span>"
            f"<b>{pct:.0f}</b></div>"
            f"<i><em style='width:{pct:.1f}%'></em></i>"
            f"<small>{html.escape(explanation or 'Фактор учтён в общей модели')}</small>"
            "</div>"
        )
    return "".join(rows)


def _checks_html(candidate: Mapping[str, Any]) -> str:
    checks = _mapping(candidate.get("checks"))
    if not checks:
        return "<div class='muted-box'>Publication gate ещё не выдал полный набор проверок.</div>"
    rows = []
    for key, value in checks.items():
        passed = bool(value)
        rows.append(
            f"<div class='check {'pass' if passed else 'fail'}'>"
            f"<span>{'✓' if passed else '×'}</span>"
            f"<b>{html.escape(text(key).replace('_', ' ').title())}</b>"
            f"<small>{'Пройдено' if passed else 'Не пройдено'}</small></div>"
        )
    return "".join(rows)


def _market_value(value: Any, digits: int = 2, suffix: str = "") -> str:
    if isinstance(value, bool):
        return "Да" if value else "Нет"
    if value is None or text(value) == "":
        return "—"
    if isinstance(value, (int, float)):
        return fmt(value, digits, suffix)
    return html.escape(text(value))


def _market_html(candidate: Mapping[str, Any]) -> str:
    market = _mapping(candidate.get("market"))
    structure = _mapping(market.get("structure"))
    liquidity = _mapping(market.get("liquidity"))
    fibonacci = _mapping(market.get("fibonacci"))
    volume = _mapping(market.get("volume"))
    momentum = _mapping(market.get("momentum"))
    volatility = _mapping(market.get("volatility"))
    confirmation = _mapping(market.get("confirmation"))
    return f"""
<div class='market-grid'>
  <section><small>Smart Money</small><h4>Структура и ликвидность</h4>
    <p><span>Swing bias</span><b>{_market_value(structure.get('swing_bias'))}</b></p>
    <p><span>Swing break</span><b>{_market_value(structure.get('swing_break'))}</b></p>
    <p><span>Internal break</span><b>{_market_value(structure.get('internal_break'))}</b></p>
    <p><span>SSL sweep</span><b>{_market_value(liquidity.get('ssl_sweep'))}</b></p>
    <p><span>BSL sweep</span><b>{_market_value(liquidity.get('bsl_sweep'))}</b></p>
    <p><span>FVG</span><b>{_market_value(confirmation.get('fvg'))}</b></p>
  </section>
  <section><small>Fibonacci</small><h4>Retracement и OTE</h4>
    <p><span>Retracement</span><b>{_market_value(fibonacci.get('retracement'), 3)}</b></p>
    <p><span>OTE low</span><b>{_market_value(fibonacci.get('ote_low'), 5)}</b></p>
    <p><span>OTE mid</span><b>{_market_value(fibonacci.get('ote_mid'), 5)}</b></p>
    <p><span>OTE high</span><b>{_market_value(fibonacci.get('ote_high'), 5)}</b></p>
  </section>
  <section><small>Order Flow</small><h4>Объёмы</h4>
    <p><span>RVOL 20</span><b>{_market_value(volume.get('rvol_20'), 2)}</b></p>
    <p><span>Percentile</span><b>{_market_value(volume.get('percentile'), 1, '%')}</b></p>
    <p><span>Imbalance</span><b>{_market_value(volume.get('imbalance'), 2)}</b></p>
    <p><span>Tick-rate ratio</span><b>{_market_value(volume.get('tick_rate_ratio'), 2)}</b></p>
  </section>
  <section><small>Market State</small><h4>Импульс и ATR</h4>
    <p><span>RSI</span><b>{_market_value(momentum.get('rsi'), 1)}</b></p>
    <p><span>EMA fast</span><b>{_market_value(momentum.get('ema_fast'), 5)}</b></p>
    <p><span>EMA slow</span><b>{_market_value(momentum.get('ema_slow'), 5)}</b></p>
    <p><span>ATR</span><b>{_market_value(volatility.get('atr'), 5)}</b></p>
    <p><span>Spread / ATR</span><b>{_market_value(volatility.get('spread_cost_atr'), 3)}</b></p>
  </section>
</div>
"""


def _risk_candidate_html(candidate: Mapping[str, Any], decision: Mapping[str, Any]) -> str:
    state = text(candidate.get("state")).upper()
    summary = _mapping(decision.get("trader_summary"))
    decision_signal_id = text(
        decision.get("signal_id")
        or _mapping(decision.get("passport")).get("signal_id")
        or summary.get("signal_id")
    )
    candidate_id = text(candidate.get("signal_id"))
    matched = bool(summary) and (not decision_signal_id or decision_signal_id == candidate_id)
    if matched:
        return f"""
<div class='risk-strip'>
  <div><small>Решение</small><b>{html.escape(human_state(summary.get('decision')))}</b></div>
  <div><small>Риск</small><b>{fmt(summary.get('actual_risk_pct'), 3, '%')}</b></div>
  <div><small>Лот</small><b>{fmt(summary.get('total_volume'), 2)}</b></div>
  <div><small>Маржа</small><b>{fmt(summary.get('margin_required'), 2)}</b></div>
  <div><small>Свободно после</small><b>{fmt(summary.get('free_margin_after'), 2)}</b></div>
</div>
"""
    if state in {"PUBLISHABLE", "PASSPORTS_READY", "DECISION_READY", "ALLOW", "BLOCK"}:
        message = "Паспорт готов. Ожидается или уточняется расчёт живого счёта."
    else:
        message = "Risk Manager подключится только после прохождения publication gate."
    return f"<div class='muted-box'>{html.escape(message)}</div>"


def _signal_card(candidate: Mapping[str, Any], index: int) -> str:
    plan = _plan(candidate)
    action = text(candidate.get("action")).upper()
    modal_id = f"signal-{index}-{_safe_id(candidate.get('signal_id'), str(index))}"
    group = _status_group(candidate)
    search = " ".join(
        (
            text(candidate.get("symbol")),
            action,
            text(candidate.get("setup_family")),
            text(candidate.get("scenario")),
            human_state(candidate.get("state")),
        )
    ).lower()
    probability = candidate.get("conservative_probability")
    probability_text = (
        fmt(100 * number(probability), 1, "%") if probability is not None else "—"
    )
    expected = candidate.get("expected_value_r")
    expected_text = fmt(expected, 2, "R") if expected is not None else "—"
    return f"""
<article class='signal-card' data-action='{html.escape(action)}'
 data-group='{group}' data-search='{html.escape(search)}'>
  <button class='card-open' type='button' data-open='{modal_id}' aria-label='Открыть паспорт'>
    <header>
      <div><span class='direction {action.lower()}'>{html.escape(action)}</span>
      <b>{html.escape(text(candidate.get('symbol')))}</b><small>M5</small></div>
      <span class='status {tone(candidate.get('state'))}'>{html.escape(human_state(candidate.get('state')))}</span>
    </header>
    <div class='chart'>{candle_svg(candidate)}</div>
    <div class='level-row'>
      <span><small>Вход</small><b>{fmt(plan.get('average_entry'), 5)}</b></span>
      <span><small>Стоп</small><b>{fmt(plan.get('stop_price'), 5)}</b></span>
      <span><small>TP1</small><b>{fmt(_first_target(candidate), 5)}</b></span>
      <span><small>RR</small><b>{fmt(plan.get('first_target_rr'), 2, 'R')}</b></span>
    </div>
    <div class='card-bottom'>
      <div><small>Quality</small><b>{fmt(candidate.get('quality_score'), 1)}</b></div>
      <div><small>95% low</small><b>{probability_text}</b></div>
      <div><small>EV</small><b>{expected_text}</b></div>
      <span>Открыть паспорт →</span>
    </div>
  </button>
</article>
"""


def _signal_dialog(
    candidate: Mapping[str, Any],
    decision: Mapping[str, Any],
    index: int,
) -> str:
    plan = _plan(candidate)
    modal_id = f"signal-{index}-{_safe_id(candidate.get('signal_id'), str(index))}"
    reasons = [text(item) for item in _sequence(candidate.get("reasons")) if text(item)]
    reason_list = "".join(f"<li>{html.escape(item)}</li>" for item in reasons)
    if not reason_list:
        reason_list = "<li>Причины будут добавлены после следующей проверки gate.</li>"
    probability = candidate.get("conservative_probability")
    probability_text = (
        fmt(100 * number(probability), 1, "%") if probability is not None else "—"
    )
    expected = candidate.get("expected_value_r")
    expected_text = fmt(expected, 2, "R") if expected is not None else "—"
    return f"""
<dialog id='{modal_id}' class='signal-dialog'>
  <form method='dialog'><button class='close' aria-label='Закрыть'>×</button></form>
  <div class='dialog-head'>
    <div><span class='direction {html.escape(text(candidate.get("action")).lower())}'>{html.escape(text(candidate.get("action")).upper())}</span>
    <h2>{html.escape(text(candidate.get('symbol')))} <small>M5</small></h2>
    <p>{html.escape(text(candidate.get('scenario')) or text(candidate.get('setup_family')))}</p></div>
    <div><span class='status {tone(candidate.get("state"))}'>{html.escape(human_state(candidate.get("state")))}</span>
    <small>{html.escape(display_time(candidate.get('created_at')))}</small></div>
  </div>
  <div class='dialog-chart'>{candle_svg(candidate)}</div>
  <div class='dialog-grid'>
    <section class='panel plan-panel'><small>Торговый план</small><h3>Лесенка входов</h3>
      {_entries_html(plan)}
      <div class='entry-row danger'><span>Стоп</span><b>{fmt(plan.get('stop_price'), 5)}</b>
      <small>{html.escape(text(plan.get('invalidation')) or 'За точкой инвалидации')}</small></div>
      {_targets_html(plan)}
    </section>
    <section class='panel thesis'><small>Логика сетапа</small><h3>Почему движок заметил рынок</h3>
      <p>{html.escape(text(candidate.get('scenario')) or 'Мультифакторный рыночный сетап.')}</p>
      <ul>{reason_list}</ul>
      <div class='stats-row'>
        <span><small>Выборка</small><b>{integer(candidate.get('historical_sample'))}</b></span>
        <span><small>Quality</small><b>{fmt(candidate.get('quality_score'), 1)}</b></span>
        <span><small>Wilson 95%</small><b>{probability_text}</b></span>
        <span><small>EV</small><b>{expected_text}</b></span>
      </div>
    </section>
  </div>
  <section class='panel'><small>Рыночный контекст</small><h3>SMC, Fibonacci, объёмы и волатильность</h3>{_market_html(candidate)}</section>
  <section class='panel'><small>Факторная модель</small><h3>Что усиливает или ослабляет сетап</h3><div class='factor-grid'>{_factor_html(candidate)}</div></section>
  <section class='panel'><small>Publication gate</small><h3>Контроль качества</h3><div class='checks'>{_checks_html(candidate)}</div></section>
  <section class='panel'><small>Risk Manager</small><h3>Персональный риск счёта</h3>{_risk_candidate_html(candidate, decision)}</section>
</dialog>
"""


def _risk_page(decision: Mapping[str, Any]) -> str:
    summary = _mapping(decision.get("trader_summary"))
    if not summary:
        return """
<div class='empty-state'><i>◇</i><h2>Risk Manager ждёт проверенный паспорт</h2>
<p>Лотность, риск деньгами и маржа появятся только после PUBLISHABLE-сетапа.</p></div>
"""
    return f"""
<div class='risk-hero'><span class='status {tone(summary.get("decision"))}'>{html.escape(human_state(summary.get("decision")))}</span>
<h2>{html.escape(text(summary.get("symbol")))} {html.escape(text(summary.get("action")))}</h2>
<p>Расчёт выполнен для подключённого счёта.</p></div>
<div class='metric-grid'>
  <div><small>Риск, %</small><b>{fmt(summary.get('actual_risk_pct'), 3, '%')}</b></div>
  <div><small>Риск деньгами</small><b>{fmt(summary.get('actual_risk_money'), 2)}</b></div>
  <div><small>Объём</small><b>{fmt(summary.get('total_volume'), 2)}</b></div>
  <div><small>Маржа</small><b>{fmt(summary.get('margin_required'), 2)}</b></div>
  <div><small>Свободно после</small><b>{fmt(summary.get('free_margin_after'), 2)}</b></div>
</div>
"""


CSS = r"""
:root{--bg:#070911;--sidebar:#090c14;--surface:#0e1320;--surface2:#131a2a;--line:#202a3f;--text:#f6f8fc;--muted:#8995ac;--violet:#8068ff;--cyan:#22d3c5;--green:#2ed6a6;--red:#ff6678}*{box-sizing:border-box}html{color-scheme:dark}body{margin:0;background:radial-gradient(circle at 75% -20%,#25204f55,transparent 35%),var(--bg);color:var(--text);font-family:Inter,Segoe UI,Arial,sans-serif}.app{display:grid;grid-template-columns:224px minmax(0,1fr);min-height:100vh}aside{position:sticky;top:0;height:100vh;padding:22px 16px;border-right:1px solid var(--line);background:var(--sidebar);display:flex;flex-direction:column}.brand{display:flex;align-items:center;gap:11px;padding:4px 8px 28px}.brand-logo{width:38px;height:38px;border-radius:12px;display:grid;place-items:center;background:linear-gradient(135deg,var(--violet),var(--cyan));font-weight:900;box-shadow:0 12px 40px #6f5cff33}.brand b{font-size:20px}.brand small{display:block;color:var(--muted);font-size:11px;margin-top:2px}nav{display:grid;gap:7px}nav button{border:0;background:transparent;color:var(--muted);border-radius:12px;padding:12px;text-align:left;font-size:14px;cursor:pointer;transition:.18s}nav button:hover,nav button.active{color:white;background:#182033}.read-only{margin-top:auto;border:1px solid var(--line);border-radius:16px;background:linear-gradient(145deg,#111828,#0d1320);padding:15px}.read-only b{font-size:13px;letter-spacing:.5px}.read-only i{display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--green);margin-right:8px;box-shadow:0 0 0 5px #2ed6a61a}.read-only p{color:var(--muted);font-size:12px;line-height:1.5;margin:10px 0 0}.content{min-width:0}.topbar{height:70px;position:sticky;top:0;z-index:20;display:flex;align-items:center;justify-content:space-between;padding:0 32px;border-bottom:1px solid var(--line);background:#070911dc;backdrop-filter:blur(18px)}.topbar>div{display:flex;align-items:center;gap:14px;color:var(--muted);font-size:12px}.account{border:1px solid var(--line);border-radius:11px;padding:8px 11px;color:white;background:var(--surface)}main{max-width:1560px;margin:auto;padding:30px 32px 55px}.view{display:none}.view.active{display:block}.hero{display:grid;grid-template-columns:minmax(0,1.5fr) minmax(270px,.5fr);gap:16px}.hero-main,.hero-side{border:1px solid var(--line);border-radius:24px;background:linear-gradient(145deg,#131a2b,#0b1720);padding:28px}.hero-main{background:radial-gradient(circle at 90% 0,#8068ff44,transparent 34%),linear-gradient(145deg,#151a2c,#0b171d)}.eyebrow{color:#b4a9ff;font-size:11px;font-weight:800;letter-spacing:1.3px}.hero h1{font-size:37px;letter-spacing:-1.35px;margin:12px 0 10px}.hero p{color:#b3bdce;max-width:720px;line-height:1.6}.hero-side small{color:var(--muted)}.hero-side b{display:block;font-size:20px;margin:13px 0 6px}.health{display:flex;align-items:center;gap:9px;color:var(--green);font-size:12px}.health i{width:8px;height:8px;border-radius:50%;background:var(--green);box-shadow:0 0 0 5px #2ed6a617}.metric-grid{display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:11px;margin:17px 0 30px}.metric-grid>div{border:1px solid var(--line);border-radius:16px;background:var(--surface);padding:17px}.metric-grid small{display:block;color:var(--muted);font-size:11px}.metric-grid b{display:block;font-size:24px;margin-top:8px}.section-head{display:flex;justify-content:space-between;align-items:flex-end;gap:15px;margin:28px 0 14px}.section-head h2{margin:0;font-size:23px}.section-head p{margin:6px 0 0;color:var(--muted)}.ghost,.filters button,.filters input{border:1px solid var(--line);border-radius:11px;background:var(--surface);color:var(--muted);padding:10px 12px}.ghost{cursor:pointer}.filters{display:flex;gap:7px;flex-wrap:wrap}.filters input{min-width:230px;color:white}.filters button{cursor:pointer}.filters button.active{color:white;border-color:#8068ff88;background:#8068ff22}.signal-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(410px,1fr));gap:15px}.signal-card{border:1px solid var(--line);border-radius:19px;background:var(--surface);overflow:hidden;transition:.2s}.signal-card:hover{transform:translateY(-2px);border-color:#394768;box-shadow:0 18px 55px #0000002e}.card-open{display:block;width:100%;padding:0;border:0;background:transparent;color:inherit;text-align:left;cursor:pointer}.signal-card header{display:flex;justify-content:space-between;align-items:center;padding:16px 17px}.signal-card header>div{display:flex;align-items:center;gap:9px}.signal-card header b{font-size:19px}.signal-card header small{color:var(--muted)}.direction,.status{display:inline-flex;align-items:center;font-size:11px;font-weight:850;padding:6px 9px;border-radius:8px}.direction.buy{color:#64ecc6;background:#143c34}.direction.sell{color:#ff9daa;background:#43232c}.status{border-radius:999px;background:#232b3d;color:#c3cada}.status.good{background:#143c34;color:#64ecc6}.status.bad{background:#43232c;color:#ff9daa}.status.accent{background:#302a55;color:#c2b9ff}.chart{height:184px;padding:0 9px}.chart svg,.dialog-chart svg{width:100%;height:100%}.wick{stroke-width:1.1}.wick.up,.body.up{stroke:var(--green);fill:var(--green)}.wick.down,.body.down{stroke:var(--red);fill:var(--red)}.price{stroke-width:1;stroke-dasharray:4 4}.price.entry{stroke:#a18dff}.price.stop{stroke:var(--red)}.price.target{stroke:var(--green)}.label{font-size:9px;font-weight:850}.label.entry{fill:#b4a6ff}.label.stop{fill:#ff8795}.label.target{fill:#4de2bd}.empty-chart{height:100%;display:grid;place-items:center;color:var(--muted);background:var(--surface2);border-radius:13px}.level-row{display:grid;grid-template-columns:repeat(4,1fr);gap:1px;background:var(--line);border-block:1px solid var(--line)}.level-row span{padding:11px;background:var(--surface)}.level-row small,.card-bottom small{display:block;color:var(--muted);font-size:10px}.level-row b{display:block;margin-top:4px;font-size:13px}.card-bottom{display:grid;grid-template-columns:repeat(3,90px) 1fr;gap:8px;align-items:center;padding:13px 16px}.card-bottom>div{padding:8px;background:var(--surface2);border-radius:10px}.card-bottom b{display:block;margin-top:4px}.card-bottom>span{justify-self:end;color:#b5a7ff;font-size:12px;font-weight:750}.empty-state{min-height:330px;border:1px solid var(--line);border-radius:20px;background:var(--surface);display:grid;place-content:center;text-align:center;padding:30px}.empty-state i{font-size:46px;color:#a18cff;font-style:normal}.empty-state p{color:var(--muted)}.pipeline{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}.pipeline>div,.panel,.risk-hero,.system-card{border:1px solid var(--line);border-radius:18px;background:var(--surface);padding:20px}.pipeline small,.pipeline p,.panel>small{color:var(--muted)}.pipeline b{display:block;margin:9px 0}.pipeline p{font-size:12px}.funnel{margin-top:15px;border:1px solid var(--line);border-radius:18px;background:var(--surface);padding:20px}.funnel p{display:grid;grid-template-columns:190px 1fr 70px;gap:14px;align-items:center}.funnel i{height:10px;border-radius:99px;background:linear-gradient(90deg,var(--violet),var(--cyan))}.system-grid{display:grid;grid-template-columns:1.2fr .8fr;gap:14px}.system-card p,.market-grid p{display:flex;justify-content:space-between;gap:12px;border-bottom:1px solid var(--line);padding:9px 0;margin:0}.system-card p span,.market-grid p span{color:var(--muted)}.system-card a{color:#b4a6ff;text-decoration:none}.signal-dialog{width:min(1180px,calc(100vw - 36px));max-height:calc(100vh - 32px);padding:0;border:1px solid #34405c;border-radius:24px;background:#0a0e17;color:var(--text);box-shadow:0 40px 130px #000b}.signal-dialog::backdrop{background:#01030ad9;backdrop-filter:blur(9px)}.signal-dialog .close{position:sticky;float:right;top:15px;right:15px;z-index:5;width:38px;height:38px;border-radius:50%;border:1px solid var(--line);background:#111827;color:white;font-size:24px;cursor:pointer}.dialog-head{display:flex;justify-content:space-between;gap:20px;align-items:flex-start;padding:28px 30px 10px}.dialog-head>div:first-child{display:grid;grid-template-columns:auto auto;align-items:center;gap:9px}.dialog-head h2{margin:0;font-size:29px}.dialog-head h2 small{font-size:13px;color:var(--muted)}.dialog-head p{grid-column:1/-1;color:#b4bed0;margin:4px 0 0}.dialog-head>div:last-child{text-align:right;display:grid;gap:10px;justify-items:end}.dialog-head>div:last-child small{color:var(--muted)}.dialog-chart{height:290px;margin:0 24px;border:1px solid var(--line);border-radius:18px;background:var(--surface);padding:10px}.dialog-grid{display:grid;grid-template-columns:.75fr 1.25fr;gap:14px;padding:14px 24px}.signal-dialog>.panel{margin:0 24px 14px}.panel h3{margin:6px 0 16px}.entry-row{display:grid;grid-template-columns:90px 110px 1fr;gap:10px;align-items:center;padding:10px 0;border-bottom:1px solid var(--line)}.entry-row span,.entry-row small{color:var(--muted)}.entry-row.danger b{color:#ff8c99}.thesis>p{color:#c0c8d6;line-height:1.6}.thesis ul{color:#aeb8ca;line-height:1.6;padding-left:18px}.stats-row{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-top:18px}.stats-row span{padding:10px;background:var(--surface2);border-radius:11px}.stats-row small{display:block;color:var(--muted);font-size:10px}.stats-row b{display:block;margin-top:5px}.market-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}.market-grid section{padding:14px;background:var(--surface2);border-radius:13px}.market-grid section>small{color:#a99cff;font-weight:750}.market-grid h4{margin:7px 0 12px}.market-grid p{font-size:12px}.factor-grid{display:grid;grid-template-columns:repeat(2,1fr);gap:10px}.factor{padding:12px;background:var(--surface2);border-radius:13px}.factor>div{display:flex;justify-content:space-between}.factor i{display:block;height:7px;background:#252d3e;border-radius:99px;margin:9px 0}.factor em{display:block;height:100%;border-radius:99px;background:linear-gradient(90deg,var(--violet),var(--cyan))}.factor small{color:var(--muted)}.checks{display:grid;grid-template-columns:repeat(3,1fr);gap:9px}.check{display:grid;grid-template-columns:30px 1fr;align-items:center;padding:11px;background:var(--surface2);border-radius:12px}.check span{grid-row:1/3;width:24px;height:24px;border-radius:50%;display:grid;place-items:center}.check small{color:var(--muted)}.check.pass span{background:#173d35;color:var(--green)}.check.fail span{background:#44242d;color:var(--red)}.muted-box{padding:16px;border:1px dashed #34405a;border-radius:13px;color:var(--muted);background:#101625}.risk-strip{display:grid;grid-template-columns:repeat(5,1fr);gap:8px}.risk-strip>div{padding:12px;background:var(--surface2);border-radius:11px}.risk-strip small{display:block;color:var(--muted)}.risk-strip b{display:block;margin-top:5px}.risk-hero{margin-bottom:14px}.risk-hero p{color:var(--muted)}@media(max-width:1100px){.metric-grid{grid-template-columns:repeat(3,1fr)}.market-grid{grid-template-columns:1fr 1fr}.pipeline{grid-template-columns:1fr 1fr}.dialog-grid{grid-template-columns:1fr}.risk-strip{grid-template-columns:repeat(3,1fr)}}@media(max-width:780px){.app{display:block}aside{position:fixed;top:auto;bottom:0;left:0;right:0;height:69px;padding:8px 10px;z-index:30;border-right:0;border-top:1px solid var(--line)}.brand,.read-only{display:none}nav{grid-template-columns:repeat(5,1fr)}nav button{text-align:center;padding:11px 5px}nav span{display:none}.content{padding-bottom:69px}.topbar{height:62px;padding:0 14px}.topbar span{display:none}main{padding:18px 13px 35px}.hero{grid-template-columns:1fr}.hero-main,.hero-side{padding:21px}.hero h1{font-size:29px}.metric-grid{grid-template-columns:1fr 1fr}.signal-grid{grid-template-columns:1fr}.section-head{align-items:flex-start;flex-direction:column}.filters input{width:100%;min-width:0}.card-bottom{grid-template-columns:repeat(3,1fr)}.card-bottom>span{grid-column:1/-1;justify-self:start}.pipeline,.system-grid,.market-grid,.factor-grid,.checks{grid-template-columns:1fr}.signal-dialog{width:calc(100vw - 12px);max-height:calc(100vh - 12px);border-radius:18px}.dialog-head{padding:22px 18px 8px;display:block}.dialog-head>div:last-child{text-align:left;justify-items:start;margin-top:12px}.dialog-chart{height:230px;margin:0 12px}.dialog-grid{padding:12px}.signal-dialog>.panel{margin:0 12px 12px}.stats-row,.risk-strip{grid-template-columns:1fr 1fr}.entry-row{grid-template-columns:75px 90px 1fr}}
"""

JS = r"""
const labels={overview:'Обзор',signals:'Сигналы',stats:'Статистика',risk:'Risk Manager',system:'Система'};
function show(name){document.querySelectorAll('.view').forEach(v=>v.classList.remove('active'));document.querySelectorAll('nav button').forEach(b=>b.classList.toggle('active',b.dataset.view===name));document.getElementById(name)?.classList.add('active');document.getElementById('page-title').textContent=labels[name]||'TradeMind';location.hash=name;scrollTo(0,0)}
document.querySelectorAll('[data-view]').forEach(b=>b.onclick=()=>show(b.dataset.view));
document.querySelectorAll('[data-jump]').forEach(b=>b.onclick=()=>show(b.dataset.jump));
document.querySelectorAll('[data-open]').forEach(b=>b.onclick=()=>document.getElementById(b.dataset.open)?.showModal());
document.querySelectorAll('dialog').forEach(d=>d.addEventListener('click',e=>{if(e.target===d)d.close()}));
const cards=[...document.querySelectorAll('#signal-feed .signal-card')];let action='ALL',group='ALL';
function apply(){const q=(document.getElementById('search')?.value||'').toLowerCase();cards.forEach(c=>{const okAction=action==='ALL'||c.dataset.action===action;const okGroup=group==='ALL'||c.dataset.group===group;const okSearch=(c.dataset.search||'').includes(q);c.style.display=okAction&&okGroup&&okSearch?'':'none'})}
document.getElementById('search')?.addEventListener('input',apply);
document.querySelectorAll('[data-action-filter]').forEach(b=>b.onclick=()=>{action=b.dataset.actionFilter;document.querySelectorAll('[data-action-filter]').forEach(x=>x.classList.toggle('active',x===b));apply()});
document.querySelectorAll('[data-group-filter]').forEach(b=>b.onclick=()=>{group=b.dataset.groupFilter;document.querySelectorAll('[data-group-filter]').forEach(x=>x.classList.toggle('active',x===b));apply()});
if(labels[location.hash.slice(1)])show(location.hash.slice(1));
"""


def render(data: Mapping[str, Any]) -> str:
    runtime = _mapping(data.get("runtime"))
    factory = _mapping(data.get("factory"))
    bridge = _mapping(data.get("bridge"))
    summary = _mapping(data.get("summary"))
    decision = _mapping(data.get("latest_decision"))
    candidates = [
        item for item in data.get("candidates", []) if isinstance(item, Mapping)
    ]
    cards = "".join(_signal_card(item, index) for index, item in enumerate(candidates))
    dialogs = "".join(
        _signal_dialog(item, decision, index) for index, item in enumerate(candidates)
    )
    if not cards:
        cards = (
            "<div class='empty-state'><i>⌁</i><h2>Свежих сетапов пока нет</h2>"
            "<p>Сканер продолжает проверять новые закрытые M5-свечи.</p></div>"
        )
    priority = [item for item in candidates if _status_group(item) in {"READY", "REVIEW"}]
    recent_cards = "".join(
        _signal_card(item, 10_000 + index) for index, item in enumerate(priority[:4])
    )
    recent_dialogs = "".join(
        _signal_dialog(item, decision, 10_000 + index)
        for index, item in enumerate(priority[:4])
    )
    if not recent_cards:
        recent_cards = "".join(
            _signal_card(item, 20_000 + index)
            for index, item in enumerate(candidates[:4])
        ) or cards
        recent_dialogs = "".join(
            _signal_dialog(item, decision, 20_000 + index)
            for index, item in enumerate(candidates[:4])
        )
    archive = integer(summary.get("archive_candidates"))
    displayed = integer(summary.get("displayed_candidates"))
    active = integer(summary.get("active_candidates"))
    fresh = integer(summary.get("fresh_factory"))
    publishable = integer(summary.get("publishable"))
    outcomes = integer(summary.get("completed_outcomes"))
    buy = integer(summary.get("buy"))
    sell = integer(summary.get("sell"))
    runtime_label = human_state(runtime.get("state"))
    factory_label = human_state(factory.get("state"))
    bridge_label = human_state(bridge.get("state"))
    risk_label = human_state(runtime.get("risk_state") or "NONE")
    hero = "Есть проверенный торговый сигнал" if publishable else "Рынок под наблюдением"
    note = (
        "Сетап прошёл статистический фильтр. Следующий шаг: персональный риск счёта."
        if publishable
        else "Движок фильтрует рынок и не показывает сырой шум как готовый сигнал."
    )
    fresh_width = min(100.0, 100.0 * fresh / max(1, archive))
    publishable_width = min(100.0, 100.0 * publishable / max(1, archive))
    return f"""<!doctype html><html lang='ru'><head><meta charset='utf-8'>
<meta name='viewport' content='width=device-width,initial-scale=1'>
<meta http-equiv='refresh' content='60'><title>TradeMind</title><style>{CSS}</style></head><body>
<div class='app'><aside><div class='brand'><div class='brand-logo'>T</div><div><b>TradeMind</b><small>Signal Intelligence</small></div></div>
<nav><button class='active' data-view='overview'>⌂ <span>Обзор</span></button><button data-view='signals'>⌁ <span>Сигналы</span></button><button data-view='stats'>◫ <span>Статистика</span></button><button data-view='risk'>◇ <span>Risk Manager</span></button><button data-view='system'>⚙ <span>Система</span></button></nav>
<div class='read-only'><i></i><b>READ-ONLY</b><p>Ордера и публикация выключены.</p></div></aside>
<div class='content'><header class='topbar'><b id='page-title'>Обзор</b><div><span>Обновлено {html.escape(display_time(data.get('updated_at')))}</span><strong class='account'>Счёт {html.escape(text(runtime.get('account_login')))}</strong></div></header><main>
<section class='view active' id='overview'>
<div class='hero'><div class='hero-main'><span class='eyebrow'>LIVE MARKET INTELLIGENCE</span><h1>{hero}</h1><p>{note}</p></div>
<div class='hero-side'><div class='health'><i></i>Контур работает</div><small>Состояние Factory</small><b>{html.escape(factory_label)}</b><p>{html.escape(runtime_label)}</p></div></div>
<div class='metric-grid'>
<div><small>В текущей ленте</small><b>{displayed}</b></div><div><small>Активные</small><b>{active}</b></div>
<div><small>Свежих в Factory</small><b>{fresh}</b></div><div><small>Publishable</small><b>{publishable}</b></div>
<div><small>BUY / SELL в ленте</small><b>{buy} / {sell}</b></div><div><small>Всего накоплено</small><b>{archive}</b></div></div>
<div class='section-head'><div><h2>Приоритетные сетапы</h2><p>Сначала свежие и допущенные, затем отклонённые наблюдения.</p></div><button class='ghost' data-jump='signals'>Открыть ленту</button></div>
<div class='signal-grid'>{recent_cards}</div>{recent_dialogs}</section>
<section class='view' id='signals'><div class='section-head'><div><h2>Лента сетапов</h2><p>Нажми на карточку, чтобы открыть полный паспорт.</p></div>
<div class='filters'><input id='search' placeholder='Инструмент или сценарий'><button class='active' data-action-filter='ALL'>Все</button><button data-action-filter='BUY'>BUY</button><button data-action-filter='SELL'>SELL</button><button class='active' data-group-filter='ALL'>Все статусы</button><button data-group-filter='READY'>Допущенные</button><button data-group-filter='REVIEW'>На проверке</button><button data-group-filter='REJECTED'>Отклонённые</button></div></div>
<div class='signal-grid' id='signal-feed'>{cards}</div>{dialogs}</section>
<section class='view' id='stats'><div class='section-head'><div><h2>Статистика контура</h2><p>Чёткая воронка без смешения архива и текущей ленты.</p></div></div>
<div class='pipeline'><div><small>Runtime</small><b>{html.escape(runtime_label)}</b><p>Новые закрытые M5-свечи.</p></div>
<div><small>Passport Factory</small><b>{html.escape(factory_label)}</b><p>История и publication gate.</p></div>
<div><small>MT5 Bridge</small><b>{html.escape(bridge_label)}</b><p>Живой счёт и спецификация.</p></div>
<div><small>Risk Manager</small><b>{html.escape(risk_label)}</b><p>ALLOW/BLOCK без ордера.</p></div></div>
<div class='funnel'><h3>Воронка</h3><p><span>Накоплено кандидатов</span><i style='width:100%'></i><b>{archive}</b></p>
<p><span>Свежих в Factory</span><i style='width:{fresh_width:.1f}%'></i><b>{fresh}</b></p>
<p><span>Publishable</span><i style='width:{publishable_width:.1f}%'></i><b>{publishable}</b></p>
<p><span>Завершённых исходов</span><i style='width:{min(100,100*outcomes/max(1,archive)):.1f}%'></i><b>{outcomes}</b></p></div></section>
<section class='view' id='risk'><div class='section-head'><div><h2>Risk Manager</h2><p>Персональный объём, риск деньгами и маржа.</p></div></div>{_risk_page(decision)}</section>
<section class='view' id='system'><div class='section-head'><div><h2>Система</h2><p>Технические детали и диагностический экран.</p></div></div>
<div class='system-grid'><div class='system-card'><h3>Контур</h3><p><span>Runtime</span><b>{html.escape(runtime_label)}</b></p><p><span>Factory</span><b>{html.escape(factory_label)}</b></p><p><span>Bridge</span><b>{html.escape(bridge_label)}</b></p><p><span>Risk</span><b>{html.escape(risk_label)}</b></p><a href='../dashboard/index.html'>Открыть технический dashboard →</a></div>
<div class='system-card'><h3>Безопасность</h3><p><span>Режим</span><b>READ-ONLY</b></p><p><span>Ордера</span><b>OFF</b></p><p><span>Публикация</span><b>OFF</b></p><p><span>Broker API</span><b>не вызывается</b></p></div></div></section>
</main></div></div><script>{JS}</script></body></html>"""


def run_product_ui(
    runtime_root: Path,
    limit: int = 24,
    candle_limit: int = 48,
) -> tuple[Path, Mapping[str, Any]]:
    root = runtime_root.expanduser().resolve()
    source = read_json(root / "dashboard" / "data.json")
    runtime = _mapping(source.get("runtime"))
    paths = _mapping(runtime.get("paths"))
    canonical = (
        Path(text(paths.get("canonical_volume"))).expanduser().resolve()
        if text(paths.get("canonical_volume"))
        else None
    )
    payload = build_payload(source, canonical, limit, candle_limit)
    output = root / "product"
    index = output / "index.html"
    atomic_write(index, render(payload))
    atomic_write(
        output / "data.json",
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
    )
    atomic_write(
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
    parser = argparse.ArgumentParser(description="TradeMind Product UI v1.23.1")
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
        print(f"TradeMind Product UI v1.23.1 failed: {exc}")
        return 1
    print("TradeMind Product UI v1.23.1")
    print("Polished read-only interface. Orders OFF. Publication OFF.")
    print(f"Signals displayed: {len(payload.get('candidates', []))}")
    print(f"Product UI: {index}")
    if args.open and hasattr(os, "startfile"):
        os.startfile(index)  # type: ignore[attr-defined]
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
