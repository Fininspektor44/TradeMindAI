"""Statistics and local HTML dashboard for SMC OTE shadow research."""

from __future__ import annotations

import csv
import html
import math
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Sequence

from trademind.ote_models import HORIZON_BARS, SIGNAL_FIELDS, STATE_FIELDS, number, value_float, value_int


def atomic_csv(path: Path, fields: tuple[str, ...], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(path)


def atomic_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)


def _metrics(values: Sequence[float]) -> tuple[float, float, float, int]:
    if not values:
        return 0.0, 0.0, 0.0, 0
    wins = sum(value > 0 for value in values)
    positive = sum(value for value in values if value > 0)
    negative = abs(sum(value for value in values if value < 0))
    profit_factor = positive / negative if negative > 0 else math.inf if positive > 0 else 0.0
    equity = peak = max_drawdown = 0.0
    streak = max_streak = 0
    for value in values:
        equity += value
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)
        if value <= 0:
            streak += 1
            max_streak = max(max_streak, streak)
        else:
            streak = 0
    return wins / len(values), profit_factor, max_drawdown, max_streak


def _status(
    completed: int,
    trading_days: int,
    profit_factor: float,
    avg_r: float,
    early: float,
    late: float,
    max_drawdown: float,
) -> tuple[str, str]:
    if completed < 30 or trading_days < 5:
        return "INSUFFICIENT_SAMPLE", "need >=30 completed signals and >=5 trading days"
    stable = avg_r > 0 and early > 0 and late > 0 and profit_factor >= 1.2 and max_drawdown <= 20
    if completed >= 100 and trading_days >= 15 and stable and profit_factor >= 1.3 and avg_r >= 0.05:
        return "VALIDATED", "sample, both halves, PF and drawdown passed"
    if stable:
        return "CANDIDATE", "positive but still requires a larger forward sample"
    return "UNSTABLE", "edge or stability requirements failed"


def build_states(signals: Sequence[dict[str, str]], captured_at: datetime) -> list[dict[str, str]]:
    groups: dict[tuple[str, str, str, str], list[dict[str, str]]] = defaultdict(list)
    for row in signals:
        score = value_int(row, "score")
        for name, threshold in (("ALL", 0), ("SCORE_60", 60), ("SCORE_70", 70), ("SCORE_80", 80)):
            if score >= threshold:
                groups[(row["symbol"], row["action"], row["variant"], name)].append(row)

    states: list[dict[str, str]] = []
    for (symbol, action, variant, score_filter), rows in sorted(groups.items()):
        for horizon, _bars in HORIZON_BARS:
            key = horizon.lower()
            completed_rows = [
                row for row in rows
                if row.get(f"outcome_{key}") in {"WIN", "LOSS", "TIMEOUT"}
                and row.get(f"result_r_{key}") not in {"", None}
            ]
            values = [value_float(row, f"result_r_{key}") for row in completed_rows]
            trading_days = len({row["signal_time"][:10] for row in completed_rows})
            win_rate, profit_factor, max_drawdown, max_streak = _metrics(values)
            midpoint = len(values) // 2
            early_values, late_values = values[:midpoint], values[midpoint:]
            avg_r = sum(values) / len(values) if values else 0.0
            early = sum(early_values) / len(early_values) if early_values else 0.0
            late = sum(late_values) / len(late_values) if late_values else 0.0
            status, reasons = _status(
                len(values), trading_days, profit_factor, avg_r, early, late, max_drawdown
            )
            states.append({
                "captured_at": captured_at.isoformat(), "symbol": symbol, "action": action,
                "variant": variant, "score_filter": score_filter, "horizon": horizon,
                "signals": str(len(rows)), "completed": str(len(values)),
                "trading_days": str(trading_days), "status": status,
                "win_rate": number(win_rate), "profit_factor_r": number(profit_factor),
                "avg_r": number(avg_r), "early_avg_r": number(early),
                "late_avg_r": number(late), "max_drawdown_r": number(max_drawdown),
                "max_loss_streak": str(max_streak), "reasons": reasons,
            })
    return states


def render_dashboard(signals: Sequence[dict[str, str]], states: Sequence[dict[str, str]]) -> str:
    status_counts: dict[str, int] = defaultdict(int)
    for row in states:
        status_counts[row["status"]] += 1
    top = sorted(
        states,
        key=lambda row: (
            row["status"] == "VALIDATED", row["status"] == "CANDIDATE",
            value_float(row, "avg_r"), value_float(row, "profit_factor_r"),
            value_int(row, "completed"),
        ),
        reverse=True,
    )[:20]
    recent = sorted(signals, key=lambda row: row["signal_time"], reverse=True)[:20]
    cards = "".join(
        f'<article><span>{html.escape(name)}</span><b>{value}</b></article>'
        for name, value in (
            ("Сигналов", len(signals)), ("Строк сравнения", len(states)),
            ("Подтверждено", status_counts.get("VALIDATED", 0)),
            ("Кандидаты", status_counts.get("CANDIDATE", 0)),
            ("Мало данных", status_counts.get("INSUFFICIENT_SAMPLE", 0)),
            ("Нестабильно", status_counts.get("UNSTABLE", 0)),
        )
    )
    top_rows = "".join(
        "<tr>"
        f"<td>{html.escape(row['symbol'])}</td><td>{html.escape(row['action'])}</td>"
        f"<td>{html.escape(row['variant'])}</td><td>{html.escape(row['score_filter'])}</td>"
        f"<td>{html.escape(row['horizon'])}</td><td>{html.escape(row['status'])}</td>"
        f"<td>{value_int(row, 'completed')}</td><td>{value_float(row, 'win_rate') * 100:.1f}%</td>"
        f"<td>{value_float(row, 'profit_factor_r'):.2f}</td><td>{value_float(row, 'avg_r'):.3f}</td>"
        "</tr>" for row in top
    ) or '<tr><td colspan="10">Пока нет сравнений</td></tr>'
    recent_rows = "".join(
        "<tr>"
        f"<td>{html.escape(row['signal_time'][:19])}</td><td>{html.escape(row['symbol'])}</td>"
        f"<td>{html.escape(row['action'])}</td><td>{html.escape(row['variant'])}</td>"
        f"<td>{value_int(row, 'score')}</td><td>{value_float(row, 'entry_price'):.6g}</td>"
        f"<td>{value_float(row, 'stop_price'):.6g}</td><td>{value_float(row, 'target_price'):.6g}</td>"
        f"<td>{value_float(row, 'rr'):.2f}</td><td>{html.escape(row['outcome_h3'])}</td>"
        "</tr>" for row in recent
    ) or '<tr><td colspan="10">OTE-сигналов пока нет</td></tr>'
    return f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>TradeMind v1.5 SMC OTE</title>
<style>:root{{--bg:#071612;--panel:#0c241d;--line:#185943;--text:#eafff6;--muted:#8ec3b2;--accent:#3de3a5}}
*{{box-sizing:border-box}}body{{margin:0;background:#071612;color:var(--text);font:15px/1.45 system-ui,sans-serif}}
main{{max-width:1500px;margin:auto;padding:28px}}h1{{font-size:48px;margin:0}}.lead{{color:var(--muted)}}
.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;margin:24px 0}}
article,section{{background:var(--panel);border:1px solid var(--line);border-radius:18px;padding:18px}}
article span{{display:block;color:var(--muted)}}article b{{font-size:34px}}section{{margin:24px 0;overflow:auto}}
table{{width:100%;border-collapse:collapse;min-width:1050px}}th,td{{padding:10px;border-bottom:1px solid #174536;text-align:left}}th{{color:var(--accent)}}</style></head>
<body><main><h1>SMC + Fibonacci OTE</h1><p class="lead">Теневое сравнение входов 61,8%, OTE 70,5% и 79%. Стоп за экстремум волны, цель на предыдущем high/low. Ордеров нет.</p>
<div class="cards">{cards}</div><section><h2>Лучшие текущие сравнения</h2><table><thead><tr><th>Инструмент</th><th>Сторона</th><th>Вход</th><th>Score</th><th>Горизонт</th><th>Статус</th><th>N</th><th>WR</th><th>PF R</th><th>Avg R</th></tr></thead><tbody>{top_rows}</tbody></table></section>
<section><h2>Последние OTE-наблюдения</h2><table><thead><tr><th>Время</th><th>Инструмент</th><th>Сторона</th><th>Вариант</th><th>Score</th><th>Entry</th><th>Stop</th><th>Target</th><th>RR</th><th>H3</th></tr></thead><tbody>{recent_rows}</tbody></table></section></main></body></html>"""


def write_outputs(
    signals_path: Path,
    states_path: Path,
    dashboard_path: Path,
    signals: list[dict[str, str]],
    states: list[dict[str, str]],
) -> None:
    atomic_csv(signals_path, SIGNAL_FIELDS, signals)
    atomic_csv(states_path, STATE_FIELDS, states)
    atomic_text(dashboard_path, render_dashboard(signals, states))
