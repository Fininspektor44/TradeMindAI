from __future__ import annotations

import csv
import json
from pathlib import Path

from trademind.product_ui import human_state, render, run_product_ui


def _candidate(symbol: str = "EURUSD") -> dict[str, object]:
    return {
        "signal_id": "sig-1",
        "created_at": "2026-08-06T04:30:00+00:00",
        "symbol": symbol,
        "action": "BUY",
        "state": "SHADOW_ONLY",
        "setup_family": "MULTIFACTOR_MARKET_SETUP",
        "scenario": "BOS + OTE + volume confirmation",
        "quality_score": 42.7,
        "conservative_probability": 0.43,
        "expected_value_r": -0.013,
        "historical_sample": 22,
        "reasons": ["Недостаточная выборка"],
        "plan": {
            "average_entry": 1.155,
            "stop_price": 1.152,
            "targets": [1.161],
            "first_target_rr": 2.0,
        },
    }


def _dashboard(canonical: Path, candidate: dict[str, object]) -> dict[str, object]:
    return {
        "updated_at": "2026-08-06T04:35:00+00:00",
        "runtime": {
            "state": "WAITING_NO_NEW_CLOSED_BARS",
            "risk_state": None,
            "account_login": "77053345",
            "server_utc_offset_hours": 3,
            "paths": {"canonical_volume": str(canonical)},
        },
        "factory": {
            "state": "WAITING_NO_PUBLISHABLE_PASSPORT",
            "fresh": 1,
            "publishable": 0,
        },
        "bridge": {"state": "WAITING_NO_PUBLISHABLE_PASSPORT"},
        "latest_decision": {},
        "summary": {"candidates": 1, "outcomes": 0},
        "candidates": [candidate],
    }


def _write_candles(path: Path) -> None:
    rows = [
        {
            "symbol": "EURUSD",
            "timeframe": "M5",
            "time": str(1785989700 + index * 300),
            "open": str(1.154 + index * 0.0002),
            "high": str(1.155 + index * 0.0002),
            "low": str(1.153 + index * 0.0002),
            "close": str(1.1545 + index * 0.0002),
        }
        for index in range(4)
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)


def test_product_ui_writes_modern_interface_with_real_candles(tmp_path: Path) -> None:
    canonical = tmp_path / "volume.csv"
    _write_candles(canonical)
    runtime = tmp_path / "runtime"
    dashboard = runtime / "dashboard"
    dashboard.mkdir(parents=True)
    (dashboard / "data.json").write_text(
        json.dumps(_dashboard(canonical, _candidate()), ensure_ascii=False),
        encoding="utf-8",
    )

    index, payload = run_product_ui(runtime)

    page = index.read_text(encoding="utf-8")
    assert payload["schema_version"] == "1.23.0"
    assert len(payload["candidates"][0]["candles"]) == 4
    assert "TradeMind" in page
    assert "Рынок синхронизирован" in page
    assert "Качественных сигналов пока нет" in page
    assert "<svg" in page
    assert "Технический dashboard" in page
    assert "WAITING_NO_PUBLISHABLE_PASSPORT" not in page


def test_product_ui_escapes_market_text() -> None:
    candidate = _candidate("<script>alert(1)</script>")
    candidate["reasons"] = ["<img src=x onerror=alert(1)>"]
    payload = _dashboard(Path("missing.csv"), candidate)
    product = {
        **payload,
        "summary": {
            "total_candidates": 1,
            "fresh_factory": 1,
            "publishable": 0,
            "completed_outcomes": 0,
            "buy": 1,
            "sell": 0,
            "average_quality": 42.7,
        },
    }

    page = render(product)

    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    assert "<img src=x onerror=alert(1)>" not in page


def test_human_state_hides_internal_codes() -> None:
    assert human_state("WAITING_NO_NEW_CLOSED_BARS") == "Рынок синхронизирован"
    assert (
        human_state("WAITING_NO_PUBLISHABLE_PASSPORT")
        == "Качественных сигналов пока нет"
    )
    assert human_state("ALLOW") == "Сделка допустима"
