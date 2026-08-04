from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path

from trademind.bybit_position_management import (
    ARMS,
    BACKFILL_MODE,
    FORWARD_MODE,
    simulate_management,
    run_position_management,
)


def _history(start_ms: int = 1_500_000) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for index in range(1, 6):
        start = index * 300_000
        rows.append(
            {
                "symbol": "BTCUSDT",
                "start_ms": start,
                "end_ms": start + 299_999,
                "open": 100.0,
                "high": 100.1,
                "low": 99.9,
                "close": 100.0,
                "spread_bps": 1.0,
            }
        )
    assert int(rows[-1]["start_ms"]) == start_ms
    return rows


def _plan(start_ms: int = 1_500_000, horizon: int = 4) -> dict[str, object]:
    return {
        "decision_id": f"BTCUSDT:{start_ms}:STRICT_SELL:RISK:BASE_STRICT",
        "signal_time": datetime.fromtimestamp(
            (start_ms + 299_999) / 1000, tz=timezone.utc
        ).isoformat(),
        "start_ms": start_ms,
        "end_ms": start_ms + 299_999,
        "source": "BYBIT",
        "symbol": "BTCUSDT",
        "action": "SELL",
        "plan_arm": "BASE_STRICT",
        "quality_score": 91,
        "entry_price": 100.0,
        "stop_price": 101.0,
        "target_price": 98.5,
        "risk_pct": 0.01,
        "rr": 1.5,
        "horizon_bars": horizon,
        "position_size_factor": 1.0,
        "estimated_cost_r": 0.2,
        "orders_enabled": 0,
    }


def test_part50_be_uses_cost_covered_stop_and_conservative_same_bar_exit() -> None:
    bars = [
        *_history(),
        {
            "symbol": "BTCUSDT",
            "start_ms": 1_800_000,
            "end_ms": 2_099_999,
            "open": 99.7,
            "high": 100.0,
            "low": 98.9,
            "close": 99.1,
        },
    ]

    row = simulate_management(
        _plan(),
        "PART50_BE",
        bars,
        datetime(2026, 8, 4, tzinfo=timezone.utc),
    )

    assert row["completed"] == 1
    assert row["partial_1_hit"] == 1
    assert row["be_exit"] == 1
    assert row["completion_reason"] == "SAME_BAR_BE_STOP_CONSERVATIVE"
    assert abs(float(row["gross_result_r"]) - 0.6) < 1e-9
    assert abs(float(row["net_result_r"]) - 0.4) < 1e-9
    assert row["exit_count"] == 2


def test_part50_runner_keeps_half_for_two_r_target() -> None:
    bars = [
        *_history(),
        {
            "symbol": "BTCUSDT",
            "start_ms": 1_800_000,
            "end_ms": 2_099_999,
            "open": 99.4,
            "high": 99.7,
            "low": 98.9,
            "close": 99.0,
        },
        {
            "symbol": "BTCUSDT",
            "start_ms": 2_100_000,
            "end_ms": 2_399_999,
            "open": 98.8,
            "high": 98.7,
            "low": 97.9,
            "close": 98.0,
        },
    ]

    row = simulate_management(
        _plan(),
        "PART50_RUNNER",
        bars,
        datetime(2026, 8, 4, tzinfo=timezone.utc),
    )

    assert row["completed"] == 1
    assert row["partial_1_hit"] == 1
    assert row["full_target_exit"] == 1
    assert row["completion_reason"] == "RUNNER_TARGET"
    assert abs(float(row["gross_result_r"]) - 1.5) < 1e-9
    assert abs(float(row["net_result_r"]) - 1.3) < 1e-9


def test_be_trail_uses_entry_atr_and_exits_on_following_bar() -> None:
    bars = [
        *_history(),
        {
            "symbol": "BTCUSDT",
            "start_ms": 1_800_000,
            "end_ms": 2_099_999,
            "open": 98.8,
            "high": 98.7,
            "low": 98.6,
            "close": 98.65,
        },
        {
            "symbol": "BTCUSDT",
            "start_ms": 2_100_000,
            "end_ms": 2_399_999,
            "open": 98.75,
            "high": 98.9,
            "low": 98.4,
            "close": 98.5,
        },
    ]

    row = simulate_management(
        _plan(),
        "BE_TRAIL",
        bars,
        datetime(2026, 8, 4, tzinfo=timezone.utc),
    )

    assert row["completed"] == 1
    assert row["be_armed"] == 1
    assert row["trail_armed"] == 1
    assert row["trail_exit"] == 1
    assert row["completion_reason"] == "TRAIL_STOP"
    assert abs(float(row["gross_result_r"]) - 1.2) < 1e-9


def _bars(count: int = 50) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for index in range(1, count + 1):
        start = index * 300_000
        price = 101.0 - index * 0.03
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


def _source(start_ms: int) -> dict[str, object]:
    entry = 100.0
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
        "gate_status": "CANDIDATE",
        "quality_score": 91,
        "eligible": 1,
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


def _write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = sorted({key for row in rows for key in row})
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def test_forward_and_backfill_are_equal_arm_but_separate_samples(tmp_path: Path) -> None:
    bars_path = tmp_path / "bars.csv"
    decisions_path = tmp_path / "strict" / "decisions.csv"
    forward_dir = tmp_path / "forward"
    backfill_dir = tmp_path / "backfill"
    _write_csv(bars_path, _bars())
    _write_csv(decisions_path, [_source(1_500_000), _source(2_400_000)])
    forward_dir.mkdir(parents=True)
    (forward_dir / "experiment_meta.json").write_text(
        json.dumps(
            {
                "schema_version": "1.14.0",
                "started_at_ms": 2_000_000,
                "started_at": "1970-01-01T00:33:20+00:00",
                "forward_only": True,
                "equal_start": True,
                "orders_enabled": False,
            }
        ),
        encoding="utf-8",
    )

    forward = run_position_management(
        bars_path,
        decisions_path,
        forward_dir,
        mode=FORWARD_MODE,
        now=datetime(2026, 8, 4, tzinfo=timezone.utc),
    )
    backfill = run_position_management(
        bars_path,
        decisions_path,
        backfill_dir,
        mode=BACKFILL_MODE,
        forward_meta_path=forward_dir / "experiment_meta.json",
        now=datetime(2026, 8, 4, tzinfo=timezone.utc),
    )

    assert forward.source_candidates == 1
    assert backfill.source_candidates == 1
    assert set(forward.arms) == set(ARMS)
    assert set(backfill.arms) == set(ARMS)
    for arm in ARMS:
        assert forward.arms[arm]["signals"] == 1
        assert backfill.arms[arm]["signals"] == 1
        forward_rows = list(
            csv.DictReader((forward_dir / arm.lower() / "signals.csv").open())
        )
        backfill_rows = list(
            csv.DictReader((backfill_dir / arm.lower() / "signals.csv").open())
        )
        assert ":2400000:" in forward_rows[0]["management_signal_id"]
        assert ":1500000:" in backfill_rows[0]["management_signal_id"]
        assert forward_rows[0]["orders_enabled"] == "0"
        assert backfill_rows[0]["orders_enabled"] == "0"

    forward_status = json.loads((forward_dir / "status.json").read_text(encoding="utf-8"))
    backfill_status = json.loads((backfill_dir / "status.json").read_text(encoding="utf-8"))
    assert forward_status["forward_only"] is True
    assert forward_status["historical_only"] is False
    assert backfill_status["historical_only"] is True
    assert backfill_status["forward_only"] is False
    assert forward_status["source_journals_modified"] is False
    assert backfill_status["source_journals_modified"] is False
    assert forward_status["orders_enabled"] is False
    assert backfill_status["orders_enabled"] is False


def test_module_has_no_order_submission_api() -> None:
    source = Path("src/trademind/bybit_position_management.py").read_text(
        encoding="utf-8"
    ).lower()
    for forbidden in ("place_order", "create_order", "send_order", "order_create"):
        assert forbidden not in source
