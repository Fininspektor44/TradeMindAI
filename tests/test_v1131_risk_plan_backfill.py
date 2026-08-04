from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from trademind.bybit_risk_plan_backfill import run_backfill
from trademind.bybit_risk_plan_experiments import ARMS


def _write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = sorted({key for row in rows for key in row})
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _bars(count: int = 44) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for index in range(count):
        start = (index + 1) * 300_000
        price = 100.0 - index * 0.08
        rows.append(
            {
                "symbol": "BTCUSDT",
                "start_ms": start,
                "end_ms": start + 299_999,
                "open": price + 0.02,
                "high": price + 0.12,
                "low": price - 0.12,
                "close": price,
                "spread_bps": 1.0,
            }
        )
    return rows


def _decision(start_ms: int, eligible: int = 1) -> dict[str, object]:
    entry = 99.0
    return {
        "decision_id": f"BTCUSDT:{start_ms}:STRICT_SELL",
        "signal_time": datetime.fromtimestamp(
            (start_ms + 299_999) / 1000, tz=timezone.utc
        ).isoformat(),
        "start_ms": start_ms,
        "end_ms": start_ms + 299_999,
        "source": "BYBIT",
        "source_id": "BYBIT_LINEAR_SHADOW_EXPERIMENTS",
        "symbol": "BTCUSDT",
        "action": "SELL",
        "scenario": "MTF_FLOW_ALIGNMENT__STRICT_SELL",
        "gate_status": "CANDIDATE" if eligible else "REJECTED",
        "quality_score": 91,
        "eligible": eligible,
        "components": "H1_PRICE_TREND|M15_DELTA|M5_DELTA_IMPULSE",
        "reasons": "STRICT_SELL confirmations passed",
        "entry_price": entry,
        "stop_price": entry + 0.25,
        "target_price": entry - 0.375,
        "risk_pct": 0.25 / entry,
        "rr": 1.5,
        "horizon_bars": 12,
        "m5_spread_bps": 1.0,
        "orders_enabled": 0,
    }


def test_backfill_uses_only_historical_candidates_and_keeps_forward_files(
    tmp_path: Path,
) -> None:
    bars_path = tmp_path / "bars.csv"
    decisions_path = tmp_path / "strict" / "decisions.csv"
    forward_dir = tmp_path / "forward"
    output_dir = tmp_path / "backfill"
    cutoff = 6_000_000
    _write_csv(bars_path, _bars())
    _write_csv(
        decisions_path,
        [
            _decision(2_400_000),
            _decision(4_800_000),
            _decision(5_100_000, eligible=0),
            _decision(7_200_000),
        ],
    )
    forward_dir.mkdir(parents=True)
    meta_path = forward_dir / "experiment_meta.json"
    meta_path.write_text(
        json.dumps(
            {
                "schema_version": "1.13.0",
                "started_at_ms": cutoff,
                "started_at": "1970-01-01T01:40:00+00:00",
                "forward_only": True,
                "orders_enabled": False,
            }
        ),
        encoding="utf-8",
    )
    sentinel = forward_dir / "sentinel.txt"
    sentinel.write_text("FORWARD_UNCHANGED", encoding="utf-8")

    summary = run_backfill(
        bars_path,
        decisions_path,
        meta_path,
        output_dir,
        now=datetime(2026, 8, 4, tzinfo=timezone.utc),
    )

    assert summary.source_candidates == 2
    assert sentinel.read_text(encoding="utf-8") == "FORWARD_UNCHANGED"
    assert set(summary.arms) == set(ARMS)
    for arm in ARMS:
        signals = list(
            csv.DictReader((output_dir / arm.lower() / "signals.csv").open(encoding="utf-8"))
        )
        assert len(signals) == 2
        assert all(int(row["start_ms"]) < cutoff for row in signals)
        assert all(row["orders_enabled"] == "0" for row in signals)

    status = json.loads((output_dir / "status.json").read_text(encoding="utf-8"))
    assert status["mode"] == "BACKFILL"
    assert status["historical_only"] is True
    assert status["forward_journals_modified"] is False
    assert status["orders_enabled"] is False
    assert status["logic_changed"] is False
    assert status["same_bar_rule"] == "STOP_FIRST_CONSERVATIVE"
    assert (output_dir / "comparison.csv").is_file()
    assert (output_dir / "dashboard" / "index.html").is_file()


def test_backfill_requires_valid_forward_only_metadata(tmp_path: Path) -> None:
    bars_path = tmp_path / "bars.csv"
    decisions_path = tmp_path / "decisions.csv"
    meta_path = tmp_path / "meta.json"
    _write_csv(bars_path, _bars())
    _write_csv(decisions_path, [_decision(2_400_000)])
    meta_path.write_text(
        json.dumps(
            {
                "started_at_ms": 6_000_000,
                "forward_only": False,
                "orders_enabled": False,
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="not marked forward-only"):
        run_backfill(bars_path, decisions_path, meta_path, tmp_path / "out")


def test_backfill_windows_runner_is_one_shot_and_separate() -> None:
    runner = Path("scripts/run_v1131_risk_plan_backfill.ps1").read_text(encoding="utf-8")
    checker = Path("scripts/check_v1131_risk_plan_backfill.ps1").read_text(encoding="utf-8")

    assert "bybit_risk_plan_backfill" in runner
    assert "bybit_risk_plans_backfill_v1_13_1" in runner
    assert "bybit_risk_plans_v1_13\\experiment_meta.json" in runner
    assert "Register-ScheduledTask" not in runner
    assert "Register-ScheduledTask" not in checker
    assert "ForwardJournalsModified" in checker
    assert "OrdersEnabled" in checker


def test_backfill_module_has_no_order_submission_api() -> None:
    source = Path("src/trademind/bybit_risk_plan_backfill.py").read_text(
        encoding="utf-8"
    ).lower()
    for forbidden in ("place_order", "create_order", "send_order", "order_create"):
        assert forbidden not in source
