"""TradeMind v1.26 H1 swing opportunity filter.

The filter has one intentionally small decision chain:
H1 direction -> M15 veto -> fresh close beyond the latest confirmed M5 pivot
-> M5 volume and delta confirmation -> enough room to the H1 swing target.

It reads local closed Bybit data only. It does not call an exchange, publish a
signal, calculate account sizing or send orders.
"""

from __future__ import annotations

import bisect
import csv
import math
import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from trademind import crypto_signal_adapter as source
from trademind.crypto_market_structure import MarketStructureEngine
from trademind.signal_intelligence import EntryOrder, SignalCandidate, TradePlan

VERSION = "1.26.0"
SETUP_FAMILY = "CRYPTO_H1_SWING_M5_VOLUME_BREAKOUT"
MIN_VOLUME_RATIO = 1.20
MIN_TARGET_RR = 1.80
MIN_TARGET_ATR_H1 = 0.70
M5_MS = 5 * 60 * 1000


@dataclass(frozen=True, slots=True)
class FlowBar:
    start_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    delta_turnover: float

    @property
    def end_ms(self) -> int:
        return self.start_ms + M5_MS


@dataclass(frozen=True, slots=True)
class Pivot:
    index: int
    price: float
    start_ms: int


@dataclass(frozen=True, slots=True)
class Opportunity:
    eligible: bool
    action: str
    reasons: tuple[str, ...]
    entry: float | None = None
    stop: float | None = None
    target: float | None = None
    breakout_level: float | None = None
    breakout_pivot_ms: int | None = None
    volume_ratio: float = 0.0
    current_volume: float = 0.0
    median_volume_20: float = 0.0
    delta_turnover: float = 0.0
    target_rr: float = 0.0
    target_atr_h1: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "eligible": self.eligible,
            "action": self.action,
            "reasons": list(self.reasons),
            "entry": self.entry,
            "stop": self.stop,
            "target": self.target,
            "breakout_level": self.breakout_level,
            "breakout_pivot_ms": self.breakout_pivot_ms,
            "volume_ratio": self.volume_ratio,
            "current_volume": self.current_volume,
            "median_volume_20": self.median_volume_20,
            "delta_turnover": self.delta_turnover,
            "target_rr": self.target_rr,
            "target_atr_h1": self.target_atr_h1,
        }


def _number(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(str(value if value is not None else default).replace(",", "."))
    except (TypeError, ValueError):
        return default
    return parsed if math.isfinite(parsed) else default


def _integer(value: Any, default: int = 0) -> int:
    try:
        return int(float(str(value if value is not None else default).replace(",", ".")))
    except (TypeError, ValueError):
        return default


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _sequence(value: Any) -> list[Any]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return list(value)
    return []


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def read_flow_bars(path: Path) -> dict[str, list[FlowBar]]:
    if not path.is_file():
        raise ValueError(f"Bybit bars source not found: {path}")
    grouped: dict[str, dict[int, FlowBar]] = {}
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            symbol = str(row.get("symbol") or "").strip().upper()
            start_ms = _integer(row.get("start_ms"))
            open_price = _number(row.get("open"))
            high = _number(row.get("high"))
            low = _number(row.get("low"))
            close = _number(row.get("close"))
            volume = max(0.0, _number(row.get("volume")))
            delta = _number(row.get("delta_turnover"))
            if (
                not symbol
                or start_ms <= 0
                or min(open_price, high, low, close) <= 0
                or high < max(open_price, close)
                or low > min(open_price, close)
                or high < low
            ):
                continue
            grouped.setdefault(symbol, {})[start_ms] = FlowBar(
                start_ms=start_ms,
                open=open_price,
                high=high,
                low=low,
                close=close,
                volume=volume,
                delta_turnover=delta,
            )
    return {
        symbol: [rows[key] for key in sorted(rows)]
        for symbol, rows in grouped.items()
        if rows
    }


class FlowHistory:
    def __init__(self, bars_by_symbol: Mapping[str, Sequence[FlowBar]]) -> None:
        self._bars = {
            str(symbol).upper(): sorted(rows, key=lambda item: item.start_ms)
            for symbol, rows in bars_by_symbol.items()
        }
        self._ends = {
            symbol: [bar.end_ms for bar in rows] for symbol, rows in self._bars.items()
        }

    @classmethod
    def from_csv(cls, path: Path) -> FlowHistory:
        return cls(read_flow_bars(path))

    def closed(self, symbol: str, as_of_ms: int) -> list[FlowBar]:
        key = symbol.upper()
        rows = self._bars.get(key, [])
        count = bisect.bisect_right(self._ends.get(key, []), as_of_ms)
        return rows[:count]


def _pivots(
    bars: Sequence[FlowBar],
    *,
    left: int = 2,
    right: int = 2,
) -> tuple[list[Pivot], list[Pivot]]:
    highs: list[Pivot] = []
    lows: list[Pivot] = []
    for index in range(left, len(bars) - right):
        window = bars[index - left : index + right + 1]
        current = bars[index]
        other_highs = [bar.high for offset, bar in enumerate(window) if offset != left]
        other_lows = [bar.low for offset, bar in enumerate(window) if offset != left]
        if current.high > max(other_highs):
            highs.append(Pivot(index=index, price=current.high, start_ms=current.start_ms))
        if current.low < min(other_lows):
            lows.append(Pivot(index=index, price=current.low, start_ms=current.start_ms))
    return highs, lows


def _reject(action: str, *reasons: str) -> Opportunity:
    return Opportunity(False, action, tuple(reasons))


def evaluate_opportunity(
    action: str,
    m5_bars: Sequence[FlowBar],
    snapshot: Mapping[str, Any],
    *,
    minimum_volume_ratio: float = MIN_VOLUME_RATIO,
    minimum_target_rr: float = MIN_TARGET_RR,
    minimum_target_atr_h1: float = MIN_TARGET_ATR_H1,
) -> Opportunity:
    action = action.upper()
    if action not in {"BUY", "SELL"}:
        return _reject(action, "ACTION_NOT_BUY_SELL")
    if snapshot.get("state") != "OK":
        return _reject(action, "STRUCTURE_NOT_READY")
    if len(m5_bars) < 25:
        return _reject(action, "M5_HISTORY_TOO_SHORT")

    timeframes = _mapping(snapshot.get("timeframes"))
    h1 = _mapping(timeframes.get("H1"))
    m15 = _mapping(timeframes.get("M15"))
    volatility = _mapping(snapshot.get("volatility"))
    direction = "BULLISH" if action == "BUY" else "BEARISH"
    opposite = "BEARISH" if action == "BUY" else "BULLISH"

    if str(h1.get("bias") or "NEUTRAL") != direction:
        return _reject(action, "H1_DIRECTION_NOT_ALIGNED")
    if str(m15.get("bias") or "NEUTRAL") == opposite:
        return _reject(action, "M15_BIAS_VETO")
    if str(m15.get("break_direction") or "NONE") == opposite:
        return _reject(action, "M15_BREAK_VETO")

    trigger = m5_bars[-1]
    previous = m5_bars[-2]
    confirmed_history = m5_bars[:-1]
    highs, lows = _pivots(confirmed_history)
    if not highs or not lows:
        return _reject(action, "M5_CONFIRMED_EXTREMUM_MISSING")

    breakout = highs[-1] if action == "BUY" else lows[-1]
    opposite_pivot = lows[-1] if action == "BUY" else highs[-1]
    fresh_breakout = (
        previous.close <= breakout.price < trigger.close
        if action == "BUY"
        else previous.close >= breakout.price > trigger.close
    )
    if not fresh_breakout:
        return _reject(action, "M5_CLOSE_DID_NOT_BREAK_LAST_EXTREMUM")

    prior_volumes = [bar.volume for bar in confirmed_history[-20:] if bar.volume > 0]
    if not prior_volumes:
        return _reject(action, "M5_VOLUME_BASELINE_MISSING")
    median_volume = statistics.median(prior_volumes)
    volume_ratio = trigger.volume / median_volume if median_volume > 0 else 0.0
    if volume_ratio < minimum_volume_ratio:
        return _reject(action, "M5_VOLUME_BELOW_THRESHOLD")
    if action == "BUY" and trigger.delta_turnover <= 0:
        return _reject(action, "M5_DELTA_NOT_BULLISH")
    if action == "SELL" and trigger.delta_turnover >= 0:
        return _reject(action, "M5_DELTA_NOT_BEARISH")

    entry = trigger.close
    stop = opposite_pivot.price
    target = _number(
        h1.get("last_swing_high") if action == "BUY" else h1.get("last_swing_low")
    )
    valid_geometry = (
        stop < entry < target if action == "BUY" else target < entry < stop
    )
    if not valid_geometry:
        return _reject(action, "H1_TARGET_OR_M5_STOP_GEOMETRY_INVALID")

    risk = abs(entry - stop)
    reward = abs(target - entry)
    target_rr = reward / risk if risk > 0 else 0.0
    if target_rr < minimum_target_rr:
        return _reject(action, "H1_TARGET_BELOW_MINIMUM_RR")

    atr_h1 = _number(volatility.get("atr_h1"))
    target_atr_h1 = reward / atr_h1 if atr_h1 > 0 else 0.0
    if target_atr_h1 < minimum_target_atr_h1:
        return _reject(action, "H1_TARGET_DISTANCE_TOO_SMALL")

    return Opportunity(
        eligible=True,
        action=action,
        reasons=(
            "H1_DIRECTION_ALIGNED",
            "M15_NO_VETO",
            "M5_LAST_EXTREMUM_CLOSE_BREAK",
            "M5_VOLUME_CONFIRMED",
            "M5_DELTA_CONFIRMED",
            "H1_TARGET_SPACE_CONFIRMED",
        ),
        entry=entry,
        stop=stop,
        target=target,
        breakout_level=breakout.price,
        breakout_pivot_ms=breakout.start_ms,
        volume_ratio=volume_ratio,
        current_volume=trigger.volume,
        median_volume_20=median_volume,
        delta_turnover=trigger.delta_turnover,
        target_rr=target_rr,
        target_atr_h1=target_atr_h1,
    )


def _market_features(
    original: SignalCandidate,
    snapshot: Mapping[str, Any],
    opportunity: Opportunity,
) -> dict[str, dict[str, Any]]:
    market = {
        key: _mapping(value) for key, value in original.market_features.items()
    }
    timeframes = _mapping(snapshot.get("timeframes"))
    h1 = _mapping(timeframes.get("H1"))
    m15 = _mapping(timeframes.get("M15"))
    volatility = _mapping(snapshot.get("volatility"))

    market["structure"] = {
        "swing_bias": h1.get("bias"),
        "swing_break": h1.get("break"),
        "last_swing_high": h1.get("last_swing_high"),
        "last_swing_low": h1.get("last_swing_low"),
        "internal_bias": m15.get("bias"),
        "internal_break": m15.get("break"),
    }
    volume = _mapping(market.get("volume"))
    volume.update(
        {
            "m5_volume": opportunity.current_volume,
            "m5_median_volume_20": opportunity.median_volume_20,
            "m5_volume_ratio_20": opportunity.volume_ratio,
            "m5_delta_turnover": opportunity.delta_turnover,
        }
    )
    market["volume"] = volume
    volatility_market = _mapping(market.get("volatility"))
    volatility_market.update(
        {
            "atr_h1": volatility.get("atr_h1"),
            "target_distance_atr_h1": opportunity.target_atr_h1,
        }
    )
    market["volatility"] = volatility_market
    confirmation = _mapping(market.get("confirmation"))
    confirmation.update(
        {
            "trigger": "M5_LAST_CONFIRMED_EXTREMUM_BREAK",
            "breakout_level": opportunity.breakout_level,
            "breakout_pivot_ms": opportunity.breakout_pivot_ms,
            "close_confirmed": True,
            "volume_confirmed": True,
            "delta_confirmed": True,
            "future_bars_used": False,
        }
    )
    market["confirmation"] = confirmation
    custom = _mapping(market.get("custom"))
    custom.update(
        {
            "opportunity_filter_version": VERSION,
            "opportunity_eligible": True,
            "opportunity_reasons": list(opportunity.reasons),
            "h1_target": opportunity.target,
            "target_rr": opportunity.target_rr,
            "decision_chain": "H1_DIRECTION>M15_VETO>M5_BREAKOUT>VOLUME_DELTA>H1_SPACE",
        }
    )
    market["custom"] = custom
    return market


def build_candidate(
    row: Mapping[str, Any],
    snapshot: Mapping[str, Any],
    opportunity: Opportunity,
) -> SignalCandidate:
    if not opportunity.eligible:
        raise ValueError("opportunity must be eligible")
    original = source.build_candidate(row)
    if opportunity.entry is None or opportunity.stop is None or opportunity.target is None:
        raise ValueError("eligible opportunity geometry is incomplete")

    plan = TradePlan(
        action=opportunity.action,
        entries=(
            EntryOrder(
                price=opportunity.entry,
                allocation=1.0,
                rationale=(
                    "Закрытие M5 за последним подтверждённым M5-экстремумом "
                    "при повышенном объёме и согласованной delta"
                ),
                order_type="STOP",
            ),
        ),
        stop_price=opportunity.stop,
        targets=(opportunity.target,),
        invalidation="Возврат за противоположный подтверждённый M5-экстремум",
        target_rationale=("Последний подтверждённый экстремум H1",),
    )
    volume_score = _clamp(opportunity.volume_ratio / 1.80)
    scores = {
        "structure": 1.0,
        "volume": volume_score,
        "confirmation": 1.0,
        "volatility": _clamp(opportunity.target_atr_h1 / 1.50),
        "execution": _number(original.factor_scores.get("execution"), 0.5),
        "session": 0.70,
        "portfolio": 0.50,
    }
    reasons = {
        "structure": (
            "H1 swing направлен в сторону сделки",
            "M15 не содержит структурного veto",
        ),
        "volume": (
            f"Объём M5 к медиане 20: {opportunity.volume_ratio:.2f}x",
            f"Delta M5: {opportunity.delta_turnover:.2f}",
        ),
        "confirmation": (
            "M5 закрылась за последним подтверждённым локальным экстремумом",
            "Фитиль без закрытия не считается пробоем",
        ),
        "volatility": (
            f"Цель H1: {opportunity.target_rr:.2f}R",
            f"Пространство до цели: {opportunity.target_atr_h1:.2f} ATR H1",
        ),
        "execution": tuple(
            str(item)
            for item in _sequence(original.factor_reasons.get("execution"))[:1]
        )
        or ("Исполнение оценивается по локальному Bybit-источнику",),
        "session": ("Крипторынок работает 24/7",),
        "portfolio": ("Размер позиции пока не рассчитывается",),
    }
    return SignalCandidate(
        observed_at=original.observed_at,
        created_at=original.created_at,
        symbol=original.symbol,
        timeframe="M5",
        setup_family=SETUP_FAMILY,
        scenario=(
            "Продолжение подтверждённого H1-свинга после закрытия M5 за последним "
            "локальным экстремумом на повышенном объёме"
        ),
        plan=plan,
        market_features=_market_features(original, snapshot, opportunity),
        factor_scores=scores,
        factor_reasons=reasons,
        provenance=(
            *original.provenance,
            f"CRYPTO_H1_SWING_FILTER_{VERSION}",
        ),
        generated_from_market_data=True,
        robot_context_only={},
    )


def _decision_as_of(row: Mapping[str, Any]) -> datetime:
    end_ms = _integer(row.get("end_ms"))
    if end_ms > 0:
        return datetime.fromtimestamp((end_ms + 1) / 1000, tz=timezone.utc)
    parsed = source._parse_time(source._text(row, "signal_time"))
    return parsed + timedelta(milliseconds=1)


def evaluate_row(
    row: Mapping[str, Any],
    engine: MarketStructureEngine,
    flow: FlowHistory,
) -> tuple[SignalCandidate | None, dict[str, Any], dict[str, Any]]:
    source_id = source._text(row, "decision_id")
    action = source._text(row, "action").upper()
    symbol = source._text(row, "symbol").upper()
    as_of = _decision_as_of(row)
    snapshot = engine.snapshot(symbol, as_of, action)
    m5_bars = flow.closed(symbol, int(as_of.timestamp() * 1000))
    opportunity = evaluate_opportunity(action, m5_bars, snapshot)
    audit = {
        "schema_version": VERSION,
        "source_decision_id": source_id,
        "symbol": symbol,
        "action": action,
        "as_of": as_of.isoformat(),
        "opportunity": opportunity.as_dict(),
        "snapshot_state": snapshot.get("state"),
        "bar_counts": snapshot.get("bar_counts"),
        "safety": {
            "read_only": True,
            "orders_enabled": False,
            "publication_enabled": False,
            "exchange_api_called": False,
            "future_bars_used": False,
        },
    }
    if not opportunity.eligible:
        rejection = {
            "schema_version": VERSION,
            "source_decision_id": source_id,
            "symbol": symbol,
            "action": action,
            "as_of": as_of.isoformat(),
            "reasons": list(opportunity.reasons),
        }
        return None, audit, rejection
    return build_candidate(row, snapshot, opportunity), audit, {}


def safety_contract() -> Mapping[str, Any]:
    return {
        "read_only": True,
        "orders_enabled": False,
        "publication_enabled": False,
        "exchange_api_called": False,
        "future_bars_used": False,
        "account_sizing_calculated": False,
    }
