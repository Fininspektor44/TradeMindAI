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
)
from trademind.validation import ValidationResult, portfolio_only, validate_rows

_HORIZON_PATTERN = re.compile(r"^outcome_(\d+)$")
_STATUS_RANK = {"OK": 0, "WARN": 1, "ERROR": 2}
_DEFAULT_SYMBOLS = "XAUUSD,XAGUSD,.USTECHCash,.US500Cash,.US30Cash,WTI,BRENT"


@dataclass(frozen=True)
class MetricLine:
    scope: str
    label: str
    horizon: int
    observations: int
    validation: ValidationResult

    @property
    def trades(self) -> int:
        return self.validation.total.trades

    @property
    def win_rate(self) -> float:
        return self.validation.total.win_rate

    @property
    def profit_factor_atr(self) -> float:
        return self.validation.total.profit_factor_atr

    @property
    def avg_net_atr(self) -> float:
        return self.validation.total.avg_net_atr

    @property
    def status(self) -> str:
        return self.validation.status

    @property
    def score(self) -> float:
        profit_factor = self.profit_factor_atr
        bounded_pf = 5.0 if not math.isfinite(profit_factor) else max(profit_factor, 0.0)
        return self.avg_net_atr * math.log1p(max(self.trades, 0)) * min(bounded_pf, 5.0)


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
    candidate_minimum: int
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
    values = {
        int(match.group(1))
        for key in rows[0]
        if (match := _HORIZON_PATTERN.fullmatch(key))
    }
    return sorted(values) or [3, 6, 12]


def _evaluated_count(rows: list[dict[str, str]], horizon: int) -> int:
    result = validate_rows(
        rows,
        horizon,
        candidate_minimum=2,
        research_minimum=2,
    )
    return result.total.trades


def _metric_line(
    scope: str,
    label: str,
    rows: list[dict[str, str]],
    horizon: int,
    candidate_minimum: int,
    minimum_sample: int,
) -> MetricLine:
    validation = validate_rows(
        rows,
        horizon,
        candidate_minimum=candidate_minimum,
        research_minimum=minimum_sample,
    )
    if scope == "ALL":
        validation = portfolio_only(validation)
    return MetricLine(
        scope=scope,
        label=label,
        horizon=horizon,
        observations=len(rows),
        validation=validation,
    )


def _scope_metrics(
    scope: str,
    rows: list[dict[str, str]],
    horizons: list[int],
    candidate_minimum: int,
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
    output: list[MetricLine] = []
    for horizon in horizons:
        for label in _EVENT_ORDER:
            group = event_groups.get(label, [])
            if group:
                output.append(
                    _metric_line(
                        scope,
                        label,
                        group,
                        horizon,
                        candidate_minimum,
                        minimum_sample,
                    )
                )
        for label in _CONTEXT_ORDER:
            group = context_groups.get(label, [])
            if group:
                output.append(
                    _metric_line(
                        scope,
                        label,
                        group,
                        horizon,
                        candidate_minimum,
                        minimum_sample,
                    )
                )
    return output


def _validated_patterns(metrics: tuple[MetricLine, ...]) -> list[MetricLine]:
    result = [item for item in metrics if item.status == "VALIDATED"]
    return sorted(result, key=lambda item: item.score, reverse=True)


def _research_candidates(metrics: tuple[MetricLine, ...], limit: int = 8) -> list[MetricLine]:
    result = [item for item in metrics if item.status == "RESEARCH_CANDIDATE"]
    return sorted(result, key=lambda item: item.score, reverse=True)[:limit]


def collect_snapshot(
    data_dir: Path,
    journal_path: Path,
    symbols: list[str],
    timeframe: str,
    *,
    schema_version: str,
    candidate_minimum: int,
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
        candidate_minimum,
        minimum_sample,
        volume_threshold,
        spread_atr_threshold,
    )
    for symbol in symbols:
        name = symbol.upper()
        metrics.extend(
            _scope_metrics(
                name,
                by_symbol.get(name, []),
                horizons,
                candidate_minimum,
                minimum_sample,
                volume_threshold,
                spread_atr_threshold,
            )
        )

    return DashboardSnapshot(
        generated_at=generated_at,
        overall_status=("OK", "WARN", "ERROR")[severity],
        journal=journal,
        symbols=symbol_lines,
        metrics=tuple(metrics),
        candidate_minimum=candidate_minimum,
        minimum_sample=minimum_sample,
        schema_version=schema_version,
        timeframe=timeframe.upper(),
    )


def _escape(value: object) -> str:
    return html.escape(str(value), quote=True)


def _number(value: float, digits: int = 2) -> str:
    if value == float("inf"):
        return "∞"
    if value == float("-inf"):
        return "−∞"
    return f"{value:.{digits}f}"


def _badge(status: str) -> str:
    labels = {
        "VALIDATED": "ПОДТВЕРЖДЕНО",
        "RESEARCH_CANDIDATE": "КАНДИДАТ",
        "UNSTABLE": "НЕСТАБИЛЬНО",
        "INSUFFICIENT_SAMPLE": "МАЛО ДАННЫХ",
        "PORTFOLIO_ONLY": "ТОЛЬКО ОБЗОР",
    }
    return (
        f'<span class="badge {status.lower()}">'
        f"{_escape(labels.get(status, status))}</span>"
    )


def _pattern_card(item: MetricLine, candidate: bool) -> str:
    title = "Исследовательский кандидат" if candidate else "Подтверждённая закономерность"
    css = "candidate" if candidate else "validated"
    validation = item.validation
    note = (
        "Обе половины истории положительны, но доказательной выборки ещё нет."
        if candidate
        else "Выборка и 95% интервал пройдены. Всё ещё нужна проверка рыночных режимов."
    )
    return (
        f'<article class="card {css}"><div class="eyebrow">{title}</div>'
        f"<h3>{_escape(item.scope)} · {_escape(item.label)} · H{item.horizon}</h3>"
        '<div class="mini-grid">'
        f"<span><b>{item.trades}</b>сделок</span>"
        f"<span><b>{_number(item.win_rate, 1)}%</b>WR</span>"
        f"<span><b>{_number(item.profit_factor_atr)}</b>PF_ATR</span>"
        f"<span><b>{_number(item.avg_net_atr, 3)}</b>avg ATR</span>"
        f"<span><b>{_number(validation.early.avg_net_atr, 3)}</b>ранняя половина</span>"
        f"<span><b>{_number(validation.late.avg_net_atr, 3)}</b>поздняя половина</span>"
        f"<span><b>{_number(validation.max_drawdown_atr, 2)}</b>max DD ATR</span>"
        f"<span><b>{validation.max_loss_streak}</b>серия убытков</span>"
        f"</div><p>{note}</p></article>"
    )


def _metric_row(item: MetricLine) -> str:
    value_class = "positive" if item.avg_net_atr > 0 else "negative"
    validation = item.validation
    return (
        f'<tr data-scope="{_escape(item.scope)}" data-horizon="{item.horizon}" '
        f'data-status="{_escape(item.status)}"><td>{_escape(item.scope)}</td>'
        f"<td>{_escape(item.label)}</td><td>H{item.horizon}</td>"
        f"<td>{item.observations}</td><td>{item.trades}</td>"
        f"<td>{_badge(item.status)}</td><td>{_number(item.win_rate, 1)}%</td>"
        f"<td>{_number(item.profit_factor_atr)}</td>"
        f'<td class="{value_class}">{_number(item.avg_net_atr, 3)}</td>'
        f"<td>{_number(validation.early.avg_net_atr, 3)}</td>"
        f"<td>{_number(validation.late.avg_net_atr, 3)}</td>"
        f"<td>{_number(validation.max_drawdown_atr, 2)}</td>"
        f"<td>{validation.max_loss_streak}</td>"
        f"<td>[{_number(validation.mean_ci_low, 3)}, "
        f"{_number(validation.mean_ci_high, 3)}]</td></tr>"
    )


def render_dashboard(snapshot: DashboardSnapshot) -> str:
    validated = _validated_patterns(snapshot.metrics)
    candidates = _research_candidates(snapshot.metrics)
    latest = snapshot.journal.latest_time.isoformat() if snapshot.journal.latest_time else "нет"
    observations = sum(item.observations for item in snapshot.symbols)
    horizons = sorted({item.horizon for item in snapshot.metrics}) or [3, 6, 12]

    symbol_cards = []
    for item in snapshot.symbols:
        evaluated = " · ".join(
            f"H{horizon}: {count}" for horizon, count in sorted(item.evaluated.items())
        )
        age = _number(item.health.age_minutes or 0.0, 1)
        symbol_cards.append(
            '<article class="card"><div class="head">'
            f"<h3>{_escape(item.symbol)}</h3>{_badge(item.health.status)}</div>"
            f'<div class="big">{item.observations}</div>'
            f'<div class="muted">наблюдений схемы {snapshot.schema_version}</div>'
            f"<p>{_escape(evaluated)}</p><p>CSV {item.health.rows} · возраст {age} мин · "
            f"спред {item.health.spread} · объём {item.health.tick_volume}</p></article>"
        )

    validated_html = (
        "".join(_pattern_card(item, False) for item in validated[:8])
        if validated
        else (
            '<article class="card"><h3>Подтверждённых закономерностей пока нет</h3>'
            f"<p>Нужно минимум {snapshot.minimum_sample} непересекающихся сделок, "
            "положительные обе половины истории и нижняя граница CI95 выше нуля.</p></article>"
        )
    )
    candidate_html = (
        "".join(_pattern_card(item, True) for item in candidates)
        if candidates
        else (
            '<article class="card"><p>Стабильных исследовательских кандидатов пока нет. '
            f"Минимум: {snapshot.candidate_minimum} сделок на одном инструменте.</p></article>"
        )
    )

    scope_options = [
        '<option value="*">Все строки</option>',
        '<option value="ALL">Портфельный обзор</option>',
    ]
    scope_options.extend(
        f'<option value="{_escape(item.symbol)}">{_escape(item.symbol)}</option>'
        for item in snapshot.symbols
    )
    horizon_options = ['<option value="*">Все</option>']
    horizon_options.extend(
        f'<option value="{horizon}">H{horizon}</option>' for horizon in horizons
    )
    status_options = [
        '<option value="*">Все</option>',
        '<option value="VALIDATED">Подтверждено</option>',
        '<option value="RESEARCH_CANDIDATE">Кандидаты</option>',
        '<option value="UNSTABLE">Нестабильно</option>',
        '<option value="INSUFFICIENT_SAMPLE">Мало данных</option>',
        '<option value="PORTFOLIO_ONLY">Портфельный обзор</option>',
    ]
    metric_rows = "".join(_metric_row(item) for item in snapshot.metrics)

    css = """
:root{color-scheme:dark;--bg:#08110f;--panel:#101c19;--line:#263a34;--text:#ecf7f2;
--muted:#9cb4ab;--green:#49d49d;--amber:#f6bd60;--red:#ef6f6c;--blue:#66a7ff}
*{box-sizing:border-box}body{margin:0;font-family:Segoe UI,Arial,sans-serif;background:var(--bg);
color:var(--text)}main{width:min(1700px,96vw);margin:auto;padding:28px 0 64px}
header{display:flex;justify-content:space-between;gap:20px;align-items:flex-start}
h1{font-size:clamp(30px,4vw,52px);margin:0}h2{margin-top:36px}p{color:var(--muted);
line-height:1.5}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));
gap:12px}.card,.kpi{background:var(--panel);border:1px solid var(--line);border-radius:16px;
padding:18px}.validated{border-color:#236b50}.candidate{border-color:#66501f}
.head{display:flex;justify-content:space-between;gap:10px;align-items:center}.big{font-size:32px;
font-weight:800}.muted{color:var(--muted);font-size:13px}.eyebrow{color:var(--green);
font-size:12px;font-weight:800;text-transform:uppercase;letter-spacing:1.2px}
.badge{display:inline-flex;padding:5px 9px;border-radius:999px;font-size:12px;font-weight:800}
.badge.ok,.badge.validated{background:#153c2d;color:#71e6b4}.badge.warn,
.badge.research_candidate,.badge.insufficient_sample{background:#463719;color:#ffd37d}
.badge.error,.badge.unstable{background:#491f21;color:#ff9b9b}.badge.portfolio_only{
background:#17304d;color:#9bc6ff}.mini-grid{display:grid;grid-template-columns:repeat(2,1fr);
gap:8px;margin:12px 0}.mini-grid span{background:#0b1513;padding:10px;border-radius:10px;
color:var(--muted)}.mini-grid b{display:block;color:var(--text);font-size:18px}
.controls{display:flex;flex-wrap:wrap;gap:10px;margin:12px 0}label{color:var(--muted);
font-size:13px}select{display:block;margin-top:5px;background:var(--panel);color:var(--text);
border:1px solid var(--line);border-radius:10px;padding:9px 12px;min-width:180px}
.table-wrap{overflow:auto;border:1px solid var(--line);border-radius:16px}table{width:100%;
border-collapse:collapse;min-width:1550px;background:var(--panel)}th,td{padding:11px 12px;
text-align:left;border-bottom:1px solid var(--line);font-variant-numeric:tabular-nums}
th{background:#15231f;font-size:12px;text-transform:uppercase;position:sticky;top:0}
.positive{color:var(--green)}.negative{color:var(--red)}footer{margin-top:24px;
color:var(--muted);font-size:13px}@media(max-width:700px){header{flex-direction:column}}
"""

    return f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>TradeMind Validation Dashboard</title><style>{css}</style></head><body><main>
<header><div><div class="eyebrow">TradeMind AI v1.1 · validation layer</div>
<h1>Research Dashboard</h1>
<p>Устойчивость по времени, риск и статистика после спреда. Без реальных ордеров.</p></div>
<div>{_badge(snapshot.overall_status)}<p>Обновлено UTC<br>
<b>{_escape(snapshot.generated_at.isoformat())}</b></p></div></header>
<section class="grid"><article class="kpi"><div class="muted">Всего наблюдений</div>
<div class="big">{observations}</div></article>
<article class="kpi"><div class="muted">Строк журнала</div>
<div class="big">{snapshot.journal.rows}</div></article>
<article class="kpi"><div class="muted">Подтверждено</div>
<div class="big">{len(validated)}</div></article>
<article class="kpi"><div class="muted">Стабильные кандидаты</div>
<div class="big">{len(candidates)}</div></article>
<article class="kpi"><div class="muted">Последнее наблюдение</div>
<div>{_escape(latest)}</div></article></section>
<h2>Инструменты</h2><section class="grid">{''.join(symbol_cards)}</section>
<h2>Подтверждённые закономерности</h2><section class="grid">{validated_html}</section>
<h2>Стабильные исследовательские кандидаты</h2>
<p>Только отдельные инструменты. Обе половины истории должны оставаться положительными.</p>
<section class="grid">{candidate_html}</section><h2>Полная таблица валидации</h2>
<div class="controls"><label>Инструмент<select id="scope">{''.join(scope_options)}</select></label>
<label>Горизонт<select id="horizon">{''.join(horizon_options)}</select></label>
<label>Статус<select id="status">{''.join(status_options)}</select></label></div>
<div class="table-wrap"><table><thead><tr><th>Инструмент</th><th>Признак</th>
<th>Горизонт</th><th>Наблюдения</th><th>Сделки</th><th>Статус</th><th>WR</th>
<th>PF_ATR</th><th>avg ATR</th><th>Ранняя</th><th>Поздняя</th><th>Max DD</th>
<th>Loss streak</th><th>CI95 avg</th></tr></thead>
<tbody id="metrics">{metric_rows}</tbody></table></div>
<footer>Схема {snapshot.schema_version} · {snapshot.timeframe} · кандидат от
{snapshot.candidate_minimum} сделок · подтверждение от {snapshot.minimum_sample} ·
портфельные строки информационные.</footer></main><script>
const scope=document.getElementById('scope'),horizon=document.getElementById('horizon'),
status=document.getElementById('status');
function filterRows(){{document.querySelectorAll('#metrics tr').forEach(row=>{{
const a=scope.value==='*'||row.dataset.scope===scope.value;
const b=horizon.value==='*'||row.dataset.horizon===horizon.value;
const c=status.value==='*'||row.dataset.status===status.value;
row.hidden=!(a&&b&&c);}});}}
[scope,horizon,status].forEach(item=>item.addEventListener('change',filterRows));
</script></body></html>"""


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate TradeMind standalone validation dashboard"
    )
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
    default_journal = Path(os.getenv("TRADEMIND_JOURNAL_DIR", "data/journal_ecn"))
    default_journal = default_journal / "signals.csv"
    parser.add_argument("--data-dir", type=Path, default=default_data_dir)
    parser.add_argument("--journal", type=Path, default=default_journal)
    parser.add_argument("--output", type=Path, default=Path("data/dashboard/index.html"))
    parser.add_argument("--symbols", default=_DEFAULT_SYMBOLS)
    parser.add_argument("--timeframe", default="M5")
    parser.add_argument("--schema-version", default="1.1")
    parser.add_argument("--candidate-min", type=int, default=30)
    parser.add_argument("--min-sample", type=int, default=300)
    parser.add_argument("--max-age-minutes", type=int, default=30)
    parser.add_argument("--volume-threshold", type=float, default=1.2)
    parser.add_argument("--spread-atr-threshold", type=float, default=0.10)
    args = parser.parse_args()

    if args.candidate_min < 2:
        parser.error("--candidate-min must be at least 2")
    if args.min_sample < args.candidate_min:
        parser.error("--min-sample must be at least --candidate-min")
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
        candidate_minimum=args.candidate_min,
        minimum_sample=args.min_sample,
        max_age_minutes=args.max_age_minutes,
        volume_threshold=args.volume_threshold,
        spread_atr_threshold=args.spread_atr_threshold,
    )
    output = args.output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render_dashboard(snapshot), encoding="utf-8")
    print(f"TradeMind validation dashboard: {output}")
    print(f"Overall status: {snapshot.overall_status}")
    print(f"Validated patterns: {len(_validated_patterns(snapshot.metrics))}")
    print(f"Research candidates: {len(_research_candidates(snapshot.metrics))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
