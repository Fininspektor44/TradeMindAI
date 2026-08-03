"""Forward-only multi-timeframe Bybit shadow research gate.

The module consumes the public read-only Bybit M5 collector output, aggregates
closed bars to M15 and H1, scores H1 context + M15 confirmation + M5 trigger,
and keeps a persistent paper journal. It never imports trading APIs and never
sends orders.
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import math
import statistics
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

SCHEMA_VERSION = "1.10.0"
BAR_MS = 300_000
M15_MS = 900_000
H1_MS = 3_600_000
SCENARIO = "MTF_FLOW_ALIGNMENT"
SOURCE_ID = "BYBIT_LINEAR_SHADOW"
VALID_STATUSES = ("REJECTED", "WATCH", "CANDIDATE", "VALIDATED")

AGG_FIELDS = (
    "schema_version", "source_id", "symbol", "timeframe", "start_ms", "end_ms",
    "bar_count", "open", "high", "low", "close", "return_pct", "volume", "turnover",
    "trade_count", "buy_trade_count", "sell_trade_count", "delta_qty", "delta_turnover",
    "cvd_turnover", "largest_trade_turnover", "avg_trade_turnover", "trade_rate_per_sec",
    "spread_bps_avg", "spread_bps_max", "book_imbalance_5_avg", "book_imbalance_5_last",
    "book_imbalance_10_avg", "book_imbalance_10_last", "book_imbalance_50_avg",
    "book_imbalance_50_last", "open_interest_start", "open_interest_end",
    "open_interest_change_pct", "open_interest_value_end", "funding_rate_avg",
    "funding_rate_last", "basis_bps_avg", "basis_bps_last", "price_24h_pct_last",
    "turnover_24h_last", "received_at",
)

DECISION_FIELDS = (
    "schema_version", "captured_at", "decision_id", "signal_time", "start_ms", "end_ms",
    "source", "source_id", "symbol", "action", "scenario", "context_timeframe",
    "decision_timeframe", "trigger_timeframe", "gate_status", "quality_score", "eligible",
    "duplicate_wave", "components", "reasons", "entry_price", "stop_price", "target_price",
    "risk_pct", "rr", "horizon_bars", "h1_return_pct", "h1_delta_turnover",
    "h1_oi_change_pct", "m15_return_pct", "m15_delta_turnover", "m15_book_imbalance_10",
    "m15_oi_change_pct", "m5_delta_turnover", "m5_trade_count", "m5_book_imbalance_10",
    "m5_spread_bps", "m5_funding_rate", "m5_basis_bps", "orders_enabled",
)

PAPER_FIELDS = (
    "schema_version", "paper_signal_id", "activated_at", "updated_at", "signal_time",
    "start_ms", "end_ms", "source", "source_id", "symbol", "action", "scenario",
    "quality_score", "gate_status", "components", "entry_price", "stop_price",
    "target_price", "risk_pct", "rr", "horizon_bars", "outcome", "result_r", "mfe_r",
    "mae_r", "completed", "completion_reason", "orders_enabled",
)

STATE_FIELDS = (
    "schema_version", "captured_at", "source", "symbol", "action", "scenario", "status",
    "completed", "trading_days", "wins", "losses", "timeouts", "win_rate",
    "profit_factor", "avg_result_r", "ci95_low", "early_avg_result_r", "late_avg_result_r",
    "max_drawdown_r", "max_loss_streak", "reasons",
)


def _float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _iso_from_ms(value: int) -> str:
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc).isoformat()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _load_csv(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [
            {str(key): str(value or "").strip() for key, value in dict(row).items()}
            for row in csv.DictReader(handle)
        ]


def _atomic_csv(path: Path, fields: Iterable[str], rows: Sequence[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.tmp")
    with temp.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=tuple(fields), extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    temp.replace(path)


def _atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.tmp")
    temp.write_text(text, encoding="utf-8")
    temp.replace(path)


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    _atomic_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def _mean(rows: Sequence[dict[str, Any]], key: str) -> float:
    values = [_float(row.get(key)) for row in rows]
    return sum(values) / len(values) if values else 0.0


def _sum(rows: Sequence[dict[str, Any]], key: str) -> float:
    return sum(_float(row.get(key)) for row in rows)


def _median(values: Iterable[float], default: float = 0.0) -> float:
    items = [float(value) for value in values if math.isfinite(float(value))]
    return statistics.median(items) if items else default


def _sign(value: float, epsilon: float = 0.0) -> int:
    if value > epsilon:
        return 1
    if value < -epsilon:
        return -1
    return 0


def _normalized_bars(rows: Sequence[dict[str, str]]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    for raw in rows:
        symbol = str(raw.get("symbol") or "").strip().upper()
        start_ms = _int(raw.get("start_ms"))
        if not symbol or start_ms <= 0 or (symbol, start_ms) in seen:
            continue
        seen.add((symbol, start_ms))
        row: dict[str, Any] = dict(raw)
        row["symbol"] = symbol
        row["start_ms"] = start_ms
        row["end_ms"] = _int(raw.get("end_ms"), start_ms + BAR_MS - 1)
        for key in (
            "open", "high", "low", "close", "volume", "turnover", "trade_count",
            "buy_trade_count", "sell_trade_count", "delta_qty", "delta_turnover",
            "cvd_turnover", "largest_trade_turnover", "avg_trade_turnover",
            "trade_rate_per_sec", "spread_bps", "book_imbalance_5", "book_imbalance_10",
            "book_imbalance_50", "open_interest", "open_interest_value", "funding_rate",
            "basis_bps", "price_24h_pct", "turnover_24h",
        ):
            row[key] = _float(raw.get(key))
        output.append(row)
    return sorted(output, key=lambda row: (row["symbol"], row["start_ms"]))


def aggregate_bars(
    rows: Sequence[dict[str, Any]], interval_ms: int, timeframe: str
) -> list[dict[str, Any]]:
    if interval_ms % BAR_MS:
        raise ValueError("interval_ms must be a multiple of M5")
    expected = interval_ms // BAR_MS
    groups: dict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        bucket = _int(row["start_ms"]) // interval_ms * interval_ms
        groups[(str(row["symbol"]), bucket)].append(row)

    output: list[dict[str, Any]] = []
    for (symbol, bucket), group in sorted(groups.items()):
        group = sorted(group, key=lambda row: _int(row["start_ms"]))
        starts = [_int(row["start_ms"]) for row in group]
        required = [bucket + BAR_MS * index for index in range(expected)]
        if starts != required:
            continue
        first, last = group[0], group[-1]
        opening = _float(first.get("open"))
        close = _float(last.get("close"))
        oi_start = _float(first.get("open_interest"))
        oi_end = _float(last.get("open_interest"))
        output.append(
            {
                "schema_version": SCHEMA_VERSION,
                "source_id": SOURCE_ID,
                "symbol": symbol,
                "timeframe": timeframe,
                "start_ms": bucket,
                "end_ms": bucket + interval_ms - 1,
                "bar_count": len(group),
                "open": opening,
                "high": max(_float(row.get("high")) for row in group),
                "low": min(_float(row.get("low")) for row in group),
                "close": close,
                "return_pct": ((close / opening) - 1.0) if opening else 0.0,
                "volume": _sum(group, "volume"),
                "turnover": _sum(group, "turnover"),
                "trade_count": int(_sum(group, "trade_count")),
                "buy_trade_count": int(_sum(group, "buy_trade_count")),
                "sell_trade_count": int(_sum(group, "sell_trade_count")),
                "delta_qty": _sum(group, "delta_qty"),
                "delta_turnover": _sum(group, "delta_turnover"),
                "cvd_turnover": _float(last.get("cvd_turnover")),
                "largest_trade_turnover": max(
                    _float(row.get("largest_trade_turnover")) for row in group
                ),
                "avg_trade_turnover": _mean(group, "avg_trade_turnover"),
                "trade_rate_per_sec": _mean(group, "trade_rate_per_sec"),
                "spread_bps_avg": _mean(group, "spread_bps"),
                "spread_bps_max": max(_float(row.get("spread_bps")) for row in group),
                "book_imbalance_5_avg": _mean(group, "book_imbalance_5"),
                "book_imbalance_5_last": _float(last.get("book_imbalance_5")),
                "book_imbalance_10_avg": _mean(group, "book_imbalance_10"),
                "book_imbalance_10_last": _float(last.get("book_imbalance_10")),
                "book_imbalance_50_avg": _mean(group, "book_imbalance_50"),
                "book_imbalance_50_last": _float(last.get("book_imbalance_50")),
                "open_interest_start": oi_start,
                "open_interest_end": oi_end,
                "open_interest_change_pct": ((oi_end / oi_start) - 1.0) if oi_start else 0.0,
                "open_interest_value_end": _float(last.get("open_interest_value")),
                "funding_rate_avg": _mean(group, "funding_rate"),
                "funding_rate_last": _float(last.get("funding_rate")),
                "basis_bps_avg": _mean(group, "basis_bps"),
                "basis_bps_last": _float(last.get("basis_bps")),
                "price_24h_pct_last": _float(last.get("price_24h_pct")),
                "turnover_24h_last": _float(last.get("turnover_24h")),
                "received_at": str(last.get("received_at") or ""),
            }
        )
    return output


def _atr_pct(rows: Sequence[dict[str, Any]], lookback: int = 6) -> float:
    sample = list(rows[-lookback:])
    if not sample:
        return 0.0
    ranges: list[float] = []
    previous_close = 0.0
    for row in sample:
        high = _float(row.get("high"))
        low = _float(row.get("low"))
        true_range = high - low
        if previous_close:
            true_range = max(true_range, abs(high - previous_close), abs(low - previous_close))
        ranges.append(true_range)
        previous_close = _float(row.get("close"))
    close = _float(sample[-1].get("close"))
    return (sum(ranges) / len(ranges) / close) if close else 0.0


def _score_decision(
    m5_history: Sequence[dict[str, Any]],
    m15_history: Sequence[dict[str, Any]],
    h1_history: Sequence[dict[str, Any]],
    captured_at: datetime,
) -> dict[str, Any]:
    trigger = m5_history[-1]
    symbol = str(trigger["symbol"])
    decision_id = f"{symbol}:{_int(trigger['start_ms'])}:{SCENARIO}"
    base = {
        "schema_version": SCHEMA_VERSION,
        "captured_at": captured_at.isoformat(),
        "decision_id": decision_id,
        "signal_time": _iso_from_ms(_int(trigger["end_ms"])),
        "start_ms": _int(trigger["start_ms"]),
        "end_ms": _int(trigger["end_ms"]),
        "source": "BYBIT",
        "source_id": SOURCE_ID,
        "symbol": symbol,
        "action": "NONE",
        "scenario": SCENARIO,
        "context_timeframe": "H1",
        "decision_timeframe": "M15",
        "trigger_timeframe": "M5",
        "gate_status": "WATCH",
        "quality_score": 0,
        "eligible": 0,
        "duplicate_wave": 0,
        "components": "",
        "reasons": "",
        "entry_price": _float(trigger.get("close")),
        "stop_price": 0.0,
        "target_price": 0.0,
        "risk_pct": 0.0,
        "rr": 1.5,
        "horizon_bars": 12,
        "orders_enabled": 0,
    }
    if len(m5_history) < 12 or len(m15_history) < 6 or len(h1_history) < 3:
        base["reasons"] = (
            f"need history M5>=12 M15>=6 H1>=3; got "
            f"{len(m5_history)}/{len(m15_history)}/{len(h1_history)}"
        )
        return base

    h1 = h1_history[-3:]
    m15 = m15_history[-1]
    h1_return = (
        (_float(h1[-1]["close"]) / _float(h1[0]["open"]) - 1.0)
        if _float(h1[0]["open"])
        else 0.0
    )
    h1_delta = _sum(h1, "delta_turnover")
    h1_oi = (
        (_float(h1[-1]["open_interest_end"]) / _float(h1[0]["open_interest_start"]) - 1.0)
        if _float(h1[0]["open_interest_start"])
        else 0.0
    )
    m15_return = _float(m15.get("return_pct"))
    m15_delta = _float(m15.get("delta_turnover"))
    m15_book = _float(m15.get("book_imbalance_10_avg"))
    m15_oi = _float(m15.get("open_interest_change_pct"))
    m5_delta = _float(trigger.get("delta_turnover"))
    m5_book = _float(trigger.get("book_imbalance_10"))
    m5_spread = _float(trigger.get("spread_bps"))
    funding = _float(trigger.get("funding_rate"))
    basis = _float(trigger.get("basis_bps"))

    h1_direction = _sign(h1_return)
    m15_direction = _sign(m15_return)
    direction = h1_direction if h1_direction and h1_direction == m15_direction else 0
    components: list[str] = []
    reasons: list[str] = []
    score = 0

    if direction:
        score += 18
        components.append("H1_PRICE_TREND")
    else:
        reasons.append("H1 and M15 price direction are not aligned")

    def aligned(value: float, points: int, name: str, epsilon: float = 0.0) -> None:
        nonlocal score
        if direction and _sign(value, epsilon) == direction:
            score += points
            components.append(name)
        else:
            reasons.append(f"{name} not aligned")

    aligned(h1_delta, 10, "H1_DELTA")
    aligned(h1_oi, 8, "H1_OI")
    aligned(m15_return, 10, "M15_PRICE")
    aligned(m15_delta, 12, "M15_DELTA")
    aligned(m15_book, 8, "M15_BOOK", 0.02)
    aligned(m15_oi, 8, "M15_OI")

    recent_m5 = list(m5_history[-12:])
    median_delta = _median(
        abs(_float(row.get("delta_turnover"))) for row in recent_m5[:-1]
    )
    delta_impulse = abs(m5_delta) >= max(median_delta * 1.15, 1e-9)
    if direction and _sign(m5_delta) == direction and delta_impulse:
        score += 10
        components.append("M5_DELTA_IMPULSE")
    else:
        reasons.append("M5 delta impulse missing or opposite")

    median_trades = _median(
        (_float(row.get("trade_count")) for row in recent_m5[:-1]), 1.0
    )
    if _float(trigger.get("trade_count")) >= median_trades * 1.10:
        score += 6
        components.append("M5_TRADE_INTENSITY")
    else:
        reasons.append("M5 trade count below impulse threshold")

    if direction and _sign(m5_book, 0.05) == direction:
        score += 5
        components.append("M5_BOOK")
    else:
        reasons.append("M5 book imbalance weak or opposite")

    median_spread = _median(
        (_float(row.get("spread_bps")) for row in recent_m5[:-1]), m5_spread
    )
    if m5_spread <= max(median_spread * 1.5, 0.25):
        score += 3
        components.append("SPREAD_OK")
    else:
        score -= 6
        reasons.append("spread expanded")

    median_largest = _median(
        _float(row.get("largest_trade_turnover")) for row in recent_m5[:-1]
    )
    if _float(trigger.get("largest_trade_turnover")) >= max(median_largest * 1.15, 1e-9):
        score += 4
        components.append("LARGE_TRADE_IMPULSE")

    crowded = direction and _sign(funding) == direction and abs(funding) >= 0.0005
    if crowded:
        score -= 5
        reasons.append("funding crowding against fresh entry")
    else:
        score += 2
        components.append("FUNDING_OK")
    if direction and _sign(basis) == direction and abs(basis) > 20:
        score -= 3
        reasons.append("basis crowding")
    else:
        score += 2
        components.append("BASIS_OK")

    score = max(0, min(100, score))
    essential = (
        direction != 0
        and _sign(m15_delta) == direction
        and _sign(m5_delta) == direction
        and delta_impulse
    )
    if essential and score >= 70:
        status = "CANDIDATE"
    elif direction and score >= 55:
        status = "WATCH"
    else:
        status = "REJECTED"

    entry = _float(trigger.get("close"))
    risk_pct = max(0.0025, min(0.025, _atr_pct(m15_history) * 0.75))
    action = "BUY" if direction > 0 else "SELL" if direction < 0 else "NONE"
    if action == "BUY":
        stop = entry * (1.0 - risk_pct)
        target = entry * (1.0 + risk_pct * 1.5)
    elif action == "SELL":
        stop = entry * (1.0 + risk_pct)
        target = entry * (1.0 - risk_pct * 1.5)
    else:
        stop = target = 0.0

    base.update(
        {
            "action": action,
            "gate_status": status,
            "quality_score": score,
            "eligible": 1 if status == "CANDIDATE" else 0,
            "components": "|".join(components),
            "reasons": " | ".join(reasons),
            "entry_price": entry,
            "stop_price": stop,
            "target_price": target,
            "risk_pct": risk_pct,
            "h1_return_pct": h1_return,
            "h1_delta_turnover": h1_delta,
            "h1_oi_change_pct": h1_oi,
            "m15_return_pct": m15_return,
            "m15_delta_turnover": m15_delta,
            "m15_book_imbalance_10": m15_book,
            "m15_oi_change_pct": m15_oi,
            "m5_delta_turnover": m5_delta,
            "m5_trade_count": _int(trigger.get("trade_count")),
            "m5_book_imbalance_10": m5_book,
            "m5_spread_bps": m5_spread,
            "m5_funding_rate": funding,
            "m5_basis_bps": basis,
        }
    )
    return base


def _load_or_create_start(path: Path, now: datetime) -> int:
    if path.is_file():
        payload = json.loads(path.read_text(encoding="utf-8"))
        return _int(payload.get("started_at_ms"))
    started = int(now.timestamp() * 1000)
    _atomic_json(
        path,
        {
            "schema_version": SCHEMA_VERSION,
            "started_at": now.isoformat(),
            "started_at_ms": started,
            "forward_only": True,
        },
    )
    return started


def _mark_duplicate_candidates(
    decisions: list[dict[str, Any]], existing: Sequence[dict[str, str]]
) -> None:
    last_signal: dict[tuple[str, str], int] = {}
    for row in existing:
        key = (str(row.get("symbol") or ""), str(row.get("action") or ""))
        last_signal[key] = max(last_signal.get(key, 0), _int(row.get("start_ms")))
    for row in sorted(decisions, key=lambda item: _int(item["start_ms"])):
        if _int(row.get("eligible")) != 1:
            continue
        key = (str(row["symbol"]), str(row["action"]))
        previous = last_signal.get(key, 0)
        if previous and _int(row["start_ms"]) - previous < 30 * 60 * 1000:
            row["gate_status"] = "REJECTED"
            row["eligible"] = 0
            row["duplicate_wave"] = 1
            row["reasons"] = " | ".join(
                part
                for part in (
                    str(row.get("reasons") or ""),
                    "duplicate inside 30-minute wave",
                )
                if part
            )
        else:
            last_signal[key] = _int(row["start_ms"])


def _new_paper_row(decision: dict[str, Any], captured_at: datetime) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "paper_signal_id": str(decision["decision_id"]),
        "activated_at": captured_at.isoformat(),
        "updated_at": captured_at.isoformat(),
        "signal_time": decision["signal_time"],
        "start_ms": decision["start_ms"],
        "end_ms": decision["end_ms"],
        "source": decision["source"],
        "source_id": decision["source_id"],
        "symbol": decision["symbol"],
        "action": decision["action"],
        "scenario": decision["scenario"],
        "quality_score": decision["quality_score"],
        "gate_status": decision["gate_status"],
        "components": decision["components"],
        "entry_price": decision["entry_price"],
        "stop_price": decision["stop_price"],
        "target_price": decision["target_price"],
        "risk_pct": decision["risk_pct"],
        "rr": decision["rr"],
        "horizon_bars": decision["horizon_bars"],
        "outcome": "OPEN",
        "result_r": "",
        "mfe_r": "",
        "mae_r": "",
        "completed": 0,
        "completion_reason": "",
        "orders_enabled": 0,
    }


def _update_outcome(
    signal: dict[str, Any], bars: Sequence[dict[str, Any]], captured_at: datetime
) -> dict[str, Any]:
    if _int(signal.get("completed")) == 1:
        return signal
    entry = _float(signal.get("entry_price"))
    stop = _float(signal.get("stop_price"))
    target = _float(signal.get("target_price"))
    risk = abs(entry - stop)
    if not entry or not risk:
        return signal
    direction = 1 if str(signal.get("action")) == "BUY" else -1
    future = [
        row
        for row in bars
        if str(row["symbol"]) == str(signal["symbol"])
        and _int(row["start_ms"]) > _int(signal["start_ms"])
    ]
    horizon = _int(signal.get("horizon_bars"), 12)
    sample = future[:horizon]
    if not sample:
        return signal

    best_r = -math.inf
    worst_r = math.inf
    outcome = "OPEN"
    result_r: float | None = None
    reason = ""
    for row in sample:
        high = _float(row.get("high"))
        low = _float(row.get("low"))
        favorable = (high - entry) / risk if direction > 0 else (entry - low) / risk
        adverse = (low - entry) / risk if direction > 0 else (entry - high) / risk
        best_r = max(best_r, favorable)
        worst_r = min(worst_r, adverse)
        stop_hit = low <= stop if direction > 0 else high >= stop
        target_hit = high >= target if direction > 0 else low <= target
        if stop_hit and target_hit:
            outcome, result_r, reason = "LOSS", -1.0, "STOP_FIRST_CONSERVATIVE"
            break
        if stop_hit:
            outcome, result_r, reason = "LOSS", -1.0, "STOP"
            break
        if target_hit:
            outcome, result_r, reason = "WIN", _float(signal.get("rr"), 1.5), "TARGET"
            break

    completed = outcome != "OPEN"
    if not completed and len(future) >= horizon:
        last_close = _float(sample[-1].get("close"))
        result_r = direction * (last_close - entry) / risk
        outcome = "TIMEOUT"
        reason = "HORIZON"
        completed = True
    signal = dict(signal)
    signal["updated_at"] = captured_at.isoformat()
    signal["mfe_r"] = "" if best_r == -math.inf else round(best_r, 6)
    signal["mae_r"] = "" if worst_r == math.inf else round(worst_r, 6)
    if completed and result_r is not None:
        signal["outcome"] = outcome
        signal["result_r"] = round(result_r, 6)
        signal["completed"] = 1
        signal["completion_reason"] = reason
    return signal


def _max_drawdown(values: Sequence[float]) -> float:
    equity = peak = drawdown = 0.0
    for value in values:
        equity += value
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    return drawdown


def _max_loss_streak(values: Sequence[float]) -> int:
    best = current = 0
    for value in values:
        if value < 0:
            current += 1
            best = max(best, current)
        else:
            current = 0
    return best


def build_states(
    journal: Sequence[dict[str, Any]], captured_at: datetime
) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in journal:
        if _int(row.get("completed")) == 1:
            groups[(str(row["symbol"]), str(row["action"]))].append(row)
    states: list[dict[str, Any]] = []
    for (symbol, action), rows in sorted(groups.items()):
        rows = sorted(rows, key=lambda row: _int(row["start_ms"]))
        results = [_float(row.get("result_r")) for row in rows]
        completed = len(results)
        wins = sum(value > 0 for value in results)
        losses = sum(value < 0 for value in results)
        timeouts = sum(str(row.get("outcome")) == "TIMEOUT" for row in rows)
        gains = sum(value for value in results if value > 0)
        gross_loss = -sum(value for value in results if value < 0)
        pf = gains / gross_loss if gross_loss else (999.0 if gains else 0.0)
        average = statistics.fmean(results) if results else 0.0
        stdev = statistics.stdev(results) if len(results) >= 2 else 0.0
        ci_low = average - 1.96 * stdev / math.sqrt(completed) if completed else 0.0
        split = max(1, completed // 2)
        early = statistics.fmean(results[:split]) if results[:split] else 0.0
        late = statistics.fmean(results[split:]) if results[split:] else early
        days = len({_iso_from_ms(_int(row["start_ms"]))[:10] for row in rows})
        drawdown = _max_drawdown(results)
        streak = _max_loss_streak(results)
        no_late_collapse = late > 0 and (early <= 0 or late >= early * 0.5)
        validated = (
            completed >= 300
            and days >= 30
            and ci_low > 0
            and no_late_collapse
            and drawdown <= 20
            and streak <= 8
        )
        candidate = (
            completed >= 50
            and days >= 5
            and average > 0
            and pf >= 1.15
            and late > 0
            and drawdown <= 25
            and streak <= 10
        )
        if validated:
            status = "VALIDATED"
            reason = "300+ forward trades, positive CI95 lower bound, stable late half"
        elif candidate:
            status = "CANDIDATE"
            reason = "positive forward evidence; 300 trades and 30 days still required"
        elif completed >= 50:
            status = "REJECTED"
            reason = "forward evidence failed expectancy or stability limits"
        else:
            status = "WATCH"
            reason = "need at least 50 completed forward observations"
        states.append(
            {
                "schema_version": SCHEMA_VERSION,
                "captured_at": captured_at.isoformat(),
                "source": "BYBIT",
                "symbol": symbol,
                "action": action,
                "scenario": SCENARIO,
                "status": status,
                "completed": completed,
                "trading_days": days,
                "wins": wins,
                "losses": losses,
                "timeouts": timeouts,
                "win_rate": wins / completed if completed else 0.0,
                "profit_factor": pf,
                "avg_result_r": average,
                "ci95_low": ci_low,
                "early_avg_result_r": early,
                "late_avg_result_r": late,
                "max_drawdown_r": drawdown,
                "max_loss_streak": streak,
                "reasons": reason,
            }
        )
    return states


def _render_dashboard(
    decisions: Sequence[dict[str, Any]],
    journal: Sequence[dict[str, Any]],
    states: Sequence[dict[str, Any]],
    started_at_ms: int,
) -> str:
    counts = {
        status: sum(str(row.get("gate_status")) == status for row in decisions)
        for status in VALID_STATUSES
    }
    completed = sum(_int(row.get("completed")) == 1 for row in journal)
    recent = sorted(journal, key=lambda row: _int(row.get("start_ms")), reverse=True)[:50]
    rows = "".join(
        "<tr>"
        f"<td>{html.escape(str(row.get('signal_time', ''))[:19])}</td>"
        f"<td>{html.escape(str(row.get('symbol', '')))}</td>"
        f"<td>{html.escape(str(row.get('action', '')))}</td>"
        f"<td>{html.escape(str(row.get('quality_score', '')))}</td>"
        f"<td>{html.escape(str(row.get('outcome', '')))}</td>"
        f"<td>{html.escape(str(row.get('result_r', '')))}</td>"
        f"<td>{html.escape(str(row.get('mfe_r', '')))}</td>"
        f"<td>{html.escape(str(row.get('mae_r', '')))}</td>"
        "</tr>"
        for row in recent
    ) or (
        '<tr><td colspan="8">Forward-сигналов пока нет. '
        "Shadow Gate ждёт новые M5-свечи.</td></tr>"
    )
    state_rows = "".join(
        "<tr>"
        f"<td>{html.escape(str(row['symbol']))}</td>"
        f"<td>{html.escape(str(row['action']))}</td>"
        f"<td>{html.escape(str(row['status']))}</td>"
        f"<td>{row['completed']}</td>"
        f"<td>{_float(row['win_rate']) * 100:.1f}%</td>"
        f"<td>{_float(row['profit_factor']):.2f}</td>"
        f"<td>{_float(row['avg_result_r']):.3f}</td>"
        f"<td>{_float(row['ci95_low']):.3f}</td>"
        "</tr>"
        for row in states
    ) or (
        '<tr><td colspan="8">Статистика появится после завершённых '
        "теневых сигналов.</td></tr>"
    )
    cards = "".join(
        f"<article><span>{label}</span><b>{value}</b></article>"
        for label, value in (
            ("Forward-сигналов", len(journal)),
            ("Завершено", completed),
            ("Candidate", counts["CANDIDATE"]),
            ("Watch", counts["WATCH"]),
            ("Rejected", counts["REJECTED"]),
            ("Orders", "OFF"),
        )
    )
    return f"""<!doctype html><html lang='ru'><head><meta charset='utf-8'>
<meta name='viewport' content='width=device-width,initial-scale=1'>
<title>TradeMind v1.10 Bybit Shadow Research</title><style>
body{{background:#061724;color:#eaf7ff;font-family:Arial;margin:28px}}h1{{font-size:42px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px}}
article{{background:#0b2a40;border:1px solid #1c5878;border-radius:16px;padding:16px}}
article span{{display:block;color:#9bc4da}}article b{{font-size:30px}}
table{{width:100%;border-collapse:collapse;margin-top:22px}}
th,td{{padding:9px;border-bottom:1px solid #17425b;text-align:left}}.ok{{color:#29e2a5}}
</style></head><body><h1>Bybit Shadow Research v1.10</h1>
<p class='ok'>Read-only. H1 context + M15 confirmation + M5 trigger. No orders.</p>
<p>Forward gate started: {html.escape(_iso_from_ms(started_at_ms))}</p>
<section class='grid'>{cards}</section><h2>Теневой журнал</h2>
<table><thead><tr><th>Время</th><th>Инструмент</th><th>Действие</th><th>Score</th>
<th>Исход</th><th>R</th><th>MFE</th><th>MAE</th></tr></thead><tbody>{rows}</tbody></table>
<h2>Форвард-статистика</h2><table><thead><tr><th>Инструмент</th><th>Действие</th>
<th>Статус</th><th>N</th><th>Win rate</th><th>PF</th><th>Avg R</th><th>CI95 low</th>
</tr></thead><tbody>{state_rows}</tbody></table></body></html>"""


@dataclass(frozen=True, slots=True)
class ShadowSummary:
    source_bars: int
    m15_bars: int
    h1_bars: int
    decisions: int
    candidates: int
    paper_signals: int
    completed: int
    output_dir: Path


def run_shadow_gate(
    bars_path: Path, output_dir: Path, now: datetime | None = None
) -> ShadowSummary:
    captured_at = now or _now()
    source = _normalized_bars(_load_csv(bars_path))
    if not source:
        raise ValueError(f"No Bybit M5 bars found: {bars_path}")
    output_dir.mkdir(parents=True, exist_ok=True)
    started_at_ms = _load_or_create_start(output_dir / "gate_meta.json", captured_at)
    m15 = aggregate_bars(source, M15_MS, "M15")
    h1 = aggregate_bars(source, H1_MS, "H1")

    by_symbol_m5: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_symbol_m15: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_symbol_h1: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in source:
        by_symbol_m5[str(row["symbol"])].append(row)
    for row in m15:
        by_symbol_m15[str(row["symbol"])].append(row)
    for row in h1:
        by_symbol_h1[str(row["symbol"])].append(row)

    decisions_path = output_dir / "decisions.csv"
    previous_decisions = _load_csv(decisions_path)
    known = {str(row.get("decision_id") or "") for row in previous_decisions}
    new_decisions: list[dict[str, Any]] = []
    for symbol, rows in sorted(by_symbol_m5.items()):
        for index, trigger in enumerate(rows):
            if _int(trigger["end_ms"]) < started_at_ms:
                continue
            decision_id = f"{symbol}:{_int(trigger['start_ms'])}:{SCENARIO}"
            if decision_id in known:
                continue
            m5_history = rows[: index + 1]
            m15_history = [
                row
                for row in by_symbol_m15[symbol]
                if _int(row["end_ms"]) <= _int(trigger["end_ms"])
            ]
            h1_history = [
                row
                for row in by_symbol_h1[symbol]
                if _int(row["end_ms"]) <= _int(trigger["end_ms"])
            ]
            new_decisions.append(
                _score_decision(m5_history, m15_history, h1_history, captured_at)
            )

    signals_path = output_dir / "signals.csv"
    existing_signals = _load_csv(signals_path)
    _mark_duplicate_candidates(new_decisions, existing_signals)
    decisions: list[dict[str, Any]] = [*previous_decisions, *new_decisions]
    decisions = sorted(
        decisions,
        key=lambda row: (_int(row.get("start_ms")), str(row.get("decision_id"))),
    )

    journal: dict[str, dict[str, Any]] = {
        str(row["paper_signal_id"]): dict(row)
        for row in existing_signals
        if row.get("paper_signal_id")
    }
    for decision in new_decisions:
        if _int(decision.get("eligible")) == 1:
            journal[str(decision["decision_id"])] = _new_paper_row(decision, captured_at)
    updated = [
        _update_outcome(row, by_symbol_m5.get(str(row["symbol"]), []), captured_at)
        for row in journal.values()
    ]
    updated = sorted(
        updated,
        key=lambda row: (_int(row.get("start_ms")), str(row.get("paper_signal_id"))),
    )
    states = build_states(updated, captured_at)

    _atomic_csv(output_dir / "aggregate_m15.csv", AGG_FIELDS, m15)
    _atomic_csv(output_dir / "aggregate_h1.csv", AGG_FIELDS, h1)
    _atomic_csv(decisions_path, DECISION_FIELDS, decisions)
    _atomic_csv(signals_path, PAPER_FIELDS, updated)
    _atomic_csv(output_dir / "states.csv", STATE_FIELDS, states)
    dashboard = output_dir / "dashboard" / "index.html"
    _atomic_text(dashboard, _render_dashboard(decisions, updated, states, started_at_ms))

    counts = {
        status: sum(str(row.get("gate_status")) == status for row in decisions)
        for status in VALID_STATUSES
    }
    completed = sum(_int(row.get("completed")) == 1 for row in updated)
    status = {
        "schema_version": SCHEMA_VERSION,
        "state": "OK",
        "updated_at": captured_at.isoformat(),
        "source_id": SOURCE_ID,
        "forward_only": True,
        "orders_enabled": False,
        "source_bars": len(source),
        "m15_bars": len(m15),
        "h1_bars": len(h1),
        "decisions": len(decisions),
        "new_decisions": len(new_decisions),
        "gate_counts": counts,
        "paper_signals": len(updated),
        "completed_paper_signals": completed,
        "bars_path": str(bars_path),
        "output_dir": str(output_dir),
        "dashboard": str(dashboard),
    }
    _atomic_json(output_dir / "status.json", status)
    return ShadowSummary(
        source_bars=len(source),
        m15_bars=len(m15),
        h1_bars=len(h1),
        decisions=len(decisions),
        candidates=counts["CANDIDATE"],
        paper_signals=len(updated),
        completed=completed,
        output_dir=output_dir,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="TradeMind v1.10 Bybit Shadow Research")
    parser.add_argument("--bars", type=Path, default=Path("data/bybit_v1_9/bybit_bars.csv"))
    parser.add_argument("--output-dir", type=Path, default=Path("data/bybit_shadow_v1_10"))
    parser.add_argument("--open-dashboard", action="store_true")
    args = parser.parse_args(argv)
    bars = args.bars.expanduser().resolve()
    output = args.output_dir.expanduser().resolve()
    try:
        summary = run_shadow_gate(bars, output)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"Bybit Shadow Research failed: {exc}")
        return 1
    print("TradeMind v1.10 Bybit Shadow Research")
    print(f"Source M5 bars: {summary.source_bars}")
    print(f"Aggregated M15/H1: {summary.m15_bars}/{summary.h1_bars}")
    print(f"Decisions: {summary.decisions}")
    print(f"Candidate decisions: {summary.candidates}")
    print(f"Forward paper signals: {summary.paper_signals}")
    print(f"Completed: {summary.completed}")
    print(f"Output: {summary.output_dir}")
    print("H1 context + M15 confirmation + M5 trigger.")
    print("No orders were sent.")
    if args.open_dashboard:
        import os

        os.startfile(output / "dashboard" / "index.html")  # type: ignore[attr-defined]
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
