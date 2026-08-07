"""Human-readable read-only report for the break-even research pipeline.

The report summarizes observed shadow-BE evidence. It never changes trading state and
never turns proxy P/L into a trading recommendation. Exact counterfactual execution is
not simulated.
"""

from __future__ import annotations

import argparse
import csv
import html
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

VERSION = "1.31.1"
MIN_AFFECTED_SAMPLE = 30
MIN_COVERAGE_RATIO = 0.80


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _integer(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _parse_time(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    _atomic_text(path, json.dumps(dict(payload), ensure_ascii=False, indent=2, sort_keys=True))


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _eligible_rows(
    runtime_status: Mapping[str, Any],
    rows: Sequence[Mapping[str, Any]],
) -> tuple[list[Mapping[str, Any]], datetime | None]:
    shadow = dict(runtime_status.get("shadow") or {})
    monitor_started_at = _parse_time(shadow.get("monitor_started_at"))
    if monitor_started_at is None:
        return list(rows), None
    eligible = []
    for row in rows:
        closed_at = _parse_time(row.get("basket_closed_at"))
        if closed_at is not None and closed_at >= monitor_started_at:
            eligible.append(row)
    return eligible, monitor_started_at


def build_summary(
    runtime_status: Mapping[str, Any],
    counterfactual_status: Mapping[str, Any],
    rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    all_completed = _integer(counterfactual_status.get("completed_baskets"), len(rows))
    eligible, monitor_started_at = _eligible_rows(runtime_status, rows)

    if monitor_started_at is None:
        completed = all_completed
        covered = _integer(counterfactual_status.get("covered_completed_baskets"))
        coverage_basis = "ALL_COMPLETED_FALLBACK"
    else:
        completed = len(eligible)
        covered = sum(_integer(row.get("mapped_shadow_epochs")) > 0 for row in eligible)
        coverage_basis = "CLOSED_SINCE_MONITOR_START"

    coverage_ratio = covered / completed if completed else 0.0
    pre_monitor_completed = max(all_completed - completed, 0)

    affected = _integer(counterfactual_status.get("affected_by_shadow_be_baskets"))
    losses_avoided = _integer(counterfactual_status.get("losses_avoided_count"))
    winners_cut = _integer(counterfactual_status.get("winners_cut_count"))
    loss_money = _number(counterfactual_status.get("loss_avoided_proxy_money"))
    cost_money = _number(counterfactual_status.get("opportunity_cost_proxy_money"))
    net_money = _number(counterfactual_status.get("net_effect_proxy_money"))

    classes = Counter(str(row.get("effect_class") or "UNKNOWN") for row in eligible)
    evidence_ready = affected >= MIN_AFFECTED_SAMPLE and coverage_ratio >= MIN_COVERAGE_RATIO
    if evidence_ready:
        review_state = "READY_FOR_HUMAN_REVIEW"
    else:
        review_state = "COLLECTING_EVIDENCE"

    return {
        "schema_version": VERSION,
        "state": "OK",
        "mode": "READ_ONLY_BE_DECISION_REPORT",
        "login": str(runtime_status.get("login") or ""),
        "runtime_updated_at": runtime_status.get("updated_at"),
        "review_state": review_state,
        "review_thresholds": {
            "minimum_affected_baskets": MIN_AFFECTED_SAMPLE,
            "minimum_coverage_ratio": MIN_COVERAGE_RATIO,
        },
        "sample": {
            "completed_baskets": completed,
            "all_completed_baskets": all_completed,
            "pre_monitor_completed_baskets": pre_monitor_completed,
            "covered_completed_baskets": covered,
            "coverage_ratio": round(coverage_ratio, 6),
            "coverage_basis": coverage_basis,
            "monitor_started_at": (
                monitor_started_at.isoformat() if monitor_started_at is not None else ""
            ),
            "affected_by_shadow_be_baskets": affected,
        },
        "effect": {
            "losses_avoided_count": losses_avoided,
            "winners_cut_count": winners_cut,
            "loss_avoided_proxy_money": round(loss_money, 6),
            "opportunity_cost_proxy_money": round(cost_money, 6),
            "net_effect_proxy_money": round(net_money, 6),
        },
        "classes": dict(sorted(classes.items())),
        "interpretation": (
            "Coverage is measured only on baskets closed since shadow monitoring began. "
            "Proxy values use the actual final basket P/L after observed snapshot-level BE events. "
            "They are research evidence, not simulated executable BE P/L and not a command to "
            "enable or disable break-even in a robot."
        ),
        "safety": {
            "read_only": True,
            "shadow_only": True,
            "orders_enabled": False,
            "position_modify_called": False,
            "broker_api_called": False,
            "robot_settings_modified": False,
            "source_csv_modified": False,
        },
    }


def _money(value: Any) -> str:
    return f"{_number(value):,.2f}"


def _pct(value: Any) -> str:
    return f"{_number(value) * 100:.1f}%"


def render_html(summary: Mapping[str, Any], rows: Sequence[Mapping[str, Any]]) -> str:
    sample = dict(summary.get("sample") or {})
    effect = dict(summary.get("effect") or {})
    recent = [row for row in rows if _integer(row.get("mapped_shadow_epochs")) > 0][-20:]
    recent.reverse()

    table_rows = []
    for row in recent:
        table_rows.append(
            "<tr>"
            f"<td>{html.escape(str(row.get('basket_closed_at') or ''))}</td>"
            f"<td>{html.escape(str(row.get('symbol') or ''))}</td>"
            f"<td>{html.escape(str(row.get('side') or ''))}</td>"
            f"<td>{html.escape(str(row.get('effect_class') or ''))}</td>"
            f"<td>{html.escape(_money(row.get('actual_net_profit')))}</td>"
            f"<td>{html.escape(_money(row.get('net_effect_proxy_money')))}</td>"
            "</tr>"
        )
    if not table_rows:
        table_rows.append('<tr><td colspan="6">Пока нет закрытых корзин с shadow-покрытием.</td></tr>')

    review = html.escape(str(summary.get("review_state") or ""))
    return f"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>TradeMind BE Research v1.31.1</title>
<style>
body{{font-family:Segoe UI,Arial,sans-serif;background:#111;color:#eee;margin:0;padding:24px}}
main{{max-width:1100px;margin:auto}}h1{{margin-bottom:4px}}.muted{{color:#aaa}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px;margin:20px 0}}
.card{{background:#1c1c1c;border:1px solid #333;border-radius:10px;padding:16px}}
.value{{font-size:28px;font-weight:700;margin-top:6px}}table{{width:100%;border-collapse:collapse;background:#181818}}
th,td{{padding:10px;border-bottom:1px solid #333;text-align:left;font-size:14px}}
th{{color:#bbb}}.safe{{color:#8fd18f}}.warn{{color:#f4c96b}}
</style>
</head>
<body><main>
<h1>TradeMind v1.31.1 · BreakEven Research</h1>
<div class="muted">Аккаунт {html.escape(str(summary.get('login') or ''))} · только статистика, без управления ордерами</div>
<div class="grid">
<div class="card"><div>Закрытых после старта</div><div class="value">{_integer(sample.get('completed_baskets'))}</div><div class="muted">Исторических до мониторинга: {_integer(sample.get('pre_monitor_completed_baskets'))}</div></div>
<div class="card"><div>Покрыто shadow BE</div><div class="value">{_integer(sample.get('covered_completed_baskets'))}</div><div>{_pct(sample.get('coverage_ratio'))}</div></div>
<div class="card"><div>BE реально повлиял бы</div><div class="value">{_integer(sample.get('affected_by_shadow_be_baskets'))}</div></div>
<div class="card"><div>Спасено убытков</div><div class="value">{_integer(effect.get('losses_avoided_count'))}</div><div>{_money(effect.get('loss_avoided_proxy_money'))}</div></div>
<div class="card"><div>Обрезано победителей</div><div class="value">{_integer(effect.get('winners_cut_count'))}</div><div>{_money(effect.get('opportunity_cost_proxy_money'))}</div></div>
<div class="card"><div>Чистый proxy-эффект</div><div class="value">{_money(effect.get('net_effect_proxy_money'))}</div></div>
</div>
<p class="warn"><strong>Статус выборки:</strong> {review}. До достаточной выборки система не делает вывода «включать/выключать BE».</p>
<p class="safe">READ-ONLY · SHADOW ONLY · ORDERS OFF · ROBOT/EXPORTER UNCHANGED</p>
<h2>Последние закрытые корзины с покрытием</h2>
<table><thead><tr><th>Закрытие</th><th>Символ</th><th>Сторона</th><th>Класс</th><th>Факт P/L</th><th>BE proxy</th></tr></thead>
<tbody>{''.join(table_rows)}</tbody></table>
<p class="muted">Покрытие считается только по корзинам, закрытым после старта shadow-мониторинга. Proxy использует фактический финальный P/L после наблюдавшегося shadow-события. Комиссии, проскальзывание и внутриминутные касания не выдумываются.</p>
</main></body></html>"""


def generate_report(
    runtime_status: Mapping[str, Any],
    counterfactual_status: Mapping[str, Any],
    counterfactual_csv: Path,
    output_dir: Path,
) -> dict[str, Any]:
    rows = _read_csv(counterfactual_csv)
    summary = build_summary(runtime_status, counterfactual_status, rows)
    _atomic_json(output_dir / "summary.json", summary)
    _atomic_text(output_dir / "index.html", render_html(summary, rows))
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="TradeMind read-only BE decision report")
    parser.add_argument("--runtime-status", required=True, type=Path)
    parser.add_argument("--counterfactual-status", required=True, type=Path)
    parser.add_argument("--counterfactual-csv", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()

    runtime_status = json.loads(args.runtime_status.read_text(encoding="utf-8-sig"))
    counter_status = json.loads(args.counterfactual_status.read_text(encoding="utf-8-sig"))
    summary = generate_report(runtime_status, counter_status, args.counterfactual_csv, args.output_dir)
    print("TradeMind v1.31.1 BreakEven Decision Report")
    print(f"Review state: {summary['review_state']}")
    print(f"Coverage: {_pct(summary['sample']['coverage_ratio'])}")
    print(f"Affected baskets: {summary['sample']['affected_by_shadow_be_baskets']}")
    print(f"Net effect proxy: {summary['effect']['net_effect_proxy_money']}")
    print(f"Report: {(args.output_dir / 'index.html').resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
