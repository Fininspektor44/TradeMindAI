from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path

import pytest

from trademind.live_signal_repository import LiveSignalRepository


def _write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        fieldnames = list(dict.fromkeys(key for row in rows for key in row))
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _unified_row(
    *,
    source: str = "FX_RESEARCH",
    action: str = "BUY",
    entry: str = "1.10000",
    stop: str = "",
    target: str = "",
    rr: str = "",
) -> dict[str, object]:
    return {
        "event_id": "event-1",
        "signal_key": "signal-1",
        "signal_time": "2026-08-04T08:55:00+00:00",
        "source": source,
        "source_id": "obs-1",
        "symbol": "EURUSD",
        "timeframe": "M5",
        "action": action,
        "scenario": "BASE_SIGNAL",
        "scenario_family": "BASE",
        "quality_score": "80",
        "entry_price": entry,
        "stop_price": stop,
        "target_price": target,
        "rr": rr,
        "horizon": "H1",
        "completed": "0",
    }


def test_fx_research_missing_levels_gets_directional_atr_plan(tmp_path: Path) -> None:
    unified = tmp_path / "unified.csv"
    observations = tmp_path / "observations.csv"
    _write_csv(unified, [_unified_row()])
    _write_csv(
        observations,
        [
            {
                "observation_id": "obs-1",
                "entry_price": "1.10000",
                "atr": "0.00100",
                "spread_cost": "0.00010",
                "point": "0.00001",
            }
        ],
    )

    snapshot = LiveSignalRepository(
        unified_path=unified,
        fx_observations_path=observations,
    ).load(datetime(2026, 8, 4, 9, 0, tzinfo=timezone.utc))
    record = snapshot.records[0]

    assert record.plan_status == "READY"
    assert record.level_source == "ATR_PLAN_V1"
    assert record.evaluation_basis == "FIXED_HORIZON_ATR"
    assert record.entry_price == pytest.approx(1.10000)
    assert record.stop_price == pytest.approx(1.09900)
    assert record.target_price == pytest.approx(1.10150)
    assert record.rr == pytest.approx(1.5)
    assert record.atr == pytest.approx(0.001)
    assert record.risk_distance == pytest.approx(0.001)


def test_fx_sell_atr_plan_has_stop_above_and_target_below(tmp_path: Path) -> None:
    unified = tmp_path / "unified.csv"
    observations = tmp_path / "observations.csv"
    _write_csv(unified, [_unified_row(action="SELL")])
    _write_csv(
        observations,
        [
            {
                "observation_id": "obs-1",
                "entry_price": "1.10000",
                "atr": "0.00100",
                "spread_cost": "0.00010",
                "point": "0.00001",
            }
        ],
    )

    record = LiveSignalRepository(
        unified_path=unified,
        fx_observations_path=observations,
    ).load(datetime(2026, 8, 4, 9, 0, tzinfo=timezone.utc)).records[0]

    assert record.stop_price > record.entry_price > record.target_price
    assert record.stop_price == pytest.approx(1.10100)
    assert record.target_price == pytest.approx(1.09850)


def test_missing_or_zero_levels_are_none_not_fake_zero_prices(tmp_path: Path) -> None:
    unified = tmp_path / "unified.csv"
    _write_csv(
        unified,
        [_unified_row(entry="1.10000", stop="0", target="0", rr="0")],
    )

    record = LiveSignalRepository(unified_path=unified).load(
        datetime(2026, 8, 4, 9, 0, tzinfo=timezone.utc)
    ).records[0]

    assert record.entry_price == pytest.approx(1.1)
    assert record.stop_price is None
    assert record.target_price is None
    assert record.rr is None
    assert record.level_source == "MISSING"
    assert record.plan_status == "INCOMPLETE"


def test_valid_source_levels_are_preserved_and_filterable(tmp_path: Path) -> None:
    unified = tmp_path / "unified.csv"
    _write_csv(
        unified,
        [
            _unified_row(
                source="SMC_OTE",
                entry="1.10000",
                stop="1.09500",
                target="1.11000",
                rr="2",
            ),
            {
                **_unified_row(),
                "event_id": "event-2",
                "signal_key": "signal-2",
                "source_id": "obs-2",
            },
        ],
    )

    repository = LiveSignalRepository(unified_path=unified)
    snapshot = repository.load(datetime(2026, 8, 4, 9, 0, tzinfo=timezone.utc))
    ready = repository.list_records(snapshot, plan_statuses=["READY"])
    incomplete = repository.list_records(snapshot, plan_statuses=["INCOMPLETE"])

    assert [record.event_id for record in ready] == ["event-1"]
    assert ready[0].level_source == "SOURCE"
    assert ready[0].evaluation_basis == "STOP_TARGET_R"
    assert [record.event_id for record in incomplete] == ["event-2"]
