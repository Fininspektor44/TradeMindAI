from __future__ import annotations

import csv
import json
from pathlib import Path

from trademind import breakeven_stat_monitor as monitor

FIELDS = [
    "time_msc",
    "account_login",
    "currency",
    "position_ticket",
    "position_id",
    "symbol",
    "magic",
    "side",
    "volume",
    "open_price",
    "current_price",
    "sl",
]


def write_snapshot(path: Path, *, time_msc: int, current: float, present: bool = True) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        if present:
            writer.writerow(
                {
                    "time_msc": time_msc,
                    "account_login": "77053345",
                    "currency": "USD",
                    "position_ticket": "1001",
                    "position_id": "501",
                    "symbol": "EURUSD",
                    "magic": "777270003",
                    "side": "BUY",
                    "volume": "0.10",
                    "open_price": "1.1000",
                    "current_price": str(current),
                    "sl": "1.0900",
                }
            )


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_shadow_be_trigger_and_revisit_are_recorded(tmp_path: Path) -> None:
    snapshot = tmp_path / "positions.csv"
    output = tmp_path / "out"

    write_snapshot(snapshot, time_msc=1_780_000_000_000, current=1.1050)
    first = monitor.run_monitor(snapshot, output)
    assert first["be_triggered_epochs"] == 0

    write_snapshot(snapshot, time_msc=1_780_000_060_000, current=1.1101)
    second = monitor.run_monitor(snapshot, output)
    assert second["be_triggered_epochs"] == 1
    assert second["be_revisited_after_trigger_epochs"] == 0

    write_snapshot(snapshot, time_msc=1_780_000_120_000, current=1.0999)
    third = monitor.run_monitor(snapshot, output)
    assert third["be_triggered_epochs"] == 1
    assert third["be_revisited_after_trigger_epochs"] == 1

    events = [json.loads(line) for line in (output / "events.jsonl").read_text().splitlines()]
    names = [event["event"] for event in events]
    assert names == ["EPOCH_STARTED", "BE_TRIGGER_REACHED", "BE_REVISIT_OBSERVED"]


def test_monitor_never_fabricates_risk_without_initial_sl(tmp_path: Path) -> None:
    snapshot = tmp_path / "positions.csv"
    output = tmp_path / "out"
    write_snapshot(snapshot, time_msc=1_780_000_000_000, current=1.1200)

    rows = list(csv.DictReader(snapshot.open(encoding="utf-8")))
    rows[0]["sl"] = "0"
    with snapshot.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)

    status = monitor.run_monitor(snapshot, output)
    assert status["trackable_basket_epochs"] == 0
    assert status["be_triggered_epochs"] == 0


def test_position_disappearance_does_not_invent_trade_outcome(tmp_path: Path) -> None:
    snapshot = tmp_path / "positions.csv"
    output = tmp_path / "out"
    write_snapshot(snapshot, time_msc=1_780_000_000_000, current=1.1110)
    monitor.run_monitor(snapshot, output)

    write_snapshot(snapshot, time_msc=1_780_000_060_000, current=1.1110, present=False)
    status = monitor.run_monitor(snapshot, output)
    assert status["open_trackable_epochs"] == 0

    state = load_json(output / "state.json")
    epoch = next(iter(state["epochs"].values()))
    assert epoch["state"] == "NO_LONGER_OPEN"

    events = [json.loads(line) for line in (output / "events.jsonl").read_text().splitlines()]
    closing = events[-1]
    assert closing["event"] == "EPOCH_NO_LONGER_OPEN"
    assert closing["final_trade_result"] == "NOT_INFERRED_FROM_POSITION_SNAPSHOT"


def test_safety_contract_is_explicit(tmp_path: Path) -> None:
    snapshot = tmp_path / "positions.csv"
    output = tmp_path / "out"
    write_snapshot(snapshot, time_msc=1_780_000_000_000, current=1.1000)
    status = monitor.run_monitor(snapshot, output)
    assert status["mode"] == "READ_ONLY_SHADOW_BREAKEVEN_STATISTICS"
    assert status["safety"] == {
        "read_only": True,
        "shadow_only": True,
        "orders_enabled": False,
        "position_modify_called": False,
        "broker_api_called": False,
        "source_csv_modified": False,
        "robot_settings_modified": False,
    }


def test_source_contains_no_execution_api() -> None:
    source = Path(monitor.__file__).read_text(encoding="utf-8")
    forbidden = ("MetaTrader5", "OrderSend", "PositionModify", "PositionClose")
    for token in forbidden:
        assert token not in source
