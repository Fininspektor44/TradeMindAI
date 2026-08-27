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
from trademind.ote_engine import build_ote_signals
from trademind.ote_models import load_volume_rows as load_ote_volume_rows
from trademind.ser8_core8_market_only_policy import CORE_8_SYMBOLS_ORDERED

SCHEMA_VERSION = "1.5.1"
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
LIVE_OBSERVATION_SYMBOLS = tuple(
    dict.fromkeys(FX_MAJORS + FROZEN_PROSPECTIVE_SYMBOLS + CORE_8_SYMBOLS_ORDERED)
)

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
    "signal_source",
    "ote_signal_id",
    "variant",
    "fib_ratio",
    "score",
    "entry_price",
    "bar_open",
    "bar_high",
    "bar_low",
    "atr",
    "signal_reasons",
    "setup_break",
    "setup_start_time",
    "setup_end_time",
    "setup_age_bars",
    "anchor_price",
    "impulse_extreme",
    "impulse_range",
    "impulse_atr",
    "fib_618",
    "fib_705",
    "fib_790",
    "stop_price",
    "target_price",
    "risk_price",
    "reward_price",
    "rr",
    "stop_buffer",
    "h1_bias",
    "h4_bias",
    "h1_aligned",
    "h4_aligned",
    "liquidity_sweep",
    "fvg_aligned",
    "confirmation",
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
    """Load healthy canonical M5 volume rows for the live universe.

    ``trademind.ote_models.load_volume_rows(path, symbols)`` is the single
    authoritative OTE loader.  Keeping this small compatibility wrapper lets
    the established v1.21/v1.22 runtime continue importing the same public
    name while removing its former FX-major-only parser/gate.
    """
    return load_ote_volume_rows(path, LIVE_OBSERVATION_SYMBOLS)


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
    entry = _float(observation, "entry_price")
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
    """Build candidate-source rows exclusively from authoritative OTE signals.

    Bars that do not produce a signal in ``build_ote_signals`` produce no row.
    Consequently neither research nor live callers can manufacture a
    directional candidate from volatility, structure observations, or market
    metadata alone.
    """
    selected_symbols = tuple(
        dict.fromkeys(symbol.strip().upper() for symbol in (symbols or LIVE_OBSERVATION_SYMBOLS))
    )
    by_symbol: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in volume_rows:
        if row.get("symbol", "").upper() in selected_symbols:
            by_symbol[row["symbol"].upper()].append(row)

    selected_rows = [
        row
        for symbol in selected_symbols
        for row in sorted(by_symbol.get(symbol, []), key=lambda item: _integer(item, "time"))
    ]
    signals = build_ote_signals(
        selected_rows,
        server_utc_offset_hours=server_utc_offset_hours,
    )
    output: list[dict[str, str]] = []
    rows_by_symbol = {
        symbol: sorted(by_symbol.get(symbol, []), key=lambda item: _integer(item, "time"))
        for symbol in selected_symbols
    }
    candles_by_symbol = {
        symbol: [_candle(row, server_utc_offset_hours) for row in rows]
        for symbol, rows in rows_by_symbol.items()
    }
    source_by_key = {
        (row["symbol"].upper(), _integer(row, "time")): (index, row)
        for symbol in selected_symbols
        for index, row in enumerate(rows_by_symbol[symbol])
    }

    for signal in signals:
        symbol = signal["symbol"].upper()
        source_epoch = _integer(signal, "source_bar_time")
        located = source_by_key.get((symbol, source_epoch))
        if located is None:
            continue
        index, source = located
        rows = rows_by_symbol[symbol]
        candles = candles_by_symbol[symbol]
        signal_time = candles[index].time + timedelta(
            seconds=max(1, _integer(source, "bar_seconds") or 300)
        )
        point = _float(source, "point")
        spread_points = _float(source, "spread_mean_points")
        spread_cost = spread_points * point
        action = signal["action"]
        aligned_sweep = signal.get("liquidity_sweep", "0")
        fvg_direction = (
            ("BULLISH" if action == "BUY" else "BEARISH")
            if signal.get("fvg_aligned") == "1"
            else "NONE"
        )

        observation = {field: "" for field in _OBSERVATION_FIELDS}
        observation.update({key: value for key, value in signal.items() if key in observation})
        observation.update(
            {
                    "schema_version": SCHEMA_VERSION,
                    "observation_id": signal["signal_id"],
                    "ote_signal_id": signal["signal_id"],
                    "signal_source": "trademind.ote_engine.build_ote_signals",
                    "signal_time": signal_time.isoformat(),
                    "source_bar_time": str(source_epoch),
                    "server_utc_offset_hours": str(server_utc_offset_hours),
                    "symbol": symbol,
                    "timeframe": "M5",
                    "session": session_for_time(signal_time),
                    "action": action,
                    "entry_price": signal["entry_price"],
                    "bar_open": _number(candles[index].open),
                    "bar_high": _number(candles[index].high),
                    "bar_low": _number(candles[index].low),
                    "atr": signal["atr"],
                    "signal_reasons": signal["reasons"],
                    "structure_version": "OTE_STRUCTURE_V1",
                    "internal_bias": signal.get("h1_bias", ""),
                    "internal_reference_high": signal.get("impulse_extreme", "") if action == "BUY" else "",
                    "internal_reference_low": signal.get("impulse_extreme", "") if action == "SELL" else "",
                    "internal_break": signal.get("setup_break", ""),
                    "swing_bias": signal.get("h4_bias", ""),
                    "swing_reference_high": signal.get("impulse_extreme", "") if action == "BUY" else signal.get("anchor_price", ""),
                    "swing_reference_low": signal.get("anchor_price", "") if action == "BUY" else signal.get("impulse_extreme", ""),
                    "swing_break": signal.get("setup_break", ""),
                    "liquidity_reference_high": signal.get("anchor_price", "") if action == "SELL" else signal.get("impulse_extreme", ""),
                    "liquidity_reference_low": signal.get("anchor_price", "") if action == "BUY" else signal.get("impulse_extreme", ""),
                    "bsl_sweep": aligned_sweep if action == "SELL" else "0",
                    "ssl_sweep": aligned_sweep if action == "BUY" else "0",
                    "fvg_direction": fvg_direction,
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
                        spread_cost / _float(signal, "atr") if _float(signal, "atr") > 0 else None
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

    print("TradeMind v1.5.1 SMC/OTE FX research stream")
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
