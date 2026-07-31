"""Generate a standalone HTML dashboard for the FX research stream."""

from __future__ import annotations

import argparse
import csv
import html
import math
import os
import webbrowser
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

_SCHEMA_VERSION = "1.4.3"
_STATUS_ORDER = {
    "VALIDATED": 0,
    "RESEARCH_CANDIDATE": 1,
    "UNSTABLE": 2,
    "INSUFFICIENT_SAMPLE": 3,
}


@dataclass(frozen=True)
class StateLine:
    symbol: str
    label: str
    session: str
    action: str
    horizon: int
    observations: int
    trades: int
    trading_days: int
    status: str
    win_rate: float
    profit_factor_atr: float
    avg_net_atr: float
    early_avg_net_atr: float
    late_avg_net_atr: float
    max_drawdown_atr: float
    max_loss_streak: int
    q_value: float | None
    reasons: str

    @property
    def evidence_score(self) -> float:
        """Ranking aid only; never a probability or expected return."""
        positive = max(self.avg_net_atr, 0.0)
        pf = self.profit_factor_atr
        bounded_pf = 5.0 if math.isinf(pf) and pf > 0 else max(0.0, min(pf, 5.0))
        drawdown_penalty = 1.0 + max(self.max_drawdown_atr, 0.0) / 10.0
        sample = math.log1p(max(self.trades, 0))
        stability = 1.0 if self.late_avg_net_atr > 0 and self.early_avg_net_atr > 0 else 0.35
        return positive * bounded_pf * sample * stability / drawdown_penalty


@dataclass(frozen=True)
class PairSummary:
    symbol: str
    observations: int
    completed_h12: int
    last_time: str
    avg_rvol: float
    avg_tick_rate: float
    avg_spread_points: float
    buy_count: int
    sell_count: int
    wait_count: int


@dataclass(frozen=True)
class DashboardSnapshot:
    generated_at: datetime
    observations: tuple[dict[str, str], ...]
    states: tuple[StateLine, ...]
    pairs: tuple[PairSummary, ...]
    status_counts: dict[str, int]


def _float(value: object, default: float = 0.0) -> float:
    text = str(value or "").strip()
    if not text:
        return default
    try:
        return float(text)
    except ValueError:
        return default


def _optional_float(value: object) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _int(value: object, default: int = 0) -> int:
    text = str(value or "").strip()
    if not text:
        return default
    try:
        return int(float(text))
    except ValueError:
        return default


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [
            {key: str(value or "").strip() for key, value in row.items()}
            for row in csv.DictReader(handle)
        ]


def _state(row: dict[str, str]) -> StateLine:
    return StateLine(
        symbol=row.get("symbol", "UNKNOWN").upper(),
        label=row.get("label", "UNKNOWN"),
        session=row.get("session", "UNKNOWN"),
        action=row.get("action", "WAIT").upper(),
        horizon=_int(row.get("horizon")),
        observations=_int(row.get("observations")),
        trades=_int(row.get("trades")),
        trading_days=_int(row.get("trading_days")),
        status=row.get("status", "INSUFFICIENT_SAMPLE").upper(),
        win_rate=_float(row.get("win_rate")),
        profit_factor_atr=_float(row.get("profit_factor_atr")),
        avg_net_atr=_float(row.get("avg_net_atr")),
        early_avg_net_atr=_float(row.get("early_avg_net_atr")),
        late_avg_net_atr=_float(row.get("late_avg_net_atr")),
        max_drawdown_atr=_float(row.get("max_drawdown_atr")),
        max_loss_streak=_int(row.get("max_loss_streak")),
        q_value=_optional_float(row.get("q_value")),
        reasons=row.get("reasons", ""),
    )


def _pair_summaries(observations: list[dict[str, str]]) -> tuple[PairSummary, ...]:
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in observations:
        grouped[row.get("symbol", "UNKNOWN").upper()].append(row)

    output: list[PairSummary] = []
    for symbol, rows in sorted(grouped.items()):
        actions = Counter(row.get("action", "WAIT").upper() for row in rows)
        last_time = max((row.get("signal_time", "") for row in rows), default="")
        output.append(
            PairSummary(
                symbol=symbol,
                observations=len(rows),
                completed_h12=sum(
                    row.get("outcome_12", "").upper() in {"WIN", "LOSS", "FLAT"}
                    for row in rows
                ),
                last_time=last_time,
                avg_rvol=sum(_float(row.get("rvol_20")) for row in rows) / len(rows),
                avg_tick_rate=sum(_float(row.get("tick_rate_per_sec")) for row in rows)
                / len(rows),
                avg_spread_points=sum(_float(row.get("spread_mean_points")) for row in rows)
                / len(rows),
                buy_count=actions["BUY"],
                sell_count=actions["SELL"],
                wait_count=actions["WAIT"],
            )
        )
    return tuple(output)


def collect_snapshot(observations_path: Path, states_path: Path) -> DashboardSnapshot:
    observation_rows = _read_csv(observations_path)
    states = tuple(_state(row) for row in _read_csv(states_path))
    status_counts = Counter(item.status for item in states)
    return DashboardSnapshot(
        generated_at=datetime.now(timezone.utc),
        observations=tuple(observation_rows),
        states=states,
        pairs=_pair_summaries(observation_rows),
        status_counts=dict(status_counts),
    )


def _escape(value: object) -> str:
    return html.escape(str(value), quote=True)


def _number(value: float | None, digits: int = 2) -> str:
    if value is None:
        return "—"
    if math.isinf(value):
        return "∞" if value > 0 else "−∞"
    if math.isnan(value):
        return "—"
    return f"{value:.{digits}f}"


def _status_badge(status: str) -> str:
    labels = {
        "VALIDATED": "ПОДТВЕРЖДЕНО",
        "RESEARCH_CANDIDATE": "КАНДИДАТ",
        "UNSTABLE": "НЕСТАБИЛЬНО",
        "INSUFFICIENT_SAMPLE": "МАЛО ДАННЫХ",
    }
    css = status.lower()
    return f'<span class="badge {css}">{_escape(labels.get(status, status))}</span>'


def _top_states(states: tuple[StateLine, ...], limit: int = 8) -> list[StateLine]:
    confirmed = [
        item
        for item in states
        if item.status in {"VALIDATED", "RESEARCH_CANDIDATE"}
    ]
    if confirmed:
        return sorted(
            confirmed,
            key=lambda item: (
                _STATUS_ORDER.get(item.status, 9),
                -item.evidence_score,
                -item.trades,
            ),
        )[:limit]

    provisional = [
        item
        for item in states
        if item.avg_net_atr > 0
        and item.trades >= 10
        and item.early_avg_net_atr > 0
        and item.late_avg_net_atr > 0
    ]
    return sorted(
        provisional,
        key=lambda item: (-item.evidence_score, -item.trades),
    )[:limit]


def _risk_states(states: tuple[StateLine, ...], limit: int = 8) -> list[StateLine]:
    risky = [
        item
        for item in states
        if item.trades > 0
        and (
            item.avg_net_atr < 0
            or item.status == "UNSTABLE"
            or item.max_drawdown_atr >= 10
        )
    ]
    return sorted(
        risky,
        key=lambda item: (
            item.avg_net_atr,
            -item.max_drawdown_atr,
            -item.max_loss_streak,
        ),
    )[:limit]


def _state_card(item: StateLine, *, provisional: bool = False) -> str:
    eyebrow = "ПРЕДВАРИТЕЛЬНО, МАЛО ДАННЫХ" if provisional else "ЛУЧШЕЕ ДОКАЗАТЕЛЬСТВО"
    return (
        f'<article class="card evidence"><div class="eyebrow">{eyebrow}</div>'
        f"<h3>{_escape(item.symbol)} · {_escape(item.label)}</h3>"
        f'<div class="subline">{_escape(item.session)} · {_escape(item.action)} · '
        f"H{item.horizon}</div>"
        '<div class="mini-grid">'
        f"<span><b>{item.trades}</b>сделок</span>"
        f"<span><b>{item.trading_days}</b>дней</span>"
        f"<span><b>{_number(item.win_rate, 1)}%</b>WR</span>"
        f"<span><b>{_number(item.profit_factor_atr)}</b>PF ATR</span>"
        f"<span><b>{_number(item.avg_net_atr, 3)}</b>avg ATR</span>"
        f"<span><b>{_number(item.max_drawdown_atr)}</b>DD ATR</span>"
        "</div>"
        f'<div class="card-status">{_status_badge(item.status)}</div>'
        "</article>"
    )


def _risk_card(item: StateLine) -> str:
    return (
        '<article class="card risk">'
        f"<h3>{_escape(item.symbol)} · {_escape(item.label)}</h3>"
        f'<div class="subline">{_escape(item.session)} · {_escape(item.action)} · '
        f"H{item.horizon}</div>"
        '<div class="mini-grid">'
        f"<span><b>{item.trades}</b>сделок</span>"
        f"<span><b>{_number(item.avg_net_atr, 3)}</b>avg ATR</span>"
        f"<span><b>{_number(item.max_drawdown_atr)}</b>DD ATR</span>"
        f"<span><b>{item.max_loss_streak}</b>серия</span>"
        "</div>"
        f"<p>{_escape(item.reasons or 'Комбинация не прошла исследовательские условия.')}</p>"
        "</article>"
    )


def _pair_card(item: PairSummary) -> str:
    freshness = item.last_time.replace("T", " ")[:16] if item.last_time else "нет данных"
    return (
        '<article class="card pair">'
        f"<h3>{_escape(item.symbol)}</h3>"
        f'<div class="big">{item.observations}</div>'
        '<div class="muted">исследовательских наблюдений</div>'
        '<div class="mini-grid">'
        f"<span><b>{item.completed_h12}</b>готово H12</span>"
        f"<span><b>{_number(item.avg_rvol)}</b>средний RVOL</span>"
        f"<span><b>{_number(item.avg_tick_rate, 3)}</b>тиков/сек</span>"
        f"<span><b>{_number(item.avg_spread_points)}</b>спред п.</span>"
        f"<span><b>{item.buy_count}</b>BUY</span>"
        f"<span><b>{item.sell_count}</b>SELL</span>"
        "</div>"
        f'<p class="muted">Последняя свеча: {_escape(freshness)} UTC</p>'
        "</article>"
    )


def _table_row(item: StateLine) -> str:
    search = " ".join(
        (
            item.symbol,
            item.label,
            item.session,
            item.action,
            item.status,
            item.reasons,
        )
    ).lower()
    avg_class = "positive" if item.avg_net_atr > 0 else "negative" if item.avg_net_atr < 0 else ""
    return (
        f'<tr data-symbol="{_escape(item.symbol)}" data-session="{_escape(item.session)}" '
        f'data-action="{_escape(item.action)}" data-horizon="{item.horizon}" '
        f'data-status="{_escape(item.status)}" data-search="{_escape(search)}">'
        f"<td>{_escape(item.symbol)}</td>"
        f"<td>{_escape(item.label)}</td>"
        f"<td>{_escape(item.session)}</td>"
        f"<td>{_escape(item.action)}</td>"
        f"<td>H{item.horizon}</td>"
        f"<td>{item.trades}</td>"
        f"<td>{item.trading_days}</td>"
        f"<td>{_status_badge(item.status)}</td>"
        f"<td>{_number(item.win_rate, 1)}%</td>"
        f"<td>{_number(item.profit_factor_atr)}</td>"
        f'<td class="{avg_class}">{_number(item.avg_net_atr, 3)}</td>'
        f"<td>{_number(item.early_avg_net_atr, 3)}</td>"
        f"<td>{_number(item.late_avg_net_atr, 3)}</td>"
        f"<td>{_number(item.max_drawdown_atr)}</td>"
        f"<td>{item.max_loss_streak}</td>"
        f"<td>{_number(item.q_value, 4)}</td>"
        f'<td class="reasons">{_escape(item.reasons)}</td>'
        "</tr>"
    )


def render_dashboard(snapshot: DashboardSnapshot) -> str:
    top = _top_states(snapshot.states)
    risks = _risk_states(snapshot.states)
    validated = snapshot.status_counts.get("VALIDATED", 0)
    candidates = snapshot.status_counts.get("RESEARCH_CANDIDATE", 0)
    unstable = snapshot.status_counts.get("UNSTABLE", 0)
    insufficient = snapshot.status_counts.get("INSUFFICIENT_SAMPLE", 0)
    completed_h12 = sum(item.completed_h12 for item in snapshot.pairs)
    last_observation = max(
        (row.get("signal_time", "") for row in snapshot.observations),
        default="",
    )
    updated = snapshot.generated_at.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")

    if validated:
        verdict = (
            f"Есть {validated} статистически подтверждённых строк. "
            "Это ещё не разрешение продавать сигналы: нужен замороженный OOS-период."
        )
        verdict_class = "good"
    elif candidates:
        verdict = (
            f"Есть {candidates} исследовательских кандидатов, но подтверждённых строк нет. "
            "Продавать как доказанную систему пока рано."
        )
        verdict_class = "warn"
    else:
        verdict = (
            "Подтверждённых закономерностей и кандидатов пока нет. "
            "Система честно накапливает выборку, а не рисует зелёную сказку."
        )
        verdict_class = "warn"

    provisional = not any(
        item.status in {"VALIDATED", "RESEARCH_CANDIDATE"} for item in top
    )
    top_html = (
        "".join(_state_card(item, provisional=provisional) for item in top)
        if top
        else '<article class="card"><p>Положительных стабильных строк пока нет.</p></article>'
    )
    risk_html = (
        "".join(_risk_card(item) for item in risks)
        if risks
        else (
            '<article class="card"><p>'
            "Явных провалов с достаточной выборкой пока нет."
            "</p></article>"
        )
    )
    pair_html = "".join(_pair_card(item) for item in snapshot.pairs)
    rows = sorted(
        snapshot.states,
        key=lambda item: (
            _STATUS_ORDER.get(item.status, 9),
            -item.evidence_score,
            item.symbol,
            item.label,
            item.session,
            item.action,
            item.horizon,
        ),
    )
    table_html = "".join(_table_row(item) for item in rows)

    symbols = sorted({item.symbol for item in snapshot.states})
    sessions = sorted({item.session for item in snapshot.states})
    labels = sorted({item.label for item in snapshot.states})
    symbol_options = "".join(
        f'<option value="{_escape(value)}">{_escape(value)}</option>' for value in symbols
    )
    session_options = "".join(
        f'<option value="{_escape(value)}">{_escape(value)}</option>' for value in sessions
    )
    label_options = "".join(
        f'<option value="{_escape(value)}">{_escape(value)}</option>' for value in labels
    )

    return f"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>TradeMind AI v1.4.3 FX Research Dashboard</title>
<style>
:root {{
  color-scheme: dark;
  --bg:#031712; --panel:#08251f; --panel2:#061e19; --line:#1a4b3f;
  --text:#e7f7f1; --muted:#8eb5a9; --green:#1ee6a1; --amber:#ffc857;
  --red:#ff6b6b; --blue:#62b6ff;
}}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:radial-gradient(circle at top,#0b2b24,var(--bg) 42%);
  color:var(--text); font:15px/1.45 Inter,Segoe UI,Arial,sans-serif; }}
main {{ max-width:1680px; margin:auto; padding:28px; }}
h1 {{ margin:0 0 8px; font-size:clamp(28px,4vw,50px); letter-spacing:-.04em; }}
h2 {{ margin:38px 0 14px; font-size:25px; }}
h3 {{ margin:5px 0 8px; font-size:18px; }}
p {{ margin:10px 0; }}
.hero {{ display:flex; justify-content:space-between; gap:24px; align-items:flex-start; }}
.kicker,.eyebrow {{ color:var(--green); text-transform:uppercase; letter-spacing:.13em;
  font-size:12px; font-weight:800; }}
.muted,.subline {{ color:var(--muted); }}
.verdict {{ margin:22px 0; padding:16px 18px; border:1px solid var(--line);
  border-left:5px solid var(--amber); background:rgba(8,37,31,.92); border-radius:14px; }}
.verdict.good {{ border-left-color:var(--green); }}
.grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(250px,1fr)); gap:14px; }}
.stats {{ grid-template-columns:repeat(auto-fit,minmax(170px,1fr)); }}
.card {{ background:linear-gradient(180deg,rgba(9,42,34,.96),rgba(5,29,24,.96));
  border:1px solid var(--line); border-radius:17px; padding:18px; min-width:0; }}
.card.evidence {{ border-color:#8b6b18; }}
.card.risk {{ border-color:#6b2f35; }}
.big {{ font-size:38px; line-height:1; font-weight:800; margin:11px 0 4px; }}
.mini-grid {{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:8px;
  margin-top:14px; }}
.mini-grid span {{ background:rgba(1,15,12,.62); padding:10px; border-radius:10px;
  color:var(--muted); }}
.mini-grid b {{ display:block; color:var(--text); font-size:18px; }}
.badge {{ display:inline-block; padding:5px 8px; border-radius:999px; font-size:11px;
  font-weight:800; letter-spacing:.04em; white-space:nowrap; }}
.validated {{ background:#064c36; color:#61ffc2; }}
.research_candidate {{ background:#594200; color:#ffe08a; }}
.unstable {{ background:#5b2228; color:#ffb0b7; }}
.insufficient_sample {{ background:#263b36; color:#b8d0c8; }}
.card-status {{ margin-top:14px; }}
.controls {{ display:grid; grid-template-columns:repeat(7,minmax(130px,1fr)); gap:10px;
  position:sticky; top:0; z-index:5; padding:12px; background:rgba(3,23,18,.96);
  border:1px solid var(--line); border-radius:14px; backdrop-filter:blur(10px); }}
select,input {{ width:100%; background:#061d18; color:var(--text); border:1px solid var(--line);
  border-radius:9px; padding:10px; }}
.table-wrap {{ overflow:auto; max-height:70vh; border:1px solid var(--line); border-radius:14px; }}
table {{ width:100%; border-collapse:collapse; min-width:1650px; background:var(--panel2); }}
th,td {{ padding:10px 11px; border-bottom:1px solid #133a31; text-align:right; }}
th {{ position:sticky; top:0; background:#0b3129; z-index:2; color:#b8d8ce; }}
th:nth-child(-n+5),td:nth-child(-n+5),td.reasons {{ text-align:left; }}
tr:hover {{ background:#0b2e27; }}
.positive {{ color:#63f0bb; font-weight:700; }}
.negative {{ color:#ff8e98; font-weight:700; }}
.reasons {{ max-width:420px; white-space:normal; color:var(--muted); }}
.footer {{ margin-top:28px; color:var(--muted); }}
@media(max-width:900px) {{
  main {{ padding:16px; }} .hero {{ display:block; }}
  .controls {{ grid-template-columns:1fr 1fr; }}
}}
</style>
</head>
<body>
<main>
<section class="hero">
<div>
<div class="kicker">TradeMind AI · read-only research</div>
<h1>FX Research Dashboard v1.4.3</h1>
<p class="muted">SMC + тиковая микроструктура + сессии + H3/H6/H12 после спреда</p>
</div>
<div class="muted">Обновлено: {_escape(updated)}<br>Последнее наблюдение:
{_escape(last_observation or "нет")}</div>
</section>

<section class="grid stats">
<article class="card"><div class="muted">Наблюдений</div>
<div class="big">{len(snapshot.observations)}</div></article>
<article class="card"><div class="muted">Готово H12</div>
<div class="big">{completed_h12}</div></article>
<article class="card"><div class="muted">Строк валидации</div>
<div class="big">{len(snapshot.states)}</div></article>
<article class="card"><div class="muted">Подтверждено</div>
<div class="big">{validated}</div></article>
<article class="card"><div class="muted">Кандидаты</div>
<div class="big">{candidates}</div></article>
<article class="card"><div class="muted">Нестабильно</div>
<div class="big">{unstable}</div></article>
<article class="card"><div class="muted">Мало данных</div>
<div class="big">{insufficient}</div></article>
</section>

<div class="verdict {verdict_class}"><b>Коммерческий вердикт:</b> {_escape(verdict)}</div>

<h2>Лучшее текущее доказательство</h2>
<div class="grid">{top_html}</div>

<h2>Красные флаги</h2>
<div class="grid">{risk_html}</div>

<h2>Покрытие валютных пар</h2>
<div class="grid">{pair_html}</div>

<h2>Все комбинации</h2>
<div class="controls">
<select id="symbol"><option value="*">Все пары</option>{symbol_options}</select>
<select id="session"><option value="*">Все сессии</option>{session_options}</select>
<select id="action"><option value="*">BUY и SELL</option>
<option>BUY</option><option>SELL</option></select>
<select id="horizon"><option value="*">Все горизонты</option>
<option value="3">H3</option><option value="6">H6</option><option value="12">H12</option></select>
<select id="status"><option value="*">Все статусы</option>
<option value="VALIDATED">Подтверждено</option>
<option value="RESEARCH_CANDIDATE">Кандидат</option>
<option value="UNSTABLE">Нестабильно</option>
<option value="INSUFFICIENT_SAMPLE">Мало данных</option></select>
<select id="label"><option value="*">Все паттерны</option>{label_options}</select>
<input id="search" placeholder="Поиск по причинам и паттернам">
</div>
<p class="muted">Показано строк: <b id="visible">{len(rows)}</b> из {len(rows)}</p>
<div class="table-wrap">
<table>
<thead><tr><th>Пара</th><th>Паттерн</th><th>Сессия</th><th>Действие</th><th>Горизонт</th>
<th>Сделки</th><th>Дни</th><th>Статус</th><th>WR</th><th>PF ATR</th><th>avg ATR</th>
<th>Ранняя</th><th>Поздняя</th><th>DD ATR</th><th>Серия</th><th>q</th><th>Причины</th></tr></thead>
<tbody id="rows">{table_html}</tbody>
</table>
</div>
<p class="footer">Панель предназначена для исследования. Она не является обещанием доходности,
не публикует клиентские сигналы и не может отправлять торговые ордера.</p>
</main>
<script>
const ids=["symbol","session","action","horizon","status","label","search"];
const controls=Object.fromEntries(ids.map(id=>[id,document.getElementById(id)]));
const rows=[...document.querySelectorAll("#rows tr")];
function apply(){{
  const q=controls.search.value.trim().toLowerCase();
  let visible=0;
  for(const row of rows){{
    const ok=
      (controls.symbol.value==="*"||row.dataset.symbol===controls.symbol.value)&&
      (controls.session.value==="*"||row.dataset.session===controls.session.value)&&
      (controls.action.value==="*"||row.dataset.action===controls.action.value)&&
      (controls.horizon.value==="*"||row.dataset.horizon===controls.horizon.value)&&
      (controls.status.value==="*"||row.dataset.status===controls.status.value)&&
      (controls.label.value==="*"||row.children[1].textContent===controls.label.value)&&
      (!q||row.dataset.search.includes(q));
    row.hidden=!ok; if(ok) visible++;
  }}
  document.getElementById("visible").textContent=visible;
}}
ids.forEach(id=>controls[id].addEventListener(id==="search"?"input":"change",apply));
</script>
</body>
</html>
"""


def write_dashboard(snapshot: DashboardSnapshot, output: Path) -> Path:
    output = output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.tmp")
    temporary.write_text(render_dashboard(snapshot), encoding="utf-8")
    os.replace(temporary, output)
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate TradeMind v1.4.3 FX dashboard")
    parser.add_argument(
        "--observations",
        type=Path,
        default=Path("data/fx_research_v1_4_2/observations.csv"),
    )
    parser.add_argument(
        "--states",
        type=Path,
        default=Path("data/fx_research_v1_4_2/latest.csv"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("data/fx_research_v1_4_2/dashboard/index.html"),
    )
    parser.add_argument("--open", action="store_true")
    args = parser.parse_args()

    observations = args.observations.expanduser().resolve()
    states = args.states.expanduser().resolve()
    if not observations.is_file():
        print(f"FX observations not found: {observations}")
        return 1
    if not states.is_file():
        print(f"FX states not found: {states}")
        return 1

    try:
        snapshot = collect_snapshot(observations, states)
        output = write_dashboard(snapshot, args.output)
    except (OSError, ValueError) as exc:
        print(f"FX dashboard failed: {exc}")
        return 1

    print("TradeMind v1.4.3 FX research dashboard")
    print(f"Observations: {len(snapshot.observations)}")
    print(f"Validation states: {len(snapshot.states)}")
    print(f"Validated: {snapshot.status_counts.get('VALIDATED', 0)}")
    print(f"Research candidates: {snapshot.status_counts.get('RESEARCH_CANDIDATE', 0)}")
    print(f"Dashboard: {output}")
    print("No orders were sent.")
    if args.open:
        webbrowser.open(output.as_uri())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
