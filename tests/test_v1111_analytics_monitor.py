from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path

from trademind.bybit_shadow_monitor import (
    _alert,
    _concurrency,
    _milestone,
    estimated_cost_r,
    run_monitor,
)
from trademind.bybit_shadow_experiments import ARMS


def _write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def _signal(index: int, result_r: float, action: str = "SELL") -> dict[str, object]:
    start_ms = 1_800_000_000_000 + index * 300_000
    signal_id = f"BTCUSDT:{start_ms}:MTF_FLOW_ALIGNMENT:STRICT_SELL"
    return {
        "schema_version": "1.11.0",
        "paper_signal_id": signal_id,
        "activated_at": "2026-08-04T00:00:00+00:00",
        "updated_at": "2026-08-04T01:00:00+00:00",
        "signal_time": "2026-08-04T00:00:00+00:00",
        "start_ms": start_ms,
        "end_ms": start_ms + 299_999,
        "source": "BYBIT",
        "source_id": "BYBIT_LINEAR_SHADOW_EXPERIMENTS",
        "symbol": "BTCUSDT",
        "action": action,
        "scenario": f"MTF_FLOW_ALIGNMENT__{action}",
        "quality_score": 95,
        "gate_status": "CANDIDATE",
        "components": "H1_PRICE_TREND|M15_DELTA|M5_DELTA_IMPULSE",
        "entry_price": 100.0,
        "stop_price": 101.0 if action == "SELL" else 99.0,
        "target_price": 98.5 if action == "SELL" else 101.5,
        "risk_pct": 0.01,
        "rr": 1.5,
        "horizon_bars": 12,
        "outcome": "WIN" if result_r > 0 else "LOSS",
        "result_r": result_r,
        "mfe_r": 1.6,
        "mae_r": -0.4,
        "completed": 1,
        "completion_reason": "TARGET" if result_r > 0 else "STOP",
        "orders_enabled": 0,
    }


def _decision(signal: dict[str, object], spread_bps: float = 2.0) -> dict[str, object]:
    return {
        "schema_version": "1.11.0",
        "captured_at": "2026-08-04T00:00:00+00:00",
        "decision_id": signal["paper_signal_id"],
        "signal_time": signal["signal_time"],
        "start_ms": signal["start_ms"],
        "end_ms": signal["end_ms"],
        "source": "BYBIT",
        "source_id": "BYBIT_LINEAR_SHADOW_EXPERIMENTS",
        "symbol": signal["symbol"],
        "action": signal["action"],
        "scenario": signal["scenario"],
        "context_timeframe": "H1",
        "decision_timeframe": "M15",
        "trigger_timeframe": "M5",
        "gate_status": "CANDIDATE",
        "quality_score": signal["quality_score"],
        "eligible": 1,
        "duplicate_wave": 0,
        "components": signal["components"],
        "reasons": "",
        "entry_price": signal["entry_price"],
        "stop_price": signal["stop_price"],
        "target_price": signal["target_price"],
        "risk_pct": signal["risk_pct"],
        "rr": signal["rr"],
        "horizon_bars": signal["horizon_bars"],
        "h1_return_pct": -0.01,
        "h1_delta_turnover": -1000,
        "h1_oi_change_pct": -0.01,
        "m15_return_pct": -0.005,
        "m15_delta_turnover": -500,
        "m15_book_imbalance_10": -0.2,
        "m15_oi_change_pct": -0.01,
        "m5_delta_turnover": -300,
        "m5_trade_count": 100,
        "m5_book_imbalance_10": -0.15,
        "m5_spread_bps": spread_bps,
        "m5_funding_rate": 0.0001,
        "m5_basis_bps": -5,
        "orders_enabled": 0,
    }


def test_cost_model_converts_round_trip_bps_to_r() -> None:
    signal = _signal(0, 1.5)
    cost_r, risk_pct, spread = estimated_cost_r(
        signal,
        _decision(signal, spread_bps=2.0),
        fee_bps_per_side=5.5,
        slippage_bps_per_side=1.0,
    )

    assert risk_pct == 0.01
    assert spread == 2.0
    assert round(cost_r, 6) == 0.15


def test_milestones_track_50_100_200_without_changing_logic() -> None:
    first = _milestone(14)
    second = _milestone(54)
    done = _milestone(205)

    assert first["next"] == 50 and first["remaining"] == 36
    assert second["next"] == 100 and second["remaining"] == 46
    assert round(second["progress_pct"], 1) == 8.0
    assert done["next"] is None and done["progress_pct"] == 100.0


def test_concurrency_uses_conservative_full_horizon_overlap() -> None:
    rows = [
        {
            "start_ms": 1_800_000_000_000,
            "holding_horizon_bars": 12,
            "action": "SELL",
        },
        {
            "start_ms": 1_800_000_000_000,
            "holding_horizon_bars": 12,
            "action": "SELL",
        },
        {
            "start_ms": 1_800_000_300_000,
            "holding_horizon_bars": 12,
            "action": "BUY",
        },
    ]

    result = _concurrency(rows)

    assert result["peak_concurrent"] == 3
    assert result["peak_same_direction"] == 2
    assert result["largest_entry_cluster"] == 2


def test_alerts_only_describe_evidence_and_never_mutate_rules() -> None:
    degrading = _alert(
        "STRICT_SELL",
        35,
        4.0,
        {"average_r": -0.1, "profit_factor": 0.8, "total_r": -2.0},
    )
    holding = _alert(
        "STRICT_SELL",
        55,
        8.0,
        {"average_r": 0.2, "profit_factor": 1.4, "total_r": 4.0},
    )
    recovering = _alert(
        "BUY_ONLY",
        40,
        -2.0,
        {"average_r": 0.15, "profit_factor": 1.3, "total_r": 3.0},
    )

    assert degrading == "EDGE_DEGRADING"
    assert holding == "EDGE_HOLDING"
    assert recovering == "RECOVERY_SIGNAL"


def test_monitor_writes_net_cost_breakdowns_and_read_only_status(tmp_path: Path) -> None:
    experiment = tmp_path / "v111"
    output = tmp_path / "monitor"
    experiment.mkdir(parents=True)
    (experiment / "status.json").write_text(
        json.dumps(
            {
                "schema_version": "1.11.0",
                "state": "OK",
                "started_at": "2026-08-04T00:00:00+00:00",
                "forward_only": True,
                "orders_enabled": False,
            }
        ),
        encoding="utf-8",
    )
    for arm in ARMS:
        rows = [_signal(0, 1.5), _signal(1, -1.0)]
        decisions = [_decision(row) for row in rows]
        _write_csv(experiment / arm.lower() / "signals.csv", rows)
        _write_csv(experiment / arm.lower() / "decisions.csv", decisions)

    summary = run_monitor(
        experiment,
        output,
        fee_bps_per_side=5.5,
        slippage_bps_per_side=1.0,
        now=datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc),
    )
    status = json.loads((output / "status.json").read_text(encoding="utf-8"))

    assert set(summary.arms) == set(ARMS)
    assert status["schema_version"] == "1.11.1"
    assert status["forward_only"] is True
    assert status["orders_enabled"] is False
    assert status["logic_changed"] is False
    assert status["arms"]["CONTROL"]["gross_total_r"] == 0.5
    assert round(status["arms"]["CONTROL"]["estimated_cost_r"], 6) == 0.3
    assert round(status["arms"]["CONTROL"]["net_total_r"], 6) == 0.2
    assert (output / "arm_summary.csv").is_file()
    assert (output / "breakdowns.csv").is_file()
    assert (output / "signal_costs.csv").is_file()
    assert (output / "dashboard" / "index.html").is_file()


def test_monitor_scripts_and_module_never_trade() -> None:
    root = Path(__file__).resolve().parents[1]
    paths = (
        root / "src" / "trademind" / "bybit_shadow_monitor.py",
        root / "scripts" / "run_v1111_analytics_monitor.ps1",
        root / "scripts" / "install_v1111_analytics_monitor_task.ps1",
        root / "scripts" / "check_v1111_analytics_monitor.ps1",
        root / "scripts" / "report_v1111_analytics_monitor.ps1",
    )
    text = "\n".join(path.read_text(encoding="utf-8") for path in paths)
    forbidden = (
        "OrderSend",
        "CTrade",
        "trade.Buy",
        "trade.Sell",
        "PositionClose",
        "place_order",
        "create_order",
    )

    assert "logic_changed" in text
    assert "orders_enabled" in text
    assert not any(token in text for token in forbidden)
