"""TradeMind Product UI v1.23, read-only presentation layer."""
from __future__ import annotations

import argparse
import csv
import html
import json
import math
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

VERSION = "1.23.0"
STATE_LABELS = {
    "RUN_COMPLETE": "Новые данные обработаны",
    "WAITING_NO_NEW_CLOSED_BARS": "Рынок синхронизирован",
    "WAITING_SOURCE_EMPTY": "Нет рыночных данных",
    "WAITING_NO_FRESH_CANDIDATES": "Нет свежих сетапов",
    "WAITING_NO_PUBLISHABLE_PASSPORT": "Качественных сигналов пока нет",
    "PASSPORTS_READY": "Есть проверенные сигналы",
    "DECISION_READY": "Риск рассчитан",
    "ALLOW": "Сделка допустима",
    "BLOCK": "Сделка заблокирована",
    "NONE": "Ожидает сигнал",
    "PENDING_GATE": "На проверке",
    "SHADOW_ONLY": "Наблюдение",
    "REJECTED": "Не прошёл фильтр",
    "OUTCOME_WIN": "Цель достигнута",
    "OUTCOME_LOSS": "Стоп получен",
    "OUTCOME_FLAT": "Без результата",
}


def text(value: Any) -> str:
    return str(value or "").strip()


def number(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def integer(value: Any, default: int = 0) -> int:
    try:
        return int(str(value or "").strip())
    except (TypeError, ValueError):
        return default


def human_state(value: Any) -> str:
    state = text(value).upper()
    return STATE_LABELS.get(state, state.replace("_", " ").title() or "Нет данных")


def tone(value: Any) -> str:
    state = text(value).upper()
    if state in {"ALLOW", "PASSPORTS_READY", "DECISION_READY", "OUTCOME_WIN"}:
        return "good"
    if state in {"BLOCK", "REJECTED", "ERROR", "OUTCOME_LOSS"}:
        return "bad"
    if state in {"PENDING_GATE", "SHADOW_ONLY", "RUN_COMPLETE"}:
        return "accent"
    return "wait"


def fmt(value: Any, digits: int = 2, suffix: str = "") -> str:
    result = number(value, math.nan)
    return f"{result:.{digits}f}{suffix}" if math.isfinite(result) else "—"


def display_time(value: Any) -> str:
    raw = text(value)
    if not raw:
        return "—"
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return raw
    return parsed.astimezone().strftime("%d.%m.%Y %H:%M") if parsed.tzinfo else raw


def read_json(path: Path) -> Mapping[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, Mapping):
        raise ValueError(f"JSON root must be an object: {path}")
    return payload


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(content, encoding="utf-8")
    temp.replace(path)


def load_candles(
    path: Path | None,
    symbols: set[str],
    offset: int,
    limit: int,
) -> dict[str, list[dict[str, float]]]:
    if path is None or not path.is_file() or not symbols:
        return {}
    output: dict[str, list[dict[str, float]]] = {symbol: [] for symbol in symbols}
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            symbol = text(row.get("symbol")).upper()
            if symbol not in output or text(row.get("timeframe")).upper() != "M5":
                continue
            try:
                epoch = integer(row.get("time"))
                opened = datetime.fromtimestamp(epoch, timezone.utc) - timedelta(hours=offset)
                candle = {
                    "time": opened.timestamp(),
                    **{
                        key: float(text(row.get(key)))
                        for key in ("open", "high", "low", "close")
                    },
                }
            except ValueError:
                continue
            if all(
                math.isfinite(candle[key])
                for key in ("open", "high", "low", "close")
            ):
                output[symbol].append(candle)
    for symbol in output:
        output[symbol].sort(key=lambda item: item["time"])
        output[symbol] = output[symbol][-limit:]
    return output


def candle_svg(candidate: Mapping[str, Any]) -> str:
    candles = candidate.get("candles", [])
    if not isinstance(candles, Sequence) or not candles:
        return "<div class='empty-chart'>Свечи ещё не загружены</div>"
    items = [item for item in candles if isinstance(item, Mapping)]
    plan = candidate.get("plan", {}) if isinstance(candidate.get("plan"), Mapping) else {}
    values = [number(item.get(key)) for item in items for key in ("low", "high")]
    values += [number(plan.get("average_entry")), number(plan.get("stop_price"))]
    targets = plan.get("targets", [])
    if isinstance(targets, Sequence) and not isinstance(targets, (str, bytes)):
        values += [number(item) for item in targets]
    values = [item for item in values if item > 0 and math.isfinite(item)]
    low, high = (min(values), max(values)) if values else (0.0, 1.0)
    pad = max((high - low) * 0.08, abs(high) * 0.0005, 1e-8)
    low, high = low - pad, high + pad
    width, height, left, right, top, bottom = 520.0, 170.0, 8.0, 58.0, 10.0, 16.0
    plot_w, plot_h = width - left - right, height - top - bottom
    step = plot_w / max(1, len(items))
    body_w = max(2.0, min(7.0, step * 0.58))

    def y(value: float) -> float:
        return top + (high - value) / max(high - low, 1e-12) * plot_h

    parts = [f"<svg viewBox='0 0 {width:.0f} {height:.0f}' aria-label='Последние M5 свечи'>"]
    for index, item in enumerate(items):
        open_, close = number(item.get("open")), number(item.get("close"))
        high_, low_ = number(item.get("high")), number(item.get("low"))
        x = left + step * index + step / 2
        css = "up" if close >= open_ else "down"
        y1, y2 = y(open_), y(close)
        parts.append(
            f"<line class='wick {css}' x1='{x:.2f}' x2='{x:.2f}' "
            f"y1='{y(high_):.2f}' y2='{y(low_):.2f}'/>"
        )
        parts.append(
            f"<rect class='body {css}' x='{x-body_w/2:.2f}' y='{min(y1,y2):.2f}' "
            f"width='{body_w:.2f}' height='{max(1.4,abs(y1-y2)):.2f}' rx='1'/>"
        )
    overlays = [
        ("ENTRY", plan.get("average_entry"), "entry"),
        ("STOP", plan.get("stop_price"), "stop"),
    ]
    if isinstance(targets, Sequence) and not isinstance(targets, (str, bytes)) and targets:
        overlays.append(("TP1", targets[0], "target"))
    for label, raw, css in overlays:
        value = number(raw, math.nan)
        if math.isfinite(value) and value > 0:
            line_y = y(value)
            parts.append(
                f"<line class='price {css}' x1='{left}' x2='{width-right}' "
                f"y1='{line_y:.2f}' y2='{line_y:.2f}'/>"
                f"<text class='label {css}' x='{width-right+6}' "
                f"y='{line_y+4:.2f}'>{label}</text>"
            )
    parts.append("</svg>")
    return "".join(parts)


def signal_card(candidate: Mapping[str, Any], index: int) -> str:
    del index
    plan = candidate.get("plan", {}) if isinstance(candidate.get("plan"), Mapping) else {}
    targets = plan.get("targets", [])
    target = (
        targets[0]
        if isinstance(targets, Sequence)
        and not isinstance(targets, (str, bytes))
        and targets
        else None
    )
    action = text(candidate.get("action")).upper()
    state = human_state(candidate.get("state"))
    reasons = candidate.get("reasons", [])
    reason = (
        "; ".join(str(item) for item in reasons[:2])
        if isinstance(reasons, Sequence) and reasons
        else "Ожидает статистического заключения."
    )
    search = " ".join(
        (
            text(candidate.get("symbol")),
            action,
            text(candidate.get("setup_family")),
            state,
        )
    ).lower()
    probability = (
        fmt(100 * number(candidate.get("conservative_probability")), 1, "%")
        if candidate.get("conservative_probability") is not None
        else "—"
    )
    expected = (
        fmt(candidate.get("expected_value_r"), 2, "R")
        if candidate.get("expected_value_r") is not None
        else "—"
    )
    return f"""
<article class='signal' data-action='{html.escape(action)}' data-search='{html.escape(search)}'>
<header><div><span class='direction {action.lower()}'>{html.escape(action)}</span><b>{html.escape(text(candidate.get('symbol')))}</b><small>M5</small></div><span class='status {tone(candidate.get('state'))}'>{html.escape(state)}</span></header>
<div class='chart'>{candle_svg(candidate)}</div>
<div class='levels'><span><small>Вход</small><b>{fmt(plan.get('average_entry'),5)}</b></span><span><small>Стоп</small><b>{fmt(plan.get('stop_price'),5)}</b></span><span><small>Цель</small><b>{fmt(target,5)}</b></span><span><small>RR</small><b>{fmt(plan.get('first_target_rr'),2,'R')}</b></span></div>
<div class='scores'><span><small>Quality</small><b>{fmt(candidate.get('quality_score'),1)}</b></span><span><small>95% low</small><b>{probability}</b></span><span><small>EV</small><b>{expected}</b></span></div>
<p>{html.escape(reason)}</p><details><summary>Полная расшифровка</summary><div class='detail'><div><small>Сетап</small><b>{html.escape(text(candidate.get('setup_family')))}</b></div><div><small>Сценарий</small><b>{html.escape(text(candidate.get('scenario')))}</b></div><div><small>Выборка</small><b>{integer(candidate.get('historical_sample'))}</b></div><div><small>Время</small><b>{html.escape(display_time(candidate.get('created_at')))}</b></div></div></details>
</article>"""


def risk_html(decision: Mapping[str, Any]) -> str:
    if not decision:
        return "<div class='empty'><div>◌</div><h2>Risk Manager ожидает проверенный сигнал</h2><p>Лотность и маржа появятся только после прохождения publication gate.</p></div>"
    summary = (
        decision.get("trader_summary", {})
        if isinstance(decision.get("trader_summary"), Mapping)
        else {}
    )
    return f"""
<div class='risk'><span class='status {tone(summary.get('decision'))}'>{html.escape(human_state(summary.get('decision')))}</span><h2>{html.escape(text(summary.get('symbol')))} {html.escape(text(summary.get('action')))}</h2><div class='kpis'><div><small>Риск</small><b>{fmt(summary.get('actual_risk_pct'),3,'%')}</b></div><div><small>Деньгами</small><b>{fmt(summary.get('actual_risk_money'),2)}</b></div><div><small>Маржа</small><b>{fmt(summary.get('margin_required'),2)}</b></div><div><small>Свободно после</small><b>{fmt(summary.get('free_margin_after'),2)}</b></div></div></div>"""


CSS = """
:root{--bg:#080b12;--panel:#101522;--panel2:#151b2b;--line:#252f45;--text:#f4f7fb;--muted:#8792a8;--accent:#7c5cff;--green:#2bd4a7;--red:#ff6b79}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font-family:Inter,Segoe UI,Arial,sans-serif}.app{display:grid;grid-template-columns:230px 1fr;min-height:100vh}aside{position:sticky;top:0;height:100vh;padding:22px 17px;border-right:1px solid var(--line);background:#0a0d15;display:flex;flex-direction:column}.brand{display:flex;gap:11px;align-items:center;padding:5px 7px 27px}.brand i{width:36px;height:36px;border-radius:11px;display:grid;place-items:center;background:linear-gradient(135deg,var(--accent),var(--green));font-style:normal;font-weight:900}.brand b{font-size:20px}.brand small{display:block;color:var(--muted);font-size:11px}nav{display:grid;gap:6px}nav button{border:0;background:transparent;color:var(--muted);padding:12px;border-radius:11px;text-align:left;font-size:14px}nav button.active,nav button:hover{background:#1b2130;color:white}.safe{margin-top:auto;padding:14px;border:1px solid var(--line);border-radius:14px;background:var(--panel)}.safe em{display:inline-block;width:8px;height:8px;background:var(--green);border-radius:50%;margin-right:8px;box-shadow:0 0 0 5px #2bd4a71a}.safe p{color:var(--muted);font-size:12px}.content{min-width:0}.top{height:74px;position:sticky;top:0;z-index:10;display:flex;align-items:center;justify-content:space-between;padding:0 32px;border-bottom:1px solid var(--line);background:#080b12dd;backdrop-filter:blur(18px)}.top>div{display:flex;gap:13px;align-items:center;color:var(--muted);font-size:12px}.top strong{color:white;background:var(--panel);border:1px solid var(--line);padding:8px 11px;border-radius:10px}main{max-width:1580px;margin:auto;padding:28px 32px 48px}.view{display:none}.view.active{display:block}.hero{min-height:205px;padding:30px;border:1px solid var(--line);border-radius:24px;background:radial-gradient(circle at 90% 0,#7c5cff40,transparent 34%),linear-gradient(135deg,#151a2d,#0d2022);display:flex;align-items:center;justify-content:space-between;gap:24px}.hero h1{font-size:38px;letter-spacing:-1.4px;margin:13px 0 9px}.hero p{color:#b3bdce}.hero>div>small{color:#b4a9ff;letter-spacing:1.3px;font-weight:700}.hero-state{min-width:260px;padding:18px;border:1px solid #ffffff18;border-radius:17px;background:#ffffff08}.hero-state small,.hero-state span{display:block;color:var(--muted)}.hero-state b{display:block;margin:8px 0}.kpis{display:grid;grid-template-columns:repeat(5,1fr);gap:11px;margin:17px 0 27px}.kpis>div{padding:17px;border:1px solid var(--line);border-radius:16px;background:var(--panel)}.kpis small{color:var(--muted)}.kpis b{display:block;font-size:24px;margin-top:7px}.heading{display:flex;justify-content:space-between;align-items:flex-end;gap:15px;margin:28px 0 14px}.heading h2{margin:0}.heading p{margin:5px 0 0;color:var(--muted)}.heading button,.tools button,.tools input{border:1px solid var(--line);background:var(--panel);color:var(--muted);padding:10px 12px;border-radius:10px}.tools{display:flex;gap:7px}.tools input{min-width:230px;color:white}.tools button.active{color:white;border-color:#7c5cff88;background:#7c5cff20}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(390px,1fr));gap:14px}.signal{border:1px solid var(--line);background:var(--panel);border-radius:18px;overflow:hidden}.signal header{display:flex;justify-content:space-between;align-items:center;padding:15px 16px}.signal header>div{display:flex;align-items:center;gap:9px}.signal header b{font-size:19px}.signal header small{color:var(--muted)}.direction,.status{font-size:11px;font-weight:800;padding:6px 8px;border-radius:8px}.direction.buy{color:#65e8c4;background:#153d35}.direction.sell{color:#ff98a3;background:#44222b}.status{border-radius:999px;background:#232b3d;color:#bec6d4}.status.good{background:#143b34;color:#5fe7c1}.status.bad{background:#43242b;color:#ff98a3}.status.accent{background:#2c2850;color:#c1b8ff}.chart{height:180px;padding:0 8px}.chart svg{width:100%;height:100%}.wick{stroke-width:1.1}.wick.up,.body.up{stroke:var(--green);fill:var(--green)}.wick.down,.body.down{stroke:var(--red);fill:var(--red)}.price{stroke-width:1;stroke-dasharray:4 4}.price.entry{stroke:#9b87ff}.price.stop{stroke:var(--red)}.price.target{stroke:var(--green)}.label{font-size:9px;font-weight:800}.label.entry{fill:#ad9fff}.label.stop{fill:#ff8792}.label.target{fill:#4de2bd}.empty-chart{height:100%;display:grid;place-items:center;color:var(--muted);background:var(--panel2);border-radius:13px}.levels{display:grid;grid-template-columns:repeat(4,1fr);gap:1px;background:var(--line);border-block:1px solid var(--line)}.levels span{padding:11px;background:var(--panel)}.levels small,.scores small{display:block;color:var(--muted);font-size:11px}.levels b{display:block;font-size:13px;margin-top:4px}.scores{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;padding:13px 16px 2px}.scores span{padding:9px;background:var(--panel2);border-radius:10px}.scores b{display:block;margin-top:4px}.signal>p{margin:12px 16px;color:#aab4c7;font-size:13px;line-height:1.5}.signal details{border-top:1px solid var(--line);padding:12px 16px}.signal summary{cursor:pointer;color:#aa9aff;font-weight:700}.detail{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:12px}.detail div{padding:10px;background:var(--panel2);border-radius:10px}.detail small{display:block;color:var(--muted)}.detail b{display:block;margin-top:4px;font-size:12px}.pipeline{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}.pipeline>div,.funnel,.risk,.system>div{padding:19px;border:1px solid var(--line);border-radius:17px;background:var(--panel)}.pipeline small,.pipeline p{color:var(--muted)}.pipeline b{display:block;margin:8px 0}.pipeline p{font-size:12px}.funnel{margin-top:15px}.funnel p{display:grid;grid-template-columns:180px 1fr 55px;gap:13px;align-items:center}.funnel i{height:10px;background:linear-gradient(90deg,var(--accent),var(--green));border-radius:99px}.empty{min-height:300px;border:1px solid var(--line);border-radius:19px;background:var(--panel);display:grid;place-content:center;text-align:center;padding:30px}.empty>div{font-size:43px;color:#9c8cff}.empty p{color:var(--muted)}.system{display:grid;grid-template-columns:1.2fr .8fr;gap:14px}.system p{display:flex;justify-content:space-between;border-bottom:1px solid var(--line);padding:10px 0}.system p span{color:var(--muted)}.system a{color:#aa9aff;text-decoration:none}.risk .kpis{grid-template-columns:repeat(4,1fr)}@media(max-width:1000px){.app{grid-template-columns:78px 1fr}.brand div,.safe,nav span{display:none}.brand{justify-content:center}.kpis{grid-template-columns:repeat(3,1fr)}.pipeline{grid-template-columns:1fr 1fr}}@media(max-width:720px){.app{display:block}aside{position:fixed;top:auto;bottom:0;left:0;right:0;height:68px;padding:8px 10px;z-index:20;border-right:0;border-top:1px solid var(--line)}.brand,.safe{display:none}nav{grid-template-columns:repeat(5,1fr)}nav button{text-align:center}.content{padding-bottom:68px}.top{padding:0 15px}.top span{display:none}main{padding:19px 14px 35px}.hero{display:block;padding:22px}.hero h1{font-size:29px}.hero-state{margin-top:19px;min-width:0}.kpis{grid-template-columns:1fr 1fr}.grid{grid-template-columns:1fr}.heading{align-items:flex-start;flex-direction:column}.tools{flex-wrap:wrap}.tools input{width:100%;min-width:0}.pipeline,.system{grid-template-columns:1fr}.levels{grid-template-columns:1fr 1fr}}
"""
JS = """
const names={overview:'Обзор',signals:'Сигналы',stats:'Статистика',risk:'Risk Manager',system:'Система'};function show(name){document.querySelectorAll('.view').forEach(v=>v.classList.remove('active'));document.querySelectorAll('nav button').forEach(b=>b.classList.toggle('active',b.dataset.view===name));document.getElementById(name)?.classList.add('active');document.getElementById('title').textContent=names[name]||'TradeMind';location.hash=name;scrollTo(0,0)}document.querySelectorAll('[data-view]').forEach(b=>b.onclick=()=>show(b.dataset.view));document.querySelectorAll('[data-jump]').forEach(b=>b.onclick=()=>show(b.dataset.jump));const cards=[...document.querySelectorAll('#cards .signal')];let filter='ALL';function apply(){const q=(document.getElementById('search')?.value||'').toLowerCase();cards.forEach(c=>c.style.display=(filter==='ALL'||c.dataset.action===filter)&&(c.dataset.search||'').includes(q)?'':'none')}document.getElementById('search')?.addEventListener('input',apply);document.querySelectorAll('[data-filter]').forEach(b=>b.onclick=()=>{filter=b.dataset.filter;document.querySelectorAll('[data-filter]').forEach(x=>x.classList.remove('active'));b.classList.add('active');apply()});if(names[location.hash.slice(1)])show(location.hash.slice(1));
"""


def render(data: Mapping[str, Any]) -> str:
    runtime = data.get("runtime", {}) if isinstance(data.get("runtime"), Mapping) else {}
    factory = data.get("factory", {}) if isinstance(data.get("factory"), Mapping) else {}
    bridge = data.get("bridge", {}) if isinstance(data.get("bridge"), Mapping) else {}
    summary = data.get("summary", {}) if isinstance(data.get("summary"), Mapping) else {}
    decision = (
        data.get("latest_decision", {})
        if isinstance(data.get("latest_decision"), Mapping)
        else {}
    )
    candidates = [
        item for item in data.get("candidates", []) if isinstance(item, Mapping)
    ]
    cards = "".join(
        signal_card(item, index) for index, item in enumerate(candidates)
    ) or "<div class='empty'><div>⌁</div><h2>Свежих сетапов пока нет</h2><p>Сканер продолжает проверять новые закрытые M5-свечи.</p></div>"
    recent = "".join(
        signal_card(item, index + 1000)
        for index, item in enumerate(candidates[:4])
    ) or cards
    runtime_label = human_state(runtime.get("state"))
    factory_label = human_state(factory.get("state"))
    bridge_label = human_state(bridge.get("state"))
    risk_label = human_state(runtime.get("risk_state") or "NONE")
    total = integer(summary.get("total_candidates"))
    fresh = integer(summary.get("fresh_factory"))
    publishable = integer(summary.get("publishable"))
    outcomes = integer(summary.get("completed_outcomes"))
    hero = "Есть проверенный торговый сетап" if publishable else "Рынок под наблюдением"
    note = (
        "Сигнал прошёл статистический фильтр и готов к расчёту риска."
        if publishable
        else "Качественный сигнал появится только после статистики и риск-проверки."
    )
    fresh_width = min(100, 100 * fresh / max(1, total))
    publishable_width = min(100, 100 * publishable / max(1, total))
    return f"""<!doctype html><html lang='ru'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><meta http-equiv='refresh' content='60'><title>TradeMind</title><style>{CSS}</style></head><body>
<div class='app'><aside><div class='brand'><i>T</i><div><b>TradeMind</b><small>Signal Intelligence</small></div></div><nav><button class='active' data-view='overview'>⌂ <span>Обзор</span></button><button data-view='signals'>⌁ <span>Сигналы</span></button><button data-view='stats'>◫ <span>Статистика</span></button><button data-view='risk'>◇ <span>Risk Manager</span></button><button data-view='system'>⚙ <span>Система</span></button></nav><div class='safe'><em></em><b>READ-ONLY</b><p>Ордера и публикация выключены.</p></div></aside>
<div class='content'><header class='top'><b id='title'>Обзор</b><div><span>Обновлено {html.escape(display_time(data.get('updated_at')))}</span><strong>Счёт {html.escape(text(runtime.get('account_login')))}</strong></div></header><main>
<section class='view active' id='overview'><div class='hero'><div><small>LIVE MARKET INTELLIGENCE</small><h1>{hero}</h1><p>{note}</p></div><div class='hero-state'><small>Состояние</small><b>{html.escape(factory_label)}</b><span>{html.escape(runtime_label)}</span></div></div><div class='kpis'><div><small>Live-сетапы</small><b>{total}</b></div><div><small>Свежие</small><b>{fresh}</b></div><div><small>Завершённые</small><b>{outcomes}</b></div><div><small>BUY / SELL</small><b>{integer(summary.get('buy'))} / {integer(summary.get('sell'))}</b></div><div><small>Средний Quality</small><b>{fmt(summary.get('average_quality'),1)}</b></div></div><div class='heading'><div><h2>Последние сетапы</h2><p>Реальные M5-свечи, Entry, Stop и TP.</p></div><button data-jump='signals'>Смотреть все</button></div><div class='grid'>{recent}</div></section>
<section class='view' id='signals'><div class='heading'><div><h2>Сигналы</h2><p>Наблюдения до публикации и расчёта риска.</p></div><div class='tools'><input id='search' placeholder='Инструмент или сетап'><button class='active' data-filter='ALL'>Все</button><button data-filter='BUY'>BUY</button><button data-filter='SELL'>SELL</button></div></div><div class='grid' id='cards'>{cards}</div></section>
<section class='view' id='stats'><div class='heading'><div><h2>Статистика</h2><p>Путь сетапа от рынка до риск-решения.</p></div></div><div class='pipeline'><div><small>Runtime</small><b>{html.escape(runtime_label)}</b><p>Закрытые M5-свечи.</p></div><div><small>Passport Factory</small><b>{html.escape(factory_label)}</b><p>История и publication gate.</p></div><div><small>MT5 Bridge</small><b>{html.escape(bridge_label)}</b><p>Живой счёт и спецификация.</p></div><div><small>Risk Manager</small><b>{html.escape(risk_label)}</b><p>ALLOW/BLOCK без ордера.</p></div></div><div class='funnel'><h3>Текущая воронка</h3><p><span>Live-кандидаты</span><i style='width:100%'></i><b>{total}</b></p><p><span>Свежие</span><i style='width:{fresh_width:.1f}%'></i><b>{fresh}</b></p><p><span>Publishable</span><i style='width:{publishable_width:.1f}%'></i><b>{publishable}</b></p></div></section>
<section class='view' id='risk'><div class='heading'><div><h2>Risk Manager</h2><p>Персональный объём, риск и маржа.</p></div></div>{risk_html(decision)}</section>
<section class='view' id='system'><div class='heading'><div><h2>Система</h2><p>Технические детали спрятаны здесь.</p></div></div><div class='system'><div><h3>Контур</h3><p><span>Runtime</span><b>{html.escape(runtime_label)}</b></p><p><span>Factory</span><b>{html.escape(factory_label)}</b></p><p><span>Bridge</span><b>{html.escape(bridge_label)}</b></p><p><span>Risk</span><b>{html.escape(risk_label)}</b></p><a href='../dashboard/index.html'>Технический dashboard →</a></div><div><h3>Безопасность</h3><p><span>Режим</span><b>READ-ONLY</b></p><p><span>Ордера</span><b>OFF</b></p><p><span>Публикация</span><b>OFF</b></p><p><span>Broker API</span><b>не вызывается</b></p></div></div></section>
</main></div></div><script>{JS}</script></body></html>"""


def build_payload(
    data: Mapping[str, Any],
    canonical: Path | None,
    limit: int,
    candle_limit: int,
) -> dict[str, Any]:
    runtime = dict(data.get("runtime", {}))
    factory = dict(data.get("factory", {}))
    bridge = dict(data.get("bridge", {}))
    candidates = [
        dict(item)
        for item in data.get("candidates", [])
        if isinstance(item, Mapping)
    ][:limit]
    symbols = {
        text(item.get("symbol")).upper()
        for item in candidates
        if text(item.get("symbol"))
    }
    candles = load_candles(
        canonical,
        symbols,
        integer(runtime.get("server_utc_offset_hours")),
        candle_limit,
    )
    for item in candidates:
        item["candles"] = candles.get(text(item.get("symbol")).upper(), [])
    raw_summary = (
        data.get("summary", {}) if isinstance(data.get("summary"), Mapping) else {}
    )
    qualities = [
        number(item.get("quality_score"), math.nan)
        for item in candidates
        if math.isfinite(number(item.get("quality_score"), math.nan))
    ]
    latest_decision = (
        dict(data.get("latest_decision", {}))
        if isinstance(data.get("latest_decision"), Mapping)
        else {}
    )
    return {
        "schema_version": VERSION,
        "updated_at": text(data.get("updated_at")),
        "runtime": runtime,
        "factory": factory,
        "bridge": bridge,
        "latest_decision": latest_decision,
        "candidates": candidates,
        "summary": {
            "total_candidates": integer(raw_summary.get("candidates")),
            "completed_outcomes": integer(raw_summary.get("outcomes")),
            "fresh_factory": integer(factory.get("fresh")),
            "publishable": integer(factory.get("publishable")),
            "buy": sum(text(item.get("action")).upper() == "BUY" for item in candidates),
            "sell": sum(text(item.get("action")).upper() == "SELL" for item in candidates),
            "average_quality": (
                sum(qualities) / len(qualities) if qualities else None
            ),
        },
        "safety": {
            "read_only": True,
            "orders_enabled": False,
            "publication_enabled": False,
            "broker_api_called": False,
        },
    }


def run_product_ui(
    runtime_root: Path,
    limit: int = 24,
    candle_limit: int = 48,
) -> tuple[Path, Mapping[str, Any]]:
    root = runtime_root.expanduser().resolve()
    source = read_json(root / "dashboard" / "data.json")
    runtime = source.get("runtime", {}) if isinstance(source.get("runtime"), Mapping) else {}
    paths = runtime.get("paths", {}) if isinstance(runtime.get("paths"), Mapping) else {}
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
                "signals": len(payload["candidates"]),
                "read_only": True,
            },
            ensure_ascii=False,
            indent=2,
        ),
    )
    return index, payload


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="TradeMind Product UI v1.23")
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
        print(f"TradeMind Product UI failed: {exc}")
        return 1
    print("TradeMind Product UI v1.23")
    print("Modern read-only interface. Orders OFF. Publication OFF.")
    print(f"Signals displayed: {len(payload['candidates'])}")
    print(f"Product UI: {index}")
    if args.open and hasattr(os, "startfile"):
        os.startfile(index)  # type: ignore[attr-defined]
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
