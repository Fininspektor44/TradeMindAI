"""Read-only FX research stream combining SMC, sessions and tick microstructure."""

from __future__ import annotations

import argparse
import csv
import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from trademind.action_validation import (
    ActionPatternValidation,
    apply_benjamini_hochberg,
    feature_labels,
    validate_action_rows,
)
from trademind.market.models import Candle
from trademind.signals import SignalEngine
from trademind.structure import MarketStructureEngine, StructureObservation

SCHEMA_VERSION = "1.4.2"
FX_MAJORS = (
    "EURUSD",
    "GBPUSD",
    "USDJPY",
    "USDCHF",
    "USDCAD",
    "AUDUSD",
    "NZDUSD",
)

# The three frozen prospective-confirmation candidates (see
# trademind.mt5_prospective_monitor / trademind.prospective_confirmation):
# .USTECHCASH SELL H12, .US30CASH BULLISH_FVG H12, XAGUSD SELL BEARISH_FVG
# H12. Exact requested symbols only -- no alias/remapping is performed here
# or anywhere downstream; the live MT5 ECN exporter (mt5/TradeMindAI_ECN_
# UniversalVolumeExporter_v1_9_4.mq5, InpCanonicalSymbols) already lists
# these instruments, so this is purely a Python-side observation-collection
# gate being widened to match what the exporter already provides.
FROZEN_PROSPECTIVE_SYMBOLS = (
    ".USTECHCASH",
    ".US30CASH",
    "XAGUSD",
)
assert not set(FX_MAJORS) & set(FROZEN_PROSPECTIVE_SYMBOLS), (
    "FROZEN_PROSPECTIVE_SYMBOLS must stay disjoint from FX_MAJORS"
)

# The live symbol universe actually collected into observations.csv.
# Additive on top of FX_MAJORS: FX_MAJORS itself is never mutated (it keeps
# meaning exactly "the seven FX majors" wherever else it's read), and this
# is the only place the observation-collection gates below are widened.
LIVE_OBSERVATION_SYMBOLS = FX_MAJORS + FROZEN_PROSPECTIVE_SYMBOLS

HORIZONS = (3, 6, 12)
_VALID_OUTCOMES = {"WIN", "LOSS", "FLAT"}

_OBSERVATION_FIELDS = (
    "schema_version",
    "observation_id",
    "signal_time",
    "source_bar_time",
    "server_utc_offset_hours",
    "symbol",
    "timeframe",
    "session",
    "action",
    "score",
    "confidence",
    "entry_price",
    "bar_open",
    "bar_high",
    "bar_low",
    "ema_fast",
    "ema_slow",
    "rsi",
    "atr",
    "signal_reasons",
    "structure_version",
    "internal_bias",
    "internal_reference_high",
    "internal_reference_low",
    "internal_break",
    "swing_bias",
    "swing_reference_high",
    "swing_reference_low",
    "swing_break",
    "liquidity_reference_high",
    "liquidity_reference_low",
    "bsl_sweep",
    "ssl_sweep",
    "bsl_sweep_depth",
    "ssl_sweep_depth",
    "bsl_sweep_depth_atr",
    "ssl_sweep_depth_atr",
    "fvg_direction",
    "fvg_size",
    "fvg_size_atr",
    "structure_event_count",
    "bar_tick_volume",
    "tick_count",
    "tick_rate_per_sec",
    "tick_rate_ratio_20",
    "bid_up",
    "bid_down",
    "ask_up",
    "ask_down",
    "mid_up",
    "mid_down",
    "direction_imbalance",
    "delta_proxy",
    "rvol_20",
    "volume_ratio_20",
    "volume_percentile_100",
    "spread_mean_points",
    "spread_min_points",
    "spread_max_points",
    "spread_last_points",
    "spread_expansion_points",
    "spread_ratio_20",
    "point",
    "spread_cost",
    "spread_cost_atr",
    "realized_abs_move_points",
    "range_per_tick_points",
    "body_per_tick_points",
    "range_efficiency_ratio_20",
    "body_efficiency_ratio_20",
    "tick_copy_status",
    "labels",
    "exit_time_3",
    "exit_price_3",
    "net_move_3",
    "progress_atr_3",
    "mfe_atr_3",
    "mae_atr_3",
    "outcome_3",
    "exit_time_6",
    "exit_price_6",
    "net_move_6",
    "progress_atr_6",
    "mfe_atr_6",
    "mae_atr_6",
    "outcome_6",
    "exit_time_12",
    "exit_price_12",
    "net_move_12",
    "progress_atr_12",
    "mfe_atr_12",
    "mae_atr_12",
    "outcome_12",
)

_STATE_FIELDS = (
    "captured_at",
    "symbol",
    "label",
    "session",
    "action",
    "horizon",
    "observations",
    "trades",
    "trading_days",
    "status",
    "win_rate",
    "profit_factor_atr",
    "avg_net_atr",
    "early_avg_net_atr",
    "late_avg_net_atr",
    "late_to_early_ratio",
    "max_drawdown_atr",
    "max_loss_streak",
    "mean_ci_low",
    "mean_ci_high",
    "p_value",
    "q_value",
    "reasons",
)


@dataclass(frozen=True)
class FxResearchSummary:
    source_rows: int
    fx_rows: int
    observations: int
    completed_h3: int
    completed_h6: int
    completed_h12: int
    states: int
    observations_path: Path
    states_path: Path


def _number(value: float | int | None) -> str:
    if value is None:
        return ""
    number = float(value)
    if math.isinf(number):
        return "inf" if number > 0 else "-inf"
    if math.isnan(number):
        return "nan"
    return f"{number:.12g}"


def _integer(row: dict[str, str], key: str) -> int:
    return int(str(row.get(key, "0") or "0").strip())


def _float(row: dict[str, str], key: str) -> float:
    return float(str(row.get(key, "0") or "0").strip())


def _atomic_write(path: Path, fieldnames: tuple[str, ...], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(path)


def session_for_time(utc_time: datetime) -> str:
    """Classify a timezone-aware timestamp into a stable UTC research session."""
    if utc_time.tzinfo is None:
        raise ValueError("session timestamp must include timezone information")
    hour = utc_time.astimezone(timezone.utc).hour
    if 0 <= hour < 7:
        return "ASIA"
    if 7 <= hour < 12:
        return "LONDON"
    if 12 <= hour < 17:
        return "LONDON_NY_OVERLAP"
    if 17 <= hour < 21:
        return "NEW_YORK"
    return "OFF_HOURS"


def _utc_time(epoch_seconds: int, server_utc_offset_hours: int) -> datetime:
    server_clock = datetime.fromtimestamp(epoch_seconds, tz=timezone.utc)
    return server_clock - timedelta(hours=server_utc_offset_hours)


def load_volume_rows(path: Path) -> tuple[list[dict[str, str]], int]:
    """Load canonical v1.4 rows and keep only healthy M5 rows for the live
    observation symbol universe (FX majors plus the frozen prospective
    candidates; see ``LIVE_OBSERVATION_SYMBOLS``)."""
    rows: list[dict[str, str]] = []
    source_rows = 0
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        for raw in csv.DictReader(handle):
            source_rows += 1
            row = {key: str(value or "").strip() for key, value in dict(raw).items()}
            symbol = row.get("symbol", "").upper()
            if symbol not in LIVE_OBSERVATION_SYMBOLS:
                continue
            if row.get("timeframe", "").upper() != "M5":
                continue
            if row.get("tick_copy_status", "").upper() != "OK":
                continue
            try:
                if _integer(row, "time") <= 0 or _float(row, "point") <= 0:
                    continue
                if _integer(row, "tick_count") <= 0:
                    continue
                for key in ("open", "high", "low", "close"):
                    value = _float(row, key)
                    if not math.isfinite(value):
                        raise ValueError(key)
            except (TypeError, ValueError):
                continue
            row["symbol"] = symbol
            row["timeframe"] = "M5"
            rows.append(row)
    rows.sort(key=lambda item: (item["symbol"], _integer(item, "time")))
    return rows, source_rows


def observed_symbols(path: Path) -> set[str]:
    """Every distinct uppercased ``symbol`` value present anywhere in the
    raw canonical volume export, independent of ``LIVE_OBSERVATION_SYMBOLS``,
    timeframe, or freshness filtering.

    This answers one narrow question: did MT5 (via the exporter) expose an
    exact row for this symbol string at all in this run? Used only for
    read-only status reporting -- e.g. to explicitly surface a requested
    frozen prospective symbol MT5 did not expose under that exact name,
    rather than letting it silently disappear inside ``load_volume_rows``'s
    stricter filter.
    """
    if not path.is_file():
        return set()
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return {
            symbol
            for raw in csv.DictReader(handle)
            if (symbol := str(raw.get("symbol") or "").strip().upper())
        }


def _candle(row: dict[str, str], server_utc_offset_hours: int) -> Candle:
    return Candle(
        symbol=row["symbol"],
        timeframe=row["timeframe"],
        time=_utc_time(_integer(row, "time"), server_utc_offset_hours),
        open=_float(row, "open"),
        high=_float(row, "high"),
        low=_float(row, "low"),
        close=_float(row, "close"),
        tick_volume=_integer(row, "bar_tick_volume"),
        spread=max(0, int(round(_float(row, "spread_mean_points")))),
    )


def _mean_previous(rows: list[dict[str, str]], index: int, key: str, window: int = 20) -> float:
    values = [
        _float(row, key)
        for row in rows[max(0, index - window) : index]
        if _float(row, key) > 0
    ]
    return sum(values) / len(values) if values else 0.0


def _ratio_to_previous(rows: list[dict[str, str]], index: int, key: str) -> float:
    mean = _mean_previous(rows, index, key)
    return _float(rows[index], key) / mean if mean > 0 else 0.0


def _structure_fields(structure: StructureObservation) -> dict[str, str]:
    return {
        "structure_version": structure.version,
        "internal_bias": structure.internal_bias.value,
        "internal_reference_high": _number(structure.internal_reference_high),
        "internal_reference_low": _number(structure.internal_reference_low),
        "internal_break": structure.internal_break.value,
        "swing_bias": structure.swing_bias.value,
        "swing_reference_high": _number(structure.swing_reference_high),
        "swing_reference_low": _number(structure.swing_reference_low),
        "swing_break": structure.swing_break.value,
        "liquidity_reference_high": _number(structure.liquidity_reference_high),
        "liquidity_reference_low": _number(structure.liquidity_reference_low),
        "bsl_sweep": "1" if structure.bsl_sweep else "0",
        "ssl_sweep": "1" if structure.ssl_sweep else "0",
        "bsl_sweep_depth": _number(structure.bsl_sweep_depth),
        "ssl_sweep_depth": _number(structure.ssl_sweep_depth),
        "bsl_sweep_depth_atr": _number(structure.bsl_sweep_depth_atr),
        "ssl_sweep_depth_atr": _number(structure.ssl_sweep_depth_atr),
        "fvg_direction": structure.fvg_direction.value,
        "fvg_size": _number(structure.fvg_size),
        "fvg_size_atr": _number(structure.fvg_size_atr),
        "structure_event_count": str(structure.event_count),
    }


def _research_labels(row: dict[str, str]) -> set[str]:
    labels = set(feature_labels(row))
    action = row["action"]
    labels.add("ALL_SIGNALS")

    rvol = _float(row, "rvol_20")
    percentile = _float(row, "volume_percentile_100")
    tick_ratio = _float(row, "tick_rate_ratio_20")
    spread_ratio = _float(row, "spread_ratio_20")
    body_ratio = _float(row, "body_efficiency_ratio_20")
    imbalance = _float(row, "direction_imbalance")
    candle_body = _float(row, "entry_price") - _float(row, "bar_open")

    if rvol >= 1.2:
        labels.add("HIGH_RVOL")
    if rvol >= 1.8:
        labels.add("EXTREME_RVOL")
    if percentile >= 80:
        labels.add("HIGH_TICK_ACTIVITY")
    if tick_ratio >= 1.2:
        labels.add("TICK_ACCELERATION")
    if imbalance >= 0.10:
        labels.add("POSITIVE_QUOTE_IMBALANCE")
    elif imbalance <= -0.10:
        labels.add("NEGATIVE_QUOTE_IMBALANCE")
    if spread_ratio >= 1.2 or _float(row, "spread_expansion_points") > 0:
        labels.add("SPREAD_EXPANDING")

    pressure = 1 if imbalance >= 0.05 else -1 if imbalance <= -0.05 else 0
    direction = 1 if action == "BUY" else -1 if action == "SELL" else 0
    if pressure and direction:
        labels.add(
            "QUOTE_PRESSURE_ALIGNED" if pressure == direction else "QUOTE_PRESSURE_CONFLICT"
        )

    if rvol >= 1.2 and body_ratio > 0 and body_ratio <= 0.60:
        labels.add("VOLUME_ABSORPTION")
    if rvol >= 1.2 and body_ratio >= 1.4:
        if candle_body > 0:
            labels.add("BULLISH_VOLUME_IMPULSE")
        elif candle_body < 0:
            labels.add("BEARISH_VOLUME_IMPULSE")
    return labels


def _apply_forward_outcomes(
    observation: dict[str, str],
    candles: list[Candle],
    index: int,
) -> None:
    action = observation["action"]
    entry = candles[index].close
    atr = _float(observation, "atr")
    spread_cost = _float(observation, "spread_cost")

    for horizon in HORIZONS:
        if index + horizon >= len(candles):
            continue
        future = candles[index + 1 : index + horizon + 1]
        exit_candle = candles[index + horizon]
        observation[f"exit_time_{horizon}"] = exit_candle.time.isoformat()
        observation[f"exit_price_{horizon}"] = _number(exit_candle.close)
        if action not in {"BUY", "SELL"}:
            observation[f"outcome_{horizon}"] = "NO_TRADE"
            continue

        direction = 1.0 if action == "BUY" else -1.0
        net = direction * (exit_candle.close - entry) - spread_cost
        if action == "BUY":
            mfe = max(candle.high - entry for candle in future)
            mae = max(entry - candle.low for candle in future)
        else:
            mfe = max(entry - candle.low for candle in future)
            mae = max(candle.high - entry for candle in future)
        observation[f"net_move_{horizon}"] = _number(net)
        observation[f"progress_atr_{horizon}"] = _number(net / atr if atr > 0 else None)
        observation[f"mfe_atr_{horizon}"] = _number(
            max(0.0, mfe) / atr if atr > 0 else None
        )
        observation[f"mae_atr_{horizon}"] = _number(
            max(0.0, mae) / atr if atr > 0 else None
        )
        observation[f"outcome_{horizon}"] = (
            "WIN" if net > 1e-12 else "LOSS" if net < -1e-12 else "FLAT"
        )


def build_fx_observations(
    volume_rows: list[dict[str, str]],
    *,
    server_utc_offset_hours: int = 0,
    symbols: tuple[str, ...] | None = None,
    include_forward_outcomes: bool = True,
) -> list[dict[str, str]]:
    """Build deterministic, research-only observations from canonical bars.

    The default symbol scope and forward-outcome behavior are unchanged for
    the production live runtime. Historical replay must pass an explicit
    broker-derived symbol tuple and disables the legacy observation-level
    forward labels so candidate construction cannot receive future fields.
    """
    selected_symbols = tuple(
        dict.fromkeys(symbol.strip().upper() for symbol in (symbols or LIVE_OBSERVATION_SYMBOLS))
    )
    by_symbol: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in volume_rows:
        if row.get("symbol", "").upper() in selected_symbols:
            by_symbol[row["symbol"].upper()].append(row)

    signal_engine = SignalEngine()
    structure_engine = MarketStructureEngine()
    minimum = max(signal_engine.minimum_candles, structure_engine.minimum_candles)
    output: list[dict[str, str]] = []

    for symbol in selected_symbols:
        rows = sorted(by_symbol.get(symbol, []), key=lambda item: _integer(item, "time"))
        candles = [_candle(row, server_utc_offset_hours) for row in rows]
        for index in range(minimum - 1, len(rows)):
            start = max(0, index - 59)
            history = candles[start : index + 1]
            result = signal_engine.analyze(history)
            structure = structure_engine.analyze(history, atr=result.atr)
            source = rows[index]
            signal_time = candles[index].time
            point = _float(source, "point")
            spread_points = _float(source, "spread_mean_points")
            spread_cost = spread_points * point

            observation = {field: "" for field in _OBSERVATION_FIELDS}
            observation.update(
                {
                    "schema_version": SCHEMA_VERSION,
                    "observation_id": f"{symbol}:M5:{_integer(source, 'time')}",
                    "signal_time": signal_time.isoformat(),
                    "source_bar_time": str(_integer(source, "time")),
                    "server_utc_offset_hours": str(server_utc_offset_hours),
                    "symbol": symbol,
                    "timeframe": "M5",
                    "session": session_for_time(signal_time),
                    "action": result.action.value,
                    "score": str(result.score),
                    "confidence": str(result.confidence),
                    "entry_price": _number(candles[index].close),
                    "bar_open": _number(candles[index].open),
                    "bar_high": _number(candles[index].high),
                    "bar_low": _number(candles[index].low),
                    "ema_fast": _number(result.ema_fast),
                    "ema_slow": _number(result.ema_slow),
                    "rsi": _number(result.rsi),
                    "atr": _number(result.atr),
                    "signal_reasons": " | ".join(result.reasons),
                    "bar_tick_volume": str(_integer(source, "bar_tick_volume")),
                    "tick_count": str(_integer(source, "tick_count")),
                    "tick_rate_per_sec": _number(_float(source, "tick_rate_per_sec")),
                    "tick_rate_ratio_20": _number(
                        _ratio_to_previous(rows, index, "tick_rate_per_sec")
                    ),
                    "bid_up": str(_integer(source, "bid_up")),
                    "bid_down": str(_integer(source, "bid_down")),
                    "ask_up": str(_integer(source, "ask_up")),
                    "ask_down": str(_integer(source, "ask_down")),
                    "mid_up": str(_integer(source, "mid_up")),
                    "mid_down": str(_integer(source, "mid_down")),
                    "direction_imbalance": _number(_float(source, "direction_imbalance")),
                    "delta_proxy": str(_integer(source, "delta_proxy")),
                    "rvol_20": _number(_float(source, "rvol_20")),
                    "volume_ratio_20": _number(_float(source, "rvol_20")),
                    "volume_percentile_100": _number(
                        _float(source, "volume_percentile_100")
                    ),
                    "spread_mean_points": _number(spread_points),
                    "spread_min_points": _number(_float(source, "spread_min_points")),
                    "spread_max_points": _number(_float(source, "spread_max_points")),
                    "spread_last_points": _number(_float(source, "spread_last_points")),
                    "spread_expansion_points": _number(
                        _float(source, "spread_expansion_points")
                    ),
                    "spread_ratio_20": _number(
                        _ratio_to_previous(rows, index, "spread_mean_points")
                    ),
                    "point": _number(point),
                    "spread_cost": _number(spread_cost),
                    "spread_cost_atr": _number(
                        spread_cost / result.atr if result.atr > 0 else None
                    ),
                    "realized_abs_move_points": _number(
                        _float(source, "realized_abs_move_points")
                    ),
                    "range_per_tick_points": _number(
                        _float(source, "range_per_tick_points")
                    ),
                    "body_per_tick_points": _number(
                        _float(source, "body_per_tick_points")
                    ),
                    "range_efficiency_ratio_20": _number(
                        _ratio_to_previous(rows, index, "range_per_tick_points")
                    ),
                    "body_efficiency_ratio_20": _number(
                        _ratio_to_previous(rows, index, "body_per_tick_points")
                    ),
                    "tick_copy_status": source.get("tick_copy_status", "").upper(),
                }
            )
            observation.update(_structure_fields(structure))
            observation["labels"] = "|".join(sorted(_research_labels(observation)))
            if include_forward_outcomes:
                _apply_forward_outcomes(observation, candles, index)
            output.append(observation)

    output.sort(key=lambda row: (row["signal_time"], row["symbol"]))
    return output


def _labels(row: dict[str, str]) -> set[str]:
    return {item for item in str(row.get("labels", "")).split("|") if item}


def validate_fx_observations(
    observations: list[dict[str, str]],
    *,
    candidate_minimum: int = 30,
    research_minimum: int = 300,
    minimum_trading_days: int = 10,
    maximum_drawdown_atr: float = 25.0,
    maximum_loss_streak: int = 10,
    minimum_late_ratio: float = 0.20,
    fdr_alpha: float = 0.10,
) -> list[tuple[str, ActionPatternValidation]]:
    """Validate exact symbol, feature, session, action and horizon combinations."""
    groups: dict[tuple[str, str, str, str], list[dict[str, str]]] = defaultdict(list)
    for row in observations:
        action = row.get("action", "").upper()
        if action not in {"BUY", "SELL"}:
            continue
        for label in _labels(row):
            groups[(row["symbol"], label, row["session"], action)].append(row)

    raw: list[ActionPatternValidation] = []
    sessions: list[str] = []
    for (symbol, label, session, action), rows in sorted(groups.items()):
        for horizon in HORIZONS:
            result = validate_action_rows(
                rows,
                horizon,
                action,
                candidate_minimum=candidate_minimum,
                research_minimum=research_minimum,
                minimum_trading_days=minimum_trading_days,
                maximum_drawdown_atr=maximum_drawdown_atr,
                maximum_loss_streak=maximum_loss_streak,
                minimum_late_ratio=minimum_late_ratio,
            )
            raw.append(
                ActionPatternValidation(
                    symbol=symbol,
                    label=f"{session}::{label}",
                    action=action,
                    horizon=horizon,
                    observations=len(rows),
                    result=result,
                )
            )
            sessions.append(session)

    adjusted = apply_benjamini_hochberg(raw, fdr_alpha=fdr_alpha)
    return list(zip(sessions, adjusted))


def _state_rows(
    validations: list[tuple[str, ActionPatternValidation]], captured_at: datetime
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for session, item in validations:
        label = item.label.split("::", 1)[1]
        result = item.result
        rows.append(
            {
                "captured_at": captured_at.isoformat(),
                "symbol": item.symbol,
                "label": label,
                "session": session,
                "action": item.action,
                "horizon": str(item.horizon),
                "observations": str(item.observations),
                "trades": str(result.total.trades),
                "trading_days": str(result.trading_days),
                "status": result.status,
                "win_rate": _number(result.total.win_rate),
                "profit_factor_atr": _number(result.total.profit_factor_atr),
                "avg_net_atr": _number(result.total.avg_net_atr),
                "early_avg_net_atr": _number(result.early.avg_net_atr),
                "late_avg_net_atr": _number(result.late.avg_net_atr),
                "late_to_early_ratio": _number(result.late_to_early_ratio),
                "max_drawdown_atr": _number(result.max_drawdown_atr),
                "max_loss_streak": str(result.max_loss_streak),
                "mean_ci_low": _number(result.mean_ci_low),
                "mean_ci_high": _number(result.mean_ci_high),
                "p_value": _number(result.p_value),
                "q_value": _number(result.q_value),
                "reasons": " | ".join(result.reasons),
            }
        )
    return sorted(
        rows,
        key=lambda row: (
            row["symbol"],
            row["label"],
            row["session"],
            row["action"],
            int(row["horizon"]),
        ),
    )


def run_fx_research(
    volume_path: Path,
    observations_path: Path,
    states_path: Path,
    *,
    server_utc_offset_hours: int = 0,
    candidate_minimum: int = 30,
    research_minimum: int = 300,
    minimum_trading_days: int = 10,
    fdr_alpha: float = 0.10,
) -> FxResearchSummary:
    volume_rows, source_rows = load_volume_rows(volume_path)
    observations = build_fx_observations(
        volume_rows,
        server_utc_offset_hours=server_utc_offset_hours,
    )
    validations = validate_fx_observations(
        observations,
        candidate_minimum=candidate_minimum,
        research_minimum=research_minimum,
        minimum_trading_days=minimum_trading_days,
        fdr_alpha=fdr_alpha,
    )
    captured_at = datetime.now().astimezone()
    _atomic_write(observations_path, _OBSERVATION_FIELDS, observations)
    _atomic_write(states_path, _STATE_FIELDS, _state_rows(validations, captured_at))
    return FxResearchSummary(
        source_rows=source_rows,
        fx_rows=len(volume_rows),
        observations=len(observations),
        completed_h3=sum(row.get("outcome_3") in _VALID_OUTCOMES for row in observations),
        completed_h6=sum(row.get("outcome_6") in _VALID_OUTCOMES for row in observations),
        completed_h12=sum(row.get("outcome_12") in _VALID_OUTCOMES for row in observations),
        states=len(validations),
        observations_path=observations_path,
        states_path=states_path,
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build read-only FX SMC, session and volume research observations"
    )
    parser.add_argument(
        "--volume",
        type=Path,
        default=Path("data/volume_v1_4/volume_bars.csv"),
    )
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
    parser.add_argument("--server-utc-offset-hours", type=int, default=0)
    parser.add_argument("--candidate-min", type=int, default=30)
    parser.add_argument("--min-sample", type=int, default=300)
    parser.add_argument("--min-trading-days", type=int, default=10)
    parser.add_argument("--fdr-alpha", type=float, default=0.10)
    args = parser.parse_args()

    volume = args.volume.expanduser().resolve()
    if not volume.is_file():
        print(f"Canonical volume file not found: {volume}")
        return 1
    if not -14 <= args.server_utc_offset_hours <= 14:
        parser.error("--server-utc-offset-hours must be between -14 and 14")
    if args.candidate_min < 2 or args.min_sample < args.candidate_min:
        parser.error("sample limits are invalid")
    if args.min_trading_days < 1 or not 0 < args.fdr_alpha <= 1:
        parser.error("validation limits are invalid")

    try:
        summary = run_fx_research(
            volume,
            args.observations.expanduser().resolve(),
            args.states.expanduser().resolve(),
            server_utc_offset_hours=args.server_utc_offset_hours,
            candidate_minimum=args.candidate_min,
            research_minimum=args.min_sample,
            minimum_trading_days=args.min_trading_days,
            fdr_alpha=args.fdr_alpha,
        )
    except (OSError, TypeError, ValueError) as exc:
        print(f"FX research failed: {exc}")
        return 1

    print("TradeMind v1.4.2 FX research stream")
    print(f"Canonical source rows: {summary.source_rows}")
    print(f"Healthy FX M5 rows: {summary.fx_rows}")
    print(f"Research observations: {summary.observations}")
    print(
        "Completed H3/H6/H12: "
        f"{summary.completed_h3}/{summary.completed_h6}/{summary.completed_h12}"
    )
    print(f"Validation states: {summary.states}")
    print(f"Observations: {summary.observations_path}")
    print(f"Latest states: {summary.states_path}")
    print("No orders were sent.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
