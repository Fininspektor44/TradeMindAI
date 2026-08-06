from __future__ import annotations

import csv
import json
from pathlib import Path

from trademind.crypto_signal_adapter import build_candidate
from trademind.product_ui_v124 import (
    crypto_candidate_to_ui,
    load_crypto_feed,
    render,
    safety_contract,
)


def source_decision() -> dict[str, object]:
    return {
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


def candidate_payload() -> dict[str, object]:
    candidate = build_candidate(source_decision())
    return {
        **candidate.as_dict(),
        "asset_class": "CRYPTO",
        "venue": "BYBIT",
        "source_gate_status": "CANDIDATE",
        "source_quality_score": 82.0,
    }


def test_crypto_candidate_converts_to_product_shape() -> None:
    payload = candidate_payload()
    evaluation = {
        "state": "SHADOW_ONLY",
        "quality_score": 71.5,
        "historical_sample": 12,
        "conservative_probability": 0.44,
        "expected_value_r": -0.1,
        "reasons": ["completed sample 12 < 30"],
        "checks": {"sample": False},
    }
    candles = [
        {"time": 1.0, "open": 64900.0, "high": 65100.0, "low": 64800.0, "close": 65000.0}
    ]
    item = crypto_candidate_to_ui(payload, evaluation, None, candles)
    assert item["asset_class"] == "CRYPTO"
    assert item["venue"] == "BYBIT"
    assert item["state"] == "SHADOW_ONLY"
    assert item["quality_score"] == 71.5
    assert item["plan"]["entries"][0]["weight"] == 1.0
    assert item["market"]["sentiment"]["funding_rate"] == 0.0001
    assert item["candles"] == candles


def test_completed_crypto_outcome_gets_outcome_state() -> None:
    item = crypto_candidate_to_ui(
        candidate_payload(),
        {},
        {"outcome": "WIN", "net_r": 1.46},
        [],
    )
    assert item["state"] == "OUTCOME_WIN"
    assert "1.46R" in item["reasons"][0]


def test_load_crypto_feed_reads_candidates_factory_and_candles(tmp_path: Path) -> None:
    root = tmp_path / "crypto"
    root.mkdir()
    payload = candidate_payload()
    (root / "candidates.jsonl").write_text(
        json.dumps(payload, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    (root / "outcomes.jsonl").write_text("", encoding="utf-8")
    (root / "status.json").write_text(
        json.dumps({"state": "OK", "updated_at": "2026-08-06T10:00:00+00:00"}),
        encoding="utf-8",
    )
    factory = root / "factory"
    factory.mkdir()
    (factory / "evaluations.json").write_text(
        json.dumps(
            {
                "evaluations": [
                    {
                        "signal_id": payload["signal_id"],
                        "state": "SHADOW_ONLY",
                        "quality_score": 70.0,
                        "historical_sample": 0,
                        "reasons": [],
                        "checks": {},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    (factory / "status.json").write_text(
        json.dumps(
            {
                "state": "WAITING_NO_PUBLISHABLE_PASSPORT",
                "fresh_candidates": 1,
                "publishable": 0,
            }
        ),
        encoding="utf-8",
    )
    bars = tmp_path / "bybit_bars.csv"
    with bars.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["symbol", "start_ms", "open", "high", "low", "close"],
        )
        writer.writeheader()
        writer.writerow(
            {
                "symbol": "BTCUSDT",
                "start_ms": "1786008600000",
                "open": "64900",
                "high": "65100",
                "low": "64800",
                "close": "65000",
            }
        )

    rows, status = load_crypto_feed(root, bars, limit=10, candle_limit=48)
    assert len(rows) == 1
    assert rows[0]["candles"][0]["close"] == 65000.0
    assert status["candidates"] == 1
    assert status["displayed"] == 1
    assert status["factory_fresh"] == 1


def test_render_adds_market_filter_crypto_badge_and_derivatives() -> None:
    item = crypto_candidate_to_ui(candidate_payload(), {}, None, [])
    page = render(
        {
            "schema_version": "1.24.0",
            "updated_at": "2026-08-06T10:00:00+00:00",
            "runtime": {"state": "RUN_COMPLETE", "account_login": "37365712"},
            "factory": {"state": "WAITING_NO_PUBLISHABLE_PASSPORT"},
            "bridge": {"state": "WAITING_NO_PUBLISHABLE_PASSPORT"},
            "decision": {},
            "summary": {
                "archive_candidates": 1,
                "displayed_candidates": 1,
                "active_candidates": 1,
                "fresh_factory": 1,
                "publishable": 0,
                "completed_outcomes": 0,
                "buy": 1,
                "sell": 0,
                "forex_displayed": 0,
                "crypto_displayed": 1,
            },
            "candidates": [item],
            "crypto": {
                "state": "OK",
                "factory_state": "WAITING_NO_PUBLISHABLE_PASSPORT",
                "displayed": 1,
            },
        }
    )
    assert "data-market-filter='CRYPTO'" in page
    assert "data-market='CRYPTO'" in page
    assert "M5 · CRYPTO" in page
    assert "CRYPTO · BYBIT" in page
    assert "Funding, basis и OI" in page
    assert "Forex и Crypto" in page
    assert "OrderSend" not in page


def test_safety_contract_is_read_only() -> None:
    assert safety_contract() == {
        "read_only": True,
        "orders_enabled": False,
        "publication_enabled": False,
        "broker_api_called": False,
    }
