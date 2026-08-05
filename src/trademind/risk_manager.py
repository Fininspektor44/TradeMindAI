"""TradeMind Risk Manager Core 1.0.

This module is the mandatory boundary between an evidence-approved signal and
any future execution adapter. It calculates account-specific staged order
volumes, verifies portfolio and margin limits, and returns one deterministic
ALLOW or BLOCK decision.

The core is broker-neutral and read-only. It imports no broker API, sends no
orders, and changes no account or robot settings. Broker adapters must provide
fresh account, portfolio, and instrument snapshots in account currency.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, ROUND_FLOOR
from pathlib import Path
from typing import Any, Mapping, Sequence

from trademind.signal_intelligence import (
    SignalCandidate,
    append_journal_event,
    candidate_from_dict,
)

RISK_MANAGER_VERSION = "1.0.0"
VALID_VENUES = {"MT5", "BYBIT", "GENERIC"}
VALID_RISK_BASES = {"BALANCE", "EQUITY", "MIN_BALANCE_EQUITY"}
VALID_SIGNAL_STATES = {"PUBLISHABLE", "APPROVED_MANUAL"}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _number(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def _optional_number(value: Any) -> float | None:
    if value is None or _text(value) == "":
        return None
    result = _number(value, math.nan)
    return result if math.isfinite(result) else None


def _parse_time(value: Any) -> datetime:
    text = _text(value)
    if not text:
        raise ValueError("timestamp is required")
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include timezone information")
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non-finite numeric value")
        return value
    if isinstance(value, datetime):
        return _iso(value)
    if isinstance(value, Mapping):
        return {
            str(key): _json_safe(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    if hasattr(value, "as_dict"):
        return _json_safe(value.as_dict())
    raise ValueError(f"unsupported JSON value: {type(value).__name__}")


def _canonical_json(value: Any) -> str:
    return json.dumps(
        _json_safe(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _pct(money: float, basis: float) -> float:
    return 100.0 * money / basis if basis > 0 else 0.0


def _round_volume_down(raw_volume: float, step: float) -> float:
    if raw_volume <= 0 or step <= 0:
        return 0.0
    raw = Decimal(str(raw_volume))
    quantum = Decimal(str(step))
    units = (raw / quantum).to_integral_value(rounding=ROUND_FLOOR)
    return float(units * quantum)


@dataclass(frozen=True, slots=True)
class AccountSnapshot:
    account_id: str
    venue: str
    currency: str
    captured_at: datetime
    balance: float
    equity: float
    free_margin: float
    margin_used: float
    high_watermark_equity: float
    day_start_equity: float
    trading_enabled: bool = True

    def __post_init__(self) -> None:
        account_id = _text(self.account_id)
        venue = _text(self.venue).upper()
        currency = _text(self.currency).upper()
        if not account_id or not currency:
            raise ValueError("account_id and currency are required")
        if venue not in VALID_VENUES:
            raise ValueError(f"unsupported venue: {venue or '<empty>'}")
        if self.captured_at.tzinfo is None:
            raise ValueError("account captured_at must include timezone information")
        if self.balance <= 0 or self.equity <= 0:
            raise ValueError("balance and equity must be positive")
        if min(self.free_margin, self.margin_used) < 0:
            raise ValueError("margin values cannot be negative")
        if self.high_watermark_equity <= 0 or self.day_start_equity <= 0:
            raise ValueError("equity references must be positive")
        object.__setattr__(self, "account_id", account_id)
        object.__setattr__(self, "venue", venue)
        object.__setattr__(self, "currency", currency)

    @property
    def drawdown_pct(self) -> float:
        peak = max(self.high_watermark_equity, self.equity)
        return _pct(max(0.0, peak - self.equity), peak)

    @property
    def daily_loss_pct(self) -> float:
        return _pct(
            max(0.0, self.day_start_equity - self.equity),
            self.day_start_equity,
        )

    @property
    def margin_usage_pct(self) -> float:
        return _pct(self.margin_used, self.equity)

    def risk_basis_money(self, risk_basis: str) -> float:
        if risk_basis == "BALANCE":
            return self.balance
        if risk_basis == "EQUITY":
            return self.equity
        if risk_basis == "MIN_BALANCE_EQUITY":
            return min(self.balance, self.equity)
        raise ValueError(f"unsupported risk basis: {risk_basis}")

    def as_dict(self) -> dict[str, Any]:
        return {
            "account_id": self.account_id,
            "venue": self.venue,
            "currency": self.currency,
            "captured_at": _iso(self.captured_at),
            "balance": self.balance,
            "equity": self.equity,
            "free_margin": self.free_margin,
            "margin_used": self.margin_used,
            "high_watermark_equity": self.high_watermark_equity,
            "day_start_equity": self.day_start_equity,
            "trading_enabled": self.trading_enabled,
            "drawdown_pct": self.drawdown_pct,
            "daily_loss_pct": self.daily_loss_pct,
            "margin_usage_pct": self.margin_usage_pct,
        }


@dataclass(frozen=True, slots=True)
class PositionRisk:
    position_id: str
    symbol: str
    correlation_group: str
    risk_money: float | None
    margin_used: float = 0.0
    action: str = ""

    def __post_init__(self) -> None:
        position_id = _text(self.position_id)
        symbol = _text(self.symbol).upper()
        group = _text(self.correlation_group).upper() or symbol
        action = _text(self.action).upper()
        if not position_id or not symbol:
            raise ValueError("position_id and symbol are required")
        if self.risk_money is not None and self.risk_money < 0:
            raise ValueError("position risk_money cannot be negative")
        if self.margin_used < 0:
            raise ValueError("position margin_used cannot be negative")
        if action and action not in {"BUY", "SELL"}:
            raise ValueError("position action must be BUY or SELL")
        object.__setattr__(self, "position_id", position_id)
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "correlation_group", group)
        object.__setattr__(self, "action", action)

    def as_dict(self) -> dict[str, Any]:
        return {
            "position_id": self.position_id,
            "symbol": self.symbol,
            "correlation_group": self.correlation_group,
            "risk_money": self.risk_money,
            "margin_used": self.margin_used,
            "action": self.action,
        }


@dataclass(frozen=True, slots=True)
class PortfolioSnapshot:
    positions: tuple[PositionRisk, ...] = ()
    open_trades: int = 0
    reserved_risk_money: float = 0.0
    reserved_margin: float = 0.0

    def __post_init__(self) -> None:
        if self.open_trades < 0:
            raise ValueError("open_trades cannot be negative")
        if self.reserved_risk_money < 0 or self.reserved_margin < 0:
            raise ValueError("reserved values cannot be negative")

    @property
    def risk_complete(self) -> bool:
        return all(item.risk_money is not None for item in self.positions)

    @property
    def known_position_risk_money(self) -> float:
        return sum(item.risk_money or 0.0 for item in self.positions)

    @property
    def total_risk_money(self) -> float:
        return self.known_position_risk_money + self.reserved_risk_money

    @property
    def effective_open_trades(self) -> int:
        return max(self.open_trades, len(self.positions))

    def symbol_risk_money(self, symbol: str) -> float:
        target = symbol.upper()
        return sum(
            item.risk_money or 0.0
            for item in self.positions
            if item.symbol == target
        )

    def correlation_risk_money(self, correlation_group: str) -> float:
        target = correlation_group.upper()
        return sum(
            item.risk_money or 0.0
            for item in self.positions
            if item.correlation_group == target
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "positions": [item.as_dict() for item in self.positions],
            "open_trades": self.open_trades,
            "effective_open_trades": self.effective_open_trades,
            "reserved_risk_money": self.reserved_risk_money,
            "reserved_margin": self.reserved_margin,
            "risk_complete": self.risk_complete,
            "total_risk_money": self.total_risk_money,
        }


@dataclass(frozen=True, slots=True)
class InstrumentSpec:
    symbol: str
    venue: str
    account_currency: str
    tick_size: float
    tick_value_per_volume: float
    volume_min: float
    volume_max: float
    volume_step: float
    correlation_group: str
    margin_per_volume: float | None = None
    contract_size: float | None = None
    leverage: float | None = None
    quote_to_account_rate: float = 1.0

    def __post_init__(self) -> None:
        symbol = _text(self.symbol).upper()
        venue = _text(self.venue).upper()
        currency = _text(self.account_currency).upper()
        group = _text(self.correlation_group).upper() or symbol
        if not symbol or not currency:
            raise ValueError("instrument symbol and account_currency are required")
        if venue not in VALID_VENUES:
            raise ValueError(f"unsupported venue: {venue or '<empty>'}")
        if min(
            self.tick_size,
            self.tick_value_per_volume,
            self.volume_min,
            self.volume_max,
            self.volume_step,
        ) <= 0:
            raise ValueError("tick and volume specifications must be positive")
        if self.volume_min > self.volume_max:
            raise ValueError("volume_min cannot exceed volume_max")
        if self.margin_per_volume is not None and self.margin_per_volume <= 0:
            raise ValueError("margin_per_volume must be positive")
        if self.contract_size is not None and self.contract_size <= 0:
            raise ValueError("contract_size must be positive")
        if self.leverage is not None and self.leverage <= 0:
            raise ValueError("leverage must be positive")
        if self.quote_to_account_rate <= 0:
            raise ValueError("quote_to_account_rate must be positive")
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "venue", venue)
        object.__setattr__(self, "account_currency", currency)
        object.__setattr__(self, "correlation_group", group)

    def risk_per_volume(self, entry_price: float, stop_price: float) -> float:
        distance = abs(entry_price - stop_price)
        ticks = distance / self.tick_size
        return ticks * self.tick_value_per_volume

    def margin_for(self, volume: float, entry_price: float) -> float | None:
        if self.margin_per_volume is not None:
            return self.margin_per_volume * volume
        if self.contract_size is None or self.leverage is None:
            return None
        notional = abs(entry_price) * self.contract_size * volume
        return notional * self.quote_to_account_rate / self.leverage

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "venue": self.venue,
            "account_currency": self.account_currency,
            "tick_size": self.tick_size,
            "tick_value_per_volume": self.tick_value_per_volume,
            "volume_min": self.volume_min,
            "volume_max": self.volume_max,
            "volume_step": self.volume_step,
            "correlation_group": self.correlation_group,
            "margin_per_volume": self.margin_per_volume,
            "contract_size": self.contract_size,
            "leverage": self.leverage,
            "quote_to_account_rate": self.quote_to_account_rate,
        }


@dataclass(frozen=True, slots=True)
class RiskProfile:
    name: str = "STANDARD"
    risk_basis: str = "MIN_BALANCE_EQUITY"
    default_trade_risk_pct: float = 0.50
    maximum_trade_risk_pct: float = 1.00
    maximum_portfolio_risk_pct: float = 3.00
    maximum_symbol_risk_pct: float = 1.50
    maximum_correlation_risk_pct: float = 2.00
    maximum_daily_loss_pct: float = 2.00
    maximum_account_drawdown_pct: float = 10.00
    maximum_margin_usage_pct: float = 50.00
    minimum_free_margin_pct: float = 25.00
    maximum_open_trades: int = 8
    maximum_account_snapshot_age_seconds: int = 120
    maximum_signal_age_seconds: int = 900
    maximum_clock_skew_seconds: int = 30
    adverse_slippage_ticks: float = 2.0
    minimum_risk_utilization_pct: float = 50.0
    require_margin_check: bool = True
    require_complete_portfolio_risk: bool = True
    allowed_signal_states: tuple[str, ...] = ("PUBLISHABLE",)

    def __post_init__(self) -> None:
        name = _text(self.name).upper() or "STANDARD"
        risk_basis = _text(self.risk_basis).upper()
        states = tuple(_text(value).upper() for value in self.allowed_signal_states)
        if risk_basis not in VALID_RISK_BASES:
            raise ValueError(f"unsupported risk basis: {risk_basis}")
        percentages = (
            self.default_trade_risk_pct,
            self.maximum_trade_risk_pct,
            self.maximum_portfolio_risk_pct,
            self.maximum_symbol_risk_pct,
            self.maximum_correlation_risk_pct,
            self.maximum_daily_loss_pct,
            self.maximum_account_drawdown_pct,
            self.maximum_margin_usage_pct,
            self.minimum_free_margin_pct,
            self.minimum_risk_utilization_pct,
        )
        if any(value < 0 for value in percentages):
            raise ValueError("risk percentages cannot be negative")
        if self.default_trade_risk_pct <= 0:
            raise ValueError("default trade risk must be positive")
        if self.default_trade_risk_pct > self.maximum_trade_risk_pct:
            raise ValueError("default trade risk exceeds maximum trade risk")
        if self.maximum_symbol_risk_pct > self.maximum_portfolio_risk_pct:
            raise ValueError("symbol risk limit exceeds portfolio risk limit")
        if self.maximum_correlation_risk_pct > self.maximum_portfolio_risk_pct:
            raise ValueError("correlation risk limit exceeds portfolio risk limit")
        if self.maximum_margin_usage_pct > 100 or self.minimum_free_margin_pct > 100:
            raise ValueError("margin percentages cannot exceed 100")
        if self.minimum_risk_utilization_pct > 100:
            raise ValueError("minimum risk utilization cannot exceed 100")
        if min(
            self.maximum_open_trades,
            self.maximum_account_snapshot_age_seconds,
            self.maximum_signal_age_seconds,
        ) <= 0:
            raise ValueError("risk profile limits must be positive")
        if self.maximum_clock_skew_seconds < 0 or self.adverse_slippage_ticks < 0:
            raise ValueError("clock skew and slippage cannot be negative")
        if not states or any(state not in VALID_SIGNAL_STATES for state in states):
            raise ValueError("allowed_signal_states contains an unsupported state")
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "risk_basis", risk_basis)
        object.__setattr__(self, "allowed_signal_states", states)

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "risk_basis": self.risk_basis,
            "default_trade_risk_pct": self.default_trade_risk_pct,
            "maximum_trade_risk_pct": self.maximum_trade_risk_pct,
            "maximum_portfolio_risk_pct": self.maximum_portfolio_risk_pct,
            "maximum_symbol_risk_pct": self.maximum_symbol_risk_pct,
            "maximum_correlation_risk_pct": self.maximum_correlation_risk_pct,
            "maximum_daily_loss_pct": self.maximum_daily_loss_pct,
            "maximum_account_drawdown_pct": self.maximum_account_drawdown_pct,
            "maximum_margin_usage_pct": self.maximum_margin_usage_pct,
            "minimum_free_margin_pct": self.minimum_free_margin_pct,
            "maximum_open_trades": self.maximum_open_trades,
            "maximum_account_snapshot_age_seconds": (
                self.maximum_account_snapshot_age_seconds
            ),
            "maximum_signal_age_seconds": self.maximum_signal_age_seconds,
            "maximum_clock_skew_seconds": self.maximum_clock_skew_seconds,
            "adverse_slippage_ticks": self.adverse_slippage_ticks,
            "minimum_risk_utilization_pct": self.minimum_risk_utilization_pct,
            "require_margin_check": self.require_margin_check,
            "require_complete_portfolio_risk": self.require_complete_portfolio_risk,
            "allowed_signal_states": list(self.allowed_signal_states),
        }


@dataclass(frozen=True, slots=True)
class RiskMessage:
    code: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


@dataclass(frozen=True, slots=True)
class SizedOrder:
    entry_index: int
    order_type: str
    planned_price: float
    effective_entry_price: float
    allocation: float
    volume: float
    risk_money: float
    risk_pct_of_basis: float
    margin_required: float | None
    rationale: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "entry_index": self.entry_index,
            "order_type": self.order_type,
            "planned_price": self.planned_price,
            "effective_entry_price": self.effective_entry_price,
            "allocation": self.allocation,
            "volume": self.volume,
            "risk_money": self.risk_money,
            "risk_pct_of_basis": self.risk_pct_of_basis,
            "margin_required": self.margin_required,
            "rationale": self.rationale,
        }


@dataclass(frozen=True, slots=True)
class RiskDecision:
    decision_id: str
    evaluated_at: datetime
    signal_id: str
    state: str
    account_id: str
    venue: str
    symbol: str
    action: str
    risk_profile: str
    risk_basis: str
    risk_basis_money: float
    requested_risk_pct: float
    risk_budget_money: float
    actual_risk_money: float
    actual_risk_pct: float
    risk_utilization_pct: float
    existing_portfolio_risk_money: float
    portfolio_risk_after_money: float
    portfolio_risk_after_pct: float
    symbol_risk_after_money: float
    symbol_risk_after_pct: float
    correlation_risk_after_money: float
    correlation_risk_after_pct: float
    margin_required: float | None
    free_margin_after: float | None
    free_margin_after_pct: float | None
    margin_usage_after_pct: float | None
    daily_loss_pct: float
    account_drawdown_pct: float
    checks: Mapping[str, bool]
    reasons: tuple[RiskMessage, ...]
    warnings: tuple[RiskMessage, ...]
    orders: tuple[SizedOrder, ...]

    @property
    def allowed(self) -> bool:
        return self.state == "ALLOW"

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": RISK_MANAGER_VERSION,
            "decision_id": self.decision_id,
            "evaluated_at": _iso(self.evaluated_at),
            "signal_id": self.signal_id,
            "state": self.state,
            "allowed": self.allowed,
            "account_id": self.account_id,
            "venue": self.venue,
            "symbol": self.symbol,
            "action": self.action,
            "risk_profile": self.risk_profile,
            "risk_basis": self.risk_basis,
            "risk_basis_money": self.risk_basis_money,
            "requested_risk_pct": self.requested_risk_pct,
            "risk_budget_money": self.risk_budget_money,
            "actual_risk_money": self.actual_risk_money,
            "actual_risk_pct": self.actual_risk_pct,
            "risk_utilization_pct": self.risk_utilization_pct,
            "existing_portfolio_risk_money": self.existing_portfolio_risk_money,
            "portfolio_risk_after_money": self.portfolio_risk_after_money,
            "portfolio_risk_after_pct": self.portfolio_risk_after_pct,
            "symbol_risk_after_money": self.symbol_risk_after_money,
            "symbol_risk_after_pct": self.symbol_risk_after_pct,
            "correlation_risk_after_money": self.correlation_risk_after_money,
            "correlation_risk_after_pct": self.correlation_risk_after_pct,
            "margin_required": self.margin_required,
            "free_margin_after": self.free_margin_after,
            "free_margin_after_pct": self.free_margin_after_pct,
            "margin_usage_after_pct": self.margin_usage_after_pct,
            "daily_loss_pct": self.daily_loss_pct,
            "account_drawdown_pct": self.account_drawdown_pct,
            "checks": dict(self.checks),
            "reasons": [item.as_dict() for item in self.reasons],
            "warnings": [item.as_dict() for item in self.warnings],
            "orders": [item.as_dict() for item in self.orders],
            "safety": {
                "orders_enabled": False,
                "broker_api_called": False,
                "account_modified": False,
                "robot_settings_modified": False,
                "risk_approved_only": self.allowed,
            },
        }


def _account_snapshot_age_seconds(account: AccountSnapshot, now: datetime) -> float:
    return (now - account.captured_at.astimezone(timezone.utc)).total_seconds()


def _signal_age_seconds(candidate: SignalCandidate, now: datetime) -> float:
    return (now - candidate.created_at.astimezone(timezone.utc)).total_seconds()


def _effective_entry_price(
    candidate: SignalCandidate,
    entry_price: float,
    instrument: InstrumentSpec,
    profile: RiskProfile,
) -> float:
    slippage = profile.adverse_slippage_ticks * instrument.tick_size
    direction = 1.0 if candidate.plan.action == "BUY" else -1.0
    return entry_price + direction * slippage


def _decision_identity(
    *,
    candidate: SignalCandidate,
    gate_state: str,
    account: AccountSnapshot,
    instrument: InstrumentSpec,
    portfolio: PortfolioSnapshot,
    profile: RiskProfile,
    requested_risk_pct: float,
) -> str:
    payload = {
        "schema_version": RISK_MANAGER_VERSION,
        "signal_id": candidate.signal_id,
        "gate_state": gate_state,
        "account": account.as_dict(),
        "instrument": instrument.as_dict(),
        "portfolio": portfolio.as_dict(),
        "profile": profile.as_dict(),
        "requested_risk_pct": requested_risk_pct,
    }
    return f"RM-{candidate.signal_id}-{_sha256(payload)[:16]}"


def evaluate_risk(
    candidate: SignalCandidate,
    *,
    gate_state: str,
    account: AccountSnapshot,
    instrument: InstrumentSpec,
    profile: RiskProfile | None = None,
    portfolio: PortfolioSnapshot | None = None,
    requested_risk_pct: float | None = None,
    now: datetime | None = None,
) -> RiskDecision:
    """Return a deterministic account-specific ALLOW or BLOCK decision."""
    rules = profile or RiskProfile()
    positions = portfolio or PortfolioSnapshot()
    captured_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    signal_state = _text(gate_state).upper()
    requested_pct = (
        rules.default_trade_risk_pct
        if requested_risk_pct is None
        else _number(requested_risk_pct)
    )
    basis = account.risk_basis_money(rules.risk_basis)
    risk_budget = basis * requested_pct / 100.0

    checks: dict[str, bool] = {}
    reasons: list[RiskMessage] = []
    warnings: list[RiskMessage] = []

    def hard_check(name: str, passed: bool, code: str, message: str) -> None:
        checks[name] = bool(passed)
        if not passed:
            reasons.append(RiskMessage(code, message))

    hard_check(
        "signal_state",
        signal_state in rules.allowed_signal_states,
        "SIGNAL_NOT_APPROVED",
        f"Signal state {signal_state or '<empty>'} is not allowed by the risk profile.",
    )
    hard_check(
        "market_generated",
        candidate.generated_from_market_data,
        "NON_MARKET_SIGNAL",
        "Candidate is not generated from market data.",
    )
    hard_check(
        "symbol_match",
        candidate.symbol == instrument.symbol,
        "SYMBOL_MISMATCH",
        f"Candidate symbol {candidate.symbol} does not match {instrument.symbol}.",
    )
    hard_check(
        "venue_match",
        account.venue == instrument.venue or instrument.venue == "GENERIC",
        "VENUE_MISMATCH",
        f"Account venue {account.venue} does not match {instrument.venue}.",
    )
    hard_check(
        "currency_match",
        account.currency == instrument.account_currency,
        "ACCOUNT_CURRENCY_MISMATCH",
        (
            f"Account currency {account.currency} does not match instrument model "
            f"currency {instrument.account_currency}."
        ),
    )
    hard_check(
        "trading_enabled",
        account.trading_enabled,
        "ACCOUNT_TRADING_DISABLED",
        "Account snapshot reports trading disabled.",
    )

    account_age = _account_snapshot_age_seconds(account, captured_at)
    account_time_valid = account_age >= -rules.maximum_clock_skew_seconds
    account_fresh = account_age <= rules.maximum_account_snapshot_age_seconds
    hard_check(
        "account_time_valid",
        account_time_valid,
        "ACCOUNT_SNAPSHOT_FROM_FUTURE",
        f"Account snapshot is {-account_age:.1f}s ahead of the risk-manager clock.",
    )
    hard_check(
        "account_snapshot_fresh",
        account_fresh,
        "ACCOUNT_SNAPSHOT_STALE",
        (
            f"Account snapshot age {max(0.0, account_age):.1f}s exceeds "
            f"{rules.maximum_account_snapshot_age_seconds}s."
        ),
    )

    signal_age = _signal_age_seconds(candidate, captured_at)
    signal_time_valid = signal_age >= -rules.maximum_clock_skew_seconds
    signal_fresh = signal_age <= rules.maximum_signal_age_seconds
    hard_check(
        "signal_time_valid",
        signal_time_valid,
        "SIGNAL_FROM_FUTURE",
        f"Signal timestamp is {-signal_age:.1f}s ahead of the risk-manager clock.",
    )
    hard_check(
        "signal_fresh",
        signal_fresh,
        "SIGNAL_STALE",
        (
            f"Signal age {max(0.0, signal_age):.1f}s exceeds "
            f"{rules.maximum_signal_age_seconds}s."
        ),
    )

    hard_check(
        "requested_risk_positive",
        requested_pct > 0,
        "INVALID_TRADE_RISK",
        "Requested trade risk must be positive.",
    )
    hard_check(
        "trade_risk_limit",
        requested_pct <= rules.maximum_trade_risk_pct,
        "TRADE_RISK_LIMIT",
        (
            f"Requested risk {requested_pct:.3f}% exceeds "
            f"{rules.maximum_trade_risk_pct:.3f}%."
        ),
    )
    hard_check(
        "daily_loss_limit",
        account.daily_loss_pct < rules.maximum_daily_loss_pct,
        "DAILY_LOSS_LOCK",
        (
            f"Daily loss {account.daily_loss_pct:.3f}% reached the "
            f"{rules.maximum_daily_loss_pct:.3f}% lock."
        ),
    )
    hard_check(
        "drawdown_limit",
        account.drawdown_pct < rules.maximum_account_drawdown_pct,
        "ACCOUNT_DRAWDOWN_LOCK",
        (
            f"Account drawdown {account.drawdown_pct:.3f}% reached the "
            f"{rules.maximum_account_drawdown_pct:.3f}% lock."
        ),
    )
    hard_check(
        "portfolio_risk_known",
        positions.risk_complete or not rules.require_complete_portfolio_risk,
        "OPEN_RISK_UNKNOWN",
        "At least one existing position has no known stop-defined risk.",
    )
    hard_check(
        "open_trade_limit",
        positions.effective_open_trades + 1 <= rules.maximum_open_trades,
        "OPEN_TRADE_LIMIT",
        (
            f"Opening this signal would create {positions.effective_open_trades + 1} "
            f"open trades; limit is {rules.maximum_open_trades}."
        ),
    )

    sized_orders: list[SizedOrder] = []
    margin_known = True
    for index, entry in enumerate(candidate.plan.entries, start=1):
        effective_entry = _effective_entry_price(candidate, entry.price, instrument, rules)
        risk_per_volume = instrument.risk_per_volume(
            effective_entry,
            candidate.plan.stop_price,
        )
        if risk_per_volume <= 0:
            reasons.append(
                RiskMessage(
                    "INVALID_STOP_DISTANCE",
                    f"Entry {index} has zero or invalid stop distance.",
                )
            )
            continue
        intended_risk = risk_budget * entry.allocation
        raw_volume = intended_risk / risk_per_volume if risk_per_volume > 0 else 0.0
        if raw_volume + 1e-12 < instrument.volume_min:
            minimum_risk = instrument.volume_min * risk_per_volume
            reasons.append(
                RiskMessage(
                    "MIN_VOLUME_EXCEEDS_RISK",
                    (
                        f"Entry {index} requires {raw_volume:.8g} volume, below broker "
                        f"minimum {instrument.volume_min:.8g}; minimum would risk "
                        f"{minimum_risk:.2f} {account.currency}."
                    ),
                )
            )
            continue
        if raw_volume > instrument.volume_max + 1e-12:
            reasons.append(
                RiskMessage(
                    "MAX_VOLUME_EXCEEDED",
                    (
                        f"Entry {index} requires {raw_volume:.8g} volume, above broker "
                        f"maximum {instrument.volume_max:.8g}."
                    ),
                )
            )
            continue
        volume = _round_volume_down(raw_volume, instrument.volume_step)
        if volume + 1e-12 < instrument.volume_min:
            reasons.append(
                RiskMessage(
                    "VOLUME_STEP_BELOW_MINIMUM",
                    (
                        f"Entry {index} rounds below minimum volume after applying "
                        f"step {instrument.volume_step:.8g}."
                    ),
                )
            )
            continue
        volume = min(volume, instrument.volume_max)
        actual_risk = volume * risk_per_volume
        margin = instrument.margin_for(volume, effective_entry)
        if margin is None:
            margin_known = False
        sized_orders.append(
            SizedOrder(
                entry_index=index,
                order_type=entry.order_type,
                planned_price=entry.price,
                effective_entry_price=effective_entry,
                allocation=entry.allocation,
                volume=volume,
                risk_money=actual_risk,
                risk_pct_of_basis=_pct(actual_risk, basis),
                margin_required=margin,
                rationale=entry.rationale,
            )
        )

    checks["volume_geometry"] = len(sized_orders) == len(candidate.plan.entries)
    if not checks["volume_geometry"] and not any(
        item.code in {
            "INVALID_STOP_DISTANCE",
            "MIN_VOLUME_EXCEEDS_RISK",
            "MAX_VOLUME_EXCEEDED",
            "VOLUME_STEP_BELOW_MINIMUM",
        }
        for item in reasons
    ):
        reasons.append(
            RiskMessage("VOLUME_GEOMETRY_FAILED", "Not all staged entries were sized.")
        )

    actual_risk = sum(item.risk_money for item in sized_orders)
    actual_risk_pct = _pct(actual_risk, basis)
    utilization = 100.0 * actual_risk / risk_budget if risk_budget > 0 else 0.0
    hard_check(
        "actual_risk_within_budget",
        actual_risk <= risk_budget + max(0.01, risk_budget * 1e-9),
        "RISK_BUDGET_EXCEEDED",
        (
            f"Calculated risk {actual_risk:.2f} exceeds budget "
            f"{risk_budget:.2f} {account.currency}."
        ),
    )
    if sized_orders and utilization < rules.minimum_risk_utilization_pct:
        warnings.append(
            RiskMessage(
                "LOW_RISK_UTILIZATION",
                (
                    f"Broker volume steps use only {utilization:.1f}% of the requested "
                    "risk budget."
                ),
            )
        )
    allocation = sum(item.allocation for item in candidate.plan.entries)
    if allocation < 0.999999:
        warnings.append(
            RiskMessage(
                "PARTIAL_ALLOCATION_PLAN",
                f"Signal entries allocate only {100.0 * allocation:.1f}% of risk budget.",
            )
        )

    existing_portfolio = positions.total_risk_money
    portfolio_after = existing_portfolio + actual_risk
    symbol_before = positions.symbol_risk_money(candidate.symbol)
    symbol_after = symbol_before + actual_risk
    group_before = positions.correlation_risk_money(instrument.correlation_group)
    group_after = group_before + actual_risk
    portfolio_after_pct = _pct(portfolio_after, basis)
    symbol_after_pct = _pct(symbol_after, basis)
    group_after_pct = _pct(group_after, basis)

    hard_check(
        "portfolio_risk_limit",
        portfolio_after_pct <= rules.maximum_portfolio_risk_pct,
        "PORTFOLIO_RISK_LIMIT",
        (
            f"Portfolio risk would become {portfolio_after_pct:.3f}%, above "
            f"{rules.maximum_portfolio_risk_pct:.3f}%."
        ),
    )
    hard_check(
        "symbol_risk_limit",
        symbol_after_pct <= rules.maximum_symbol_risk_pct,
        "SYMBOL_RISK_LIMIT",
        (
            f"{candidate.symbol} risk would become {symbol_after_pct:.3f}%, above "
            f"{rules.maximum_symbol_risk_pct:.3f}%."
        ),
    )
    hard_check(
        "correlation_risk_limit",
        group_after_pct <= rules.maximum_correlation_risk_pct,
        "CORRELATION_RISK_LIMIT",
        (
            f"Correlation group {instrument.correlation_group} risk would become "
            f"{group_after_pct:.3f}%, above "
            f"{rules.maximum_correlation_risk_pct:.3f}%."
        ),
    )

    hard_check(
        "margin_model_available",
        margin_known or not rules.require_margin_check,
        "MARGIN_MODEL_MISSING",
        "Instrument snapshot cannot calculate required margin.",
    )
    margin_required: float | None
    free_margin_after: float | None
    free_margin_after_pct: float | None
    margin_usage_after_pct: float | None
    if margin_known:
        margin_required = sum(item.margin_required or 0.0 for item in sized_orders)
        margin_required += positions.reserved_margin
        free_margin_after = account.free_margin - margin_required
        free_margin_after_pct = _pct(max(0.0, free_margin_after), account.equity)
        margin_usage_after_pct = _pct(
            account.margin_used + margin_required,
            account.equity,
        )
        hard_check(
            "free_margin_nonnegative",
            free_margin_after >= 0,
            "INSUFFICIENT_FREE_MARGIN",
            (
                f"Required margin {margin_required:.2f} exceeds free margin "
                f"{account.free_margin:.2f} {account.currency}."
            ),
        )
        hard_check(
            "minimum_free_margin",
            free_margin_after_pct >= rules.minimum_free_margin_pct,
            "FREE_MARGIN_BUFFER",
            (
                f"Free margin after entry would be {free_margin_after_pct:.2f}% of "
                f"equity, below {rules.minimum_free_margin_pct:.2f}%."
            ),
        )
        hard_check(
            "margin_usage_limit",
            margin_usage_after_pct <= rules.maximum_margin_usage_pct,
            "MARGIN_USAGE_LIMIT",
            (
                f"Margin usage after entry would be {margin_usage_after_pct:.2f}%, "
                f"above {rules.maximum_margin_usage_pct:.2f}%."
            ),
        )
    else:
        margin_required = None
        free_margin_after = None
        free_margin_after_pct = None
        margin_usage_after_pct = None
        checks["free_margin_nonnegative"] = not rules.require_margin_check
        checks["minimum_free_margin"] = not rules.require_margin_check
        checks["margin_usage_limit"] = not rules.require_margin_check
        if not rules.require_margin_check:
            warnings.append(
                RiskMessage(
                    "MARGIN_CHECK_SKIPPED",
                    "Margin model is unavailable and the profile allows calculation without it.",
                )
            )

    state = "ALLOW" if not reasons and all(checks.values()) else "BLOCK"
    decision_id = _decision_identity(
        candidate=candidate,
        gate_state=signal_state,
        account=account,
        instrument=instrument,
        portfolio=positions,
        profile=rules,
        requested_risk_pct=requested_pct,
    )
    return RiskDecision(
        decision_id=decision_id,
        evaluated_at=captured_at,
        signal_id=candidate.signal_id,
        state=state,
        account_id=account.account_id,
        venue=account.venue,
        symbol=candidate.symbol,
        action=candidate.plan.action,
        risk_profile=rules.name,
        risk_basis=rules.risk_basis,
        risk_basis_money=basis,
        requested_risk_pct=requested_pct,
        risk_budget_money=risk_budget,
        actual_risk_money=actual_risk,
        actual_risk_pct=actual_risk_pct,
        risk_utilization_pct=utilization,
        existing_portfolio_risk_money=existing_portfolio,
        portfolio_risk_after_money=portfolio_after,
        portfolio_risk_after_pct=portfolio_after_pct,
        symbol_risk_after_money=symbol_after,
        symbol_risk_after_pct=symbol_after_pct,
        correlation_risk_after_money=group_after,
        correlation_risk_after_pct=group_after_pct,
        margin_required=margin_required,
        free_margin_after=free_margin_after,
        free_margin_after_pct=free_margin_after_pct,
        margin_usage_after_pct=margin_usage_after_pct,
        daily_loss_pct=account.daily_loss_pct,
        account_drawdown_pct=account.drawdown_pct,
        checks=checks,
        reasons=tuple(reasons),
        warnings=tuple(warnings),
        orders=tuple(sized_orders),
    )


def account_from_dict(payload: Mapping[str, Any]) -> AccountSnapshot:
    equity = _number(payload.get("equity"))
    return AccountSnapshot(
        account_id=_text(payload.get("account_id", payload.get("login"))),
        venue=_text(payload.get("venue")),
        currency=_text(payload.get("currency")),
        captured_at=_parse_time(payload.get("captured_at")),
        balance=_number(payload.get("balance")),
        equity=equity,
        free_margin=_number(payload.get("free_margin")),
        margin_used=_number(payload.get("margin_used")),
        high_watermark_equity=_number(
            payload.get("high_watermark_equity"), equity
        ),
        day_start_equity=_number(payload.get("day_start_equity"), equity),
        trading_enabled=bool(payload.get("trading_enabled", True)),
    )


def position_from_dict(payload: Mapping[str, Any]) -> PositionRisk:
    return PositionRisk(
        position_id=_text(payload.get("position_id", payload.get("ticket"))),
        symbol=_text(payload.get("symbol")),
        correlation_group=_text(payload.get("correlation_group")),
        risk_money=_optional_number(payload.get("risk_money")),
        margin_used=_number(payload.get("margin_used")),
        action=_text(payload.get("action")),
    )


def portfolio_from_dict(payload: Mapping[str, Any]) -> PortfolioSnapshot:
    rows = payload.get("positions", [])
    return PortfolioSnapshot(
        positions=tuple(position_from_dict(item) for item in rows),
        open_trades=int(payload.get("open_trades", 0)),
        reserved_risk_money=_number(payload.get("reserved_risk_money")),
        reserved_margin=_number(payload.get("reserved_margin")),
    )


def instrument_from_dict(payload: Mapping[str, Any]) -> InstrumentSpec:
    return InstrumentSpec(
        symbol=_text(payload.get("symbol")),
        venue=_text(payload.get("venue")),
        account_currency=_text(payload.get("account_currency")),
        tick_size=_number(payload.get("tick_size")),
        tick_value_per_volume=_number(payload.get("tick_value_per_volume")),
        volume_min=_number(payload.get("volume_min")),
        volume_max=_number(payload.get("volume_max")),
        volume_step=_number(payload.get("volume_step")),
        correlation_group=_text(payload.get("correlation_group")),
        margin_per_volume=_optional_number(payload.get("margin_per_volume")),
        contract_size=_optional_number(payload.get("contract_size")),
        leverage=_optional_number(payload.get("leverage")),
        quote_to_account_rate=_number(payload.get("quote_to_account_rate"), 1.0),
    )


def profile_from_dict(payload: Mapping[str, Any]) -> RiskProfile:
    return RiskProfile(
        name=_text(payload.get("name")) or "STANDARD",
        risk_basis=_text(payload.get("risk_basis")) or "MIN_BALANCE_EQUITY",
        default_trade_risk_pct=_number(payload.get("default_trade_risk_pct"), 0.50),
        maximum_trade_risk_pct=_number(payload.get("maximum_trade_risk_pct"), 1.00),
        maximum_portfolio_risk_pct=_number(
            payload.get("maximum_portfolio_risk_pct"), 3.00
        ),
        maximum_symbol_risk_pct=_number(
            payload.get("maximum_symbol_risk_pct"), 1.50
        ),
        maximum_correlation_risk_pct=_number(
            payload.get("maximum_correlation_risk_pct"), 2.00
        ),
        maximum_daily_loss_pct=_number(payload.get("maximum_daily_loss_pct"), 2.00),
        maximum_account_drawdown_pct=_number(
            payload.get("maximum_account_drawdown_pct"), 10.00
        ),
        maximum_margin_usage_pct=_number(
            payload.get("maximum_margin_usage_pct"), 50.00
        ),
        minimum_free_margin_pct=_number(payload.get("minimum_free_margin_pct"), 25.00),
        maximum_open_trades=int(payload.get("maximum_open_trades", 8)),
        maximum_account_snapshot_age_seconds=int(
            payload.get("maximum_account_snapshot_age_seconds", 120)
        ),
        maximum_signal_age_seconds=int(payload.get("maximum_signal_age_seconds", 900)),
        maximum_clock_skew_seconds=int(payload.get("maximum_clock_skew_seconds", 30)),
        adverse_slippage_ticks=_number(payload.get("adverse_slippage_ticks"), 2.0),
        minimum_risk_utilization_pct=_number(
            payload.get("minimum_risk_utilization_pct"), 50.0
        ),
        require_margin_check=bool(payload.get("require_margin_check", True)),
        require_complete_portfolio_risk=bool(
            payload.get("require_complete_portfolio_risk", True)
        ),
        allowed_signal_states=tuple(
            _text(value)
            for value in payload.get("allowed_signal_states", ["PUBLISHABLE"])
        ),
    )


def _read_json(path: Path) -> Mapping[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, Mapping):
        raise ValueError(f"JSON root must be an object: {path}")
    return payload


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    temporary.replace(path)


def _candidate_and_state(
    *,
    passport_path: Path | None,
    candidate_path: Path | None,
    gate_state: str | None,
) -> tuple[SignalCandidate, str]:
    if passport_path is not None:
        payload = _read_json(passport_path)
        candidate_payload = payload.get("candidate", {})
        decision_payload = payload.get("gate_decision", {})
        if not isinstance(candidate_payload, Mapping) or not isinstance(
            decision_payload, Mapping
        ):
            raise ValueError("passport candidate and gate_decision must be objects")
        return candidate_from_dict(candidate_payload), _text(decision_payload.get("state"))
    if candidate_path is None:
        raise ValueError("candidate or passport is required")
    if not _text(gate_state):
        raise ValueError("--gate-state is required with --candidate")
    return candidate_from_dict(_read_json(candidate_path)), _text(gate_state)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="TradeMind Risk Manager Core 1.0 account-specific sizing gate"
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--passport", type=Path)
    source.add_argument("--candidate", type=Path)
    parser.add_argument("--gate-state")
    parser.add_argument("--account", type=Path, required=True)
    parser.add_argument("--instrument", type=Path, required=True)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--portfolio", type=Path)
    parser.add_argument("--requested-risk-pct", type=float)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--journal", type=Path)
    args = parser.parse_args(argv)

    try:
        candidate, state = _candidate_and_state(
            passport_path=args.passport,
            candidate_path=args.candidate,
            gate_state=args.gate_state,
        )
        account = account_from_dict(_read_json(args.account))
        instrument = instrument_from_dict(_read_json(args.instrument))
        profile = profile_from_dict(_read_json(args.profile))
        portfolio = (
            portfolio_from_dict(_read_json(args.portfolio))
            if args.portfolio is not None
            else PortfolioSnapshot()
        )
        decision = evaluate_risk(
            candidate,
            gate_state=state,
            account=account,
            instrument=instrument,
            profile=profile,
            portfolio=portfolio,
            requested_risk_pct=args.requested_risk_pct,
        )
        payload = decision.as_dict()
        _atomic_json(args.output, payload)
        if args.journal is not None:
            append_journal_event(
                args.journal,
                signal_id=candidate.signal_id,
                event_type="RISK_DECISION",
                payload=payload,
                recorded_at=decision.evaluated_at,
            )
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        print(f"Risk manager failed: {exc}")
        return 1

    print("TradeMind Risk Manager Core 1.0")
    print("Sizing and limits only. Orders OFF. Broker API not called.")
    print(f"Signal: {decision.signal_id}")
    print(f"Account: {decision.account_id} ({decision.venue})")
    print(f"State: {decision.state}")
    print(
        "Risk requested/actual: "
        f"{decision.requested_risk_pct:.3f}%/"
        f"{decision.actual_risk_pct:.3f}% "
        f"({decision.actual_risk_money:.2f})"
    )
    print(
        "Portfolio risk after: "
        f"{decision.portfolio_risk_after_pct:.3f}% "
        f"({decision.portfolio_risk_after_money:.2f})"
    )
    if decision.margin_required is not None:
        print(
            "Margin required/free after: "
            f"{decision.margin_required:.2f}/"
            f"{decision.free_margin_after:.2f}"
        )
    for order in decision.orders:
        print(
            f"Entry {order.entry_index}: {order.order_type} "
            f"price={order.planned_price:.10g} volume={order.volume:.8g} "
            f"risk={order.risk_money:.2f}"
        )
    for reason in decision.reasons:
        print(f"BLOCK {reason.code}: {reason.message}")
    for warning in decision.warnings:
        print(f"WARN {warning.code}: {warning.message}")
    print(f"Output: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
