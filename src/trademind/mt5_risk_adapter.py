"""TradeMind MT5 Read-Only Risk Adapter 1.0.

The adapter converts CSV snapshots from the dedicated MQL5 risk exporter into
validated AccountSnapshot, PortfolioSnapshot and InstrumentSpec JSON payloads
accepted by Risk Manager Core 1.0.

It never imports MetaTrader libraries, calls a broker API, sends orders, or
changes terminal settings. Stale, incomplete or inconsistent snapshots fail
closed instead of producing an execution-ready payload.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from trademind.risk_manager import (
    AccountSnapshot,
    InstrumentSpec,
    PortfolioSnapshot,
    PositionRisk,
)

ADAPTER_VERSION = "1.0.0"
ACCOUNT_REQUIRED_FIELDS = (
    "time_msc",
    "account_login",
    "server",
    "currency",
    "balance",
    "equity",
    "margin",
    "free_margin",
    "leverage",
    "open_positions",
    "trade_allowed",
    "terminal_connected",
)
POSITION_REQUIRED_FIELDS = (
    "time_msc",
    "account_login",
    "currency",
    "position_ticket",
    "position_id",
    "symbol",
    "side",
    "volume",
    "open_price",
    "current_price",
    "sl",
)
SYMBOL_REQUIRED_FIELDS = (
    "time_msc",
    "account_login",
    "currency",
    "symbol",
    "trade_mode",
    "tick_size",
    "tick_value",
    "tick_value_profit",
    "tick_value_loss",
    "volume_min",
    "volume_max",
    "volume_step",
    "contract_size",
    "margin_initial",
    "margin_buy_per_volume",
    "margin_sell_per_volume",
    "leverage",
)
VALID_ACTIONS = {"BUY", "SELL"}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _number(value: Any, default: float = 0.0) -> float:
    try:
        number = float(_text(value).replace(",", "."))
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(float(_text(value)))
    except (TypeError, ValueError):
        return default


def _bool(value: Any, default: bool = False) -> bool:
    text = _text(value).lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def _from_millis(value: Any) -> datetime:
    milliseconds = _int(value)
    if milliseconds <= 0:
        raise ValueError(f"invalid time_msc: {value}")
    return datetime.fromtimestamp(milliseconds / 1000.0, tz=timezone.utc)


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


def _optional_positive(value: Any) -> float | None:
    number = _number(value)
    return number if number > 0 else None


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    temporary.replace(path)


def _read_csv_stable(
    path: Path,
    required_fields: Sequence[str],
    *,
    allow_empty: bool = False,
    attempts: int = 4,
) -> list[dict[str, str]]:
    """Read an exporter CSV while tolerating one concurrent rewrite."""
    if not path.is_file():
        raise ValueError(f"CSV file not found: {path}")

    last_error = "unknown read error"
    for attempt in range(attempts):
        try:
            before = path.stat()
            text = path.read_text(encoding="utf-8-sig")
            after = path.stat()
            if before.st_mtime_ns != after.st_mtime_ns or before.st_size != after.st_size:
                raise ValueError("file changed during read")
            reader = csv.DictReader(io.StringIO(text))
            fields = list(reader.fieldnames or ())
            missing = [field for field in required_fields if field not in fields]
            if missing:
                raise ValueError(f"missing fields: {', '.join(missing)}")
            rows = [
                {key: _text(value) for key, value in dict(row).items()}
                for row in reader
                if any(_text(value) for value in dict(row).values())
            ]
            if not rows and not allow_empty:
                raise ValueError("CSV has no data rows")
            return rows
        except (OSError, UnicodeError, csv.Error, ValueError) as exc:
            last_error = str(exc)
            if attempt + 1 < attempts:
                time.sleep(0.08)
    raise ValueError(f"cannot read stable CSV {path}: {last_error}")


def _latest_batch(rows: Sequence[dict[str, str]]) -> tuple[datetime, list[dict[str, str]]]:
    if not rows:
        raise ValueError("latest batch requested from empty rows")
    latest_msc = max(_int(row.get("time_msc")) for row in rows)
    if latest_msc <= 0:
        raise ValueError("snapshot rows have no valid time_msc")
    return _from_millis(latest_msc), [
        dict(row) for row in rows if _int(row.get("time_msc")) == latest_msc
    ]


def _snapshot_age_seconds(captured_at: datetime, now: datetime) -> float:
    return (now - captured_at.astimezone(timezone.utc)).total_seconds()


def _assert_fresh(
    label: str,
    captured_at: datetime,
    now: datetime,
    maximum_age_seconds: float,
    maximum_clock_skew_seconds: float,
) -> float:
    age = _snapshot_age_seconds(captured_at, now)
    if age < -maximum_clock_skew_seconds:
        raise ValueError(f"{label} snapshot is {-age:.1f}s in the future")
    if age > maximum_age_seconds:
        raise ValueError(
            f"{label} snapshot is stale: {age:.1f}s > {maximum_age_seconds:.1f}s"
        )
    return max(0.0, age)


def _load_mapping(path: Path | None) -> Mapping[str, Any]:
    if path is None:
        return {}
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, Mapping):
        raise ValueError("correlation mapping root must be an object")
    return payload


def _correlation_group(mapping: Mapping[str, Any], symbol: str, action: str) -> str:
    symbols = mapping.get("symbols", {})
    if isinstance(symbols, Mapping):
        configured = symbols.get(symbol)
        if isinstance(configured, str) and _text(configured):
            return _text(configured).upper()
        if isinstance(configured, Mapping):
            directional = configured.get(action)
            if _text(directional):
                return _text(directional).upper()
            generic = configured.get("DEFAULT")
            if _text(generic):
                return _text(generic).upper()
    return f"SYMBOL:{symbol}"


def _passport_identity(path: Path) -> tuple[str, str]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, Mapping):
        raise ValueError("passport root must be an object")
    candidate = payload.get("candidate", payload)
    if not isinstance(candidate, Mapping):
        raise ValueError("passport candidate must be an object")
    plan = candidate.get("plan", {})
    if not isinstance(plan, Mapping):
        raise ValueError("passport plan must be an object")
    symbol = _text(candidate.get("symbol")).upper()
    action = _text(plan.get("action")).upper()
    if not symbol or action not in VALID_ACTIONS:
        raise ValueError("passport must contain symbol and BUY/SELL plan action")
    return symbol, action


@dataclass(frozen=True, slots=True)
class AdapterBundle:
    account: AccountSnapshot
    portfolio: PortfolioSnapshot
    instrument: InstrumentSpec
    status: Mapping[str, Any]

    def write(self, output_dir: Path) -> dict[str, Path]:
        account_path = output_dir / "account.json"
        portfolio_path = output_dir / "portfolio.json"
        instrument_path = output_dir / f"instrument_{self.instrument.symbol}.json"
        status_path = output_dir / "status.json"
        _atomic_json(account_path, self.account.as_dict())
        _atomic_json(portfolio_path, self.portfolio.as_dict())
        _atomic_json(instrument_path, self.instrument.as_dict())
        _atomic_json(status_path, dict(self.status))
        return {
            "account": account_path,
            "portfolio": portfolio_path,
            "instrument": instrument_path,
            "status": status_path,
        }


def _build_account(
    rows: Sequence[dict[str, str]],
) -> tuple[AccountSnapshot, dict[str, Any], list[str]]:
    latest_time, latest_rows = _latest_batch(rows)
    latest = latest_rows[-1]
    login = _text(latest.get("account_login"))
    currency = _text(latest.get("currency")).upper()
    server = _text(latest.get("server"))
    if not login or not currency:
        raise ValueError("latest account row has no login or currency")

    scoped = [
        row
        for row in rows
        if _text(row.get("account_login")) == login
        and _text(row.get("currency")).upper() == currency
    ]
    if not scoped:
        raise ValueError("account history has no rows for latest login/currency")
    history = sorted(scoped, key=lambda row: _int(row.get("time_msc")))
    equity_values = [_number(row.get("equity")) for row in history]
    if any(value <= 0 for value in equity_values):
        raise ValueError("account history contains non-positive equity")
    high_watermark = max(equity_values)
    latest_day = latest_time.date()
    day_rows = [
        row for row in history if _from_millis(row.get("time_msc")).date() == latest_day
    ]
    day_start_equity = _number((day_rows or [latest])[0].get("equity"))
    trade_allowed = _bool(latest.get("trade_allowed"))
    terminal_connected = _bool(latest.get("terminal_connected"))

    account = AccountSnapshot(
        account_id=login,
        venue="MT5",
        currency=currency,
        captured_at=latest_time,
        balance=_number(latest.get("balance")),
        equity=_number(latest.get("equity")),
        free_margin=_number(latest.get("free_margin")),
        margin_used=_number(latest.get("margin")),
        high_watermark_equity=high_watermark,
        day_start_equity=day_start_equity,
        trading_enabled=trade_allowed and terminal_connected,
    )
    warnings: list[str] = []
    if not terminal_connected:
        warnings.append("MT5 terminal reports no broker connection")
    if not trade_allowed:
        warnings.append("MT5 account reports trading disabled")
    metadata = {
        "captured_at": _iso(latest_time),
        "server": server,
        "leverage": _int(latest.get("leverage")),
        "reported_open_positions": _int(latest.get("open_positions")),
        "history_rows": len(history),
    }
    return account, metadata, warnings


def _symbol_rows_by_name(rows: Sequence[dict[str, str]]) -> dict[str, dict[str, str]]:
    return {
        _text(row.get("symbol")).upper(): dict(row)
        for row in rows
        if _text(row.get("symbol"))
    }


def _tick_value_loss(row: Mapping[str, Any]) -> float:
    values = (
        _number(row.get("tick_value_loss")),
        _number(row.get("tick_value")),
        _number(row.get("tick_value_profit")),
    )
    return next((value for value in values if value > 0), 0.0)


def _position_risk(
    row: Mapping[str, Any],
    spec: Mapping[str, Any],
) -> tuple[float | None, str | None]:
    side = _text(row.get("side")).upper()
    stop = _number(row.get("sl"))
    volume = _number(row.get("volume"))
    current = _number(row.get("current_price"))
    if current <= 0:
        current = _number(row.get("open_price"))
    tick_size = _number(spec.get("tick_size"))
    tick_value = _tick_value_loss(spec)
    if side not in VALID_ACTIONS:
        return None, "position side is not BUY or SELL"
    if stop <= 0:
        return None, "position has no stop loss"
    if volume <= 0 or current <= 0 or tick_size <= 0 or tick_value <= 0:
        return None, "position or symbol risk inputs are incomplete"
    distance = max(0.0, current - stop) if side == "BUY" else max(0.0, stop - current)
    return distance / tick_size * tick_value * volume, None


def _build_portfolio(
    rows: Sequence[dict[str, str]],
    symbol_rows: Mapping[str, dict[str, str]],
    mapping: Mapping[str, Any],
    account: AccountSnapshot,
    reported_open_positions: int,
) -> tuple[PortfolioSnapshot, list[str]]:
    positions: list[PositionRisk] = []
    warnings: list[str] = []
    for row in rows:
        if _text(row.get("account_login")) != account.account_id:
            raise ValueError("position snapshot login does not match account snapshot")
        if _text(row.get("currency")).upper() != account.currency:
            raise ValueError("position snapshot currency does not match account snapshot")
        symbol = _text(row.get("symbol")).upper()
        if not symbol:
            continue
        side = _text(row.get("side")).upper()
        spec = symbol_rows.get(symbol)
        risk_money: float | None = None
        issue: str | None = None
        if spec is None:
            issue = "symbol specification is missing"
        else:
            risk_money, issue = _position_risk(row, spec)
        if issue:
            warnings.append(f"{symbol} position {_text(row.get('position_ticket'))}: {issue}")
        position_id = _text(row.get("position_id")) or _text(row.get("position_ticket"))
        positions.append(
            PositionRisk(
                position_id=position_id,
                symbol=symbol,
                correlation_group=_correlation_group(mapping, symbol, side),
                risk_money=(round(risk_money, 8) if risk_money is not None else None),
                margin_used=0.0,
                action=side,
            )
        )

    effective_count = max(reported_open_positions, len(positions))
    if reported_open_positions != len(positions):
        warnings.append(
            "account open-position count differs from the latest position snapshot: "
            f"account={reported_open_positions}, rows={len(positions)}"
        )
    return (
        PortfolioSnapshot(
            positions=tuple(positions),
            open_trades=effective_count,
            reserved_risk_money=0.0,
            reserved_margin=0.0,
        ),
        warnings,
    )


def _build_instrument(
    row: Mapping[str, Any],
    *,
    action: str,
    account: AccountSnapshot,
    mapping: Mapping[str, Any],
) -> InstrumentSpec:
    symbol = _text(row.get("symbol")).upper()
    if _text(row.get("account_login")) != account.account_id:
        raise ValueError("instrument snapshot login does not match account snapshot")
    if _text(row.get("currency")).upper() != account.currency:
        raise ValueError("instrument snapshot currency does not match account snapshot")

    trade_mode = _text(row.get("trade_mode")).upper()
    if trade_mode in {"DISABLED", "CLOSEONLY"}:
        raise ValueError(f"{symbol} trade mode is {trade_mode}")
    if action == "BUY" and trade_mode == "SHORTONLY":
        raise ValueError(f"{symbol} allows short positions only")
    if action == "SELL" and trade_mode == "LONGONLY":
        raise ValueError(f"{symbol} allows long positions only")

    tick_value = _tick_value_loss(row)
    margin_field = (
        "margin_buy_per_volume" if action == "BUY" else "margin_sell_per_volume"
    )
    margin_per_volume = _optional_positive(row.get(margin_field))
    if margin_per_volume is None:
        margin_per_volume = _optional_positive(row.get("margin_initial"))

    return InstrumentSpec(
        symbol=symbol,
        venue="MT5",
        account_currency=account.currency,
        tick_size=_number(row.get("tick_size")),
        tick_value_per_volume=tick_value,
        volume_min=_number(row.get("volume_min")),
        volume_max=_number(row.get("volume_max")),
        volume_step=_number(row.get("volume_step")),
        correlation_group=_correlation_group(mapping, symbol, action),
        margin_per_volume=margin_per_volume,
        contract_size=_optional_positive(row.get("contract_size")),
        leverage=_optional_positive(row.get("leverage")),
        quote_to_account_rate=1.0,
    )


def adapt_mt5_exports(
    *,
    account_csv: Path,
    positions_csv: Path,
    symbols_csv: Path,
    symbol: str,
    action: str,
    correlations: Path | None = None,
    maximum_age_seconds: float = 120.0,
    maximum_clock_skew_seconds: float = 30.0,
    now: datetime | None = None,
) -> AdapterBundle:
    captured_now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    requested_symbol = _text(symbol).upper()
    requested_action = _text(action).upper()
    if not requested_symbol or requested_action not in VALID_ACTIONS:
        raise ValueError("symbol and BUY/SELL action are required")
    if maximum_age_seconds <= 0 or maximum_clock_skew_seconds < 0:
        raise ValueError("freshness limits are invalid")

    account_rows = _read_csv_stable(account_csv, ACCOUNT_REQUIRED_FIELDS)
    position_rows = _read_csv_stable(
        positions_csv,
        POSITION_REQUIRED_FIELDS,
        allow_empty=True,
    )
    symbol_rows = _read_csv_stable(symbols_csv, SYMBOL_REQUIRED_FIELDS)
    mapping = _load_mapping(correlations)

    account, account_meta, warnings = _build_account(account_rows)
    account_age = _assert_fresh(
        "account",
        account.captured_at,
        captured_now,
        maximum_age_seconds,
        maximum_clock_skew_seconds,
    )

    symbol_time, latest_symbols = _latest_batch(symbol_rows)
    symbol_age = _assert_fresh(
        "symbol",
        symbol_time,
        captured_now,
        maximum_age_seconds,
        maximum_clock_skew_seconds,
    )
    symbols_by_name = _symbol_rows_by_name(latest_symbols)
    selected = symbols_by_name.get(requested_symbol)
    if selected is None:
        raise ValueError(
            f"symbol {requested_symbol} is absent from the latest MT5 Market Watch snapshot"
        )

    if position_rows:
        position_time, latest_positions = _latest_batch(position_rows)
    else:
        position_time = datetime.fromtimestamp(
            positions_csv.stat().st_mtime,
            tz=timezone.utc,
        )
        latest_positions = []
    position_age = _assert_fresh(
        "position",
        position_time,
        captured_now,
        maximum_age_seconds,
        maximum_clock_skew_seconds,
    )

    portfolio, portfolio_warnings = _build_portfolio(
        latest_positions,
        symbols_by_name,
        mapping,
        account,
        _int(account_meta.get("reported_open_positions")),
    )
    warnings.extend(portfolio_warnings)
    instrument = _build_instrument(
        selected,
        action=requested_action,
        account=account,
        mapping=mapping,
    )

    status = {
        "schema_version": ADAPTER_VERSION,
        "state": "READY",
        "captured_at": _iso(captured_now),
        "account_id": account.account_id,
        "server": account_meta.get("server", ""),
        "currency": account.currency,
        "requested_symbol": requested_symbol,
        "requested_action": requested_action,
        "account_snapshot_age_seconds": round(account_age, 3),
        "position_snapshot_age_seconds": round(position_age, 3),
        "symbol_snapshot_age_seconds": round(symbol_age, 3),
        "account_history_rows": account_meta.get("history_rows", 0),
        "latest_position_rows": len(latest_positions),
        "latest_symbol_rows": len(latest_symbols),
        "portfolio_risk_complete": portfolio.risk_complete,
        "warnings": warnings,
        "sources": {
            "account_csv": str(account_csv),
            "positions_csv": str(positions_csv),
            "symbols_csv": str(symbols_csv),
            "correlations": str(correlations) if correlations else "",
        },
        "safety": {
            "read_only": True,
            "orders_enabled": False,
            "broker_api_called": False,
            "terminal_settings_changed": False,
            "robot_settings_changed": False,
        },
    }
    return AdapterBundle(
        account=account,
        portfolio=portfolio,
        instrument=instrument,
        status=status,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="TradeMind MT5 Read-Only Risk Adapter 1.0"
    )
    parser.add_argument("--account-csv", type=Path, required=True)
    parser.add_argument("--positions-csv", type=Path, required=True)
    parser.add_argument("--symbols-csv", type=Path, required=True)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--passport", type=Path)
    source.add_argument("--symbol")
    parser.add_argument("--action")
    parser.add_argument("--correlations", type=Path)
    parser.add_argument("--maximum-age-seconds", type=float, default=120.0)
    parser.add_argument("--maximum-clock-skew-seconds", type=float, default=30.0)
    parser.add_argument("--now")
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)

    try:
        if args.passport is not None:
            symbol, action = _passport_identity(args.passport)
        else:
            symbol = _text(args.symbol)
            action = _text(args.action)
            if not action:
                raise ValueError("--action is required with --symbol")
        bundle = adapt_mt5_exports(
            account_csv=args.account_csv,
            positions_csv=args.positions_csv,
            symbols_csv=args.symbols_csv,
            symbol=symbol,
            action=action,
            correlations=args.correlations,
            maximum_age_seconds=args.maximum_age_seconds,
            maximum_clock_skew_seconds=args.maximum_clock_skew_seconds,
            now=_parse_time(args.now) if args.now else None,
        )
        paths = bundle.write(args.output_dir)
    except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError) as exc:
        print(f"MT5 risk adapter failed: {exc}")
        return 1

    print("TradeMind MT5 Read-Only Risk Adapter 1.0")
    print("CSV snapshots only. Orders OFF. Broker API not called.")
    print(f"Account: {bundle.account.account_id} ({bundle.account.currency})")
    print(f"Instrument: {bundle.instrument.symbol} {action.upper()}")
    print(
        "Open positions / known stop risk: "
        f"{bundle.portfolio.effective_open_trades}/"
        f"{sum(item.risk_money is not None for item in bundle.portfolio.positions)}"
    )
    print(f"Portfolio risk complete: {bundle.portfolio.risk_complete}")
    for warning in bundle.status.get("warnings", []):
        print(f"WARN: {warning}")
    for name, path in paths.items():
        print(f"{name}: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
