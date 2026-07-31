from __future__ import annotations

import csv
import json
from datetime import datetime, timedelta, timezone

from trademind.paper_gate import load_config, run_gate


def _journal_row(
    when: datetime,
    index: int,
    *,
    value: float,
    volume_ratio: float = 1.5,
) -> dict[str, str]:
    return {
        "schema_version": "1.1",
        "signal_id": f".USTECHCASH:M5:{index}",
        "signal_time": when.isoformat(),
        "symbol": ".USTECHCASH",
        "timeframe": "M5",
        "action": "SELL",
        "score": "2",
        "confidence": "70",
        "entry_price": "100",
        "spread_points": "1",
        "spread_cost_atr": "0.01",
        "volume_ratio_20": str(volume_ratio),
        "internal_break": "NONE",
        "swing_break": "NONE",
        "bsl_sweep": "0",
        "ssl_sweep": "0",
        "fvg_direction": "NONE",
        "internal_bias": "BEARISH",
        "swing_bias": "BEARISH",
        "atr": "1",
        "outcome_3": "WIN" if value > 0 else "LOSS",
        "progress_atr_3": str(value),
        "net_move_3": str(value),
        "mfe_atr_3": "0.4",
        "mae_atr_3": "0.1",
        "exit_time_3": (when + timedelta(minutes=15)).isoformat(),
    }


def test_paper_gate_uses_frozen_training_cutoff_and_non_overlap(tmp_path) -> None:
    cutoff = datetime(2026, 2, 1, tzinfo=timezone.utc)
    training_start = cutoff - timedelta(days=12)
    rows: list[dict[str, str]] = []
    for index in range(60):
        when = training_start + timedelta(hours=4 * index)
        rows.append(_journal_row(when, index, value=0.5 if index % 2 == 0 else -0.1))
    rows.extend(
        [
            _journal_row(cutoff + timedelta(minutes=5), 1001, value=0.2),
            _journal_row(cutoff + timedelta(minutes=10), 1002, value=0.3),
            _journal_row(cutoff + timedelta(minutes=25), 1003, value=-0.1),
        ]
    )

    journal = tmp_path / "signals.csv"
    fieldnames = sorted({key for row in rows for key in row})
    with journal.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    config_path = tmp_path / "gate.json"
    config_path.write_text(
        json.dumps(
            {
                "version": "1.3",
                "schema_version": "1.1",
                "training_cutoff": cutoff.isoformat(),
                "candidate_minimum": 30,
                "research_minimum": 300,
                "minimum_training_days": 10,
                "fdr_alpha": 0.10,
                "rules": [
                    {
                        "id": "primary",
                        "symbol": ".USTECHCASH",
                        "label": "HIGH_VOLUME",
                        "action": "SELL",
                        "horizon": 3,
                        "tier": "PRIMARY_OOS",
                        "max_q_value": 0.10,
                        "max_drawdown_atr": 10,
                        "max_loss_streak": 5,
                        "minimum_late_ratio": 0.2,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    output = tmp_path / "paper.csv"
    status = tmp_path / "status.csv"

    summary = run_gate(journal, config_path, output, status, generated_at=cutoff)

    assert summary.decisions[0].eligible
    assert summary.paper_signals == 2
    with output.open("r", encoding="utf-8", newline="") as handle:
        paper = list(csv.DictReader(handle))
    assert [row["source_signal_id"] for row in paper] == [
        ".USTECHCASH:M5:1001",
        ".USTECHCASH:M5:1003",
    ]
    assert all(datetime.fromisoformat(row["signal_time"]) >= cutoff for row in paper)


def test_config_rejects_naive_cutoff(tmp_path) -> None:
    path = tmp_path / "bad.json"
    path.write_text(
        json.dumps(
            {
                "training_cutoff": "2026-02-01T00:00:00",
                "rules": [
                    {
                        "id": "x",
                        "symbol": "XAUUSD",
                        "label": "BSL_SWEEP",
                        "action": "BUY",
                        "horizon": 3,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    try:
        load_config(path)
    except ValueError as exc:
        assert "timezone" in str(exc)
    else:
        raise AssertionError("naive cutoff must be rejected")
