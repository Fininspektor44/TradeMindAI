from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path

from trademind.crypto_signal_adapter_v125 import (
    STRUCTURE_SETUP_FAMILY,
    build_candidate,
    run_adapter,
    safety_contract,
)
from trademind.signal_intelligence import candidate_from_dict


def decision_row(signal_time: str, **overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "decision_id": "BTCUSDT:1000:MTF_FLOW_ALIGNMENT",
        "signal_time": signal_time,
        "symbol": "BTCUSDT",
        "action": "BUY",
        "gate_status": "CANDIDATE",
        "quality_score": "82",
        "components": "H1_PRICE|M15_DELTA|M5_DELTA_IMPULSE",
        "reasons": "",
        "entry_price": "105",
        "stop_price": "103.95",
        "target_price": "106.575",
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


def snapshot() -> dict[str, object]:
    return {
        "state": "OK",
        "as_of": "2026-08-06T10:00:00+00:00",
        "bar_counts": {"M5": 240, "M15": 80, "H1": 20},
        "timeframes": {
            "H1": {
                "bias": "BULLISH",
                "break": "BULLISH_BOS",
                "break_direction": "BULLISH",
                "break_level": 104,
                "last_swing_high": 106,
                "last_swing_low": 100,
            },
            "M15": {
                "bias": "BULLISH",
                "break": "BULLISH_CHOCH",
                "break_direction": "BULLISH",
                "break_level": 103,
            },
        },
        "liquidity": {
            "ssl_sweep": True,
            "bsl_sweep": False,
            "sweep_type": "SSL_SWEEP",
            "sweep_level": 102,
            "sweep_depth_atr": 0.4,
        },
        "fvg": {
            "type": "BULLISH_FVG",
            "lower": 103,
            "upper": 103.2,
            "size_atr": 0.3,
        },
        "fibonacci": {
            "retracement": 0.705,
            "ote_low": 0.618,
            "ote_mid": 0.705,
            "ote_high": 0.790,
            "ote_hit": True,
            "level_618": 104.2,
            "level_705": 103.8,
            "level_790": 103.4,
            "impulse_start": 100,
            "impulse_end": 106,
            "score": 1.0,
        },
        "volatility": {"atr_m5": 0.4, "atr_m15": 0.8, "atr_h1": 1.5},
        "factor_scores": {
            "structure": 1.0,
            "liquidity": 1.0,
            "fibonacci": 1.0,
            "confirmation": 1.0,
        },
        "factor_reasons": {
            "structure": ["H1 bullish BOS", "M15 bullish CHoCH"],
            "liquidity": ["SSL sweep", "bullish FVG"],
            "fibonacci": ["OTE 70.5%", "OTE reached"],
            "confirmation": ["native structure aligned"],
        },
    }


def write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def write_bars(path: Path, count: int = 240) -> datetime:
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
    )


def test_build_candidate_merges_native_structure_without_changing_source_plan() -> None:
    candidate = build_candidate(
        decision_row("2026-08-06T10:00:00+00:00"),
        snapshot(),
    )
    assert candidate.setup_family == STRUCTURE_SETUP_FAMILY
    assert candidate.plan.first_target_rr == 1.5
    assert candidate.market_features["structure"]["swing_break"] == "BULLISH_BOS"
    assert candidate.market_features["liquidity"]["fvg"] == "BULLISH_FVG"
    assert candidate.market_features["fibonacci"]["ote_hit"] is True
    assert candidate.factor_scores["fibonacci"] == 1.0


def test_run_adapter_writes_point_in_time_structure_audit(tmp_path: Path) -> None:
    bars = tmp_path / "bars.csv"
    signal_time = write_bars(bars).isoformat()
    decisions = tmp_path / "decisions.csv"
    signals = tmp_path / "signals.csv"
    output = tmp_path / "crypto"
    row = decision_row(signal_time)
    write_csv(decisions, [row])
    write_csv(
        signals,
        [
            {
                "paper_signal_id": row["decision_id"],
                "signal_time": signal_time,
                "updated_at": signal_time,
                "completed": "1",
                "result_r": "1.5",
                "outcome": "WIN",
            }
        ],
    )
    result = run_adapter(
        decisions,
        signals,
        bars,
        output,
        cost_r=0.04,
        now=datetime(2026, 8, 6, 10, 1, tzinfo=timezone.utc),
    )
    assert result.candidates == 1
    assert result.outcomes == 1
    assert result.structure_ok == 1

    payload = json.loads((output / "candidates.jsonl").read_text(encoding="utf-8"))
    candidate = candidate_from_dict(payload)
    assert candidate.setup_family == STRUCTURE_SETUP_FAMILY
    assert payload["structure_state"] == "OK"
    assert payload["orders_enabled"] is False

    audit = json.loads(
        (output / "structure_snapshots.jsonl").read_text(encoding="utf-8")
    )
    assert audit["signal_id"] == candidate.signal_id
    assert audit["safety"]["future_bars_used"] is False

    status = json.loads((output / "status.json").read_text(encoding="utf-8"))
    assert status["structure_ok"] == 1
    assert status["safety"]["source_files_modified"] is False


def test_safety_contract_is_read_only() -> None:
    assert safety_contract() == {
        "read_only": True,
        "orders_enabled": False,
        "publication_enabled": False,
        "broker_api_called": False,
        "future_bars_used": False,
    }
