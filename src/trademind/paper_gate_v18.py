"""Forward-only paper signal gate for the TradeMind v1.8 research pipeline.

The gate reads Unified Signal Center outputs, evaluates every scenario with
asset-specific quality and evidence rules, suppresses duplicate waves, and
writes a persistent paper journal. Historical rows can train the gate but are
never backfilled into the paper journal before the frozen gate start time.
No MetaTrader order function is imported or called.
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable, Sequence

SCHEMA_VERSION = "1.8.0"
VALID_GATE_STATUSES = {"REJECTED", "WATCH", "CANDIDATE", "VALIDATED"}
ACTIVE_GATE_STATUSES = {"WATCH", "CANDIDATE", "VALIDATED"}
STATUS_RANK = {"REJECTED": 0, "WATCH": 1, "CANDIDATE": 2, "VALIDATED": 3}

CRYPTO_SYMBOLS = {
    "BTCUSD",
    "ETHUSD",
    "SOLUSD",
    "XRPUSD",
    "LTCUSD",
    "BCHUSD",
    "ADAUSD",
    "DOGEUSD",
}
FX_SYMBOLS = {"EURUSD", "GBPUSD", "USDJPY", "USDCHF", "USDCAD", "AUDUSD", "NZDUSD"}
METAL_SYMBOLS = {"XAUUSD", "XAGUSD"}
OIL_SYMBOLS = {"WTI", "BRENT", "USOIL", "UKOIL"}
INDEX_TOKENS = ("US30", "US500", "USTECH", "NAS100", "SPX", "DAX", "GER40", "UK100")

MINIMUM_SCORE = {
    "CRYPTO": 68,
    "FX": 65,
    "METALS": 70,
    "INDICES": 70,
    "OIL": 68,
    "OTHER": 70,
}
COOLDOWN_MINUTES = {
    "CRYPTO": 30,
    "FX": 30,
    "METALS": 30,
    "INDICES": 30,
    "OIL": 30,
    "OTHER": 30,
}
CONFLICT_COMPONENTS = {
    "STRUCTURE_CONFLICT",
    "QUOTE_PRESSURE_CONFLICT",
    "HIGH_SPREAD",
    "SPREAD_EXPANDING",
}
CORE_COMPONENTS = {
    "INTERNAL_BOS",
    "SWING_BOS",
    "INTERNAL_CHOCH",
    "SWING_CHOCH",
    "INTERNAL_BREAK",
    "SWING_BREAK",
    "BOS",
    "CHOCH",
    "BREAK",
    "LIQUIDITY_SWEEP",
    "FVG",
    "OTE",
    "CONFIRMATION",
}

STATE_FIELDS = (
    "captured_at",
    "asset_class",
    "source",
    "symbol",
    "action",
    "scenario",
    "score_filter",
    "horizon",
    "metric_unit",
    "completed",
    "trading_days",
    "unified_status",
    "gate_status",
    "win_rate",
    "profit_factor",
    "avg_result",
    "early_avg_result",
    "late_avg_result",
    "max_drawdown",
    "max_loss_streak",
    "reasons",
)
DECISION_FIELDS = (
    "captured_at",
    "decision_id",
    "wave_key",
    "source",
    "source_id",
    "source_event_id",
    "signal_time",
    "asset_class",
    "symbol",
    "timeframe",
    "session",
    "action",
    "scenario",
    "scenario_family",
    "components",
    "quality_score",
    "score_filter",
    "horizon",
    "horizon_minutes",
    "metric_unit",
    "risk_model",
    "entry_price",
    "stop_price",
    "target_price",
    "rr",
    "gate_status",
    "eligible",
    "duplicate_wave",
    "evidence_completed",
    "evidence_days",
    "evidence_pf",
    "evidence_avg",
    "evidence_late",
    "evidence_drawdown",
    "evidence_loss_streak",
    "outcome",
    "result",
    "mfe",
    "mae",
    "completed",
    "reasons",
)
PAPER_FIELDS = (
    "schema_version",
    "paper_signal_id",
    "activated_at",
    "updated_at",
    "wave_key",
    "source",
    "source_id",
    "signal_time",
    "asset_class",
    "symbol",
    "timeframe",
    "session",
    "action",
    "scenario",
    "scenario_family",
    "components",
    "quality_score",
    "gate_status",
    "risk_model",
    "entry_price",
    "stop_price",
    "target_price",
    "rr",
    "horizon",
    "horizon_minutes",
    "metric_unit",
    "outcome",
    "result",
    "mfe",
    "mae",
    "completed",
    "reasons",
)


@dataclass(frozen=True, slots=True)
class GateSummary:
    gate_started_at: datetime
    source_rows: int
    source_states: int
    decisions: int
    watch: int
    candidates: int
    validated: int
    rejected: int
    paper_signals: int
    completed_paper_signals: int
    decisions_path: Path
    states_path: Path
    signals_path: Path
    dashboard_path: Path


def _text(row: dict[str, str], key: str) -> str:
    return str(row.get(key, "") or "").strip()


def _int(row: dict[str, str], key: str, default: int = 0) -> int:
    try:
        return int(float(_text(row, key) or default))
    except (TypeError, ValueError):
        return default


def _float(row: dict[str, str], key: str, default: float = 0.0) -> float:
    try:
        return float(_text(row, key) or default)
    except (TypeError, ValueError):
        return default


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("signal_time must contain a timezone offset")
    return parsed


def _load_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [
            {str(key): str(value or "").strip() for key, value in dict(row).items()}
            for row in csv.DictReader(handle)
        ]


def _atomic_csv(path: Path, fields: Iterable[str], rows: Sequence[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=tuple(fields), extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(path)


def _atomic_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)


def _atomic_json(path: Path, payload: dict[str, str]) -> None:
    _atomic_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def normalize_symbol(symbol: str) -> str:
    return "".join(character for character in symbol.upper() if character.isalnum())


def asset_class(symbol: str) -> str:
    normalized = normalize_symbol(symbol)
    if normalized in CRYPTO_SYMBOLS:
        return "CRYPTO"
    if normalized in FX_SYMBOLS:
        return "FX"
    if normalized in METAL_SYMBOLS:
        return "METALS"
    if normalized in OIL_SYMBOLS:
        return "OIL"
    if any(token in normalized for token in INDEX_TOKENS):
        return "INDICES"
    return "OTHER"


def _components(row: dict[str, str]) -> set[str]:
    return {item.strip().upper() for item in _text(row, "components").split("|") if item.strip()}


def _score_filter(score: int) -> str:
    if score >= 80:
        return "SCORE_80"
    if score >= 70:
        return "SCORE_70"
    if score >= 60:
        return "SCORE_60"
    return "ALL"


def _state_key(row: dict[str, str], score_filter: str | None = None) -> tuple[str, ...]:
    return (
        _text(row, "source").upper(),
        _text(row, "symbol").upper(),
        _text(row, "action").upper(),
        _text(row, "scenario").upper(),
        (score_filter or _text(row, "score_filter")).upper(),
        _text(row, "horizon").upper(),
        _text(row, "metric_unit").upper(),
    )


def state_gate_status(row: dict[str, str]) -> tuple[str, str]:
    completed = _int(row, "completed")
    days = _int(row, "trading_days")
    profit_factor = _float(row, "profit_factor")
    average = _float(row, "avg_result")
    early = _float(row, "early_avg_result")
    late = _float(row, "late_avg_result")
    drawdown = _float(row, "max_drawdown")
    streak = _int(row, "max_loss_streak")

    if completed < 50 or days < 3:
        return "WATCH", "need >=50 completed observations and >=3 trading days"
    validated = (
        completed >= 150
        and days >= 10
        and average >= 0.05
        and profit_factor >= 1.25
        and early > 0
        and late > 0
        and drawdown <= 20
        and streak <= 8
    )
    if validated:
        return "VALIDATED", "large stable sample with positive expectancy"
    candidate = (
        completed >= 50
        and days >= 5
        and average > 0
        and profit_factor >= 1.15
        and late > 0
        and drawdown <= 25
        and streak <= 10
    )
    if candidate:
        return "CANDIDATE", "positive evidence, forward sample still required"
    return "REJECTED", "evidence failed expectancy, stability or risk limits"


def build_gate_states(
    unified_states: Sequence[dict[str, str]], captured_at: datetime
) -> list[dict[str, str]]:
    output: list[dict[str, str]] = []
    for row in unified_states:
        status, reason = state_gate_status(row)
        output.append(
            {
                "captured_at": captured_at.isoformat(),
                "asset_class": asset_class(_text(row, "symbol")),
                "source": _text(row, "source").upper(),
                "symbol": _text(row, "symbol").upper(),
                "action": _text(row, "action").upper(),
                "scenario": _text(row, "scenario").upper(),
                "score_filter": _text(row, "score_filter").upper(),
                "horizon": _text(row, "horizon").upper(),
                "metric_unit": _text(row, "metric_unit").upper(),
                "completed": _text(row, "completed"),
                "trading_days": _text(row, "trading_days"),
                "unified_status": _text(row, "status").upper(),
                "gate_status": status,
                "win_rate": _text(row, "win_rate"),
                "profit_factor": _text(row, "profit_factor"),
                "avg_result": _text(row, "avg_result"),
                "early_avg_result": _text(row, "early_avg_result"),
                "late_avg_result": _text(row, "late_avg_result"),
                "max_drawdown": _text(row, "max_drawdown"),
                "max_loss_streak": _text(row, "max_loss_streak"),
                "reasons": reason,
            }
        )
    return sorted(
        output,
        key=lambda item: (
            item["asset_class"],
            item["symbol"],
            item["action"],
            item["scenario"],
            item["horizon"],
            item["score_filter"],
        ),
    )


def _preferred_horizon(source: str, rows: Sequence[dict[str, str]]) -> dict[str, str]:
    preferred = "M30" if source == "FX_RESEARCH" else "H3" if source == "SMC_OTE" else ""
    exact = next((row for row in rows if _text(row, "horizon").upper() == preferred), None)
    return exact or min(rows, key=lambda row: _int(row, "horizon_minutes", 10**9))


def _risk_model(row: dict[str, str]) -> str:
    if _text(row, "stop_price") and _text(row, "target_price"):
        return "SOURCE_LEVELS"
    return f"{_text(row, 'metric_unit').upper()}_OBSERVATION"


def _decision_for_row(
    row: dict[str, str],
    state_map: dict[tuple[str, ...], dict[str, str]],
    captured_at: datetime,
) -> dict[str, str]:
    symbol = _text(row, "symbol").upper()
    market = asset_class(symbol)
    score = _int(row, "quality_score")
    components = _components(row)
    score_filter = _score_filter(score)
    state = state_map.get(_state_key(row, score_filter))
    reasons: list[str] = []
    status = "WATCH"

    minimum = MINIMUM_SCORE[market]
    if score < minimum:
        status = "REJECTED"
        reasons.append(f"quality score {score} below {market} minimum {minimum}")
    conflicts = sorted(components & CONFLICT_COMPONENTS)
    if conflicts:
        status = "REJECTED"
        reasons.append("conflicting factors: " + ",".join(conflicts))
    if not components & CORE_COMPONENTS:
        status = "REJECTED"
        reasons.append("no core structure, liquidity, FVG or OTE factor")

    if state is None:
        reasons.append(f"no exact evidence state for {score_filter}")
    else:
        evidence_status, evidence_reason = state_gate_status(state)
        if status != "REJECTED":
            status = evidence_status
        reasons.append(evidence_reason)

    source = _text(row, "source").upper()
    source_id = _text(row, "source_id")
    wave_key = f"{source}:{source_id}"
    return {
        "captured_at": captured_at.isoformat(),
        "decision_id": _text(row, "event_id"),
        "wave_key": wave_key,
        "source": source,
        "source_id": source_id,
        "source_event_id": _text(row, "event_id"),
        "signal_time": _text(row, "signal_time"),
        "asset_class": market,
        "symbol": symbol,
        "timeframe": _text(row, "timeframe").upper(),
        "session": _text(row, "session").upper(),
        "action": _text(row, "action").upper(),
        "scenario": _text(row, "scenario").upper(),
        "scenario_family": _text(row, "scenario_family").upper(),
        "components": _text(row, "components").upper(),
        "quality_score": str(score),
        "score_filter": score_filter,
        "horizon": _text(row, "horizon").upper(),
        "horizon_minutes": _text(row, "horizon_minutes"),
        "metric_unit": _text(row, "metric_unit").upper(),
        "risk_model": _risk_model(row),
        "entry_price": _text(row, "entry_price"),
        "stop_price": _text(row, "stop_price"),
        "target_price": _text(row, "target_price"),
        "rr": _text(row, "rr"),
        "gate_status": status,
        "eligible": "1" if status in ACTIVE_GATE_STATUSES else "0",
        "duplicate_wave": "0",
        "evidence_completed": _text(state or {}, "completed"),
        "evidence_days": _text(state or {}, "trading_days"),
        "evidence_pf": _text(state or {}, "profit_factor"),
        "evidence_avg": _text(state or {}, "avg_result"),
        "evidence_late": _text(state or {}, "late_avg_result"),
        "evidence_drawdown": _text(state or {}, "max_drawdown"),
        "evidence_loss_streak": _text(state or {}, "max_loss_streak"),
        "outcome": _text(row, "outcome").upper(),
        "result": _text(row, "result"),
        "mfe": _text(row, "mfe"),
        "mae": _text(row, "mae"),
        "completed": _text(row, "completed"),
        "reasons": " | ".join(reasons),
    }


def _decision_rank(row: dict[str, str]) -> tuple[int, int, int, str]:
    return (
        STATUS_RANK.get(_text(row, "gate_status"), 0),
        _int(row, "quality_score"),
        len(_components(row)),
        _text(row, "scenario"),
    )


def _mark_duplicate(row: dict[str, str], reason: str) -> None:
    row["gate_status"] = "REJECTED"
    row["eligible"] = "0"
    row["duplicate_wave"] = "1"
    row["reasons"] = " | ".join(part for part in (row["reasons"], reason) if part)


def build_decisions(
    unified_rows: Sequence[dict[str, str]],
    unified_states: Sequence[dict[str, str]],
    captured_at: datetime,
) -> list[dict[str, str]]:
    state_map = {_state_key(row): row for row in unified_states}
    horizon_groups: dict[tuple[str, str, str], list[dict[str, str]]] = defaultdict(list)
    for row in unified_rows:
        source = _text(row, "source").upper()
        source_id = _text(row, "source_id")
        scenario = _text(row, "scenario").upper()
        if source and source_id and scenario and _text(row, "signal_time"):
            horizon_groups[(source, source_id, scenario)].append(row)

    decisions = [
        _decision_for_row(_preferred_horizon(source, rows), state_map, captured_at)
        for (source, _source_id, _scenario), rows in sorted(horizon_groups.items())
    ]

    by_wave: dict[str, list[dict[str, str]]] = defaultdict(list)
    for decision in decisions:
        by_wave[decision["wave_key"]].append(decision)
    best_by_wave: list[dict[str, str]] = []
    for alternatives in by_wave.values():
        best = max(alternatives, key=_decision_rank)
        best_by_wave.append(best)
        for alternative in alternatives:
            if alternative is not best:
                _mark_duplicate(alternative, "lower-ranked scenario for the same source event")

    grouped: dict[tuple[str, str, str], list[dict[str, str]]] = defaultdict(list)
    for decision in best_by_wave:
        if decision["gate_status"] in ACTIVE_GATE_STATUSES:
            grouped[(decision["asset_class"], decision["symbol"], decision["action"])].append(decision)

    for (market, _symbol, _action), group in grouped.items():
        ordered = sorted(group, key=lambda row: _parse_time(row["signal_time"]))
        clusters: list[list[dict[str, str]]] = []
        current: list[dict[str, str]] = []
        last_time: datetime | None = None
        cooldown = timedelta(minutes=COOLDOWN_MINUTES[market])
        for decision in ordered:
            moment = _parse_time(decision["signal_time"])
            if current and last_time is not None and moment - last_time > cooldown:
                clusters.append(current)
                current = []
            current.append(decision)
            last_time = moment
        if current:
            clusters.append(current)
        for cluster in clusters:
            winner = max(cluster, key=_decision_rank)
            for decision in cluster:
                if decision is not winner:
                    _mark_duplicate(decision, "duplicate signal inside the same market wave")

    return sorted(decisions, key=lambda row: (row["signal_time"], row["decision_id"]))


def load_or_create_gate_start(path: Path, now: datetime) -> datetime:
    if path.is_file():
        payload = json.loads(path.read_text(encoding="utf-8"))
        started_at = _parse_time(str(payload["started_at"]))
        return started_at
    payload = {"schema_version": SCHEMA_VERSION, "started_at": now.isoformat()}
    _atomic_json(path, payload)
    return now


def build_paper_journal(
    decisions: Sequence[dict[str, str]],
    existing_rows: Sequence[dict[str, str]],
    gate_started_at: datetime,
    captured_at: datetime,
) -> list[dict[str, str]]:
    journal = {_text(row, "paper_signal_id"): dict(row) for row in existing_rows if _text(row, "paper_signal_id")}
    for decision in decisions:
        if decision["eligible"] != "1":
            continue
        signal_time = _parse_time(decision["signal_time"])
        if signal_time < gate_started_at:
            continue
        paper_id = f"{decision['wave_key']}:{decision['horizon']}"
        previous = journal.get(paper_id, {})
        activated_at = _text(previous, "activated_at") or captured_at.isoformat()
        journal[paper_id] = {
            "schema_version": SCHEMA_VERSION,
            "paper_signal_id": paper_id,
            "activated_at": activated_at,
            "updated_at": captured_at.isoformat(),
            "wave_key": decision["wave_key"],
            "source": decision["source"],
            "source_id": decision["source_id"],
            "signal_time": decision["signal_time"],
            "asset_class": decision["asset_class"],
            "symbol": decision["symbol"],
            "timeframe": decision["timeframe"],
            "session": decision["session"],
            "action": decision["action"],
            "scenario": decision["scenario"],
            "scenario_family": decision["scenario_family"],
            "components": decision["components"],
            "quality_score": decision["quality_score"],
            "gate_status": decision["gate_status"],
            "risk_model": decision["risk_model"],
            "entry_price": decision["entry_price"],
            "stop_price": decision["stop_price"],
            "target_price": decision["target_price"],
            "rr": decision["rr"],
            "horizon": decision["horizon"],
            "horizon_minutes": decision["horizon_minutes"],
            "metric_unit": decision["metric_unit"],
            "outcome": decision["outcome"],
            "result": decision["result"],
            "mfe": decision["mfe"],
            "mae": decision["mae"],
            "completed": decision["completed"],
            "reasons": decision["reasons"],
        }
    return sorted(journal.values(), key=lambda row: (row["signal_time"], row["paper_signal_id"]))


def render_dashboard(
    decisions: Sequence[dict[str, str]],
    states: Sequence[dict[str, str]],
    journal: Sequence[dict[str, str]],
    gate_started_at: datetime,
) -> str:
    status_counts = {status: 0 for status in VALID_GATE_STATUSES}
    for decision in decisions:
        status_counts[decision["gate_status"]] += 1
    completed = sum(row["completed"] == "1" for row in journal)
    asset_counts: dict[str, int] = defaultdict(int)
    for row in journal:
        asset_counts[row["asset_class"]] += 1

    cards = "".join(
        f"<article><span>{html.escape(label)}</span><b>{value}</b></article>"
        for label, value in (
            ("Paper-сигналов", len(journal)),
            ("Завершено", completed),
            ("Watch", status_counts["WATCH"]),
            ("Кандидаты", status_counts["CANDIDATE"]),
            ("Подтверждено", status_counts["VALIDATED"]),
            ("Отклонено", status_counts["REJECTED"]),
        )
    )
    asset_text = ", ".join(f"{name}: {count}" for name, count in sorted(asset_counts.items())) or "пока нет"

    recent = sorted(journal, key=lambda row: row["signal_time"], reverse=True)[:50]
    recent_rows = "".join(
        "<tr>"
        f"<td>{html.escape(row['signal_time'][:19])}</td><td>{html.escape(row['asset_class'])}</td>"
        f"<td>{html.escape(row['symbol'])}</td><td>{html.escape(row['action'])}</td>"
        f"<td>{html.escape(row['scenario'])}</td><td>{html.escape(row['gate_status'])}</td>"
        f"<td>{html.escape(row['quality_score'])}</td><td>{html.escape(row['risk_model'])}</td>"
        f"<td>{html.escape(row['outcome'])}</td><td>{html.escape(row['result'])}</td>"
        f"<td>{html.escape(row['mfe'])}</td><td>{html.escape(row['mae'])}</td>"
        "</tr>"
        for row in recent
    ) or '<tr><td colspan="12">Forward-журнал пока пуст. Это нормально сразу после запуска.</td></tr>'

    top_states = sorted(
        states,
        key=lambda row: (
            STATUS_RANK.get(row["gate_status"], 0),
            _float(row, "avg_result"),
            _float(row, "profit_factor"),
            _int(row, "completed"),
        ),
        reverse=True,
    )[:50]
    state_rows = "".join(
        "<tr>"
        f"<td>{html.escape(row['asset_class'])}</td><td>{html.escape(row['symbol'])}</td>"
        f"<td>{html.escape(row['action'])}</td><td>{html.escape(row['scenario'])}</td>"
        f"<td>{html.escape(row['score_filter'])}</td><td>{html.escape(row['horizon'])}</td>"
        f"<td>{html.escape(row['gate_status'])}</td><td>{html.escape(row['completed'])}</td>"
        f"<td>{_float(row, 'win_rate') * 100:.1f}%</td>"
        f"<td>{_float(row, 'profit_factor'):.2f}</td><td>{_float(row, 'avg_result'):.3f}</td>"
        "</tr>"
        for row in top_states
    ) or '<tr><td colspan="11">Состояний пока нет</td></tr>'

    return f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>TradeMind v1.8 Paper Signal Gate</title><style>
:root{{--bg:#071019;--panel:#101e2b;--line:#29445d;--text:#eef8ff;--muted:#91abc0;--mint:#5ce1b9;--gold:#ffd166}}
*{{box-sizing:border-box}}body{{margin:0;background:radial-gradient(circle at top,#16304a,#071019 58%);color:var(--text);font:15px/1.45 system-ui,sans-serif}}
main{{max-width:1600px;margin:auto;padding:30px}}h1{{font-size:clamp(36px,5vw,66px);margin:0}}.lead{{color:var(--muted);max-width:1100px}}
.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px;margin:26px 0}}article,section,.notice{{background:#101e2be8;border:1px solid var(--line);border-radius:18px;padding:18px}}
article span{{display:block;color:var(--muted)}}article b{{font-size:34px}}.notice{{border-left:5px solid var(--gold);margin:22px 0}}
section{{margin:24px 0;overflow:auto}}table{{width:100%;border-collapse:collapse;min-width:1200px}}th,td{{padding:10px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}}th{{color:var(--mint);position:sticky;top:0;background:#101e2b}}</style></head><body><main>
<h1>Paper Signal Gate</h1>
<p class="lead">Forward-only шлюз всех сценариев TradeMind. История оценивает закономерности, но paper-журнал начинается только после зафиксированного запуска. Крипта, FX, металлы, индексы и нефть не смешиваются. Повторы одной волны подавляются. Ордеров нет.</p>
<div class="cards">{cards}</div>
<div class="notice"><b>Старт forward-журнала:</b> {html.escape(gate_started_at.isoformat())}<br><b>Распределение:</b> {html.escape(asset_text)}</div>
<section><h2>Forward paper-журнал</h2><table><thead><tr><th>Время</th><th>Класс</th><th>Инструмент</th><th>Сторона</th><th>Сценарий</th><th>Статус</th><th>Score</th><th>Риск</th><th>Итог</th><th>Результат</th><th>MFE</th><th>MAE</th></tr></thead><tbody>{recent_rows}</tbody></table></section>
<section><h2>Лучшие доказательства</h2><table><thead><tr><th>Класс</th><th>Инструмент</th><th>Сторона</th><th>Сценарий</th><th>Фильтр</th><th>Горизонт</th><th>Gate</th><th>N</th><th>WR</th><th>PF</th><th>Avg</th></tr></thead><tbody>{state_rows}</tbody></table></section>
</main></body></html>"""


def run_paper_gate(
    unified_signals_path: Path,
    unified_states_path: Path,
    output_dir: Path,
    now: datetime | None = None,
) -> GateSummary:
    captured_at = now or datetime.now().astimezone()
    source_rows = _load_csv(unified_signals_path)
    source_states = _load_csv(unified_states_path)
    output_dir.mkdir(parents=True, exist_ok=True)
    meta_path = output_dir / "gate_meta.json"
    gate_started_at = load_or_create_gate_start(meta_path, captured_at)
    decisions = build_decisions(source_rows, source_states, captured_at)
    states = build_gate_states(source_states, captured_at)
    signals_path = output_dir / "signals.csv"
    existing = _load_csv(signals_path) if signals_path.is_file() else []
    journal = build_paper_journal(decisions, existing, gate_started_at, captured_at)
    decisions_path = output_dir / "decisions.csv"
    states_path = output_dir / "latest.csv"
    dashboard_path = output_dir / "dashboard" / "index.html"
    _atomic_csv(decisions_path, DECISION_FIELDS, decisions)
    _atomic_csv(states_path, STATE_FIELDS, states)
    _atomic_csv(signals_path, PAPER_FIELDS, journal)
    _atomic_text(dashboard_path, render_dashboard(decisions, states, journal, gate_started_at))
    counts = {status: sum(row["gate_status"] == status for row in decisions) for status in VALID_GATE_STATUSES}
    return GateSummary(
        gate_started_at=gate_started_at,
        source_rows=len(source_rows),
        source_states=len(source_states),
        decisions=len(decisions),
        watch=counts["WATCH"],
        candidates=counts["CANDIDATE"],
        validated=counts["VALIDATED"],
        rejected=counts["REJECTED"],
        paper_signals=len(journal),
        completed_paper_signals=sum(row["completed"] == "1" for row in journal),
        decisions_path=decisions_path,
        states_path=states_path,
        signals_path=signals_path,
        dashboard_path=dashboard_path,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Run TradeMind v1.8 forward-only paper gate")
    parser.add_argument(
        "--unified-signals",
        type=Path,
        default=Path("data/unified_signal_center_v1_6/signals.csv"),
    )
    parser.add_argument(
        "--unified-states",
        type=Path,
        default=Path("data/unified_signal_center_v1_6/latest.csv"),
    )
    parser.add_argument("--output-dir", type=Path, default=Path("data/paper_signal_gate_v1_8"))
    args = parser.parse_args()
    inputs = (args.unified_signals.expanduser().resolve(), args.unified_states.expanduser().resolve())
    for path in inputs:
        if not path.is_file():
            print(f"Required Unified Center file not found: {path}")
            return 1
    try:
        summary = run_paper_gate(inputs[0], inputs[1], args.output_dir.expanduser().resolve())
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        print(f"Paper Signal Gate failed: {exc}")
        return 1
    print("TradeMind v1.8 Forward Paper Signal Gate")
    print(f"Gate started at: {summary.gate_started_at.isoformat()}")
    print(f"Unified signal rows: {summary.source_rows}")
    print(f"Unified state rows: {summary.source_states}")
    print(f"Decisions: {summary.decisions}")
    print(f"WATCH/CANDIDATE/VALIDATED/REJECTED: {summary.watch}/{summary.candidates}/{summary.validated}/{summary.rejected}")
    print(f"Forward paper signals: {summary.paper_signals}")
    print(f"Completed paper signals: {summary.completed_paper_signals}")
    print(f"Decisions: {summary.decisions_path}")
    print(f"States: {summary.states_path}")
    print(f"Signals: {summary.signals_path}")
    print(f"Dashboard: {summary.dashboard_path}")
    print("No orders were sent.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
