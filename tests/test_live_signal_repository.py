from __future__ import annotations

import csv
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from trademind.live_signal_repository import LiveSignalRepository


def _write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        fieldnames = list(dict.fromkeys(key for row in rows for key in row))
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def test_loads_mt5_and_bybit_without_losing_trade_levels(tmp_path: Path) -> None:
    now = datetime(2026, 8, 4, 9, 0, tzinfo=timezone.utc)
    unified = tmp_path / "unified" / "signals.csv"
    bybit = tmp_path / "bybit_shadow_v1_11" / "signals.csv"
    mt5_status = tmp_path / "mt5_status.json"
    bybit_status = tmp_path / "bybit_status.json"
    _write_csv(
        unified,
        [
            {
                "event_id": "FX:obs-1:BOS_PLUS_SWEEP:H1",
                "signal_key": "FX:obs-1:BOS_PLUS_SWEEP",
                "signal_time": "2026-08-04T08:55:00+00:00",
                "source": "FX_RESEARCH",
                "source_id": "obs-1",
                "symbol": "XAUUSD",
                "timeframe": "M5",
                "action": "BUY",
                "scenario": "BOS_PLUS_SWEEP",
                "scenario_family": "COMBINATION",
                "components": "LIQUIDITY_SWEEP|SWING_BOS",
                "quality_score": "82",
                "entry_price": "2418.50",
                "stop_price": "2412.30",
                "target_price": "2430.90",
                "rr": "2.0",
                "horizon": "H1",
                "outcome": "",
                "result": "",
                "mfe": "0.7",
                "mae": "-0.2",
                "completed": "0",
                "reasons": "sweep then structure break",
            }
        ],
    )
    _write_csv(
        bybit,
        [
            {
                "paper_signal_id": "BTCUSDT:1800:CONTROL",
                "signal_time": "2026-08-04T08:30:00+00:00",
                "symbol": "BTCUSDT",
                "timeframe": "M5",
                "action": "SELL",
                "scenario": "MTF_FLOW_ALIGNMENT__CONTROL",
                "components": "H1_PRICE_TREND|M5_BOOK",
                "quality_score": "91",
                "entry_price": "114000",
                "stop_price": "114500",
                "target_price": "113000",
                "planned_rr": "2",
                "outcome": "WIN",
                "result_r": "2.0",
                "mfe_r": "2.4",
                "mae_r": "-0.3",
                "completed": "1",
                "completion_reason": "TARGET_FIRST",
            }
        ],
    )
    mt5_status.write_text(json.dumps({"updated_at": now.isoformat()}), encoding="utf-8")
    bybit_status.write_text(json.dumps({"updated_at": now.isoformat()}), encoding="utf-8")

    repository = LiveSignalRepository(
        unified_path=unified,
        bybit_paths=[bybit],
        status_paths={"MT5": mt5_status, "BYBIT": bybit_status},
    )
    snapshot = repository.load(now)

    assert not snapshot.errors
    assert len(snapshot.records) == 2
    mt5 = next(record for record in snapshot.records if record.source == "MT5")
    crypto = next(record for record in snapshot.records if record.source == "BYBIT")
    assert mt5.status == "NEW"
    assert mt5.entry_price == 2418.50
    assert mt5.stop_price == 2412.30
    assert mt5.target_price == 2430.90
    assert mt5.rr == 2.0
    assert mt5.components == ("LIQUIDITY_SWEEP", "SWING_BOS")
    assert crypto.status == "WIN"
    assert crypto.result == 2.0
    assert crypto.mfe == 2.4
    assert crypto.mae == -0.3
    assert mt5.freshness == crypto.freshness == "FRESH"


def test_stale_health_is_an_overlay_not_a_fake_trade_outcome(tmp_path: Path) -> None:
    now = datetime(2026, 8, 4, 9, 0, tzinfo=timezone.utc)
    unified = tmp_path / "signals.csv"
    status = tmp_path / "status.json"
    _write_csv(
        unified,
        [
            {
                "event_id": "event-1",
                "signal_key": "signal-1",
                "signal_time": "2026-08-04T08:00:00+00:00",
                "source": "FX_RESEARCH",
                "source_id": "obs-1",
                "symbol": "EURUSD",
                "timeframe": "M5",
                "action": "SELL",
                "scenario": "SWEEP",
                "scenario_family": "LIQUIDITY",
                "components": "LIQUIDITY_SWEEP",
                "quality_score": "75",
                "completed": "0",
            }
        ],
    )
    stale_time = now - timedelta(minutes=30)
    status.write_text(json.dumps({"generated_at": stale_time.isoformat()}), encoding="utf-8")

    snapshot = LiveSignalRepository(
        unified_path=unified,
        status_paths={"MT5": status},
        stale_after_seconds=600,
    ).load(now)
    record = snapshot.records[0]

    assert record.status == "ACTIVE"
    assert record.stale is True
    assert record.freshness == "STALE"
    assert record.outcome == ""


def test_filters_and_detail_lookup_are_deterministic(tmp_path: Path) -> None:
    now = datetime(2026, 8, 4, 9, 0, tzinfo=timezone.utc)
    unified = tmp_path / "signals.csv"
    rows = []
    for index, (symbol, action, score, outcome) in enumerate(
        [
            ("XAUUSD", "BUY", 85, "WIN"),
            ("EURUSD", "SELL", 65, "LOSS"),
            ("GBPUSD", "BUY", 78, ""),
        ],
        start=1,
    ):
        rows.append(
            {
                "event_id": f"event-{index}",
                "signal_key": f"signal-{index}",
                "signal_time": f"2026-08-04T08:{index:02d}:00+00:00",
                "source": "FX_RESEARCH",
                "source_id": f"obs-{index}",
                "symbol": symbol,
                "timeframe": "M5",
                "action": action,
                "scenario": "BASE_SIGNAL",
                "scenario_family": "BASE",
                "quality_score": str(score),
                "outcome": outcome,
                "result": "1" if outcome == "WIN" else "-1" if outcome == "LOSS" else "",
                "completed": "1" if outcome else "0",
            }
        )
    _write_csv(unified, rows)

    repository = LiveSignalRepository(unified_path=unified)
    snapshot = repository.load(now)
    selected = repository.list_records(
        snapshot,
        actions=["BUY"],
        min_score=80,
        statuses=["WIN"],
    )

    assert [record.event_id for record in selected] == ["event-1"]
    assert repository.get(snapshot, "event-2").symbol == "EURUSD"
    assert repository.get(snapshot, "missing") is None


def test_invalid_rows_are_reported_and_never_break_healthy_sources(tmp_path: Path) -> None:
    now = datetime(2026, 8, 4, 9, 0, tzinfo=timezone.utc)
    unified = tmp_path / "signals.csv"
    _write_csv(
        unified,
        [
            {
                "event_id": "broken",
                "signal_key": "broken",
                "signal_time": "2026-08-04 08:00:00",
                "source": "FX_RESEARCH",
                "source_id": "obs-broken",
                "symbol": "EURUSD",
                "action": "WAIT",
            },
            {
                "event_id": "healthy",
                "signal_key": "healthy",
                "signal_time": "2026-08-04T08:30:00+00:00",
                "source": "FX_RESEARCH",
                "source_id": "obs-healthy",
                "symbol": "GBPUSD",
                "action": "BUY",
                "quality_score": "70",
                "completed": "0",
            },
        ],
    )

    snapshot = LiveSignalRepository(unified_path=unified).load(now)

    assert [record.event_id for record in snapshot.records] == ["healthy"]
    assert len(snapshot.errors) == 1
    assert "timestamp must include timezone information" in snapshot.errors[0]


def test_module_is_read_only_and_has_no_order_submission_symbols() -> None:
    module = Path(__file__).resolve().parents[1] / "src" / "trademind" / "live_signal_repository.py"
    text = module.read_text(encoding="utf-8")
    forbidden = (
        "OrderSend",
        "OrderSendAsync",
        "CTrade",
        "trade.Buy",
        "trade.Sell",
        "PositionClose",
        "place_order",
        "create_order",
    )
    assert not any(token in text for token in forbidden)
    assert "with path.open(\"r\"" in text
