from __future__ import annotations

import csv
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from trademind.bybit_shadow import (
    BAR_MS,
    H1_MS,
    M15_MS,
    SCENARIO,
    _new_paper_row,
    _score_decision,
    _update_outcome,
    aggregate_bars,
    build_states,
    run_shadow_gate,
)


def _bar(index: int, *, base_ms: int = 1_800_000_000_000, direction: int = 1) -> dict[str, object]:
    start = base_ms + index * BAR_MS
    opening = 100.0 + index * 0.10 * direction
    close = opening + 0.08 * direction
    return {
        "schema_version": "1.9",
        "source_id": "BYBIT_LINEAR",
        "symbol": "BTCUSDT",
        "timeframe": "M5",
        "start_ms": start,
        "end_ms": start + BAR_MS - 1,
        "open": opening,
        "high": max(opening, close) + 0.03,
        "low": min(opening, close) - 0.03,
        "close": close,
        "volume": 10,
        "turnover": 1000,
        "trade_count": 100 + index,
        "buy_trade_count": 70 if direction > 0 else 30,
        "sell_trade_count": 30 if direction > 0 else 70,
        "taker_buy_qty": 7,
        "taker_sell_qty": 3,
        "taker_buy_turnover": 700,
        "taker_sell_turnover": 300,
        "delta_qty": 5 * direction,
        "delta_turnover": 100 * (index + 1) * direction,
        "cvd_turnover": 1000 * (index + 1) * direction,
        "largest_trade_turnover": 100 + index * 5,
        "avg_trade_turnover": 10,
        "trade_rate_per_sec": 1,
        "best_bid": close - 0.01,
        "best_ask": close + 0.01,
        "spread_bps": 0.1,
        "book_imbalance_5": 0.20 * direction,
        "book_imbalance_10": 0.25 * direction,
        "book_imbalance_50": 0.15 * direction,
        "last_price": close,
        "mark_price": close,
        "index_price": close,
        "basis_bps": 5 * direction,
        "open_interest": 1000 + index * 10 * direction,
        "open_interest_value": 100000,
        "funding_rate": 0.0001 * direction,
        "next_funding_time": 0,
        "price_24h_pct": 0.01 * direction,
        "turnover_24h": 100_000_000,
        "received_at": "2026-08-03T00:00:00+00:00",
    }


def _write_bars(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = list(rows[0])
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def test_aggregation_preserves_volume_flow_oi_and_book_metrics() -> None:
    rows = [_bar(index) for index in range(36)]
    m15 = aggregate_bars(rows, M15_MS, "M15")
    h1 = aggregate_bars(rows, H1_MS, "H1")

    assert len(m15) == 12
    assert len(h1) == 3
    assert m15[0]["bar_count"] == 3
    assert m15[0]["trade_count"] == sum(100 + index for index in range(3))
    assert m15[0]["delta_turnover"] == sum(100 * (index + 1) for index in range(3))
    assert m15[0]["book_imbalance_10_avg"] == 0.25
    assert m15[0]["open_interest_change_pct"] > 0


def test_aligned_h1_m15_m5_flow_creates_candidate() -> None:
    rows = [_bar(index) for index in range(36)]
    decision = _score_decision(
        rows,
        aggregate_bars(rows, M15_MS, "M15"),
        aggregate_bars(rows, H1_MS, "H1"),
        datetime.now(timezone.utc),
    )

    assert decision["scenario"] == SCENARIO
    assert decision["action"] == "BUY"
    assert decision["gate_status"] == "CANDIDATE"
    assert decision["quality_score"] >= 70
    assert "H1_DELTA" in decision["components"]
    assert "M15_BOOK" in decision["components"]
    assert "M5_DELTA_IMPULSE" in decision["components"]
    assert decision["orders_enabled"] == 0


def test_gate_is_forward_only_and_never_backfills_history(tmp_path: Path) -> None:
    bars_path = tmp_path / "bybit_bars.csv"
    output = tmp_path / "shadow"
    historical = [_bar(index) for index in range(36)]
    _write_bars(bars_path, historical)
    started = datetime.fromtimestamp(
        (int(historical[-1]["end_ms"]) + 1) / 1000,
        tz=timezone.utc,
    )

    first = run_shadow_gate(bars_path, output, now=started)
    assert first.decisions == 0
    assert first.paper_signals == 0

    forward = [*historical, _bar(36)]
    _write_bars(bars_path, forward)
    second = run_shadow_gate(bars_path, output, now=started + timedelta(minutes=5))
    status = json.loads((output / "status.json").read_text(encoding="utf-8"))

    assert second.decisions == 1
    assert status["forward_only"] is True
    assert status["orders_enabled"] is False
    assert status["source_id"] == "BYBIT_LINEAR_SHADOW"


def test_same_bar_target_and_stop_is_scored_conservatively() -> None:
    rows = [_bar(index) for index in range(36)]
    decision = _score_decision(
        rows,
        aggregate_bars(rows, M15_MS, "M15"),
        aggregate_bars(rows, H1_MS, "H1"),
        datetime.now(timezone.utc),
    )
    signal = _new_paper_row(decision, datetime.now(timezone.utc))
    future = _bar(36)
    future["low"] = decision["stop_price"] * 0.999
    future["high"] = decision["target_price"] * 1.001

    result = _update_outcome(signal, [future], datetime.now(timezone.utc))

    assert result["outcome"] == "LOSS"
    assert result["result_r"] == -1.0
    assert result["completion_reason"] == "STOP_FIRST_CONSERVATIVE"


def test_validation_requires_300_forward_trades_30_days_and_positive_ci95() -> None:
    journal: list[dict[str, object]] = []
    base_ms = 1_800_000_000_000
    for index in range(300):
        result = -1.0 if index % 3 == 0 else 1.5
        journal.append(
            {
                "symbol": "BTCUSDT",
                "action": "BUY",
                "start_ms": base_ms + index * 3 * 60 * 60 * 1000,
                "completed": 1,
                "result_r": result,
                "outcome": "LOSS" if result < 0 else "WIN",
            }
        )

    state = build_states(journal, datetime.now(timezone.utc))[0]

    assert state["trading_days"] >= 30
    assert state["completed"] == 300
    assert state["ci95_low"] > 0
    assert state["status"] == "VALIDATED"
