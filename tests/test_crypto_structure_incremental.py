from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path

from trademind.crypto_structure_incremental import run_incremental, safety_contract


def write_csv(path: Path, rows: list[dict[str, object]], fields: list[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = fields or list(rows[0])
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def decision_row(index: int, signal_time: str) -> dict[str, object]:
    return {
        "decision_id": f"BTCUSDT:{index}:MTF_FLOW_ALIGNMENT",
        "signal_time": signal_time,
        "symbol": "BTCUSDT",
        "action": "BUY" if index % 2 else "SELL",
        "gate_status": "CANDIDATE",
        "quality_score": "82",
        "components": "H1_PRICE|M15_DELTA|M5_DELTA_IMPULSE",
        "reasons": "",
        "entry_price": "105",
        "stop_price": "103.95" if index % 2 else "106.05",
        "target_price": "106.575" if index % 2 else "103.425",
        "risk_pct": "0.01",
        "h1_return_pct": "0.012",
        "h1_delta_turnover": "1500000",
        "h1_oi_change_pct": "0.008",
        "m15_return_pct": "0.004",
        "m15_delta_turnover": "550000",
        "m15_book_imbalance_10": "0.18",
        "m15_oi_change_pct": "0.003",
        "m5_delta_turnover": "220000",
        "m5_trade_count": "1820",
        "m5_book_imbalance_10": "0.14",
        "m5_spread_bps": "1.2",
        "m5_funding_rate": "0.0001",
        "m5_basis_bps": "7.5",
    }


def write_bars(path: Path, count: int = 240) -> str:
    start_ms = 1_786_003_200_000
    rows: list[dict[str, object]] = []
    for index in range(count):
        close = 100 + index * 0.02 + ((index % 12) - 6) * 0.08
        rows.append(
            {
                "symbol": "BTCUSDT",
                "start_ms": start_ms + index * 300_000,
                "open": close - 0.05,
                "high": close + 0.25,
                "low": close - 0.25,
                "close": close,
            }
        )
    write_csv(path, rows)
    return datetime.fromtimestamp(
        (start_ms + count * 300_000) / 1000,
        tz=timezone.utc,
    ).isoformat()


def signal_fields() -> list[str]:
    return [
        "paper_signal_id",
        "signal_time",
        "updated_at",
        "completed",
        "result_r",
        "outcome",
    ]


def test_incremental_backfill_preserves_archive_and_finishes_in_batches(
    tmp_path: Path,
) -> None:
    bars = tmp_path / "bars.csv"
    signal_time = write_bars(bars)
    decisions = tmp_path / "decisions.csv"
    signals = tmp_path / "signals.csv"
    output = tmp_path / "crypto"
    rows = [decision_row(index, signal_time) for index in range(1, 6)]
    write_csv(decisions, rows)
    write_csv(signals, [], signal_fields())

    first = run_incremental(decisions, signals, bars, output, batch_size=2)
    assert first.processed_batch == 2
    assert first.total_candidates == 2
    assert first.remaining_decisions == 3

    second = run_incremental(decisions, signals, bars, output, batch_size=2)
    assert second.processed_batch == 2
    assert second.total_candidates == 4
    assert second.remaining_decisions == 1

    third = run_incremental(decisions, signals, bars, output, batch_size=2)
    assert third.processed_batch == 1
    assert third.total_candidates == 5
    assert third.remaining_decisions == 0

    candidates = [
        json.loads(line)
        for line in (output / "candidates.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert len(candidates) == 5
    assert len({row["source_decision_id"] for row in candidates}) == 5
    status = json.loads((output / "status.json").read_text(encoding="utf-8"))
    assert status["state"] == "OK"
    assert status["processed_batch"] == 1
    assert status["remaining_decisions"] == 0


def test_existing_candidates_receive_new_outcomes_without_full_rebuild(
    tmp_path: Path,
) -> None:
    bars = tmp_path / "bars.csv"
    signal_time = write_bars(bars)
    decisions = tmp_path / "decisions.csv"
    signals = tmp_path / "signals.csv"
    output = tmp_path / "crypto"
    rows = [decision_row(index, signal_time) for index in range(1, 4)]
    write_csv(decisions, rows)
    write_csv(signals, [], signal_fields())

    run_incremental(decisions, signals, bars, output, batch_size=1)
    newest = rows[-1]
    write_csv(
        signals,
        [
            {
                "paper_signal_id": newest["decision_id"],
                "signal_time": signal_time,
                "updated_at": signal_time,
                "completed": "1",
                "result_r": "1.5",
                "outcome": "WIN",
            }
        ],
        signal_fields(),
    )

    result = run_incremental(decisions, signals, bars, output, batch_size=1)
    assert result.total_candidates == 2
    assert result.total_outcomes == 1
    outcomes = [
        json.loads(line)
        for line in (output / "outcomes.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert outcomes[0]["outcome"] == "WIN"
    assert outcomes[0]["net_r"] == 1.46


def test_safety_contract_is_read_only() -> None:
    assert safety_contract() == {
        "read_only": True,
        "orders_enabled": False,
        "publication_enabled": False,
        "broker_api_called": False,
        "future_bars_used": False,
        "source_files_modified": False,
    }
