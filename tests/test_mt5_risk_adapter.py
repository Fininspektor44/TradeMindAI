from __future__ import annotations

import csv
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from trademind.mt5_risk_adapter import adapt_mt5_exports


NOW = datetime(2026, 8, 5, 18, 0, tzinfo=timezone.utc)
LOGIN = "37365712"


def _msc(value: datetime) -> int:
    return int(value.timestamp() * 1000)


def _write_csv(path: Path, fields: list[str], rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _account_fields() -> list[str]:
    return [
        "time_msc",
        "account_login",
        "server",
        "currency",
        "balance",
        "equity",
        "margin",
        "free_margin",
        "margin_level",
        "leverage",
        "open_positions",
        "trade_allowed",
        "terminal_connected",
    ]


def _position_fields() -> list[str]:
    return [
        "time_msc",
        "account_login",
        "server",
        "currency",
        "position_ticket",
        "position_id",
        "position_time_msc",
        "symbol",
        "magic",
        "side",
        "volume",
        "open_price",
        "current_price",
        "sl",
        "tp",
        "profit",
        "swap",
        "comment",
    ]


def _symbol_fields() -> list[str]:
    return [
        "time_msc",
        "account_login",
        "server",
        "currency",
        "symbol",
        "digits",
        "trade_mode",
        "bid",
        "ask",
        "tick_size",
        "tick_value",
        "tick_value_profit",
        "tick_value_loss",
        "volume_min",
        "volume_max",
        "volume_step",
        "contract_size",
        "margin_initial",
        "margin_maintenance",
        "margin_buy_per_volume",
        "margin_sell_per_volume",
        "leverage",
    ]


def _account_row(
    captured_at: datetime,
    *,
    balance: float = 10_000.0,
    equity: float = 10_000.0,
    margin: float = 0.0,
    free_margin: float = 10_000.0,
    open_positions: int = 1,
    trade_allowed: int = 1,
    terminal_connected: int = 1,
) -> dict[str, object]:
    return {
        "time_msc": _msc(captured_at),
        "account_login": LOGIN,
        "server": "RoboForex-Demo",
        "currency": "USD",
        "balance": balance,
        "equity": equity,
        "margin": margin,
        "free_margin": free_margin,
        "margin_level": 0,
        "leverage": 500,
        "open_positions": open_positions,
        "trade_allowed": trade_allowed,
        "terminal_connected": terminal_connected,
    }


def _position_row(
    captured_at: datetime,
    *,
    symbol: str = "EURUSD",
    side: str = "BUY",
    volume: float = 0.10,
    current_price: float = 1.1000,
    stop: float = 1.0950,
) -> dict[str, object]:
    return {
        "time_msc": _msc(captured_at),
        "account_login": LOGIN,
        "server": "RoboForex-Demo",
        "currency": "USD",
        "position_ticket": "1001",
        "position_id": "9001",
        "position_time_msc": _msc(captured_at - timedelta(hours=1)),
        "symbol": symbol,
        "magic": 0,
        "side": side,
        "volume": volume,
        "open_price": 1.0980,
        "current_price": current_price,
        "sl": stop,
        "tp": 1.1100,
        "profit": 20.0,
        "swap": 0.0,
        "comment": "manual",
    }


def _symbol_row(
    captured_at: datetime,
    *,
    symbol: str = "EURUSD",
    trade_mode: str = "FULL",
) -> dict[str, object]:
    return {
        "time_msc": _msc(captured_at),
        "account_login": LOGIN,
        "server": "RoboForex-Demo",
        "currency": "USD",
        "symbol": symbol,
        "digits": 5,
        "trade_mode": trade_mode,
        "bid": 1.0999,
        "ask": 1.1000,
        "tick_size": 0.0001,
        "tick_value": 10.0,
        "tick_value_profit": 10.0,
        "tick_value_loss": 10.0,
        "volume_min": 0.01,
        "volume_max": 100.0,
        "volume_step": 0.01,
        "contract_size": 100_000.0,
        "margin_initial": 0.0,
        "margin_maintenance": 0.0,
        "margin_buy_per_volume": 220.0,
        "margin_sell_per_volume": 219.8,
        "leverage": 500,
    }


def _files(
    tmp_path: Path,
    *,
    account_rows: list[dict[str, object]] | None = None,
    position_rows: list[dict[str, object]] | None = None,
    symbol_rows: list[dict[str, object]] | None = None,
) -> tuple[Path, Path, Path]:
    captured = NOW - timedelta(seconds=10)
    account = tmp_path / "mt5_risk_account.csv"
    positions = tmp_path / "mt5_risk_positions.csv"
    symbols = tmp_path / "mt5_risk_symbols.csv"
    _write_csv(
        account,
        _account_fields(),
        account_rows if account_rows is not None else [_account_row(captured)],
    )
    _write_csv(
        positions,
        _position_fields(),
        position_rows if position_rows is not None else [_position_row(captured)],
    )
    _write_csv(
        symbols,
        _symbol_fields(),
        symbol_rows if symbol_rows is not None else [_symbol_row(captured)],
    )
    return account, positions, symbols


def test_adapter_builds_account_portfolio_and_instrument(tmp_path: Path) -> None:
    account_rows = [
        _account_row(NOW - timedelta(days=1), equity=9_800.0),
        _account_row(NOW.replace(hour=0, minute=5), equity=9_950.0),
        _account_row(
            NOW - timedelta(seconds=10),
            balance=10_000.0,
            equity=9_900.0,
            margin=300.0,
            free_margin=9_600.0,
        ),
        _account_row(NOW - timedelta(hours=2), equity=10_100.0),
    ]
    account, positions, symbols = _files(tmp_path, account_rows=account_rows)

    bundle = adapt_mt5_exports(
        account_csv=account,
        positions_csv=positions,
        symbols_csv=symbols,
        symbol="EURUSD",
        action="BUY",
        now=NOW,
    )

    assert bundle.account.account_id == LOGIN
    assert bundle.account.balance == pytest.approx(10_000.0)
    assert bundle.account.equity == pytest.approx(9_900.0)
    assert bundle.account.high_watermark_equity == pytest.approx(10_100.0)
    assert bundle.account.day_start_equity == pytest.approx(9_950.0)
    assert bundle.account.margin_used == pytest.approx(300.0)
    assert bundle.account.free_margin == pytest.approx(9_600.0)
    assert bundle.account.trading_enabled is True

    assert bundle.portfolio.effective_open_trades == 1
    assert bundle.portfolio.risk_complete is True
    assert bundle.portfolio.positions[0].risk_money == pytest.approx(50.0)
    assert bundle.portfolio.positions[0].action == "BUY"

    assert bundle.instrument.symbol == "EURUSD"
    assert bundle.instrument.tick_value_per_volume == pytest.approx(10.0)
    assert bundle.instrument.margin_per_volume == pytest.approx(220.0)
    assert bundle.instrument.volume_step == pytest.approx(0.01)
    assert bundle.status["state"] == "READY"
    assert bundle.status["safety"]["orders_enabled"] is False
    assert bundle.status["safety"]["broker_api_called"] is False


def test_position_without_stop_marks_portfolio_risk_incomplete(tmp_path: Path) -> None:
    captured = NOW - timedelta(seconds=10)
    account, positions, symbols = _files(
        tmp_path,
        position_rows=[_position_row(captured, stop=0.0)],
    )

    bundle = adapt_mt5_exports(
        account_csv=account,
        positions_csv=positions,
        symbols_csv=symbols,
        symbol="EURUSD",
        action="BUY",
        now=NOW,
    )

    assert bundle.portfolio.risk_complete is False
    assert bundle.portfolio.positions[0].risk_money is None
    assert any("no stop loss" in warning for warning in bundle.status["warnings"])


def test_directional_correlation_mapping_is_applied(tmp_path: Path) -> None:
    account, positions, symbols = _files(tmp_path)
    mapping = tmp_path / "correlations.json"
    mapping.write_text(
        json.dumps(
            {
                "symbols": {
                    "EURUSD": {
                        "BUY": "FX_USD_SHORT",
                        "SELL": "FX_USD_LONG",
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    bundle = adapt_mt5_exports(
        account_csv=account,
        positions_csv=positions,
        symbols_csv=symbols,
        symbol="EURUSD",
        action="BUY",
        correlations=mapping,
        now=NOW,
    )

    assert bundle.instrument.correlation_group == "FX_USD_SHORT"
    assert bundle.portfolio.positions[0].correlation_group == "FX_USD_SHORT"


def test_stale_symbol_snapshot_fails_closed(tmp_path: Path) -> None:
    account, positions, symbols = _files(
        tmp_path,
        symbol_rows=[_symbol_row(NOW - timedelta(minutes=5))],
    )

    with pytest.raises(ValueError, match="symbol snapshot is stale"):
        adapt_mt5_exports(
            account_csv=account,
            positions_csv=positions,
            symbols_csv=symbols,
            symbol="EURUSD",
            action="BUY",
            now=NOW,
        )


def test_requested_symbol_must_exist_in_latest_market_watch_snapshot(
    tmp_path: Path,
) -> None:
    account, positions, symbols = _files(tmp_path)

    with pytest.raises(ValueError, match="absent from the latest MT5 Market Watch"):
        adapt_mt5_exports(
            account_csv=account,
            positions_csv=positions,
            symbols_csv=symbols,
            symbol="XAUUSD",
            action="BUY",
            now=NOW,
        )


def test_empty_position_snapshot_uses_fresh_file_timestamp(tmp_path: Path) -> None:
    captured = NOW - timedelta(seconds=10)
    account, positions, symbols = _files(
        tmp_path,
        account_rows=[_account_row(captured, open_positions=0)],
        position_rows=[],
    )
    os.utime(positions, (captured.timestamp(), captured.timestamp()))

    bundle = adapt_mt5_exports(
        account_csv=account,
        positions_csv=positions,
        symbols_csv=symbols,
        symbol="EURUSD",
        action="SELL",
        now=NOW,
    )

    assert bundle.portfolio.positions == ()
    assert bundle.portfolio.effective_open_trades == 0
    assert bundle.portfolio.risk_complete is True
    assert bundle.instrument.margin_per_volume == pytest.approx(219.8)


def test_trade_disabled_account_is_preserved_for_risk_manager_block(
    tmp_path: Path,
) -> None:
    captured = NOW - timedelta(seconds=10)
    account, positions, symbols = _files(
        tmp_path,
        account_rows=[
            _account_row(
                captured,
                trade_allowed=0,
                terminal_connected=1,
            )
        ],
    )

    bundle = adapt_mt5_exports(
        account_csv=account,
        positions_csv=positions,
        symbols_csv=symbols,
        symbol="EURUSD",
        action="BUY",
        now=NOW,
    )

    assert bundle.account.trading_enabled is False
    assert any("trading disabled" in warning for warning in bundle.status["warnings"])


def test_incompatible_symbol_trade_mode_fails_closed(tmp_path: Path) -> None:
    captured = NOW - timedelta(seconds=10)
    account, positions, symbols = _files(
        tmp_path,
        symbol_rows=[_symbol_row(captured, trade_mode="SHORTONLY")],
    )

    with pytest.raises(ValueError, match="short positions only"):
        adapt_mt5_exports(
            account_csv=account,
            positions_csv=positions,
            symbols_csv=symbols,
            symbol="EURUSD",
            action="BUY",
            now=NOW,
        )


def test_bundle_writes_risk_manager_payloads(tmp_path: Path) -> None:
    account, positions, symbols = _files(tmp_path)
    bundle = adapt_mt5_exports(
        account_csv=account,
        positions_csv=positions,
        symbols_csv=symbols,
        symbol="EURUSD",
        action="BUY",
        now=NOW,
    )

    paths = bundle.write(tmp_path / "output")

    assert set(paths) == {"account", "portfolio", "instrument", "status"}
    account_payload = json.loads(paths["account"].read_text(encoding="utf-8"))
    portfolio_payload = json.loads(paths["portfolio"].read_text(encoding="utf-8"))
    instrument_payload = json.loads(paths["instrument"].read_text(encoding="utf-8"))
    assert account_payload["venue"] == "MT5"
    assert portfolio_payload["risk_complete"] is True
    assert instrument_payload["symbol"] == "EURUSD"


def test_mql_exporter_contains_no_trading_calls() -> None:
    source = Path("mt5/TradeMind_MT5_Risk_Snapshot_Exporter.mq5").read_text(
        encoding="utf-8"
    )

    forbidden = (
        "OrderSend(",
        "OrderSendAsync(",
        "PositionModify(",
        "PositionClose(",
        "CTrade ",
        ".Buy(",
        ".Sell(",
    )
    assert not any(token in source for token in forbidden)
    assert "OrderCalcMargin(" in source
    assert "ACCOUNT_MARGIN_FREE" in source
    assert "SYMBOL_TRADE_TICK_VALUE_LOSS" in source
