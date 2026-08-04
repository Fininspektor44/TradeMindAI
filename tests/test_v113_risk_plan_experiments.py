from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path

from trademind.bybit_risk_plan_experiments import (
    ARMS,
    MAX_COST_R_FOR_HYBRID,
    _true_range_atr,
    apply_risk_plan,
    run_risk_plan_experiments,
)


def _bars(count: int = 36) -> list[dict[str, object]]:
    rows = []
    for index in range(count):
        start = (index + 1) * 300_000
        price = 100.0 - index * 0.04
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


def _source(start_ms: int = 7_200_000) -> dict[str, object]:
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


def test_wide_arms_use_atr_stop_and_both_take_profit_ratios() -> None:
    bars = _bars()
    source = _source()
    history = [row for row in bars if int(row["start_ms"]) <= int(source["start_ms"])]
    atr = _true_range_atr(history)

    wide_15 = apply_risk_plan(source, "WIDE15_R15", bars)
    wide_15_r20 = apply_risk_plan(source, "WIDE15_R20", bars)
    wide_20 = apply_risk_plan(source, "WIDE20_R15", bars)
    wide_20_r20 = apply_risk_plan(source, "WIDE20_R20", bars)

    assert float(wide_15["risk_distance"]) >= 1.5 * atr
    assert float(wide_20["risk_distance"]) >= 2.0 * atr
    assert float(wide_15["rr"]) == 1.5
    assert float(wide_15_r20["rr"]) == 2.0
    assert float(wide_20["rr"]) == 1.5
    assert float(wide_20_r20["rr"]) == 2.0
    assert int(wide_15["horizon_bars"]) == 18
    assert int(wide_20["horizon_bars"]) == 24


def test_structure_hybrid_enforces_cost_floor_and_same_money_risk() -> None:
    bars = _bars()
    source = _source()
    source["stop_price"] = 99.05
    source["target_price"] = 98.925
    source["risk_pct"] = 0.05 / 99.0

    plan = apply_risk_plan(source, "STRUCTURE_LIQ", bars)

    assert float(plan["estimated_cost_r"]) <= MAX_COST_R_FOR_HYBRID + 1e-9
    assert float(plan["position_size_factor"]) <= 1.0
    assert float(plan["rr"]) >= 1.5
    assert float(plan["target_price"]) < float(plan["entry_price"]) < float(plan["stop_price"])
    assert int(plan["orders_enabled"]) == 0


def test_run_is_forward_only_and_gives_every_arm_the_same_new_source(tmp_path: Path) -> None:
    bars_path = tmp_path / "bars.csv"
    decisions_path = tmp_path / "strict" / "decisions.csv"
    output_dir = tmp_path / "risk"
    bars = _bars(44)
    _write_csv(bars_path, bars)

    old = _source(1_500_000)
    new = _source(2_400_000)
    _write_csv(decisions_path, [old, new])
    output_dir.mkdir(parents=True)
    (output_dir / "experiment_meta.json").write_text(
        json.dumps(
            {
                "schema_version": "1.13.0",
                "started_at_ms": 2_000_000,
                "started_at": "1970-01-01T00:33:20+00:00",
                "forward_only": True,
                "orders_enabled": False,
            }
        ),
        encoding="utf-8",
    )

    summary = run_risk_plan_experiments(
        bars_path,
        decisions_path,
        output_dir,
        now=datetime(2026, 8, 4, tzinfo=timezone.utc),
    )

    assert summary.source_candidates == 1
    assert set(summary.arms) == set(ARMS)
    for arm in ARMS:
        assert summary.arms[arm]["signals"] == 1
        signal_rows = list(csv.DictReader((output_dir / arm.lower() / "signals.csv").open()))
        assert len(signal_rows) == 1
        assert ":2400000:" in signal_rows[0]["paper_signal_id"]
        assert ":1500000:" not in signal_rows[0]["paper_signal_id"]
        assert signal_rows[0]["orders_enabled"] == "0"

    status = json.loads((output_dir / "status.json").read_text(encoding="utf-8"))
    assert status["forward_only"] is True
    assert status["equal_start"] is True
    assert status["orders_enabled"] is False
    assert status["logic_changed"] is False


def test_module_has_no_order_submission_api() -> None:
    source = Path("src/trademind/bybit_risk_plan_experiments.py").read_text(
        encoding="utf-8"
    ).lower()
    for forbidden in ("place_order", "create_order", "send_order", "order_create"):
        assert forbidden not in source
