from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path

from trademind.live_signal_repository import LiveSignalRepository
from trademind.live_signal_server import LiveSignalService


def _write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def test_console_collapses_scenarios_and_horizons_into_one_trade_idea(tmp_path: Path) -> None:
    path = tmp_path / "signals.csv"
    rows: list[dict[str, object]] = []
    for scenario, family, score in (
        ("OTE_ALL", "OTE", 61),
        ("OTE_ZONE", "OTE", 63),
        ("BOS_PLUS_OTE", "COMBINATION", 68),
    ):
        for horizon, outcome, completed in (
            ("H3", "WIN", "1"),
            ("H12", "", "0"),
        ):
            rows.append(
                {
                    "event_id": f"SMC:signal-1:{scenario}:{horizon}",
                    "signal_key": f"SMC:signal-1:{scenario}",
                    "signal_time": "2026-08-04T09:50:00+00:00",
                    "source": "SMC_OTE",
                    "source_id": "signal-1",
                    "symbol": "ETHUSD",
                    "timeframe": "M5",
                    "action": "SELL",
                    "scenario": scenario,
                    "scenario_family": family,
                    "components": "BOS|OTE",
                    "quality_score": str(score),
                    "entry_price": "1858.00",
                    "stop_price": "1860.68",
                    "target_price": "1853.48",
                    "rr": "1.68",
                    "horizon": horizon,
                    "outcome": outcome,
                    "result": "1.0" if outcome else "",
                    "mfe": "1.2",
                    "mae": "-0.3",
                    "completed": completed,
                    "reasons": f"reason for {scenario}",
                }
            )
    _write_csv(path, rows)

    service = LiveSignalService(LiveSignalRepository(unified_path=path))
    snapshot = service.snapshot()

    assert len(snapshot.records) == 1
    record = snapshot.records[0]
    assert record.symbol == "ETHUSD"
    assert record.scenario == "BOS_PLUS_OTE +2"
    assert record.horizon == "H3/H12"
    assert record.score == 68
    assert record.status == "ACTIVE"
    assert record.components == ("BOS", "OTE")
    assert "Scenarios: BOS_PLUS_OTE · OTE_ALL · OTE_ZONE" in record.reasons
    assert "Horizons: H3 · H12" in record.reasons

    summary = service.summary(snapshot)
    assert summary["total"] == 1
    assert summary["by_status"] == {"ACTIVE": 1}


def test_collapse_is_read_only_and_keeps_source_csv_unchanged(tmp_path: Path) -> None:
    path = tmp_path / "signals.csv"
    rows = [
        {
            "event_id": "event-1",
            "signal_key": "signal-1",
            "signal_time": "2026-08-04T09:50:00+00:00",
            "source": "FX_RESEARCH",
            "source_id": "obs-1",
            "symbol": "XAUUSD",
            "timeframe": "M5",
            "action": "BUY",
            "scenario": "BASE_SIGNAL",
            "scenario_family": "BASE",
            "components": "",
            "quality_score": "70",
            "entry_price": "2400",
            "stop_price": "2390",
            "target_price": "2420",
            "rr": "2",
            "horizon": "H1",
            "outcome": "",
            "result": "",
            "mfe": "",
            "mae": "",
            "completed": "0",
            "reasons": "base",
        }
    ]
    _write_csv(path, rows)
    before = path.read_bytes()

    service = LiveSignalService(LiveSignalRepository(unified_path=path))
    service.snapshot()

    assert path.read_bytes() == before
    assert datetime.now(timezone.utc).tzinfo is not None
