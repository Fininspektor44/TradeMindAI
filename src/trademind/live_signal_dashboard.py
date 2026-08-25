"""TradeMind Live Signal Dashboard v1.22.

The dashboard is a read-only presentation layer over the live runtime, passport
factory and signal-to-risk bridge files. It never recalculates the publication
gate, calls a broker, publishes a signal or sends an order.
"""

from __future__ import annotations

import argparse
import html
import json
import math
import os
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from trademind.mt5_canonical_accounts import MARKET_DATA_ACCOUNT_LOGIN
from trademind.signal_evidence import load_outcomes
from trademind.signal_intelligence import SignalCandidate
from trademind.signal_shadow import load_candidates

DASHBOARD_VERSION = "1.22.0"
DASHBOARD_OK = "OK"
DEFAULT_LIMIT = 60


def _text(value: Any) -> str:
    return str(value or "").strip()


def _number(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def _integer(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _read_json(path: Path) -> Mapping[str, Any]:
    if not path.is_file():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, Mapping):
        raise ValueError(f"JSON root must be an object: {path}")
    return payload


def _atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    _atomic_text(
        path,
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
    )


def _display_time(value: Any) -> str:
    text = _text(value)
    if not text:
        return "—"
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    if parsed.tzinfo is None:
        return text
    return parsed.astimezone().strftime("%d.%m.%Y %H:%M:%S")


def _age_text(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds} сек"
    minutes, remainder = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} мин {remainder:02d} сек"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} ч {minutes:02d} мин"


def _fmt(value: Any, digits: int = 3, suffix: str = "") -> str:
    number = _number(value, math.nan)
    if not math.isfinite(number):
        return "—"
    return f"{number:.{digits}f}{suffix}"


def _candidate_market(candidate: SignalCandidate) -> dict[str, Any]:
    features = candidate.market_features
    structure = dict(features.get("structure", {}))
    liquidity = dict(features.get("liquidity", {}))
    fibonacci = dict(features.get("fibonacci", {}))
    volume = dict(features.get("volume", {}))
    momentum = dict(features.get("momentum", {}))
    volatility = dict(features.get("volatility", {}))
    confirmation = dict(features.get("confirmation", {}))
    session = dict(features.get("session", {}))
    execution = dict(features.get("execution", {}))
    return {
        "structure": {
            "swing_bias": _text(structure.get("swing_bias")) or "UNKNOWN",
            "swing_break": _text(structure.get("swing_break")) or "NONE",
            "internal_bias": _text(structure.get("internal_bias")) or "UNKNOWN",
            "internal_break": _text(structure.get("internal_break")) or "NONE",
            "protected_low": structure.get("protected_low"),
            "protected_high": structure.get("protected_high"),
        },
        "liquidity": {
            "bsl_sweep": bool(liquidity.get("bsl_sweep")),
            "ssl_sweep": bool(liquidity.get("ssl_sweep")),
            "bsl_depth_atr": liquidity.get("bsl_sweep_depth_atr"),
            "ssl_depth_atr": liquidity.get("ssl_sweep_depth_atr"),
        },
        "fibonacci": {
            "retracement": fibonacci.get("retracement"),
            "ote_low": fibonacci.get("ote_low"),
            "ote_mid": fibonacci.get("ote_mid"),
            "ote_high": fibonacci.get("ote_high"),
        },
        "volume": {
            "rvol_20": volume.get("rvol_20"),
            "percentile": volume.get("volume_percentile_100"),
            "imbalance": volume.get("direction_imbalance"),
            "tick_rate_ratio": volume.get("tick_rate_ratio_20"),
        },
        "momentum": {
            "impulse_atr": momentum.get("impulse_atr"),
            "body_efficiency": momentum.get("body_efficiency_ratio_20"),
        },
        "volatility": {
            "atr": volatility.get("atr"),
            "spread_cost_atr": volatility.get("spread_cost_atr"),
            "spread_ratio": volatility.get("spread_ratio_20"),
        },
        "confirmation": {
            "fvg": _text(confirmation.get("fvg")) or "NONE",
            "fvg_size_atr": confirmation.get("fvg_size_atr"),
        },
        "session": _text(session.get("name")) or "UNKNOWN",
        "execution": {
            "point": execution.get("point"),
            "spread_mean_points": execution.get("spread_mean_points"),
            "spread_max_points": execution.get("spread_max_points"),
        },
    }


def _candidate_view(
    candidate: SignalCandidate,
    *,
    evaluation: Mapping[str, Any] | None,
    outcome: Mapping[str, Any] | None,
    now: datetime,
) -> dict[str, Any]:
    evaluation = evaluation or {}
    age_seconds = max(
        0.0,
        (now - candidate.created_at.astimezone(timezone.utc)).total_seconds(),
    )
    state = _text(evaluation.get("state")) or "PENDING_GATE"
    if outcome:
        state = f"OUTCOME_{_text(outcome.get('outcome')).upper() or 'COMPLETED'}"
    return {
        "signal_id": candidate.signal_id,
        "created_at": _iso(candidate.created_at),
        "age_seconds": age_seconds,
        "symbol": candidate.symbol,
        "timeframe": candidate.timeframe,
        "action": candidate.plan.action,
        "setup_family": candidate.setup_family,
        "scenario": candidate.scenario,
        "state": state,
        "quality_score": evaluation.get("quality_score"),
        "conservative_probability": evaluation.get("conservative_probability"),
        "expected_value_r": evaluation.get("expected_value_r"),
        "historical_sample": _integer(evaluation.get("historical_sample")),
        "checks": dict(evaluation.get("checks", {}))
        if isinstance(evaluation.get("checks"), Mapping)
        else {},
        "reasons": list(evaluation.get("reasons", []))
        if isinstance(evaluation.get("reasons"), Sequence)
        and not isinstance(evaluation.get("reasons"), (str, bytes))
        else [],
        "plan": candidate.plan.as_dict(),
        "factor_scores": dict(candidate.factor_scores),
        "factor_reasons": {
            key: list(values) for key, values in candidate.factor_reasons.items()
        },
        "market": _candidate_market(candidate),
        "outcome": dict(outcome) if outcome else None,
    }


def _load_evaluations(path: Path) -> dict[str, Mapping[str, Any]]:
    payload = _read_json(path)
    values = payload.get("evaluations", [])
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        return {}
    result: dict[str, Mapping[str, Any]] = {}
    for item in values:
        if not isinstance(item, Mapping):
            continue
        signal_id = _text(item.get("signal_id"))
        if signal_id:
            result[signal_id] = item
    return result


def _load_outcome_map(path: Path) -> dict[str, Mapping[str, Any]]:
    if not path.is_file():
        return {}
    return {outcome.signal_id: outcome.as_dict() for outcome in load_outcomes(path)}


def _resolve_payload_path(raw: Any, *, root: Path) -> Path | None:
    text = _text(raw)
    if not text:
        return None
    path = Path(text).expanduser()
    return path if path.is_absolute() else (root / path).resolve()


def _pipeline_stage(label: str, state: str, note: str) -> str:
    safe_state = html.escape(state or "UNKNOWN")
    css = "ok" if state in {"RUN_COMPLETE", "PASSPORTS_READY", "DECISION_READY", "ALLOW"} else "wait"
    if state in {"ERROR", "BLOCK", "REJECTED"}:
        css = "bad"
    return (
        f"<div class='stage {css}'><span>{html.escape(label)}</span>"
        f"<b>{safe_state}</b><small>{html.escape(note)}</small></div>"
    )


def _factor_rows(candidate: Mapping[str, Any]) -> str:
    rows: list[str] = []
    scores = candidate.get("factor_scores", {})
    reasons = candidate.get("factor_reasons", {})
    if not isinstance(scores, Mapping):
        return ""
    for name, raw in sorted(scores.items(), key=lambda item: (-_number(item[1]), str(item[0]))):
        score = max(0.0, min(1.0, _number(raw)))
        details = reasons.get(name, []) if isinstance(reasons, Mapping) else []
        title = "; ".join(str(item) for item in details)
        rows.append(
            "<div class='factor'>"
            f"<div class='factor-head'><span>{html.escape(str(name))}</span>"
            f"<b>{100 * score:.0f}</b></div>"
            f"<div class='track'><i style='width:{100 * score:.1f}%'></i></div>"
            f"<small>{html.escape(title)}</small></div>"
        )
    return "".join(rows)


def _checks(candidate: Mapping[str, Any]) -> str:
    checks = candidate.get("checks", {})
    if not isinstance(checks, Mapping) or not checks:
        return "<span class='muted'>Gate ещё не сформировал проверки.</span>"
    return "".join(
        f"<span class='chip {'pass' if bool(value) else 'fail'}'>"
        f"{'✓' if bool(value) else '×'} {html.escape(str(name))}</span>"
        for name, value in sorted(checks.items())
    )


def _reason_list(candidate: Mapping[str, Any]) -> str:
    reasons = candidate.get("reasons", [])
    if not reasons:
        return "<li>Отказных причин нет или кандидат ещё ожидает gate.</li>"
    return "".join(f"<li>{html.escape(str(reason))}</li>" for reason in reasons)


def _market_table(candidate: Mapping[str, Any]) -> str:
    market = candidate.get("market", {})
    if not isinstance(market, Mapping):
        return ""
    structure = market.get("structure", {}) if isinstance(market.get("structure"), Mapping) else {}
    liquidity = market.get("liquidity", {}) if isinstance(market.get("liquidity"), Mapping) else {}
    fibonacci = market.get("fibonacci", {}) if isinstance(market.get("fibonacci"), Mapping) else {}
    volume = market.get("volume", {}) if isinstance(market.get("volume"), Mapping) else {}
    momentum = market.get("momentum", {}) if isinstance(market.get("momentum"), Mapping) else {}
    volatility = market.get("volatility", {}) if isinstance(market.get("volatility"), Mapping) else {}
    confirmation = market.get("confirmation", {}) if isinstance(market.get("confirmation"), Mapping) else {}
    lines = [
        ("Структура", f"swing {structure.get('swing_bias', '—')} / {structure.get('swing_break', '—')}; internal {structure.get('internal_bias', '—')} / {structure.get('internal_break', '—')}"),
        ("Ликвидность", f"BSL sweep {int(bool(liquidity.get('bsl_sweep')))}; SSL sweep {int(bool(liquidity.get('ssl_sweep')))}"),
        ("Fibonacci", f"retracement {_fmt(fibonacci.get('retracement'))}; OTE {_fmt(fibonacci.get('ote_low'))}–{_fmt(fibonacci.get('ote_high'))}"),
        ("Объёмы", f"RVOL {_fmt(volume.get('rvol_20'), 2)}; percentile {_fmt(volume.get('percentile'), 1)}; imbalance {_fmt(volume.get('imbalance'))}"),
        ("Импульс", f"impulse/ATR {_fmt(momentum.get('impulse_atr'), 2)}; body efficiency {_fmt(momentum.get('body_efficiency'), 2)}"),
        ("Волатильность", f"ATR {_fmt(volatility.get('atr'), 6)}; spread/ATR {_fmt(volatility.get('spread_cost_atr'), 3)}"),
        ("Подтверждение", f"FVG {confirmation.get('fvg', '—')}; size {_fmt(confirmation.get('fvg_size_atr'), 3)} ATR"),
        ("Сессия", _text(market.get("session")) or "—"),
    ]
    return "".join(
        f"<tr><th>{html.escape(label)}</th><td>{html.escape(value)}</td></tr>"
        for label, value in lines
    )


def _plan(candidate: Mapping[str, Any]) -> str:
    plan = candidate.get("plan", {})
    if not isinstance(plan, Mapping):
        return ""
    entries = plan.get("entries", [])
    entry_html = "".join(
        "<li>"
        f"{html.escape(_text(entry.get('order_type')))} "
        f"{_fmt(entry.get('price'), 6)} · {100 * _number(entry.get('allocation')):.0f}%"
        f"<small>{html.escape(_text(entry.get('rationale')))}</small></li>"
        for entry in entries
        if isinstance(entry, Mapping)
    )
    targets = ", ".join(_fmt(value, 6) for value in plan.get("targets", []))
    return (
        f"<ul class='entries'>{entry_html}</ul>"
        "<div class='plan-grid'>"
        f"<span>Средний вход<b>{_fmt(plan.get('average_entry'), 6)}</b></span>"
        f"<span>Stop<b>{_fmt(plan.get('stop_price'), 6)}</b></span>"
        f"<span>Targets<b>{html.escape(targets or '—')}</b></span>"
        f"<span>RR до TP1<b>{_fmt(plan.get('first_target_rr'), 2)}R</b></span>"
        "</div>"
    )


def _outcome(candidate: Mapping[str, Any]) -> str:
    outcome = candidate.get("outcome")
    if not isinstance(outcome, Mapping):
        return "<span class='muted'>Исход ещё не завершён.</span>"
    return (
        f"<b>{html.escape(_text(outcome.get('outcome')))}</b> · "
        f"{_fmt(outcome.get('net_r'), 3)}R · "
        f"MFE {_fmt(outcome.get('mfe_r'), 2)}R · "
        f"MAE {_fmt(outcome.get('mae_r'), 2)}R"
    )


def _candidate_card(candidate: Mapping[str, Any]) -> str:
    action = _text(candidate.get("action"))
    state = _text(candidate.get("state"))
    quality = candidate.get("quality_score")
    probability = candidate.get("conservative_probability")
    expected = candidate.get("expected_value_r")
    search_text = " ".join(
        _text(candidate.get(key))
        for key in ("symbol", "action", "setup_family", "state", "scenario")
    ).lower()
    return f"""
<article class='candidate' data-state='{html.escape(state)}' data-search='{html.escape(search_text)}'>
  <header>
    <div><span class='action {html.escape(action.lower())}'>{html.escape(action)}</span>
    <b class='symbol'>{html.escape(_text(candidate.get('symbol')))}</b>
    <span>{html.escape(_text(candidate.get('timeframe')))}</span></div>
    <span class='state'>{html.escape(state)}</span>
  </header>
  <div class='candidate-meta'>
    <span>{_display_time(candidate.get('created_at'))}</span>
    <span>возраст {_age_text(_number(candidate.get('age_seconds')))}</span>
    <span>N={_integer(candidate.get('historical_sample'))}</span>
  </div>
  <h3>{html.escape(_text(candidate.get('setup_family')))}</h3>
  <p>{html.escape(_text(candidate.get('scenario')))}</p>
  <section class='metrics'>
    <span>Quality<b>{_fmt(quality, 1)}</b></span>
    <span>95% low<b>{_fmt(100 * _number(probability), 1, '%') if probability is not None else '—'}</b></span>
    <span>EV<b>{_fmt(expected, 3, 'R') if expected is not None else '—'}</b></span>
  </section>
  <details open><summary>Торговый план</summary>{_plan(candidate)}</details>
  <details><summary>Почему движок увидел сетап</summary><div class='factors'>{_factor_rows(candidate)}</div></details>
  <details><summary>Рыночные данные</summary><table class='market'>{_market_table(candidate)}</table></details>
  <details><summary>Gate и причины решения</summary><div class='checks'>{_checks(candidate)}</div><ul class='reasons'>{_reason_list(candidate)}</ul></details>
  <details><summary>Теневой исход</summary><div class='outcome'>{_outcome(candidate)}</div></details>
  <footer>{html.escape(_text(candidate.get('signal_id')))}</footer>
</article>"""


def _risk_panel(bridge_status: Mapping[str, Any], decision: Mapping[str, Any]) -> str:
    if not decision:
        return (
            "<div class='empty'><b>Risk Manager ждёт паспорт.</b> "
            f"Bridge: {html.escape(_text(bridge_status.get('state')) or 'UNKNOWN')}. "
            "Лотность не рассчитывается без PUBLISHABLE-сетапа.</div>"
        )
    summary = decision.get("trader_summary", {})
    passport = decision.get("passport", {})
    if not isinstance(summary, Mapping):
        summary = {}
    if not isinstance(passport, Mapping):
        passport = {}
    orders = summary.get("orders", [])
    order_rows = "".join(
        "<tr>"
        f"<td>{_integer(order.get('entry_index'))}</td>"
        f"<td>{html.escape(_text(order.get('order_type')))}</td>"
        f"<td>{_fmt(order.get('planned_price'), 6)}</td>"
        f"<td>{_fmt(order.get('volume'), 4)}</td>"
        f"<td>{_fmt(order.get('risk_money'), 2)}</td></tr>"
        for order in orders
        if isinstance(order, Mapping)
    )
    reasons = summary.get("block_reasons", [])
    reason_html = "".join(
        f"<li><b>{html.escape(_text(item.get('code')))}</b>: "
        f"{html.escape(_text(item.get('message')))}</li>"
        for item in reasons
        if isinstance(item, Mapping)
    ) or "<li>Блокирующих причин нет.</li>"
    return f"""
<div class='risk-head'>
  <span class='risk-state'>{html.escape(_text(summary.get('decision')))}</span>
  <b>{html.escape(_text(summary.get('symbol')))} {html.escape(_text(summary.get('action')))}</b>
  <small>{html.escape(_text(passport.get('signal_id')))}</small>
</div>
<div class='metrics risk-metrics'>
  <span>Риск запрошен<b>{_fmt(summary.get('requested_risk_pct'), 3, '%')}</b></span>
  <span>Риск фактический<b>{_fmt(summary.get('actual_risk_pct'), 3, '%')}</b></span>
  <span>Риск деньгами<b>{_fmt(summary.get('actual_risk_money'), 2)}</b></span>
  <span>Портфель после<b>{_fmt(summary.get('portfolio_risk_after_pct'), 3, '%')}</b></span>
  <span>Маржа<b>{_fmt(summary.get('margin_required'), 2)}</b></span>
  <span>Свободно после<b>{_fmt(summary.get('free_margin_after'), 2)}</b></span>
</div>
<table class='orders'><thead><tr><th>#</th><th>Тип</th><th>Цена</th><th>Объём</th><th>Риск</th></tr></thead><tbody>{order_rows}</tbody></table>
<ul class='reasons'>{reason_html}</ul>"""


def _render(data: Mapping[str, Any]) -> str:
    runtime = data.get("runtime", {}) if isinstance(data.get("runtime"), Mapping) else {}
    factory = data.get("factory", {}) if isinstance(data.get("factory"), Mapping) else {}
    bridge = data.get("bridge", {}) if isinstance(data.get("bridge"), Mapping) else {}
    summary = data.get("summary", {}) if isinstance(data.get("summary"), Mapping) else {}
    candidates = data.get("candidates", [])
    decision = data.get("latest_decision", {})
    candidate_html = "".join(
        _candidate_card(candidate)
        for candidate in candidates
        if isinstance(candidate, Mapping)
    ) or "<div class='empty'>Live-кандидатов пока нет. Runtime продолжает наблюдение.</div>"
    pipeline = "".join(
        (
            _pipeline_stage("Runtime", _text(runtime.get("state")), "новые закрытые M5-бары"),
            _pipeline_stage("Passport Factory", _text(factory.get("state")), "статистика и publication gate"),
            _pipeline_stage("MT5 Bridge", _text(bridge.get("state")), "живой счёт и спецификация"),
            _pipeline_stage("Risk Manager", _text(runtime.get("risk_state")) or "NONE", "ALLOW/BLOCK без отправки ордера"),
        )
    )
    return f"""<!doctype html>
<html lang='ru'><head><meta charset='utf-8'>
<meta name='viewport' content='width=device-width,initial-scale=1'>
<meta http-equiv='refresh' content='60'>
<title>TradeMind Live Signal Dashboard v1.22</title>
<style>
:root{{--bg:#06131d;--panel:#0b2231;--panel2:#0e2b3d;--line:#1e5872;--text:#eaf7ff;--muted:#8fb0c2;--green:#36e1a4;--yellow:#ffd166;--red:#ff6b7c;--blue:#55b9ff}}
*{{box-sizing:border-box}}body{{margin:0;background:radial-gradient(circle at top right,#0d3042 0,#06131d 42%);color:var(--text);font-family:Segoe UI,Arial,sans-serif}}
main{{max-width:1660px;margin:auto;padding:26px}}h1{{margin:0;font-size:34px}}h2{{margin-top:32px}}p{{line-height:1.45}}.top{{display:flex;justify-content:space-between;gap:20px;align-items:flex-start}}.top small,.muted{{color:var(--muted)}}.safe{{padding:10px 14px;border:1px solid #167452;background:#0b392e;border-radius:12px;color:var(--green);font-weight:700}}
.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;margin:18px 0}}.card,.stage,.candidate,.risk,.empty{{background:linear-gradient(145deg,var(--panel),#081b27);border:1px solid var(--line);border-radius:15px;padding:16px;box-shadow:0 10px 24px #0004}}.card span{{color:var(--muted)}}.card b{{display:block;font-size:27px;margin-top:5px}}
.pipeline{{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}}.stage{{display:flex;flex-direction:column;gap:8px;border-top:4px solid var(--yellow)}}.stage.ok{{border-top-color:var(--green)}}.stage.bad{{border-top-color:var(--red)}}.stage small{{color:var(--muted)}}
.toolbar{{display:flex;gap:10px;flex-wrap:wrap;margin:16px 0}}input,button{{background:#0a2535;color:var(--text);border:1px solid var(--line);border-radius:10px;padding:10px 12px}}button{{cursor:pointer}}button.active{{border-color:var(--green);color:var(--green)}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(430px,1fr));gap:16px}}.candidate{{padding:0;overflow:hidden}}.candidate>header{{display:flex;justify-content:space-between;align-items:center;padding:15px 17px;background:#0e3044}}.action{{font-weight:800;padding:5px 9px;border-radius:8px;margin-right:8px}}.action.buy{{background:#0d503d;color:#6af0bd}}.action.sell{{background:#592333;color:#ff93a1}}.symbol{{font-size:21px;margin-right:7px}}.state{{font-size:12px;border:1px solid #3d7894;border-radius:20px;padding:6px 9px}}.candidate-meta{{display:flex;gap:14px;flex-wrap:wrap;padding:11px 17px;color:var(--muted);font-size:13px}}.candidate h3,.candidate>p,.candidate>details,.candidate>section{{margin-left:17px;margin-right:17px}}.candidate h3{{margin-bottom:5px}}.candidate>p{{color:#c4dbe7}}details{{border-top:1px solid #183e51;padding:12px 0}}summary{{cursor:pointer;font-weight:700}}.candidate footer{{padding:10px 17px;background:#071721;color:#62879a;font:11px Consolas,monospace;overflow-wrap:anywhere}}
.metrics{{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-top:14px;margin-bottom:14px}}.metrics span,.plan-grid span{{background:#071b28;border-radius:10px;padding:9px;color:var(--muted)}}.metrics b,.plan-grid b{{display:block;color:var(--text);font-size:18px;margin-top:3px}}.entries{{padding-left:20px}}.entries li{{margin:7px 0}}.entries small{{display:block;color:var(--muted)}}.plan-grid{{display:grid;grid-template-columns:repeat(2,1fr);gap:8px}}
.factors{{display:grid;grid-template-columns:repeat(2,1fr);gap:10px;margin-top:12px}}.factor{{background:#071a26;padding:9px;border-radius:10px}}.factor-head{{display:flex;justify-content:space-between}}.track{{height:6px;background:#193b4b;border-radius:10px;margin:6px 0;overflow:hidden}}.track i{{display:block;height:100%;background:linear-gradient(90deg,#39a8ef,var(--green))}}.factor small{{color:var(--muted)}}
.market,.orders{{width:100%;border-collapse:collapse;margin-top:10px}}.market th,.market td,.orders th,.orders td{{padding:7px;border-bottom:1px solid #173b4d;text-align:left}}.market th{{width:125px;color:var(--muted)}}.checks{{display:flex;flex-wrap:wrap;gap:6px;margin-top:10px}}.chip{{padding:5px 8px;border-radius:20px;font-size:12px}}.chip.pass{{background:#0d4938;color:#5ceab7}}.chip.fail{{background:#532331;color:#ff91a0}}.reasons{{padding-left:20px;color:#d7e7ef}}.outcome{{margin-top:10px}}
.risk{{margin-top:14px}}.risk-head{{display:flex;gap:12px;align-items:center;flex-wrap:wrap}}.risk-head small{{color:var(--muted)}}.risk-state{{font-weight:800;padding:7px 10px;background:#133e50;border-radius:9px}}.risk-metrics{{grid-template-columns:repeat(auto-fit,minmax(150px,1fr))}}
@media(max-width:850px){{main{{padding:14px}}.top{{display:block}}.pipeline{{grid-template-columns:1fr 1fr}}.grid{{grid-template-columns:1fr}}.factors{{grid-template-columns:1fr}}}}
</style></head><body><main>
<div class='top'><div><h1>TradeMind Live Signal Dashboard v1.22</h1>
<p>Обновлено: {html.escape(_display_time(data.get('updated_at')))} · аккаунт {html.escape(_text(runtime.get('account_login')))}</p></div>
<div class='safe'>READ-ONLY · Ордера OFF · Публикация OFF</div></div>
<section class='cards'>
<div class='card'><span>Закрытых FX M5-баров</span><b>{_integer(runtime.get('closed_fx_m5_rows'))}</b></div>
<div class='card'><span>Live-кандидатов</span><b>{_integer(summary.get('candidates'))}</b></div>
<div class='card'><span>Завершённых live-исходов</span><b>{_integer(summary.get('outcomes'))}</b></div>
<div class='card'><span>Свежих у Factory</span><b>{_integer(factory.get('fresh_candidates'))}</b></div>
<div class='card'><span>PUBLISHABLE</span><b>{_integer(factory.get('publishable'))}</b></div>
<div class='card'><span>Последний закрытый бар</span><b style='font-size:18px'>{html.escape(_display_time(runtime.get('latest_closed_bar_at')))}</b></div>
</section>
<section class='pipeline'>{pipeline}</section>
<h2>Решение по живому счёту</h2><section class='risk'>{_risk_panel(bridge, decision if isinstance(decision, Mapping) else {})}</section>
<h2>Последние кандидаты</h2>
<div class='toolbar'><input id='search' placeholder='Символ, BUY/SELL, сетап, статус'>
<button class='active' data-filter='ALL'>Все</button><button data-filter='PUBLISHABLE'>PUBLISHABLE</button>
<button data-filter='SHADOW_ONLY'>SHADOW_ONLY</button><button data-filter='REJECTED'>REJECTED</button>
<button data-filter='OUTCOME'>С исходом</button></div>
<section class='grid' id='candidates'>{candidate_html}</section>
</main><script>
const cards=[...document.querySelectorAll('.candidate')];let filter='ALL';
function apply(){{const q=document.getElementById('search').value.toLowerCase();cards.forEach(c=>{{const state=c.dataset.state||'';const okFilter=filter==='ALL'||state.includes(filter);const okSearch=(c.dataset.search||'').includes(q);c.style.display=okFilter&&okSearch?'':'none';}})}}
document.getElementById('search').addEventListener('input',apply);document.querySelectorAll('[data-filter]').forEach(b=>b.addEventListener('click',()=>{{filter=b.dataset.filter;document.querySelectorAll('[data-filter]').forEach(x=>x.classList.remove('active'));b.classList.add('active');apply();}}));
</script></body></html>"""


@dataclass(frozen=True, slots=True)
class DashboardRun:
    status: Mapping[str, Any]
    dashboard_path: Path
    data_path: Path


def run_live_dashboard(
    *,
    runtime_root: Path,
    login: str,
    runtime_status: Mapping[str, Any] | None = None,
    candidate_limit: int = DEFAULT_LIMIT,
    now: datetime | None = None,
) -> DashboardRun:
    captured_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if candidate_limit < 1:
        raise ValueError("candidate_limit must be positive")
    runtime_root = runtime_root.expanduser().resolve()
    dashboard_dir = runtime_root / "dashboard"
    dashboard_path = dashboard_dir / "index.html"
    data_path = dashboard_dir / "data.json"

    runtime = dict(runtime_status or _read_json(runtime_root / "status.json"))
    factory_status = dict(_read_json(runtime_root / "factory" / "status.json"))
    evaluations = _load_evaluations(runtime_root / "factory" / "evaluations.json")
    bridge_root = runtime_root / "bridge" / _text(login)
    bridge_status = dict(_read_json(bridge_root / "status.json"))
    latest_decision = dict(_read_json(bridge_root / "latest_decision.json"))

    candidates_path = runtime_root / "candidates.jsonl"
    outcomes_path = runtime_root / "outcomes.jsonl"
    candidates = load_candidates(candidates_path) if candidates_path.is_file() else []
    outcomes = _load_outcome_map(outcomes_path)
    candidates.sort(key=lambda item: (item.created_at, item.signal_id), reverse=True)
    candidate_views = [
        _candidate_view(
            candidate,
            evaluation=evaluations.get(candidate.signal_id),
            outcome=outcomes.get(candidate.signal_id),
            now=captured_at,
        )
        for candidate in candidates[:candidate_limit]
    ]
    state_counts = Counter(_text(item.get("state")) for item in candidate_views)
    summary = {
        "candidates": len(candidates),
        "outcomes": len(outcomes),
        "displayed_candidates": len(candidate_views),
        "states": dict(sorted(state_counts.items())),
        "buy": sum(item.plan.action == "BUY" for item in candidates),
        "sell": sum(item.plan.action == "SELL" for item in candidates),
    }
    payload = {
        "schema_version": DASHBOARD_VERSION,
        "updated_at": _iso(captured_at),
        "runtime": runtime,
        "factory": factory_status,
        "bridge": bridge_status,
        "latest_decision": latest_decision,
        "summary": summary,
        "candidates": candidate_views,
        "safety": {
            "read_only": True,
            "orders_enabled": False,
            "signal_publication_enabled": False,
            "broker_api_called": False,
            "dashboard_recalculates_gate": False,
        },
    }
    _atomic_json(data_path, payload)
    _atomic_text(dashboard_path, _render(payload))
    status = {
        "schema_version": DASHBOARD_VERSION,
        "state": DASHBOARD_OK,
        "updated_at": _iso(captured_at),
        "dashboard": str(dashboard_path),
        "data": str(data_path),
        "candidates": len(candidates),
        "displayed_candidates": len(candidate_views),
        "factory_state": _text(factory_status.get("state")),
        "bridge_state": _text(bridge_status.get("state")),
        "risk_state": _text(runtime.get("risk_state")) or "NONE",
        "safety": dict(payload["safety"]),
    }
    _atomic_json(dashboard_dir / "status.json", status)
    return DashboardRun(status=status, dashboard_path=dashboard_path, data_path=data_path)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="TradeMind Live Signal Dashboard v1.22")
    parser.add_argument(
        "--runtime-root",
        type=Path,
        default=Path("data/live_signal_runtime_v1"),
    )
    # Selects the per-account live bridge workspace
    # (<runtime-root>/bridge/<login>). Defaults to the canonical MARKET DATA
    # account -- this dashboard only ever reads market-data/live-signal
    # evidence, and must never silently fall back to an obsolete account's
    # workspace.
    parser.add_argument("--login", default=MARKET_DATA_ACCOUNT_LOGIN)
    parser.add_argument("--candidate-limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument("--open-dashboard", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = run_live_dashboard(
            runtime_root=args.runtime_root,
            login=args.login,
            candidate_limit=args.candidate_limit,
        )
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        print(f"Live Signal Dashboard failed: {exc}")
        return 1

    print("TradeMind Live Signal Dashboard v1.22")
    print("Read-only presentation. No gate recalculation. Orders OFF.")
    print(f"Candidates displayed: {result.status['displayed_candidates']}")
    print(f"Dashboard: {result.dashboard_path}")
    if args.open_dashboard and hasattr(os, "startfile"):
        os.startfile(result.dashboard_path)  # type: ignore[attr-defined]
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
