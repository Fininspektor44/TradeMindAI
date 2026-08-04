from __future__ import annotations

import csv
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from trademind.bybit_shadow import BAR_MS
from trademind.bybit_shadow_experiments import (
    ARMS,
    STRICT_SELL_COMPONENTS,
    apply_arm_policy,
    run_experiments,
)


def _decision(action: str = "SELL") -> dict[str, object]:
    direction = -1 if action == "SELL" else 1
    return {
        "schema_version": "1.10.0",
        "decision_id": "BTCUSDT:1800000000000:MTF_FLOW_ALIGNMENT",
        "scenario": "MTF_FLOW_ALIGNMENT",
        "source_id": "BYBIT_LINEAR_SHADOW",
        "action": action,
        "gate_status": "CANDIDATE",
        "eligible": 1,
        "quality_score": 92,
        "components": "|".join(sorted(STRICT_SELL_COMPONENTS)),
        "reasons": "",
        "h1_return_pct": 0.01 * direction,
        "h1_delta_turnover": 1000 * direction,
        "m15_return_pct": 0.005 * direction,
        "m15_delta_turnover": 500 * direction,
        "m15_book_imbalance_10": 0.20 * direction,
        "m5_delta_turnover": 300 * direction,
        "m5_book_imbalance_10": 0.15 * direction,
        "orders_enabled": 0,
    }


def _bar(index: int, *, base_ms: int = 1_800_000_000_000) -> dict[str, object]:
    start = base_ms + index * BAR_MS
    opening = 100.0 + index * 0.10
    close = opening + 0.08
    return {
        "schema_version": "1.9",
        "source_id": "BYBIT_LINEAR",
        "symbol": "BTCUSDT",
        "timeframe": "M5",
        "start_ms": start,
        "end_ms": start + BAR_MS - 1,
        "open": opening,
        "high": close + 0.03,
        "low": opening - 0.03,
        "close": close,
        "volume": 10,
        "turnover": 1000,
        "trade_count": 100 + index,
        "buy_trade_count": 70,
        "sell_trade_count": 30,
        "taker_buy_qty": 7,
        "taker_sell_qty": 3,
        "taker_buy_turnover": 700,
        "taker_sell_turnover": 300,
        "delta_qty": 5,
        "delta_turnover": 100 * (index + 1),
        "cvd_turnover": 1000 * (index + 1),
        "largest_trade_turnover": 100 + index * 5,
        "avg_trade_turnover": 10,
        "trade_rate_per_sec": 1,
        "best_bid": close - 0.01,
        "best_ask": close + 0.01,
        "spread_bps": 0.1,
        "book_imbalance_5": 0.20,
        "book_imbalance_10": 0.25,
        "book_imbalance_50": 0.15,
        "last_price": close,
        "mark_price": close,
        "index_price": close,
        "basis_bps": 5,
        "open_interest": 1000 + index * 10,
        "open_interest_value": 100000,
        "funding_rate": 0.0001,
        "next_funding_time": 0,
        "price_24h_pct": 0.01,
        "turnover_24h": 100_000_000,
        "received_at": "2026-08-04T00:00:00+00:00",
    }


def _write_bars(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def test_control_preserves_v110_candidate_policy() -> None:
    base = _decision("SELL")
    row = apply_arm_policy(base, "CONTROL")

    assert row["eligible"] == 1
    assert row["gate_status"] == "CANDIDATE"
    assert row["action"] == "SELL"
    assert str(row["decision_id"]).endswith(":CONTROL")
    assert row["orders_enabled"] == 0


def test_buy_only_accepts_buy_and_rejects_sell_without_relaxing_score() -> None:
    buy = apply_arm_policy(_decision("BUY"), "BUY_ONLY")
    sell = apply_arm_policy(_decision("SELL"), "BUY_ONLY")

    assert buy["eligible"] == 1
    assert buy["gate_status"] == "CANDIDATE"
    assert sell["eligible"] == 0
    assert sell["gate_status"] == "REJECTED"
    assert "BUY_ONLY policy" in str(sell["reasons"])


def test_strict_sell_is_subset_of_control_and_requires_all_confirmations() -> None:
    passed = apply_arm_policy(_decision("SELL"), "STRICT_SELL")
    weak = _decision("SELL")
    weak["components"] = str(weak["components"]).replace("M5_BOOK|", "")
    rejected = apply_arm_policy(weak, "STRICT_SELL")
    buy = apply_arm_policy(_decision("BUY"), "STRICT_SELL")

    assert passed["eligible"] == 1
    assert passed["gate_status"] == "CANDIDATE"
    assert rejected["eligible"] == 0
    assert rejected["gate_status"] == "REJECTED"
    assert buy["eligible"] == 0
    assert buy["gate_status"] == "REJECTED"


def test_all_arms_start_together_and_ignore_old_v110_observations(tmp_path: Path) -> None:
    bars_path = tmp_path / "bybit_bars.csv"
    output = tmp_path / "v111"
    historical = [_bar(index) for index in range(36)]
    _write_bars(bars_path, historical)
    started = datetime.fromtimestamp(
        (int(historical[-1]["end_ms"]) + 1) / 1000,
        tz=timezone.utc,
    )

    first = run_experiments(bars_path, output, now=started)
    assert set(first.arms) == set(ARMS)
    assert all(item["decisions"] == 0 for item in first.arms.values())
    assert all(item["signals"] == 0 for item in first.arms.values())

    _write_bars(bars_path, [*historical, _bar(36)])
    second = run_experiments(bars_path, output, now=started + timedelta(minutes=5))
    status = json.loads((output / "status.json").read_text(encoding="utf-8"))

    assert all(item["decisions"] == 1 for item in second.arms.values())
    assert status["started_at_ms"] == first.started_at_ms
    assert status["forward_only"] is True
    assert status["orders_enabled"] is False
    assert status["schema_version"] == "1.11.0"
    for arm in ARMS:
        assert (output / arm.lower() / "decisions.csv").is_file()
        assert (output / arm.lower() / "signals.csv").is_file()


def test_task_scripts_keep_v110_running_and_never_trade() -> None:
    root = Path(__file__).resolve().parents[1]
    installer = (root / "scripts" / "install_v111_shadow_experiments_task.ps1").read_text(
        encoding="utf-8"
    )
    runner = (root / "scripts" / "run_v111_shadow_experiments.ps1").read_text(
        encoding="utf-8"
    )

    assert "TradeMindAI-v1.11-ShadowExperiments" in installer
    assert "existing v1.10 Shadow task and journal are not changed" in installer
    assert "Unregister-ScheduledTask" not in installer
    assert "-WindowStyle Hidden" in installer
    assert "bybit_shadow_experiments" in runner
    forbidden = ("OrderSend", "CTrade", "trade.Buy", "trade.Sell", "PositionClose")
    assert not any(token in installer + runner for token in forbidden)
