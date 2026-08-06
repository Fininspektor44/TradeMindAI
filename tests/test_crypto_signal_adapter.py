from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path

from trademind.crypto_signal_adapter import build_candidate, run_adapter, safety_contract
from trademind.signal_intelligence import candidate_from_dict


def decision_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "decision_id": "BTCUSDT:1000:MTF_FLOW_ALIGNMENT",
        "signal_time": "2026-08-06T09:30:00+00:00",
        "symbol": "BTCUSDT",
        "action": "BUY",
        "gate_status": "CANDIDATE",
        "quality_score": "82",
        "components": "H1_PRICE|M15_DELTA|M5_DELTA_IMPULSE",
        "reasons": "",
        "entry_price": "65000",
        "stop_price": "64350",
        "target_price": "65975",
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
    row.update(overrides)
    return row


def write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = list(rows[0])
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def test_build_candidate_uses_real_bybit_flow_fields() -> None:
    candidate = build_candidate(decision_row())
    assert candidate.symbol == "BTCUSDT"
    assert candidate.plan.action == "BUY"
    assert candidate.plan.first_target_rr == 1.5
    assert candidate.market_features["custom"]["asset_class"] == "CRYPTO"
    assert candidate.market_features["execution"]["funding_rate"] == 0.0001
    assert candidate.market_features["sentiment"]["h1_open_interest_change_pct"] == 0.008
    assert candidate.factor_scores["fibonacci"] == 0.0
    assert candidate.generated_from_market_data is True


def test_run_adapter_writes_factory_compatible_candidates_and_outcomes(tmp_path: Path) -> None:
    decisions = tmp_path / "decisions.csv"
    signals = tmp_path / "signals.csv"
    output = tmp_path / "crypto"
    write_csv(decisions, [decision_row(), decision_row(decision_id="bad", action="NONE")])
    write_csv(
        signals,
        [
            {
                "paper_signal_id": "BTCUSDT:1000:MTF_FLOW_ALIGNMENT",
                "signal_time": "2026-08-06T09:30:00+00:00",
                "updated_at": "2026-08-06T10:00:00+00:00",
                "completed": "1",
                "result_r": "1.5",
                "outcome": "WIN",
            }
        ],
    )

    result = run_adapter(
        decisions,
        signals,
        output,
        cost_r=0.04,
        now=datetime(2026, 8, 6, 10, 1, tzinfo=timezone.utc),
    )

    assert result.candidates == 1
    assert result.outcomes == 1
    assert result.rejected_rows == 1
    candidate_payload = json.loads((output / "candidates.jsonl").read_text(encoding="utf-8"))
    candidate = candidate_from_dict(candidate_payload)
    assert candidate.signal_id == candidate_payload["signal_id"]
    assert candidate_payload["asset_class"] == "CRYPTO"
    assert candidate_payload["venue"] == "BYBIT"
    assert candidate_payload["orders_enabled"] is False

    outcome_payload = json.loads((output / "outcomes.jsonl").read_text(encoding="utf-8"))
    assert outcome_payload["signal_id"] == candidate.signal_id
    assert outcome_payload["outcome"] == "WIN"
    assert outcome_payload["net_r"] == 1.46
    status = json.loads((output / "status.json").read_text(encoding="utf-8"))
    assert status["state"] == "OK"
    assert status["safety"]["broker_api_called"] is False


def test_sell_geometry_and_negative_outcome(tmp_path: Path) -> None:
    row = decision_row(
        decision_id="ETHUSDT:2000:MTF_FLOW_ALIGNMENT",
        symbol="ETHUSDT",
        action="SELL",
        entry_price="3500",
        stop_price="3535",
        target_price="3447.5",
        h1_return_pct="-0.01",
        m15_return_pct="-0.004",
        h1_delta_turnover="-100000",
        m15_delta_turnover="-70000",
        m5_delta_turnover="-25000",
        m15_book_imbalance_10="-0.12",
        m5_book_imbalance_10="-0.15",
    )
    candidate = build_candidate(row)
    assert candidate.plan.action == "SELL"
    assert candidate.plan.targets[1] == 3430

    decisions = tmp_path / "decisions.csv"
    signals = tmp_path / "signals.csv"
    write_csv(decisions, [row])
    write_csv(
        signals,
        [
            {
                "paper_signal_id": row["decision_id"],
                "signal_time": row["signal_time"],
                "updated_at": "2026-08-06T10:10:00+00:00",
                "completed": "1",
                "result_r": "-1",
                "outcome": "LOSS",
            }
        ],
    )
    run_adapter(decisions, signals, tmp_path / "out", cost_r=0.04)
    payload = json.loads((tmp_path / "out" / "outcomes.jsonl").read_text(encoding="utf-8"))
    assert payload["outcome"] == "LOSS"
    assert payload["net_r"] == -1.04


def test_safety_contract_is_read_only() -> None:
    assert safety_contract() == {
        "read_only": True,
        "orders_enabled": False,
        "publication_enabled": False,
        "broker_api_called": False,
    }
