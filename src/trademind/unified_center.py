"""Unified read-only signal center for all TradeMind research scenarios."""

from __future__ import annotations

import argparse
import csv
import html
import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable, Sequence

SCHEMA_VERSION = "1.6.0"
VALID_ACTIONS = {"BUY", "SELL"}
VALID_OUTCOMES = {"WIN", "LOSS", "FLAT", "TIMEOUT"}
SCORE_FILTERS = (("ALL", 0), ("SCORE_60", 60), ("SCORE_70", 70), ("SCORE_80", 80))

FX_HORIZONS = (
    ("M15", 15, "outcome_3", "progress_atr_3", "mfe_atr_3", "mae_atr_3"),
    ("M30", 30, "outcome_6", "progress_atr_6", "mfe_atr_6", "mae_atr_6"),
    ("H1", 60, "outcome_12", "progress_atr_12", "mfe_atr_12", "mae_atr_12"),
)
OTE_HORIZONS = (
    ("H3", 180, "outcome_h3", "result_r_h3", "mfe_r_h3", "mae_r_h3"),
    ("H6", 360, "outcome_h6", "result_r_h6", "mfe_r_h6", "mae_r_h6"),
    ("H12", 720, "outcome_h12", "result_r_h12", "mfe_r_h12", "mae_r_h12"),
)

SIGNAL_FIELDS = (
    "schema_version",
    "event_id",
    "signal_key",
    "signal_time",
    "source",
    "source_id",
    "symbol",
    "timeframe",
    "session",
    "action",
    "scenario",
    "scenario_family",
    "components",
    "confluence_count",
    "source_score",
    "quality_score",
    "score_bucket",
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
    "h1_bias",
    "h4_bias",
    "reasons",
)
STATE_FIELDS = (
    "captured_at",
    "source",
    "symbol",
    "action",
    "scenario",
    "scenario_family",
    "score_filter",
    "horizon",
    "horizon_minutes",
    "metric_unit",
    "signals",
    "completed",
    "trading_days",
    "status",
    "win_rate",
    "profit_factor",
    "avg_result",
    "early_avg_result",
    "late_avg_result",
    "max_drawdown",
    "max_loss_streak",
    "reasons",
)


@dataclass(frozen=True, slots=True)
class Scenario:
    name: str
    family: str


@dataclass(frozen=True, slots=True)
class UnifiedSummary:
    fx_source_rows: int
    ote_source_rows: int
    signal_keys: int
    horizon_rows: int
    scenarios: int
    completed_rows: int
    states: int
    signals_path: Path
    states_path: Path
    dashboard_path: Path


def _text(row: dict[str, str], key: str) -> str:
    return str(row.get(key, "") or "").strip()


def _float(row: dict[str, str], key: str, default: float = 0.0) -> float:
    try:
        return float(_text(row, key) or default)
    except (TypeError, ValueError):
        return default


def _int(row: dict[str, str], key: str, default: int = 0) -> int:
    try:
        return int(float(_text(row, key) or default))
    except (TypeError, ValueError):
        return default


def _truthy(row: dict[str, str], key: str) -> bool:
    return _text(row, key).lower() in {"1", "true", "yes", "y"}


def _number(value: float | int | None) -> str:
    if value is None:
        return ""
    number = float(value)
    if math.isinf(number):
        return "inf" if number > 0 else "-inf"
    if math.isnan(number):
        return "nan"
    return f"{number:.12g}"


def _clamp_score(value: float) -> int:
    return max(0, min(100, int(round(value))))


def _score_bucket(score: int) -> str:
    if score >= 80:
        return "80_PLUS"
    if score >= 70:
        return "70_79"
    if score >= 60:
        return "60_69"
    return "BELOW_60"


def _labels(row: dict[str, str]) -> set[str]:
    return {item.strip().upper() for item in _text(row, "labels").split("|") if item.strip()}


def _break_component(row: dict[str, str], key: str, scope: str) -> str | None:
    value = _text(row, key).upper()
    if "_" not in value:
        return None
    direction, kind = value.split("_", 1)
    expected = "BULLISH" if _text(row, "action").upper() == "BUY" else "BEARISH"
    if direction != expected or kind not in {"BOS", "CHOCH", "BREAK"}:
        return None
    return f"{scope}_{kind}"


def fx_components(row: dict[str, str]) -> set[str]:
    """Return canonical factors present on one action-aware FX observation."""
    action = _text(row, "action").upper()
    if action not in VALID_ACTIONS:
        return set()
    labels = _labels(row)
    components: set[str] = set()

    for key, scope in (("internal_break", "INTERNAL"), ("swing_break", "SWING")):
        component = _break_component(row, key, scope)
        if component:
            components.add(component)

    if (action == "BUY" and _truthy(row, "ssl_sweep")) or (
        action == "SELL" and _truthy(row, "bsl_sweep")
    ):
        components.add("LIQUIDITY_SWEEP")

    expected_fvg = "BULLISH" if action == "BUY" else "BEARISH"
    if _text(row, "fvg_direction").upper() == expected_fvg:
        components.add("FVG")

    direct_labels = {
        "STRUCTURE_ALIGNED",
        "STRUCTURE_CONFLICT",
        "HIGH_VOLUME",
        "NORMAL_VOLUME",
        "HIGH_RVOL",
        "EXTREME_RVOL",
        "HIGH_TICK_ACTIVITY",
        "TICK_ACCELERATION",
        "QUOTE_PRESSURE_ALIGNED",
        "QUOTE_PRESSURE_CONFLICT",
        "VOLUME_ABSORPTION",
        "LOW_SPREAD",
        "HIGH_SPREAD",
        "SPREAD_EXPANDING",
    }
    components.update(labels & direct_labels)

    impulse = "BULLISH_VOLUME_IMPULSE" if action == "BUY" else "BEARISH_VOLUME_IMPULSE"
    if impulse in labels:
        components.add("VOLUME_IMPULSE")
    return components


def fx_scenarios(row: dict[str, str]) -> list[Scenario]:
    """Build standalone and combined scenarios from all embedded FX factors."""
    components = fx_components(row)
    if not components:
        return []
    scenarios = {Scenario("BASE_SIGNAL", "BASE")}
    family_by_component = {
        "INTERNAL_BOS": "STRUCTURE",
        "SWING_BOS": "STRUCTURE",
        "INTERNAL_CHOCH": "STRUCTURE",
        "SWING_CHOCH": "STRUCTURE",
        "INTERNAL_BREAK": "STRUCTURE",
        "SWING_BREAK": "STRUCTURE",
        "LIQUIDITY_SWEEP": "LIQUIDITY",
        "FVG": "IMBALANCE",
        "STRUCTURE_ALIGNED": "CONTEXT",
        "STRUCTURE_CONFLICT": "CONTEXT",
        "HIGH_VOLUME": "VOLUME",
        "NORMAL_VOLUME": "VOLUME",
        "HIGH_RVOL": "VOLUME",
        "EXTREME_RVOL": "VOLUME",
        "HIGH_TICK_ACTIVITY": "MICROSTRUCTURE",
        "TICK_ACCELERATION": "MICROSTRUCTURE",
        "QUOTE_PRESSURE_ALIGNED": "MICROSTRUCTURE",
        "QUOTE_PRESSURE_CONFLICT": "MICROSTRUCTURE",
        "VOLUME_ABSORPTION": "VOLUME",
        "VOLUME_IMPULSE": "VOLUME",
        "LOW_SPREAD": "COST",
        "HIGH_SPREAD": "COST",
        "SPREAD_EXPANDING": "COST",
    }
    for component in components:
        scenarios.add(Scenario(component, family_by_component.get(component, "CONTEXT")))

    has_bos = bool({"INTERNAL_BOS", "SWING_BOS"} & components)
    has_choch = bool({"INTERNAL_CHOCH", "SWING_CHOCH"} & components)
    has_break = bool({"INTERNAL_BREAK", "SWING_BREAK"} & components)
    has_sweep = "LIQUIDITY_SWEEP" in components
    has_fvg = "FVG" in components

    if has_bos and has_sweep:
        scenarios.add(Scenario("BOS_PLUS_SWEEP", "COMBINATION"))
    if has_choch and has_fvg:
        scenarios.add(Scenario("CHOCH_PLUS_FVG", "COMBINATION"))
    if has_sweep and has_fvg:
        scenarios.add(Scenario("SWEEP_PLUS_FVG", "COMBINATION"))
    if has_break and has_sweep:
        scenarios.add(Scenario("BREAK_PLUS_SWEEP", "COMBINATION"))
    core = sum((has_bos or has_choch or has_break, has_sweep, has_fvg))
    if core >= 2:
        scenarios.add(Scenario("SMC_MULTI_FACTOR", "MULTI_FACTOR"))
    if len(components) >= 4:
        scenarios.add(Scenario("FULL_CONFLUENCE", "MULTI_FACTOR"))
    return sorted(scenarios, key=lambda item: (item.family, item.name))


def ote_components(row: dict[str, str]) -> set[str]:
    action = _text(row, "action").upper()
    if action not in VALID_ACTIONS:
        return set()
    components = {"OTE"}
    break_value = _text(row, "setup_break").upper()
    if "BOS" in break_value:
        components.add("BOS")
    elif "CHOCH" in break_value:
        components.add("CHOCH")
    elif "BREAK" in break_value:
        components.add("BREAK")
    if _truthy(row, "liquidity_sweep"):
        components.add("LIQUIDITY_SWEEP")
    if _truthy(row, "fvg_aligned"):
        components.add("FVG")
    if _truthy(row, "confirmation"):
        components.add("CONFIRMATION")
    if _truthy(row, "h1_aligned"):
        components.add("H1_ALIGNED")
    if _truthy(row, "h4_aligned"):
        components.add("H4_ALIGNED")
    if _float(row, "rvol_20") >= 1.2:
        components.add("HIGH_RVOL")
    if _float(row, "tick_rate_ratio_20") >= 1.2:
        components.add("TICK_ACCELERATION")
    imbalance = _float(row, "direction_imbalance")
    if (action == "BUY" and imbalance >= 0.05) or (action == "SELL" and imbalance <= -0.05):
        components.add("QUOTE_PRESSURE_ALIGNED")
    if _float(row, "spread_ratio_20") >= 1.2:
        components.add("SPREAD_EXPANDING")
    return components


def ote_scenarios(row: dict[str, str]) -> list[Scenario]:
    components = ote_components(row)
    if not components:
        return []
    variant_map = {
        "TOUCH_618": "OTE_618",
        "TOUCH_705": "OTE_705",
        "TOUCH_790": "OTE_790",
        "ZONE_TOUCH": "OTE_ZONE",
        "CONFIRMED_ZONE": "OTE_CONFIRMED_ZONE",
    }
    scenarios = {
        Scenario("OTE_ALL", "OTE"),
        Scenario(variant_map.get(_text(row, "variant").upper(), "OTE_OTHER"), "OTE"),
    }
    for component, scenario in (
        ("BOS", "BOS_PLUS_OTE"),
        ("CHOCH", "CHOCH_PLUS_OTE"),
        ("BREAK", "BREAK_PLUS_OTE"),
        ("LIQUIDITY_SWEEP", "SWEEP_PLUS_OTE"),
        ("FVG", "FVG_PLUS_OTE"),
        ("CONFIRMATION", "CONFIRMED_OTE"),
    ):
        if component in components:
            scenarios.add(Scenario(scenario, "COMBINATION"))
    core = len(components & {"BOS", "CHOCH", "BREAK", "LIQUIDITY_SWEEP", "FVG", "CONFIRMATION"})
    if core >= 3:
        scenarios.add(Scenario("MULTI_FACTOR_OTE", "MULTI_FACTOR"))
    if len(components) >= 6:
        scenarios.add(Scenario("FULL_CONFLUENCE_OTE", "MULTI_FACTOR"))
    return sorted(scenarios, key=lambda item: (item.family, item.name))


POSITIVE_WEIGHTS = {
    "INTERNAL_BOS": 5,
    "SWING_BOS": 7,
    "INTERNAL_CHOCH": 6,
    "SWING_CHOCH": 8,
    "INTERNAL_BREAK": 3,
    "SWING_BREAK": 4,
    "BOS": 6,
    "CHOCH": 7,
    "BREAK": 3,
    "LIQUIDITY_SWEEP": 8,
    "FVG": 5,
    "STRUCTURE_ALIGNED": 5,
    "HIGH_RVOL": 3,
    "EXTREME_RVOL": 2,
    "HIGH_TICK_ACTIVITY": 2,
    "TICK_ACCELERATION": 3,
    "QUOTE_PRESSURE_ALIGNED": 4,
    "VOLUME_ABSORPTION": 2,
    "VOLUME_IMPULSE": 4,
    "LOW_SPREAD": 3,
    "OTE": 7,
    "CONFIRMATION": 7,
    "H1_ALIGNED": 4,
    "H4_ALIGNED": 6,
}
NEGATIVE_WEIGHTS = {
    "STRUCTURE_CONFLICT": 8,
    "QUOTE_PRESSURE_CONFLICT": 5,
    "HIGH_SPREAD": 8,
    "SPREAD_EXPANDING": 5,
}


def quality_score(source_score: int, components: set[str]) -> int:
    """Transparent ranking score, not a profitability claim."""
    value = float(source_score)
    value += sum(POSITIVE_WEIGHTS.get(item, 0) for item in components)
    value -= sum(NEGATIVE_WEIGHTS.get(item, 0) for item in components)
    return _clamp_score(value)


def _load_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [
            {key: str(value or "").strip() for key, value in dict(row).items()}
            for row in csv.DictReader(handle)
        ]


def _event_row(
    *,
    source: str,
    source_id: str,
    row: dict[str, str],
    scenario: Scenario,
    components: set[str],
    horizon: tuple[str, int, str, str, str, str],
    metric_unit: str,
) -> dict[str, str]:
    horizon_name, minutes, outcome_key, result_key, mfe_key, mae_key = horizon
    source_score = _int(row, "score")
    score = quality_score(source_score, components)
    signal_key = f"{source}:{source_id}:{scenario.name}"
    outcome = _text(row, outcome_key).upper()
    result_text = _text(row, result_key)
    completed = outcome in VALID_OUTCOMES and bool(result_text)
    reasons = _text(row, "signal_reasons") or _text(row, "reasons")
    return {
        "schema_version": SCHEMA_VERSION,
        "event_id": f"{signal_key}:{horizon_name}",
        "signal_key": signal_key,
        "signal_time": _text(row, "signal_time"),
        "source": source,
        "source_id": source_id,
        "symbol": _text(row, "symbol").upper(),
        "timeframe": _text(row, "timeframe").upper() or "M5",
        "session": _text(row, "session").upper(),
        "action": _text(row, "action").upper(),
        "scenario": scenario.name,
        "scenario_family": scenario.family,
        "components": "|".join(sorted(components)),
        "confluence_count": str(len(components)),
        "source_score": str(source_score),
        "quality_score": str(score),
        "score_bucket": _score_bucket(score),
        "entry_price": _text(row, "entry_price"),
        "stop_price": _text(row, "stop_price"),
        "target_price": _text(row, "target_price"),
        "rr": _text(row, "rr"),
        "horizon": horizon_name,
        "horizon_minutes": str(minutes),
        "metric_unit": metric_unit,
        "outcome": outcome,
        "result": result_text,
        "mfe": _text(row, mfe_key),
        "mae": _text(row, mae_key),
        "completed": "1" if completed else "0",
        "h1_bias": _text(row, "h1_bias").upper(),
        "h4_bias": _text(row, "h4_bias").upper(),
        "reasons": f"{reasons} | scenario={scenario.name}".strip(" |"),
    }


def build_unified_rows(
    fx_rows: Sequence[dict[str, str]],
    ote_rows: Sequence[dict[str, str]],
) -> list[dict[str, str]]:
    output: dict[str, dict[str, str]] = {}
    for row in fx_rows:
        action = _text(row, "action").upper()
        source_id = _text(row, "observation_id")
        if action not in VALID_ACTIONS or not source_id or not _text(row, "signal_time"):
            continue
        components = fx_components(row)
        for scenario in fx_scenarios(row):
            for horizon in FX_HORIZONS:
                event = _event_row(
                    source="FX_RESEARCH",
                    source_id=source_id,
                    row=row,
                    scenario=scenario,
                    components=components,
                    horizon=horizon,
                    metric_unit="ATR",
                )
                output[event["event_id"]] = event

    for row in ote_rows:
        action = _text(row, "action").upper()
        source_id = _text(row, "signal_id")
        if action not in VALID_ACTIONS or not source_id or not _text(row, "signal_time"):
            continue
        components = ote_components(row)
        for scenario in ote_scenarios(row):
            for horizon in OTE_HORIZONS:
                event = _event_row(
                    source="SMC_OTE",
                    source_id=source_id,
                    row=row,
                    scenario=scenario,
                    components=components,
                    horizon=horizon,
                    metric_unit="R",
                )
                output[event["event_id"]] = event
    return sorted(output.values(), key=lambda item: (item["signal_time"], item["event_id"]))


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("signal_time must contain timezone information")
    return parsed


def _non_overlapping(rows: Sequence[dict[str, str]], horizon_minutes: int) -> list[dict[str, str]]:
    selected: list[dict[str, str]] = []
    next_allowed: datetime | None = None
    for row in sorted(rows, key=lambda item: item["signal_time"]):
        signal_time = _parse_time(row["signal_time"])
        if next_allowed is not None and signal_time < next_allowed:
            continue
        selected.append(row)
        next_allowed = signal_time + timedelta(minutes=horizon_minutes)
    return selected


def _metrics(values: Sequence[float]) -> tuple[float, float, float, int]:
    if not values:
        return 0.0, 0.0, 0.0, 0
    wins = sum(value > 0 for value in values)
    positive = sum(value for value in values if value > 0)
    negative = abs(sum(value for value in values if value < 0))
    profit_factor = positive / negative if negative else math.inf if positive else 0.0
    equity = 0.0
    peak = 0.0
    drawdown = 0.0
    streak = 0
    max_streak = 0
    for value in values:
        equity += value
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
        if value <= 0:
            streak += 1
            max_streak = max(max_streak, streak)
        else:
            streak = 0
    return wins / len(values), profit_factor, drawdown, max_streak


def _state_status(
    completed: int,
    trading_days: int,
    profit_factor: float,
    average: float,
    early: float,
    late: float,
    drawdown: float,
) -> tuple[str, str]:
    if completed < 30 or trading_days < 5:
        return "INSUFFICIENT_SAMPLE", "need >=30 completed signals and >=5 trading days"
    stable = average > 0 and early > 0 and late > 0 and profit_factor >= 1.2 and drawdown <= 20
    if completed >= 100 and trading_days >= 15 and stable and profit_factor >= 1.3 and average >= 0.05:
        return "VALIDATED", "sample, both halves, PF, average and drawdown passed"
    if stable:
        return "CANDIDATE", "positive but requires a larger forward sample"
    return "UNSTABLE", "edge or stability requirements failed"


def build_states(rows: Sequence[dict[str, str]], captured_at: datetime) -> list[dict[str, str]]:
    groups: dict[tuple[str, ...], list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        score = _int(row, "quality_score")
        for score_filter, threshold in SCORE_FILTERS:
            if score >= threshold:
                key = (
                    row["source"],
                    row["symbol"],
                    row["action"],
                    row["scenario"],
                    row["scenario_family"],
                    score_filter,
                    row["horizon"],
                    row["horizon_minutes"],
                    row["metric_unit"],
                )
                groups[key].append(row)

    states: list[dict[str, str]] = []
    for key, group in sorted(groups.items()):
        source, symbol, action, scenario, family, score_filter, horizon, minutes, unit = key
        selected = _non_overlapping(group, int(minutes))
        completed_rows = [row for row in selected if row["completed"] == "1"]
        values = [_float(row, "result") for row in completed_rows]
        trading_days = len({_parse_time(row["signal_time"]).date() for row in completed_rows})
        win_rate, profit_factor, drawdown, max_streak = _metrics(values)
        midpoint = len(values) // 2
        early_values = values[:midpoint]
        late_values = values[midpoint:]
        average = sum(values) / len(values) if values else 0.0
        early = sum(early_values) / len(early_values) if early_values else 0.0
        late = sum(late_values) / len(late_values) if late_values else 0.0
        status, reasons = _state_status(
            len(values), trading_days, profit_factor, average, early, late, drawdown
        )
        states.append(
            {
                "captured_at": captured_at.isoformat(),
                "source": source,
                "symbol": symbol,
                "action": action,
                "scenario": scenario,
                "scenario_family": family,
                "score_filter": score_filter,
                "horizon": horizon,
                "horizon_minutes": minutes,
                "metric_unit": unit,
                "signals": str(len(selected)),
                "completed": str(len(values)),
                "trading_days": str(trading_days),
                "status": status,
                "win_rate": _number(win_rate),
                "profit_factor": _number(profit_factor),
                "avg_result": _number(average),
                "early_avg_result": _number(early),
                "late_avg_result": _number(late),
                "max_drawdown": _number(drawdown),
                "max_loss_streak": str(max_streak),
                "reasons": reasons,
            }
        )
    return states


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


def render_dashboard(rows: Sequence[dict[str, str]], states: Sequence[dict[str, str]]) -> str:
    unique_signals = {row["signal_key"] for row in rows}
    scenarios = {row["scenario"] for row in rows}
    completed = sum(row["completed"] == "1" for row in rows)
    status_counts: dict[str, int] = defaultdict(int)
    for state in states:
        status_counts[state["status"]] += 1

    verdict = (
        "Есть подтверждённые сценарии. Их всё равно держим в теневом режиме до отдельного forward-gate."
        if status_counts.get("VALIDATED", 0)
        else "Подтверждённых сценариев пока нет. Центр честно сравнивает все модели и копит выборку."
    )
    cards = "".join(
        f"<article><span>{html.escape(label)}</span><b>{value}</b></article>"
        for label, value in (
            ("Уникальных сигналов", len(unique_signals)),
            ("Сценариев", len(scenarios)),
            ("Строк горизонтов", len(rows)),
            ("Завершено", completed),
            ("Подтверждено", status_counts.get("VALIDATED", 0)),
            ("Кандидаты", status_counts.get("CANDIDATE", 0)),
            ("Мало данных", status_counts.get("INSUFFICIENT_SAMPLE", 0)),
            ("Нестабильно", status_counts.get("UNSTABLE", 0)),
        )
    )

    status_rank = {"VALIDATED": 4, "CANDIDATE": 3, "INSUFFICIENT_SAMPLE": 2, "UNSTABLE": 1}
    top = sorted(
        states,
        key=lambda row: (
            status_rank.get(row["status"], 0),
            _float(row, "avg_result"),
            _float(row, "profit_factor"),
            _int(row, "completed"),
        ),
        reverse=True,
    )[:40]
    top_rows = "".join(
        "<tr>"
        f"<td>{html.escape(row['source'])}</td><td>{html.escape(row['symbol'])}</td>"
        f"<td>{html.escape(row['action'])}</td><td>{html.escape(row['scenario'])}</td>"
        f"<td>{html.escape(row['score_filter'])}</td><td>{html.escape(row['horizon'])}</td>"
        f"<td>{html.escape(row['metric_unit'])}</td><td>{html.escape(row['status'])}</td>"
        f"<td>{_int(row, 'completed')}</td><td>{_float(row, 'win_rate') * 100:.1f}%</td>"
        f"<td>{_float(row, 'profit_factor'):.2f}</td><td>{_float(row, 'avg_result'):.3f}</td>"
        "</tr>"
        for row in top
    ) or '<tr><td colspan="12">Пока нет сравнений</td></tr>'

    latest_by_key: dict[str, dict[str, str]] = {}
    for row in rows:
        current = latest_by_key.get(row["signal_key"])
        if current is None or int(row["horizon_minutes"]) < int(current["horizon_minutes"]):
            latest_by_key[row["signal_key"]] = row
    recent = sorted(latest_by_key.values(), key=lambda row: row["signal_time"], reverse=True)[:40]
    recent_rows = "".join(
        "<tr>"
        f"<td>{html.escape(row['signal_time'][:19])}</td><td>{html.escape(row['source'])}</td>"
        f"<td>{html.escape(row['symbol'])}</td><td>{html.escape(row['action'])}</td>"
        f"<td>{html.escape(row['scenario'])}</td><td>{_int(row, 'quality_score')}</td>"
        f"<td>{html.escape(row['session'])}</td><td>{html.escape(row['components'])}</td>"
        "</tr>"
        for row in recent
    ) or '<tr><td colspan="8">Сигналов пока нет</td></tr>'

    return f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>TradeMind v1.6 Unified Signal Center</title><style>
:root{{--bg:#080d17;--panel:#111b2b;--line:#263c5d;--text:#edf6ff;--muted:#91a8c4;--accent:#5ee0b7;--gold:#ffd166}}
*{{box-sizing:border-box}}body{{margin:0;background:radial-gradient(circle at top,#13233b,#080d17 55%);color:var(--text);font:15px/1.45 system-ui,sans-serif}}
main{{max-width:1580px;margin:auto;padding:30px}}h1{{font-size:clamp(34px,5vw,64px);margin:0}}.lead{{color:var(--muted);max-width:950px}}
.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;margin:26px 0}}
article,section,.verdict{{background:#111b2be6;border:1px solid var(--line);border-radius:18px;padding:18px}}
article span{{display:block;color:var(--muted)}}article b{{font-size:34px}}.verdict{{border-left:5px solid var(--gold);margin:22px 0}}
section{{margin:24px 0;overflow:auto}}table{{width:100%;border-collapse:collapse;min-width:1150px}}
th,td{{padding:10px;border-bottom:1px solid #263c5d;text-align:left;vertical-align:top}}th{{color:var(--accent);position:sticky;top:0;background:#111b2b}}
code{{color:var(--gold)}}</style></head><body><main>
<h1>Unified Signal Center</h1>
<p class="lead">Единый теневой центр всех зашитых сценариев: BOS, CHoCH, break, sweep, FVG, объём, микроструктура, спред, OTE 61,8/70,5/79 и их комбинации. Метрики ATR и R никогда не смешиваются. Ордеров нет.</p>
<div class="cards">{cards}</div><div class="verdict"><b>Коммерческий вердикт:</b> {html.escape(verdict)}</div>
<section><h2>Лучшие текущие доказательства</h2><table><thead><tr><th>Источник</th><th>Инструмент</th><th>Сторона</th><th>Сценарий</th><th>Score</th><th>Горизонт</th><th>Ед.</th><th>Статус</th><th>N</th><th>WR</th><th>PF</th><th>Avg</th></tr></thead><tbody>{top_rows}</tbody></table></section>
<section><h2>Последние сигналы и причины</h2><table><thead><tr><th>Время</th><th>Источник</th><th>Инструмент</th><th>Сторона</th><th>Сценарий</th><th>Quality</th><th>Сессия</th><th>Факторы</th></tr></thead><tbody>{recent_rows}</tbody></table></section>
</main></body></html>"""


def run_unified_center(
    fx_path: Path,
    ote_path: Path,
    signals_path: Path,
    states_path: Path,
    dashboard_path: Path,
) -> UnifiedSummary:
    fx_rows = _load_csv(fx_path)
    ote_rows = _load_csv(ote_path)
    rows = build_unified_rows(fx_rows, ote_rows)
    states = build_states(rows, datetime.now().astimezone())
    _atomic_csv(signals_path, SIGNAL_FIELDS, rows)
    _atomic_csv(states_path, STATE_FIELDS, states)
    _atomic_text(dashboard_path, render_dashboard(rows, states))
    return UnifiedSummary(
        fx_source_rows=len(fx_rows),
        ote_source_rows=len(ote_rows),
        signal_keys=len({row["signal_key"] for row in rows}),
        horizon_rows=len(rows),
        scenarios=len({row["scenario"] for row in rows}),
        completed_rows=sum(row["completed"] == "1" for row in rows),
        states=len(states),
        signals_path=signals_path,
        states_path=states_path,
        dashboard_path=dashboard_path,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Run read-only unified TradeMind signal center")
    parser.add_argument(
        "--fx-observations",
        type=Path,
        default=Path("data/fx_research_v1_4_2/observations.csv"),
    )
    parser.add_argument(
        "--ote-signals", type=Path, default=Path("data/smc_ote_v1_5/signals.csv")
    )
    parser.add_argument(
        "--signals", type=Path, default=Path("data/unified_signal_center_v1_6/signals.csv")
    )
    parser.add_argument(
        "--states", type=Path, default=Path("data/unified_signal_center_v1_6/latest.csv")
    )
    parser.add_argument(
        "--dashboard",
        type=Path,
        default=Path("data/unified_signal_center_v1_6/dashboard/index.html"),
    )
    args = parser.parse_args()

    fx_path = args.fx_observations.expanduser().resolve()
    ote_path = args.ote_signals.expanduser().resolve()
    for name, path in (("FX observations", fx_path), ("SMC OTE signals", ote_path)):
        if not path.is_file():
            print(f"{name} not found: {path}")
            return 1
    try:
        summary = run_unified_center(
            fx_path,
            ote_path,
            args.signals.expanduser().resolve(),
            args.states.expanduser().resolve(),
            args.dashboard.expanduser().resolve(),
        )
    except (OSError, TypeError, ValueError) as exc:
        print(f"Unified signal center failed: {exc}")
        return 1

    print("TradeMind v1.6 Unified Signal Center")
    print(f"FX source rows: {summary.fx_source_rows}")
    print(f"OTE source rows: {summary.ote_source_rows}")
    print(f"Unique signal scenarios: {summary.signal_keys}")
    print(f"Horizon rows: {summary.horizon_rows}")
    print(f"Scenario types: {summary.scenarios}")
    print(f"Completed rows: {summary.completed_rows}")
    print(f"Comparison states: {summary.states}")
    print(f"Signals: {summary.signals_path}")
    print(f"Latest: {summary.states_path}")
    print(f"Dashboard: {summary.dashboard_path}")
    print("No orders were sent.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
