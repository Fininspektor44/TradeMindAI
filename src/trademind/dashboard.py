"""Generate a standalone HTML dashboard for TradeMind ECN research."""

from __future__ import annotations

import argparse
import csv
import html
import math
import os
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from trademind.health import DataHealth, JournalHealth, inspect_journal, inspect_market_file
from trademind.market.csv_provider import CsvMarketDataProvider
from trademind.smc_stats import (
    _CONTEXT_ORDER,
    _EVENT_ORDER,
    _context_groups,
    _event_groups,
    _normalized_metrics,
    _prepared,
    _sample_status,
)

_HORIZON_PATTERN = re.compile(r"^outcome_(\d+)$")
_STATUS_RANK = {"OK": 0, "WARN": 1, "ERROR": 2}
_DEFAULT_SYMBOLS = "XAUUSD,XAGUSD,.USTECHCash,.US500Cash,.US30Cash,WTI,BRENT"


@dataclass(frozen=True)
class MetricLine:
    scope: str
    label: str
    horizon: int
    observations: int
    trades: int
    status: str
    win_rate: float
    profit_factor_atr: float
    avg_net_atr: float

    @property
    def score(self) -> float:
        trade_weight = math.log1p(max(self.trades, 0))
        pf_weight = 1.0
        if math.isfinite(self.profit_factor_atr):
            pf_weight = min(max(self.profit_factor_atr, 0.0), 5.0)
        return self.avg_net_atr * trade_weight * pf_weight


@dataclass(frozen=True)
class SymbolLine:
    symbol: str
    health: DataHealth
    observations: int
    evaluated: dict[int, int]


@dataclass(frozen=True)
class DashboardSnapshot:
    generated_at: datetime
    overall_status: str
    journal: JournalHealth
    symbols: tuple[SymbolLine, ...]
    metrics: tuple[MetricLine, ...]
    minimum_sample: int
    schema_version: str
    timeframe: str


def _load_rows(path: Path, schema_version: str) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    with path.open("r", encoding="utf-8", newline="") as handle:
        return [
            dict(row)
            for row in csv.DictReader(handle)
            if row.get("schema_version", "").strip() == schema_version
        ]


def _horizons(rows: list[dict[str, str]]) -> list[int]:
    if not rows:
        return [3, 6, 12]
    detected = {
        int(match.group(1))
        for key in rows[0]
        if (match := _HORIZON_PATTERN.fullmatch(key))
    }
    return sorted(detected) or [3, 6, 12]


def _evaluated_count(rows: list[dict[str, str]], horizon: int) -> int:
    prepared = _prepared(rows, horizon, True)
    return sum(
        row.get(f"outcome_{horizon}") in {"WIN", "LOSS", "FLAT"}
        for row in prepared
    )


def _metric_line(
    scope: str,
    label: str,
    rows: list[dict[str, str]],
    horizon: int,
    minimum_sample: int,
) -> MetricLine:
    metrics = _normalized_metrics(_prepared(rows, horizon, True), horizon)
    trades = int(metrics["total"])
    return MetricLine(
        scope=scope,
        label=label,
        horizon=horizon,
        observations=len(rows),
        trades=trades,
        status=_sample_status(trades, minimum_sample),
        win_rate=metrics["win_rate"],
        profit_factor_atr=metrics["profit_factor_atr"],
        avg_net_atr=metrics["avg_net_atr"],
    )


def _scope_metrics(
    scope: str,
    rows: list[dict[str, str]],
    horizons: list[int],
    minimum_sample: int,
    volume_threshold: float,
    spread_atr_threshold: float,
) -> list[MetricLine]:
    event_groups = _event_groups(rows)
    context_groups = _context_groups(
        rows,
        volume_threshold=volume_threshold,
        spread_atr_threshold=spread_atr_threshold,
    )
    result: list[MetricLine] = []
    for horizon in horizons:
        for label in _EVENT_ORDER:
            group = event_groups.get(label, [])
            if group:
                result.append(_metric_line(scope, label, group, horizon, minimum_sample))
        for label in _CONTEXT_ORDER:
            group = context_groups.get(label, [])
            if group:
                result.append(_metric_line(scope, label, group, horizon, minimum_sample))
    return result


def _confirmed_patterns(metrics: tuple[MetricLine, ...]) -> list[MetricLine]:
    confirmed = [
        item
        for item in metrics
        if item.status == "RESEARCH_SAMPLE"
        and item.trades > 0
        and item.profit_factor_atr > 1.0
        and item.avg_net_atr > 0
    ]
    return sorted(confirmed, key=lambda item: item.score, reverse=True)


def _research_candidates(metrics: tuple[MetricLine, ...], limit: int = 8) -> list[MetricLine]:
    candidates = [
        item
        for item in metrics
        if item.status == "INSUFFICIENT_SAMPLE"
        and item.trades >= 10
        and item.profit_factor_atr > 1.0
        and item.avg_net_atr > 0
    ]
    return sorted(candidates, key=lambda item: item.score, reverse=True)[:limit]


def collect_snapshot(
    data_dir: Path,
    journal_path: Path,
    symbols: list[str],
    timeframe: str,
    *,
    schema_version: str,
    minimum_sample: int,
    max_age_minutes: int,
    volume_threshold: float,
    spread_atr_threshold: float,
    now: datetime | None = None,
) -> DashboardSnapshot:
    generated_at = now or datetime.now(timezone.utc)
    rows = _load_rows(journal_path, schema_version)
    horizons = _horizons(rows)
    by_symbol: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        by_symbol[row.get("symbol", "UNKNOWN").strip().upper()].append(row)

    health_checks = [
        inspect_market_file(
            data_dir / CsvMarketDataProvider.filename(symbol, timeframe),
            symbol,
            timeframe,
            now=generated_at,
            max_age_minutes=max_age_minutes,
        )
        for symbol in symbols
    ]
    journal = inspect_journal(
        journal_path,
        symbols,
        schema_version=schema_version,
        now=generated_at,
        max_age_minutes=max_age_minutes,
    )
    severity = max(
        [_STATUS_RANK.get(item.status, 2) for item in health_checks]
        + [_STATUS_RANK.get(journal.status, 2)],
        default=0,
    )
    overall_status = ("OK", "WARN", "ERROR")[severity]

    symbol_lines = tuple(
        SymbolLine(
            symbol=symbol.upper(),
            health=health,
            observations=len(by_symbol.get(symbol.upper(), [])),
            evaluated={
                horizon: _evaluated_count(by_symbol.get(symbol.upper(), []), horizon)
                for horizon in horizons
            },
        )
        for symbol, health in zip(symbols, health_checks)
    )

    metrics = _scope_metrics(
        "ALL",
        rows,
        horizons,
        minimum_sample,
        volume_threshold,
        spread_atr_threshold,
    )
    for symbol in symbols:
        symbol_name = symbol.upper()
        metrics.extend(
            _scope_metrics(
                symbol_name,
                by_symbol.get(symbol_name, []),
                horizons,
                minimum_sample,
                volume_threshold,
                spread_atr_threshold,
            )
        )

    return DashboardSnapshot(
        generated_at=generated_at,
        overall_status=overall_status,
        journal=journal,
        symbols=symbol_lines,
        metrics=tuple(metrics),
        minimum_sample=minimum_sample,
        schema_version=schema_version,
        timeframe=timeframe.upper(),
    )


def _escaped(value: object) -> str:
    return html.escape(str(value), quote=True)


def _number(value: float, digits: int = 2) -> str:
    if value == float("inf"):
        return "∞"
    if value == float("-inf"):
        return "−∞"
    return f"{value:.{digits}f}"


def _status_badge(status: str) -> str:
    return f'<span class="badge {status.lower()}">{_escaped(status)}</span>'


def _pattern_row(item: MetricLine) -> str:
    sample_class = "confirmed" if item.status == "RESEARCH_SAMPLE" else "insufficient"
    value_class = "positive" if item.avg_net_atr > 0 else "negative"
    return (
        f'<tr data-scope="{_escaped(item.scope)}" '
        f'data-horizon="{item.horizon}" data-status="{sample_class}">'
        f"<td>{_escaped(item.scope)}</td>"
        f"<td>{_escaped(item.label)}</td>"
        f"<td>{item.horizon}</td>"
        f"<td>{item.observations}</td>"
        f"<td>{item.trades}</td>"
        f"<td>{_status_badge(item.status)}</td>"
        f"<td>{_number(item.win_rate, 1)}%</td>"
        f"<td>{_number(item.profit_factor_atr)}</td>"
        f'<td class="{value_class}">{_number(item.avg_net_atr, 3)}</td>'
        "</tr>"
    )


def _summary_pattern(item: MetricLine, warning: bool = False) -> str:
    marker = "Исследовательский кандидат" if warning else "Подтверждённая закономерность"
    css_class = "candidate-card" if warning else "confirmed-card"
    note = (
        "Выборка ещё недостаточна. Не использовать для торговли или изменения весов."
        if warning
        else "Минимальный порог пройден. Следующий фильтр — устойчивость по времени и режимам."
    )
    return (
        f'<article class="{css_class}">'
        f'<div class="eyebrow">{marker}</div>'
        f"<h3>{_escaped(item.scope)} · {_escaped(item.label)} · H{item.horizon}</h3>"
        '<div class="pattern-grid">'
        f"<span><b>{item.trades}</b> сделок</span>"
        f"<span><b>{_number(item.win_rate, 1)}%</b> WR</span>"
        f"<span><b>{_number(item.profit_factor_atr)}</b> PF_ATR</span>"
        f"<span><b>{_number(item.avg_net_atr, 3)}</b> avg ATR</span>"
        "</div>"
        f"<p>{note}</p>"
        "</article>"
    )


def render_dashboard(snapshot: DashboardSnapshot) -> str:
    confirmed = _confirmed_patterns(snapshot.metrics)
    candidates = _research_candidates(snapshot.metrics)
    latest = snapshot.journal.latest_time.isoformat() if snapshot.journal.latest_time else "нет"
    total_observations = sum(item.observations for item in snapshot.symbols)
    horizons = sorted({item.horizon for item in snapshot.metrics}) or [3, 6, 12]

    symbol_cards: list[str] = []
    for item in snapshot.symbols:
        evaluated = " · ".join(
            f"H{horizon}: {count}" for horizon, count in sorted(item.evaluated.items())
        )
        age = _number(item.health.age_minutes or 0.0, 1)
        symbol_cards.append(
            '<article class="symbol-card">'
            '<div class="card-head">'
            f"<h3>{_escaped(item.symbol)}</h3>{_status_badge(item.health.status)}</div>"
            f'<div class="big-number">{item.observations}</div>'
            f'<div class="muted">наблюдений схемы {snapshot.schema_version}</div>'
            f"<p>{_escaped(evaluated)}</p>"
            f"<p>CSV: {item.health.rows} свечей · возраст {age} мин · "
            f"спред {item.health.spread} · объём {item.health.tick_volume}</p>"
            "</article>"
        )

    if confirmed:
        confirmed_html = "".join(_summary_pattern(item) for item in confirmed[:8])
    else:
        confirmed_html = (
            '<div class="empty-state"><h3>Подтверждённых закономерностей пока нет</h3>'
            f"<p>Требуется минимум {snapshot.minimum_sample} непересекающихся оценённых "
            "сделок в одной группе. Маленькая выборка не называется преимуществом.</p></div>"
        )

    if candidates:
        candidate_html = "".join(_summary_pattern(item, warning=True) for item in candidates)
    else:
        candidate_html = (
            '<div class="empty-state"><p>Даже исследовательских кандидатов пока недостаточно.'</n            "p></div>"
        )

    rows_html = "".join(_pattern_row(item) for item in snapshot.metrics)
    scope_options = '<option value="*">Все строки</option><option value="ALL">Портфель</option>'
    scope_options += "".join(
        f'<option value="{_escaped(item.symbol)}">{_escaped(item.symbol)}</option>'
        for item in snapshot.symbols
    )
    horizon_options = '<option value="*">Все</option>' + "".join(
        f'<option value="{horizon}">H{horizon}</option>' for horizon in horizons
    )

    return f"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>TradeMind Research Dashboard</title>
<style>
:root {{ color-scheme:dark; --bg:#08110f; --panel:#101c19; --line:#263a34;
--text:#ecf7f2; --muted:#9cb4ab; --green:#49d49d; --amber:#f6bd60; --red:#ef6f6c; }}
* {{ box-sizing:border-box; }}
body {{ margin:0; font-family:Segoe UI,Arial,sans-serif; background:var(--bg); color:var(--text); }}
main {{ width:min(1500px,96vw); margin:0 auto; padding:28px 0 64px; }}
header {{ display:flex; justify-content:space-between; gap:20px; align-items:flex-start; margin-bottom:22px; }}
h1 {{ margin:0; font-size:clamp(28px,4vw,52px); letter-spacing:-1.5px; }}
h2 {{ margin:36px 0 14px; font-size:24px; }}
h3 {{ margin:0 0 10px; }}
p {{ color:var(--muted); line-height:1.5; }}
.eyebrow {{ color:var(--green); font-size:12px; font-weight:700; letter-spacing:1.4px;
text-transform:uppercase; margin-bottom:8px; }}
.badge {{ display:inline-flex; padding:5px 9px; border-radius:999px; font-size:12px;
font-weight:800; white-space:nowrap; }}
.badge.ok,.badge.research_sample {{ background:#153c2d; color:#71e6b4; }}
.badge.warn,.badge.insufficient_sample {{ background:#463719; color:#ffd37d; }}
.badge.error {{ background:#491f21; color:#ff9b9b; }}
.hero-status {{ text-align:right; }}
.hero-status .badge {{ font-size:15px; padding:9px 14px; }}
.grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(230px,1fr)); gap:12px; }}
.kpi,.symbol-card,.confirmed-card,.candidate-card,.empty-state {{ background:var(--panel);
border:1px solid var(--line); border-radius:16px; padding:18px; }}
.kpis {{ grid-template-columns:repeat(auto-fit,minmax(190px,1fr)); }}
.big-number {{ font-size:32px; font-weight:800; margin-top:8px; }}
.muted {{ color:var(--muted); font-size:13px; }}
.card-head {{ display:flex; justify-content:space-between; gap:10px; align-items:center; }}
.pattern-grid {{ display:grid; grid-template-columns:repeat(2,1fr); gap:8px; margin:14px 0; }}
.pattern-grid span {{ background:#0b1513; border-radius:10px; padding:10px; color:var(--muted); }}
.pattern-grid b {{ color:var(--text); display:block; font-size:18px; }}
.confirmed-card {{ border-color:#236b50; }}
.candidate-card {{ border-color:#66501f; }}
.controls {{ display:flex; flex-wrap:wrap; gap:10px; margin:12px 0; }}
label {{ color:var(--muted); font-size:13px; }}
select {{ display:block; margin-top:5px; background:var(--panel); color:var(--text);
border:1px solid var(--line); border-radius:10px; padding:9px 12px; min-width:170px; }}
.table-wrap {{ overflow:auto; border:1px solid var(--line); border-radius:16px; }}
table {{ width:100%; border-collapse:collapse; min-width:980px; background:var(--panel); }}
th,td {{ padding:11px 12px; text-align:left; border-bottom:1px solid var(--line); }}
th {{ position:sticky; top:0; background:#15231f; color:#c9ddd5; font-size:12px;
text-transform:uppercase; letter-spacing:.5px; }}
td {{ font-variant-numeric:tabular-nums; }}
.positive {{ color:var(--green); }}
.negative {{ color:var(--red); }}
footer {{ margin-top:24px; color:var(--muted); font-size:13px; }}
@media(max-width:700px) {{ header {{ flex-direction:column; }} .hero-status {{ text-align:left; }} }}
</style>
</head>
<body>
<main>
<header>
<div><div class="eyebrow">TradeMind AI v1.0 · исследовательский контур</div>
<h1>Research Dashboard</h1>
<p>Наблюдения, качество данных и статистика после спреда. Без реальных ордеров.</p></div>
<div class="hero-status">{_status_badge(snapshot.overall_status)}
<p>Обновлено UTC<br><b>{_escaped(snapshot.generated_at.isoformat())}</b></p></div>
</header>
<section class="grid kpis">
<article class="kpi"><div class="muted">Всего наблюдений</div><div class="big-number">{total_observations}</div></article>
<article class="kpi"><div class="muted">Строк журнала</div><div class="big-number">{snapshot.journal.rows}</div></article>
<article class="kpi"><div class="muted">Дубликаты signal_id</div><div class="big-number">{snapshot.journal.duplicate_ids}</div></article>
<article class="kpi"><div class="muted">Последнее наблюдение</div><div class="big-number" style="font-size:18px">{_escaped(latest)}</div></article>
</section>
<h2>Инструменты</h2>
<section class="grid">{''.join(symbol_cards)}</section>
<h2>Подтверждённые закономерности</h2>
<section class="grid">{confirmed_html}</section>
<h2>Исследовательские кандидаты</h2>
<p>Не рекомендации. Только положительные ранние группы с выборкой от 10 сделок.</p>
<section class="grid">{candidate_html}</section>
<h2>Полная таблица исследований</h2>
<div class="controls">
<label>Инструмент<select id="scopeFilter">{scope_options}</select></label>
<label>Горизонт<select id="horizonFilter">{horizon_options}</select></label>
<label>Выборка<select id="statusFilter"><option value="*">Все</option>
<option value="confirmed">Только достаточная</option>
<option value="insufficient">Недостаточная</option></select></label>
</div>
<div class="table-wrap"><table><thead><tr><th>Инструмент</th><th>Признак</th><th>Горизонт</th>
<th>Наблюдения</th><th>Сделки</th><th>Статус</th><th>WR</th><th>PF_ATR</th><th>avg ATR</th>
</tr></thead><tbody id="metricsBody">{rows_html}</tbody></table></div>
<footer>Схема {snapshot.schema_version} · таймфрейм {snapshot.timeframe} · минимум выборки
{snapshot.minimum_sample} сделок · PF рассчитывается в ATR-нормализованных единицах.</footer>
</main>
<script>
const scope=document.getElementById('scopeFilter');
const horizon=document.getElementById('horizonFilter');
const status=document.getElementById('statusFilter');
function applyFilters() {{
  document.querySelectorAll('#metricsBody tr').forEach(row => {{
    const scopeOk=scope.value==='*'||row.dataset.scope===scope.value;
    const horizonOk=horizon.value==='*'||row.dataset.horizon===horizon.value;
    const statusOk=status.value==='*'||row.dataset.status===status.value;
    row.hidden=!(scopeOk&&horizonOk&&statusOk);
  }});
}}
[scope,horizon,status].forEach(item=>item.addEventListener('change',applyFilters));
</script>
</body>
</html>
"""


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate TradeMind standalone research dashboard")
    default_data_dir = Path(
        os.getenv(
            "TRADEMIND_DATA_DIR",
            Path(os.getenv("APPDATA", ""))
            / "MetaQuotes"
            / "Terminal"
            / "Common"
            / "Files"
            / "TradeMindAI_ECN",
        )
    )
    default_journal = (
        Path(os.getenv("TRADEMIND_JOURNAL_DIR", "data/journal_ecn")) / "signals.csv"
    )
    parser.add_argument("--data-dir", type=Path, default=default_data_dir)
    parser.add_argument("--journal", type=Path, default=default_journal)
    parser.add_argument("--output", type=Path, default=Path("data/dashboard/index.html"))
    parser.add_argument("--symbols", default=_DEFAULT_SYMBOLS)
    parser.add_argument("--timeframe", default="M5")
    parser.add_argument("--schema-version", default="1.1")
    parser.add_argument("--min-sample", type=int, default=300)
    parser.add_argument("--max-age-minutes", type=int, default=30)
    parser.add_argument("--volume-threshold", type=float, default=1.2)
    parser.add_argument("--spread-atr-threshold", type=float, default=0.10)
    args = parser.parse_args()

    if args.min_sample < 1:
        parser.error("--min-sample must be at least 1")
    if args.max_age_minutes < 1:
        parser.error("--max-age-minutes must be at least 1")
    if args.volume_threshold <= 0:
        parser.error("--volume-threshold must be greater than zero")
    if args.spread_atr_threshold <= 0:
        parser.error("--spread-atr-threshold must be greater than zero")

    symbols = [item.strip().upper() for item in args.symbols.split(",") if item.strip()]
    if not symbols:
        parser.error("--symbols must contain at least one symbol")

    snapshot = collect_snapshot(
        args.data_dir.expanduser().resolve(),
        args.journal.expanduser().resolve(),
        symbols,
        args.timeframe,
        schema_version=args.schema_version,
        minimum_sample=args.min_sample,
        max_age_minutes=args.max_age_minutes,
        volume_threshold=args.volume_threshold,
        spread_atr_threshold=args.spread_atr_threshold,
    )
    output = args.output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render_dashboard(snapshot), encoding="utf-8")
    print(f"TradeMind dashboard generated: {output}")
    print(f"Overall status: {snapshot.overall_status}")
    print(f"Observations: {sum(item.observations for item in snapshot.symbols)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
