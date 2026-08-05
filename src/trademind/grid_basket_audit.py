"""Coverage-aware public wrapper around the grid basket analytics engine."""

from __future__ import annotations

import argparse
import csv
import html
import json
import os
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from trademind.grid_basket_analytics import (
    BASKET_FIELDS,
    LEG_RISK_FIELDS,
    SYMBOL_FIELDS,
    GridAnalyticsSummary,
    _atomic_csv,
    _atomic_json,
    _atomic_text,
    _float,
    _int,
    _text,
    run_grid_analytics,
)


def _read_csv(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _parse_time(value: Any) -> datetime | None:
    text = _text(value)
    if not text:
        return None
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _optional_max(rows: Sequence[dict[str, Any]], field: str) -> float | str:
    values = [_float(row.get(field)) for row in rows if _text(row.get(field))]
    return max(values) if values else ""


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _optional_number(value: Any, digits: int = 2) -> str:
    return f"{_float(value):.{digits}f}" if _text(value) else "—"


def _drawdown_map(legs_path: Path) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in _read_csv(legs_path):
        grouped[_text(row.get("basket_id"))].append(row)
    result: dict[str, dict[str, Any]] = {}
    for basket_id, rows in grouped.items():
        first = rows[0]
        result[basket_id] = {
            "robot": _text(first.get("robot")),
            "magic": _text(first.get("magic")),
            "symbol": _text(first.get("symbol")),
            "side": _text(first.get("side")).upper(),
            "max_legs": max((_int(row.get("leg_no")) for row in rows), default=0),
            "money": _optional_max(rows, "max_drawdown_money"),
            "pct": _optional_max(rows, "max_drawdown_pct"),
            "adverse": _optional_max(rows, "max_adverse_points"),
        }
    return result


def _collector_metrics(
    history: Sequence[dict[str, Any]],
    snapshot_status: dict[str, Any],
) -> dict[str, Any]:
    started_at = _parse_time(snapshot_status.get("monitoring_started_at"))
    if started_at is None:
        return {
            "collector_started_at": "",
            "collector_eligible_baskets": 0,
            "collector_measured_baskets": 0,
            "collector_drawdown_coverage": 0.0,
        }

    eligible: list[dict[str, Any]] = []
    for row in history:
        closed_at = _parse_time(row.get("closed_at"))
        if closed_at is None or closed_at >= started_at:
            eligible.append(row)

    measured = [row for row in eligible if _text(row.get("max_drawdown_money"))]
    return {
        "collector_started_at": started_at.isoformat(),
        "collector_eligible_baskets": len(eligible),
        "collector_measured_baskets": len(measured),
        "collector_drawdown_coverage": len(measured) / len(eligible) if eligible else 0.0,
    }


def _render_audit_dashboard(
    status: dict[str, Any],
    risk: Sequence[dict[str, Any]],
    symbols: Sequence[dict[str, Any]],
) -> str:
    historical_coverage = 100.0 * _float(status.get("drawdown_coverage"))
    collector_coverage = 100.0 * _float(status.get("collector_drawdown_coverage"))
    measured_count = _int(status.get("drawdown_measured_baskets"))
    eligible_count = _int(status.get("collector_eligible_baskets"))
    collector_measured = _int(status.get("collector_measured_baskets"))

    if historical_coverage > 0:
        drawdown_value = f"{_float(status.get('worst_drawdown_money')):.2f}"
        drawdown_note = f"по {measured_count} измеренным корзинам"
    else:
        drawdown_value = "НЕ ИЗМЕРЕНА"
        drawdown_note = "нужен сбор плавающей просадки"

    account_latest = _float(status.get("latest_account_floating_drawdown_money"))
    account_worst = _float(status.get("worst_account_floating_drawdown_money"))
    collector_note = (
        f"{collector_measured} из {eligible_count} корзин после запуска"
        if eligible_count
        else "снимков после запуска пока нет"
    )
    collector_started = html.escape(_text(status.get("collector_started_at")) or "—")

    risk_rows = "".join(
        "<tr>"
        f"<td>{_int(row.get('leg_no'))}</td>"
        f"<td>{_int(row.get('baskets_reaching_leg'))}</td>"
        f"<td>{100 * _float(row.get('next_leg_rate')):.1f}%</td>"
        f"<td>{100 * _float(row.get('stop_exit_rate')):.1f}%</td>"
        f"<td>{_int(row.get('drawdown_sample_size'))}</td>"
        f"<td>{_optional_number(row.get('average_max_drawdown_money'))}</td>"
        f"<td>{_optional_number(row.get('worst_max_drawdown_money'))}</td>"
        f"<td>{_float(row.get('average_net_profit')):.2f}</td>"
        "</tr>"
        for row in risk
    )
    symbol_rows = "".join(
        "<tr>"
        f"<td>{html.escape(_text(row.get('robot')))}</td>"
        f"<td>{html.escape(_text(row.get('symbol')))} {html.escape(_text(row.get('side')))}</td>"
        f"<td>{_int(row.get('completed'))}</td>"
        f"<td>{100 * _float(row.get('win_rate')):.1f}%</td>"
        f"<td>{_float(row.get('net_profit')):.2f}</td>"
        f"<td>{_float(row.get('profit_factor')):.2f}</td>"
        f"<td>{_optional_number(row.get('worst_max_drawdown_money'))}</td>"
        f"<td>{_int(row.get('max_legs'))}</td>"
        "</tr>"
        for row in sorted(symbols, key=lambda item: _float(item.get("net_profit")), reverse=True)
    )
    return f"""<!doctype html><html lang='ru'><head><meta charset='utf-8'>
<meta name='viewport' content='width=device-width,initial-scale=1'>
<title>TradeMind Grid Basket Analytics v1.15.1</title><style>
body{{background:#07131e;color:#e8f5ff;font-family:Arial;margin:28px}}h1{{font-size:38px}}
.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:14px}}
.card{{background:#0c293d;border:1px solid #1d5878;border-radius:15px;padding:18px}}
.card b{{font-size:28px}}.card small{{color:#a9c5d8}}table{{width:100%;border-collapse:collapse;margin:18px 0 34px}}
th,td{{padding:9px;border-bottom:1px solid #183f56;text-align:left}}.ok{{color:#31e4a5}}.warn{{color:#ffd36d}}
.note{{background:#102131;border-left:4px solid #ffd36d;padding:12px 16px;margin:18px 0}}
</style></head><body><h1>Grid Basket Analytics v1.15.1</h1>
<p class='ok'>Только чтение. Ордера выключены. Исходные файлы не изменяются.</p>
<p class='warn'>Это аудит риска сетки, а не генератор торговых сигналов.</p>
<section class='cards'>
<div class='card'><span>Корзин</span><br><b>{_int(status.get('baskets'))}</b></div>
<div class='card'><span>Закрыто</span><br><b>{_int(status.get('completed_baskets'))}</b></div>
<div class='card'><span>Чистый результат</span><br><b>{_float(status.get('net_profit')):.2f}</b></div>
<div class='card'><span>Худшая DD корзины</span><br><b>{drawdown_value}</b><br><small>{drawdown_note}</small></div>
<div class='card'><span>Покрытие с запуска</span><br><b>{collector_coverage:.1f}%</b><br><small>{collector_note}</small></div>
<div class='card'><span>Историческое покрытие</span><br><b>{historical_coverage:.1f}%</b><br><small>старые корзины без снимков не считаются нулём</small></div>
<div class='card'><span>DD счёта сейчас / максимум</span><br><b>{account_latest:.2f} / {account_worst:.2f}</b><br><small>весь счёт, не одна корзина</small></div>
<div class='card'><span>Макс. одновременно</span><br><b>{_int(status.get('max_concurrent_baskets'))}</b></div>
</section>
<div class='note'>Сборщик работает с: <b>{collector_started}</b>. Таблица DD ниже опирается только на реально измеренные корзины.</div>
<h2>Риск по коленям</h2><table><thead><tr><th>Колено</th><th>Дошли</th><th>Пошли дальше</th><th>Стоп</th><th>N DD</th><th>Средняя DD</th><th>Худшая DD</th><th>Средний net</th></tr></thead><tbody>{risk_rows}</tbody></table>
<h2>Роботы и пары</h2><table><thead><tr><th>Робот</th><th>Инструмент</th><th>N</th><th>Win rate</th><th>Net</th><th>PF</th><th>Худшая DD</th><th>Макс. колен</th></tr></thead><tbody>{symbol_rows}</tbody></table>
</body></html>"""


def _patch_outputs(
    legs_path: Path,
    output_dir: Path,
    status: dict[str, Any],
) -> dict[str, Any]:
    dd = _drawdown_map(legs_path)
    history_path = output_dir / "basket_history.csv"
    risk_path = output_dir / "risk_by_leg.csv"
    symbol_path = output_dir / "symbol_report.csv"
    history = _read_csv(history_path)
    risk = _read_csv(risk_path)
    symbols = _read_csv(symbol_path)

    for row in history:
        item = dd.get(_text(row.get("basket_id")), {})
        row["max_drawdown_money"] = item.get("money", "")
        row["max_drawdown_pct"] = item.get("pct", "")
        row["max_adverse_points"] = item.get("adverse", "")

    for row in risk:
        leg_no = _int(row.get("leg_no"))
        reached = [item for item in dd.values() if _int(item.get("max_legs")) >= leg_no]
        money = [_float(item["money"]) for item in reached if _text(item.get("money"))]
        pct = [_float(item["pct"]) for item in reached if _text(item.get("pct"))]
        row["drawdown_sample_size"] = len(money)
        row["average_max_drawdown_money"] = round(_mean(money), 6) if money else ""
        row["worst_max_drawdown_money"] = round(max(money), 6) if money else ""
        row["average_max_drawdown_pct"] = round(_mean(pct), 6) if pct else ""
        row["worst_max_drawdown_pct"] = round(max(pct), 6) if pct else ""

    for row in symbols:
        key = (
            _text(row.get("robot")),
            _text(row.get("magic")),
            _text(row.get("symbol")),
            _text(row.get("side")).upper(),
        )
        group = [
            item
            for item in dd.values()
            if (item["robot"], item["magic"], item["symbol"], item["side"]) == key
        ]
        money = [_float(item["money"]) for item in group if _text(item.get("money"))]
        pct = [_float(item["pct"]) for item in group if _text(item.get("pct"))]
        row["average_max_drawdown_money"] = round(_mean(money), 6) if money else ""
        row["worst_max_drawdown_money"] = round(max(money), 6) if money else ""
        row["average_max_drawdown_pct"] = round(_mean(pct), 6) if pct else ""
        row["worst_max_drawdown_pct"] = round(max(pct), 6) if pct else ""

    money_covered = [item for item in dd.values() if _text(item.get("money"))]
    pct_covered = [item for item in dd.values() if _text(item.get("pct"))]
    total = len(dd)
    status["drawdown_measured_baskets"] = len(money_covered)
    status["drawdown_coverage"] = len(money_covered) / total if total else 0.0
    status["drawdown_pct_coverage"] = len(pct_covered) / total if total else 0.0
    status["worst_drawdown_money"] = max(
        (_float(item["money"]) for item in money_covered), default=0.0
    )
    status["worst_drawdown_pct"] = max(
        (_float(item["pct"]) for item in pct_covered), default=0.0
    )
    status["drawdown_missing_is_zero"] = False

    snapshot_status = _read_json(output_dir / "snapshots" / "status.json")
    status.update(_collector_metrics(history, snapshot_status))
    for field in (
        "latest_balance",
        "latest_equity",
        "latest_account_floating_money",
        "latest_account_floating_drawdown_money",
        "latest_account_floating_drawdown_pct",
        "worst_account_floating_drawdown_money",
        "worst_account_floating_drawdown_pct",
        "worst_account_peak_equity_drawdown_money",
        "worst_account_peak_equity_drawdown_pct",
    ):
        status[field] = snapshot_status.get(field, 0.0)
    status["account_scope"] = snapshot_status.get(
        "account_scope", "WHOLE_ACCOUNT_UNFILTERED"
    )

    _atomic_csv(history_path, BASKET_FIELDS, history)
    _atomic_csv(risk_path, LEG_RISK_FIELDS, risk)
    _atomic_csv(symbol_path, SYMBOL_FIELDS, symbols)
    _atomic_json(output_dir / "status.json", status)
    _atomic_text(
        output_dir / "dashboard" / "index.html",
        _render_audit_dashboard(status, risk, symbols),
    )
    return status


def run_grid_audit(
    legs_path: Path,
    output_dir: Path,
    *,
    now: datetime | None = None,
) -> GridAnalyticsSummary:
    summary = run_grid_analytics(legs_path, output_dir, now=now)
    status = _patch_outputs(legs_path, output_dir, dict(summary.status))
    return GridAnalyticsSummary(
        source_rows=summary.source_rows,
        baskets=summary.baskets,
        completed_baskets=summary.completed_baskets,
        output_dir=summary.output_dir,
        status=status,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="TradeMind v1.15.1 grid basket risk audit")
    parser.add_argument(
        "--legs",
        type=Path,
        default=Path("data/grid_basket_v1_15/basket_legs.csv"),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("data/grid_basket_analytics_v1_15"),
    )
    parser.add_argument("--open-dashboard", action="store_true")
    args = parser.parse_args(argv)
    try:
        summary = run_grid_audit(
            args.legs.expanduser().resolve(),
            args.output_dir.expanduser().resolve(),
        )
    except (OSError, ValueError, csv.Error, json.JSONDecodeError) as exc:
        print(f"Grid basket audit failed: {exc}")
        return 1

    status = summary.status
    print("TradeMind v1.15.1 Grid Basket Risk Audit")
    print("Read-only. Signal generation OFF. Orders OFF. Source files unchanged.")
    print(f"Baskets: {summary.baskets}, completed: {summary.completed_baskets}")
    print(f"Net: {status['net_profit']:.2f}, PF: {status['profit_factor']:.3f}")
    print(
        "Drawdown coverage, historical/collector period: "
        f"{100 * status['drawdown_coverage']:.1f}%/"
        f"{100 * status['collector_drawdown_coverage']:.1f}%"
    )
    if status["drawdown_coverage"] > 0:
        print(
            f"Worst measured basket DD: {status['worst_drawdown_money']:.2f} / "
            f"{status['worst_drawdown_pct']:.2f}%"
        )
    else:
        print("Worst basket DD: NOT MEASURED YET (snapshot collector required)")
    print(
        "Account floating DD, latest/worst: "
        f"{_float(status.get('latest_account_floating_drawdown_money')):.2f}/"
        f"{_float(status.get('worst_account_floating_drawdown_money')):.2f}"
    )
    print(f"Output: {summary.output_dir}")
    if args.open_dashboard:
        os.startfile(summary.output_dir / "dashboard" / "index.html")  # type: ignore[attr-defined]
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
