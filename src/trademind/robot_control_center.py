"""Read-only comparison dashboard for multiple TradeMind grid robot reports."""

from __future__ import annotations

import argparse
import csv
import html
import json
import os
import webbrowser
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import quote

CONTROL_CENTER_VERSION = "1.15.2"
MIN_PRELIMINARY_DD_SAMPLE = 30
STRONG_DD_SAMPLE = 100

SUMMARY_FIELDS = (
    "schema_version",
    "robot",
    "account_login",
    "baskets",
    "completed_baskets",
    "open_baskets",
    "wins",
    "losses",
    "net_profit",
    "average_net_profit",
    "profit_factor",
    "drawdown_measured_baskets",
    "historical_drawdown_coverage",
    "collector_eligible_baskets",
    "collector_measured_baskets",
    "collector_drawdown_coverage",
    "worst_basket_drawdown_money",
    "worst_basket_drawdown_pct",
    "latest_account_drawdown_money",
    "latest_account_drawdown_pct",
    "worst_account_drawdown_money",
    "worst_account_drawdown_pct",
    "max_legs",
    "max_concurrent_baskets",
    "longest_closed_minutes",
    "longest_observed_minutes",
    "quality",
)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _float(value: Any, default: float = 0.0) -> float:
    try:
        text = _text(value).replace(",", ".")
        return float(text) if text else default
    except (TypeError, ValueError):
        return default


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(float(_text(value)))
    except (TypeError, ValueError):
        return default


def _parse_time(value: Any) -> datetime | None:
    text = _text(value)
    if not text:
        return None
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def _atomic_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(value, encoding="utf-8")
    temporary.replace(path)


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    _atomic_text(path, json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _atomic_csv(
    path: Path,
    fields: Sequence[str],
    rows: Sequence[dict[str, Any]],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(fields), extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(path)


def _minutes_text(value: Any) -> str:
    minutes = max(0.0, _float(value))
    if minutes < 60:
        return f"{minutes:.0f} мин"
    hours = minutes / 60.0
    if hours < 48:
        return f"{hours:.1f} ч"
    return f"{hours / 24.0:.1f} дн"


def _number(value: Any, digits: int = 2, suffix: str = "") -> str:
    if not _text(value):
        return "—"
    return f"{_float(value):.{digits}f}{suffix}"


def _profit_factor_text(value: Any, losses: int) -> str:
    factor = _float(value)
    if losses == 0 and factor >= 999:
        return "∞ (закрытых убытков нет)"
    return f"{factor:.2f}"


def _quality_label(measured: int, unmatched: int, snapshots_present: bool) -> str:
    if not snapshots_present:
        return "DD не собирается"
    if unmatched:
        return f"Есть {unmatched} несопоставленных снимков"
    if measured >= STRONG_DD_SAMPLE:
        return "Сильная выборка DD"
    if measured >= MIN_PRELIMINARY_DD_SAMPLE:
        return "Предварительно пригодно"
    return f"Слишком мало DD: {measured}/{MIN_PRELIMINARY_DD_SAMPLE}"


@dataclass(frozen=True, slots=True)
class ReportSpec:
    name: str
    account_login: str
    report_dir: Path


@dataclass(frozen=True, slots=True)
class RobotMetrics:
    spec: ReportSpec
    status: dict[str, Any]
    snapshot_status: dict[str, Any]
    history: list[dict[str, str]]
    risk: list[dict[str, str]]
    snapshots: list[dict[str, str]]
    summary: dict[str, Any]
    open_rows: list[dict[str, Any]]


def _parse_spec(value: str) -> ReportSpec:
    parts = value.split("|", 2)
    if len(parts) != 3 or not all(part.strip() for part in parts):
        raise ValueError(
            "Report must use NAME|ACCOUNT_LOGIN|REPORT_DIRECTORY format"
        )
    report_dir = Path(parts[2]).expanduser().resolve()
    if not (report_dir / "status.json").is_file():
        raise ValueError(f"Report status not found: {report_dir / 'status.json'}")
    return ReportSpec(
        name=parts[0].strip(),
        account_login=parts[1].strip(),
        report_dir=report_dir,
    )


def _snapshot_lookup(rows: Sequence[dict[str, str]]) -> dict[str, dict[str, str]]:
    result: dict[str, dict[str, str]] = {}
    for row in rows:
        basket_id = _text(row.get("basket_id"))
        if basket_id:
            result[basket_id] = dict(row)
    return result


def _load_metrics(spec: ReportSpec, captured_at: datetime) -> RobotMetrics:
    status = _read_json(spec.report_dir / "status.json")
    history = _read_csv(spec.report_dir / "basket_history.csv")
    risk = _read_csv(spec.report_dir / "risk_by_leg.csv")
    snapshot_dir = spec.report_dir / "snapshots"
    snapshot_status = _read_json(snapshot_dir / "status.json")
    snapshots = _read_csv(snapshot_dir / "basket_snapshot_drawdown.csv")
    by_basket = _snapshot_lookup(snapshots)

    completed_rows = [row for row in history if _int(row.get("completed")) == 1]
    open_history = [row for row in history if _int(row.get("completed")) == 0]
    net_profit = _float(status.get("net_profit"))
    completed = _int(status.get("completed_baskets"), len(completed_rows))
    measured = _int(status.get("drawdown_measured_baskets"))
    unmatched = _int(snapshot_status.get("unmatched_position_snapshot_rows"))

    latest_balance = _float(status.get("latest_balance"))
    latest_account_dd = _float(
        status.get("latest_account_floating_drawdown_money")
    )
    worst_account_dd = _float(
        status.get("worst_account_floating_drawdown_money")
    )
    latest_account_dd_pct = (
        100.0 * latest_account_dd / latest_balance if latest_balance > 0 else 0.0
    )
    worst_account_dd_pct = _float(
        status.get("worst_account_floating_drawdown_pct")
    )

    opened_times = [
        value
        for value in (_parse_time(row.get("opened_at")) for row in history)
        if value is not None
    ]
    activity_times = [
        value
        for row in history
        for value in (
            _parse_time(row.get("opened_at")),
            _parse_time(row.get("closed_at")),
        )
        if value is not None
    ]
    closed_durations = [
        _float(row.get("duration_minutes")) for row in completed_rows
    ]
    longest_observed = max(
        (_float(row.get("basket_age_minutes")) for row in snapshots),
        default=0.0,
    )

    open_rows: list[dict[str, Any]] = []
    for row in open_history:
        basket_id = _text(row.get("basket_id"))
        snapshot = by_basket.get(basket_id, {})
        opened_at = _parse_time(row.get("opened_at"))
        age_minutes = _float(snapshot.get("basket_age_minutes"))
        if age_minutes <= 0 and opened_at is not None:
            age_minutes = max(
                0.0,
                (captured_at - opened_at).total_seconds() / 60.0,
            )
        open_rows.append(
            {
                "basket_id": basket_id,
                "symbol": _text(row.get("symbol")),
                "side": _text(row.get("side")).upper(),
                "max_legs": _int(row.get("max_legs")),
                "opened_at": _text(row.get("opened_at")),
                "age_minutes": round(age_minutes, 3),
                "latest_floating_money": (
                    snapshot.get("latest_floating_money", "")
                ),
                "worst_drawdown_money": snapshot.get(
                    "max_drawdown_money", row.get("max_drawdown_money", "")
                ),
                "worst_drawdown_pct": snapshot.get(
                    "max_drawdown_pct", row.get("max_drawdown_pct", "")
                ),
                "latest_volume": snapshot.get("latest_volume", ""),
                "latest_positions": snapshot.get("latest_positions", ""),
                "has_snapshot": bool(snapshot),
            }
        )
    open_rows.sort(key=lambda row: _float(row["age_minutes"]), reverse=True)

    snapshots_present = (snapshot_dir / "status.json").is_file()
    summary = {
        "schema_version": CONTROL_CENTER_VERSION,
        "robot": spec.name,
        "account_login": spec.account_login,
        "baskets": _int(status.get("baskets"), len(history)),
        "completed_baskets": completed,
        "open_baskets": _int(status.get("open_baskets"), len(open_history)),
        "wins": _int(status.get("wins")),
        "losses": _int(status.get("losses")),
        "net_profit": round(net_profit, 6),
        "average_net_profit": round(net_profit / completed, 6) if completed else 0.0,
        "profit_factor": _float(status.get("profit_factor")),
        "drawdown_measured_baskets": measured,
        "historical_drawdown_coverage": _float(status.get("drawdown_coverage")),
        "collector_eligible_baskets": _int(
            status.get("collector_eligible_baskets")
        ),
        "collector_measured_baskets": _int(
            status.get("collector_measured_baskets")
        ),
        "collector_drawdown_coverage": _float(
            status.get("collector_drawdown_coverage")
        ),
        "worst_basket_drawdown_money": _float(
            status.get("worst_drawdown_money")
        ),
        "worst_basket_drawdown_pct": _float(status.get("worst_drawdown_pct")),
        "latest_account_drawdown_money": round(latest_account_dd, 6),
        "latest_account_drawdown_pct": round(latest_account_dd_pct, 6),
        "worst_account_drawdown_money": round(worst_account_dd, 6),
        "worst_account_drawdown_pct": round(worst_account_dd_pct, 6),
        "latest_balance": round(latest_balance, 6),
        "latest_equity": round(_float(status.get("latest_equity")), 6),
        "max_legs": _int(status.get("max_legs")),
        "max_concurrent_baskets": _int(status.get("max_concurrent_baskets")),
        "longest_closed_minutes": round(max(closed_durations, default=0.0), 3),
        "longest_observed_minutes": round(longest_observed, 3),
        "history_started_at": min(opened_times).isoformat() if opened_times else "",
        "latest_activity_at": max(activity_times).isoformat() if activity_times else "",
        "collector_started_at": _text(status.get("collector_started_at")),
        "collector_latest_at": _text(snapshot_status.get("monitoring_latest_at")),
        "position_snapshot_rows": _int(
            snapshot_status.get("position_snapshot_rows")
        ),
        "unmatched_position_snapshot_rows": unmatched,
        "quality": _quality_label(measured, unmatched, snapshots_present),
        "orders_enabled": bool(status.get("orders_enabled", False)),
        "source_modified": bool(status.get("source_modified", False)),
        "logic_changed": bool(status.get("logic_changed", False)),
        "report_dir": str(spec.report_dir),
    }
    return RobotMetrics(
        spec=spec,
        status=status,
        snapshot_status=snapshot_status,
        history=history,
        risk=risk,
        snapshots=snapshots,
        summary=summary,
        open_rows=open_rows,
    )


def _dashboard_link(report_dir: Path, dashboard_dir: Path) -> str:
    target = report_dir / "dashboard" / "index.html"
    relative = os.path.relpath(target, dashboard_dir).replace("\\", "/")
    return quote(relative, safe="/.:_")


def _robot_card(metrics: RobotMetrics, dashboard_dir: Path) -> str:
    item = metrics.summary
    losses = _int(item.get("losses"))
    pf = _profit_factor_text(item.get("profit_factor"), losses)
    quality_class = (
        "good"
        if _int(item.get("drawdown_measured_baskets")) >= MIN_PRELIMINARY_DD_SAMPLE
        else "warn"
    )
    link = _dashboard_link(metrics.spec.report_dir, dashboard_dir)
    return f"""
<article class='robot-card'>
  <div class='robot-head'>
    <div><h2>{html.escape(metrics.spec.name)}</h2>
    <small>Счёт {html.escape(metrics.spec.account_login)}</small></div>
    <a href='{link}'>Открыть полный отчёт</a>
  </div>
  <div class='mini-grid'>
    <div><span>Net</span><b>{_float(item['net_profit']):.2f}</b></div>
    <div><span>Корзины</span><b>{_int(item['completed_baskets'])} / {_int(item['baskets'])}</b></div>
    <div><span>Открыто</span><b>{_int(item['open_baskets'])}</b></div>
    <div><span>PF</span><b>{html.escape(pf)}</b></div>
    <div><span>Худшая DD корзины</span>
      <b>{_float(item['worst_basket_drawdown_money']):.2f}</b>
      <small>{_float(item['worst_basket_drawdown_pct']):.2f}%</small></div>
    <div><span>DD счёта сейчас / максимум</span>
      <b>{_float(item['latest_account_drawdown_money']):.2f} /
      {_float(item['worst_account_drawdown_money']):.2f}</b>
      <small>{_float(item['latest_account_drawdown_pct']):.2f}% /
      {_float(item['worst_account_drawdown_pct']):.2f}%</small></div>
  </div>
  <p class='{quality_class}'>{html.escape(_text(item['quality']))}</p>
</article>"""


def _comparison_table(metrics: Sequence[RobotMetrics]) -> str:
    labels = [html.escape(item.spec.name) for item in metrics]
    header = "".join(f"<th>{label}</th>" for label in labels)

    def row(label: str, values: Sequence[str]) -> str:
        cells = "".join(f"<td>{value}</td>" for value in values)
        return f"<tr><th>{html.escape(label)}</th>{cells}</tr>"

    rows = [
        row("Счёт", [html.escape(item.spec.account_login) for item in metrics]),
        row(
            "Период истории",
            [
                html.escape(_text(item.summary.get("history_started_at")) or "—")
                for item in metrics
            ],
        ),
        row(
            "Закрыто / открыто",
            [
                f"{_int(item.summary['completed_baskets'])} / "
                f"{_int(item.summary['open_baskets'])}"
                for item in metrics
            ],
        ),
        row(
            "Чистый результат",
            [f"{_float(item.summary['net_profit']):.2f}" for item in metrics],
        ),
        row(
            "Средний net на закрытую корзину",
            [f"{_float(item.summary['average_net_profit']):.2f}" for item in metrics],
        ),
        row(
            "Win / loss",
            [
                f"{_int(item.summary['wins'])} / {_int(item.summary['losses'])}"
                for item in metrics
            ],
        ),
        row(
            "PF",
            [
                html.escape(
                    _profit_factor_text(
                        item.summary["profit_factor"],
                        _int(item.summary["losses"]),
                    )
                )
                for item in metrics
            ],
        ),
        row(
            "Худшая DD корзины",
            [
                f"{_float(item.summary['worst_basket_drawdown_money']):.2f} / "
                f"{_float(item.summary['worst_basket_drawdown_pct']):.2f}%"
                for item in metrics
            ],
        ),
        row(
            "DD счёта сейчас / максимум",
            [
                f"{_float(item.summary['latest_account_drawdown_money']):.2f} / "
                f"{_float(item.summary['worst_account_drawdown_money']):.2f}"
                for item in metrics
            ],
        ),
        row(
            "Измерено корзин DD",
            [
                f"{_int(item.summary['drawdown_measured_baskets'])}"
                for item in metrics
            ],
        ),
        row(
            "Покрытие после запуска",
            [
                f"{100 * _float(item.summary['collector_drawdown_coverage']):.1f}%"
                for item in metrics
            ],
        ),
        row(
            "Историческое покрытие DD",
            [
                f"{100 * _float(item.summary['historical_drawdown_coverage']):.1f}%"
                for item in metrics
            ],
        ),
        row(
            "Макс. колен",
            [f"{_int(item.summary['max_legs'])}" for item in metrics],
        ),
        row(
            "Макс. одновременно",
            [
                f"{_int(item.summary['max_concurrent_baskets'])}"
                for item in metrics
            ],
        ),
        row(
            "Самая долгая закрытая корзина",
            [
                _minutes_text(item.summary["longest_closed_minutes"])
                for item in metrics
            ],
        ),
        row(
            "Самая долгая наблюдаемая корзина",
            [
                _minutes_text(item.summary["longest_observed_minutes"])
                for item in metrics
            ],
        ),
        row(
            "Качество данных",
            [html.escape(_text(item.summary["quality"])) for item in metrics],
        ),
    ]
    return (
        "<table class='comparison'><thead><tr><th>Метрика</th>"
        f"{header}</tr></thead><tbody>{''.join(rows)}</tbody></table>"
    )


def _open_baskets_table(metrics: Sequence[RobotMetrics]) -> str:
    rows: list[str] = []
    for item in metrics:
        for basket in item.open_rows:
            rows.append(
                "<tr>"
                f"<td>{html.escape(item.spec.name)}</td>"
                f"<td>{html.escape(_text(basket['symbol']))} "
                f"{html.escape(_text(basket['side']))}</td>"
                f"<td>{_int(basket['max_legs'])}</td>"
                f"<td>{html.escape(_text(basket['opened_at']))}</td>"
                f"<td>{_minutes_text(basket['age_minutes'])}</td>"
                f"<td>{_number(basket['latest_floating_money'])}</td>"
                f"<td>{_number(basket['worst_drawdown_money'])}</td>"
                f"<td>{_number(basket['worst_drawdown_pct'], suffix='%')}</td>"
                f"<td>{_number(basket['latest_volume'])}</td>"
                f"<td>{_number(basket['latest_positions'], digits=0)}</td>"
                "</tr>"
            )
    if not rows:
        return "<p class='good'>Открытых корзин нет.</p>"
    return """
<table><thead><tr><th>Робот</th><th>Инструмент</th><th>Колен</th>
<th>Открыта</th><th>Возраст</th><th>Сейчас P/L</th><th>Худшая DD</th>
<th>Худшая DD %</th><th>Объём</th><th>Позиций</th></tr></thead>
<tbody>""" + "".join(rows) + "</tbody></table>"


def _risk_table(metrics: Sequence[RobotMetrics]) -> str:
    rows: list[str] = []
    for item in metrics:
        for risk in item.risk:
            rows.append(
                "<tr>"
                f"<td>{html.escape(item.spec.name)}</td>"
                f"<td>{_int(risk.get('leg_no'))}</td>"
                f"<td>{_int(risk.get('baskets_reaching_leg'))}</td>"
                f"<td>{100 * _float(risk.get('next_leg_rate')):.1f}%</td>"
                f"<td>{_int(risk.get('drawdown_sample_size'))}</td>"
                f"<td>{_number(risk.get('average_max_drawdown_money'))}</td>"
                f"<td>{_number(risk.get('worst_max_drawdown_money'))}</td>"
                f"<td>{_float(risk.get('average_net_profit')):.2f}</td>"
                "</tr>"
            )
    return """
<table><thead><tr><th>Робот</th><th>Колено</th><th>Дошли</th>
<th>Пошли дальше</th><th>N DD</th><th>Средняя DD</th><th>Худшая DD</th>
<th>Средний net</th></tr></thead><tbody>""" + "".join(rows) + "</tbody></table>"


def _render_dashboard(
    metrics: Sequence[RobotMetrics],
    output_dir: Path,
    captured_at: datetime,
    comparison_ready: bool,
) -> str:
    dashboard_dir = output_dir / "dashboard"
    cards = "".join(_robot_card(item, dashboard_dir) for item in metrics)
    readiness = (
        "Выборка DD достигла минимального порога для предварительного сравнения."
        if comparison_ready
        else "Сравнение риска пока не готово: каждому роботу нужно минимум "
        f"{MIN_PRELIMINARY_DD_SAMPLE} измеренных корзин DD."
    )
    readiness_class = "good" if comparison_ready else "warn"
    return f"""<!doctype html><html lang='ru'><head><meta charset='utf-8'>
<meta name='viewport' content='width=device-width,initial-scale=1'>
<title>TradeMind Control Center v{CONTROL_CENTER_VERSION}</title><style>
:root{{color-scheme:dark}}body{{margin:28px;background:#06121c;color:#e8f5ff;
font-family:Arial,sans-serif}}h1{{font-size:38px;margin-bottom:8px}}h2{{margin:0}}
a{{color:#76d4ff}}small{{color:#a8c3d4}}.good{{color:#39e4a8}}.warn{{color:#ffd36d}}
.banner{{padding:14px 18px;background:#102638;border-left:4px solid #ffd36d;
border-radius:8px;margin:18px 0}}.robot-grid{{display:grid;
grid-template-columns:repeat(auto-fit,minmax(420px,1fr));gap:16px}}
.robot-card{{background:#0b2638;border:1px solid #1e5a78;border-radius:16px;padding:18px}}
.robot-head{{display:flex;justify-content:space-between;gap:16px;align-items:flex-start}}
.mini-grid{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px;
margin-top:16px}}.mini-grid div{{background:#0e3046;border-radius:11px;padding:12px}}
.mini-grid span{{display:block;color:#b8d0df;margin-bottom:6px}}.mini-grid b{{font-size:22px}}
table{{width:100%;border-collapse:collapse;margin:18px 0 34px;background:#091d2b}}
th,td{{padding:10px;border-bottom:1px solid #194156;text-align:left;vertical-align:top}}
thead th{{background:#0e3046;position:sticky;top:0}}section{{margin-top:34px}}
.note{{color:#a8c3d4}}@media(max-width:700px){{body{{margin:14px}}.robot-grid{{display:block}}
.robot-card{{margin-bottom:14px}}.mini-grid{{grid-template-columns:1fr}}table{{font-size:13px}}}}
</style></head><body>
<h1>TradeMind Control Center v{CONTROL_CENTER_VERSION}</h1>
<p class='good'>Только чтение. Ордера выключены. Роботы и исходные CSV не изменяются.</p>
<p class='note'>Обновлено: {html.escape(captured_at.isoformat())}</p>
<div class='banner {readiness_class}'>{html.escape(readiness)}</div>
<div class='robot-grid'>{cards}</div>
<section><h2>Сравнение</h2>
<p class='note'>Доллары между счетами нельзя сравнивать в лоб. Смотри также DD в процентах,
размер выборки и длительность зависания.</p>{_comparison_table(metrics)}</section>
<section><h2>Открытые корзины сейчас</h2>{_open_baskets_table(metrics)}</section>
<section><h2>Риск по коленям</h2>
<p class='note'>Средняя и худшая DD считаются только по реально измеренным корзинам.</p>
{_risk_table(metrics)}</section>
</body></html>"""


def run_control_center(
    specs: Sequence[ReportSpec],
    output_dir: Path,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    if len(specs) < 2:
        raise ValueError("At least two reports are required for comparison")
    captured_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    metrics = [_load_metrics(spec, captured_at) for spec in specs]
    comparison_ready = all(
        _int(item.summary.get("drawdown_measured_baskets"))
        >= MIN_PRELIMINARY_DD_SAMPLE
        and _int(item.summary.get("unmatched_position_snapshot_rows")) == 0
        for item in metrics
    )
    status = {
        "schema_version": CONTROL_CENTER_VERSION,
        "state": "OK",
        "updated_at": captured_at.isoformat(),
        "comparison_ready": comparison_ready,
        "minimum_dd_sample": MIN_PRELIMINARY_DD_SAMPLE,
        "strong_dd_sample": STRONG_DD_SAMPLE,
        "orders_enabled": False,
        "logic_changed": False,
        "source_modified": False,
        "robots": [item.summary for item in metrics],
        "dashboard": str(output_dir / "dashboard" / "index.html"),
        "output_dir": str(output_dir),
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    _atomic_json(output_dir / "status.json", status)
    _atomic_csv(
        output_dir / "robot_summary.csv",
        SUMMARY_FIELDS,
        [item.summary for item in metrics],
    )
    _atomic_text(
        output_dir / "dashboard" / "index.html",
        _render_dashboard(metrics, output_dir, captured_at, comparison_ready),
    )
    return status


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="TradeMind read-only multi-robot control center"
    )
    parser.add_argument(
        "--report",
        action="append",
        required=True,
        help="NAME|ACCOUNT_LOGIN|REPORT_DIRECTORY; repeat for each robot",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("data/control_center_v1_15"),
    )
    parser.add_argument("--open-dashboard", action="store_true")
    args = parser.parse_args(argv)
    try:
        specs = [_parse_spec(value) for value in args.report]
        output_dir = args.output_dir.expanduser().resolve()
        status = run_control_center(specs, output_dir)
    except (OSError, ValueError, csv.Error, json.JSONDecodeError) as exc:
        print(f"TradeMind control center failed: {exc}")
        return 1

    print(f"TradeMind Control Center v{CONTROL_CENTER_VERSION}")
    print("Read-only. Orders OFF. Strategy logic unchanged. Source files unchanged.")
    for item in status["robots"]:
        print(
            f"{item['robot']} account={item['account_login']}: "
            f"baskets={item['baskets']} completed={item['completed_baskets']} "
            f"open={item['open_baskets']} net={item['net_profit']:.2f} "
            f"DD sample={item['drawdown_measured_baskets']}"
        )
    print(
        "Risk comparison: "
        + ("PRELIMINARY READY" if status["comparison_ready"] else "NOT READY YET")
    )
    print(f"Output: {status['output_dir']}")
    if args.open_dashboard:
        dashboard = Path(status["dashboard"])
        if hasattr(os, "startfile"):
            os.startfile(dashboard)  # type: ignore[attr-defined]
        else:
            webbrowser.open(dashboard.as_uri())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
