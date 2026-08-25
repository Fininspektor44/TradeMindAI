from __future__ import annotations

import csv
import json
from pathlib import Path

from trademind.product_ui_v1231 import (
    VERSION,
    build_payload,
    render,
    run_product_ui,
    sort_candidates,
)


def _candidate(
    signal_id: str,
    state: str,
    *,
    symbol: str = "EURUSD",
    action: str = "BUY",
    quality: float = 42.7,
    created_at: str = "2026-08-06T04:30:00+00:00",
) -> dict[str, object]:
    return {
        "signal_id": signal_id,
        "created_at": created_at,
        "symbol": symbol,
        "action": action,
        "state": state,
        "setup_family": "SMC_OTE_CONTINUATION",
        "scenario": "SSL sweep, bullish BOS, OTE retracement and volume expansion",
        "quality_score": quality,
        "conservative_probability": 0.63,
        "expected_value_r": 0.31,
        "historical_sample": 43,
        "checks": {"sample": state == "PUBLISHABLE", "ev": True},
        "reasons": ["Недостаточная выборка"] if state == "REJECTED" else [],
        "factor_scores": {
            "structure": 0.92,
            "liquidity": 0.88,
            "fibonacci": 0.84,
            "volume": 0.81,
        },
        "factor_reasons": {
            "structure": ["bullish BOS"],
            "liquidity": ["sell-side liquidity swept"],
        },
        "market": {
            "structure": {
                "swing_bias": "BULLISH",
                "swing_break": "BOS",
                "internal_break": "CHOCH",
            },
            "liquidity": {"ssl_sweep": True, "bsl_sweep": False},
            "fibonacci": {
                "retracement": 0.618,
                "ote_low": 1.098,
                "ote_mid": 1.097,
                "ote_high": 1.096,
            },
            "volume": {
                "rvol_20": 1.7,
                "percentile": 86.0,
                "imbalance": 0.42,
                "tick_rate_ratio": 1.4,
            },
            "momentum": {"impulse_atr": 1.8, "body_efficiency_ratio_20": 1.2},
            "volatility": {"atr": 0.0012, "spread_cost_atr": 0.03},
            "confirmation": {"fvg": "BULLISH"},
        },
        "plan": {
            "entries": [
                {"price": 1.1000, "weight": 0.5, "rationale": "Fibonacci 61.8%"},
                {"price": 1.0980, "weight": 0.3, "rationale": "OTE zone"},
                {"price": 1.0960, "weight": 0.2, "rationale": "Fibonacci 79%"},
            ],
            "average_entry": 1.0986,
            "stop_price": 1.0920,
            "targets": [1.1070, 1.1120],
            "first_target_rr": 1.5,
            "invalidation": "Close below protected low",
            "target_rationale": ["Prior high", "External liquidity"],
        },
    }


def _source(canonical: Path) -> dict[str, object]:
    return {
        "updated_at": "2026-08-06T05:00:00+00:00",
        "runtime": {
            "state": "WAITING_NO_NEW_CLOSED_BARS",
            "risk_state": None,
            "account_login": "77053345",
            "server_utc_offset_hours": 3,
            "paths": {"canonical_volume": str(canonical)},
        },
        "factory": {
            "state": "WAITING_NO_PUBLISHABLE_PASSPORT",
            "fresh": 2,
            "publishable": 1,
        },
        "bridge": {"state": "WAITING_NO_PUBLISHABLE_PASSPORT"},
        "latest_decision": {},
        "summary": {"candidates": 54, "outcomes": 0},
        "candidates": [
            _candidate(
                "rejected",
                "REJECTED",
                symbol="USDJPY",
                quality=80,
                created_at="2026-08-06T04:55:00+00:00",
            ),
            _candidate(
                "ready",
                "PUBLISHABLE",
                symbol="EURUSD",
                quality=60,
                created_at="2026-08-06T04:45:00+00:00",
            ),
        ],
    }


def _write_candles(path: Path) -> None:
    rows = []
    for symbol in ("EURUSD", "USDJPY"):
        for index in range(4):
            rows.append(
                {
                    "symbol": symbol,
                    "timeframe": "M5",
                    "time": str(1785989700 + index * 300),
                    "open": str(1.10 + index * 0.0002),
                    "high": str(1.101 + index * 0.0002),
                    "low": str(1.099 + index * 0.0002),
                    "close": str(1.1005 + index * 0.0002),
                }
            )
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)


def test_publishable_candidates_are_ranked_before_rejected() -> None:
    ordered = sort_candidates(
        [
            _candidate("bad", "REJECTED", quality=99),
            _candidate("good", "PUBLISHABLE", quality=50),
        ]
    )
    assert ordered[0]["signal_id"] == "good"


def test_payload_separates_archive_count_from_current_feed(tmp_path: Path) -> None:
    canonical = tmp_path / "volume.csv"
    _write_candles(canonical)

    payload = build_payload(_source(canonical), canonical, 24, 48)

    assert payload["schema_version"] == VERSION
    assert payload["summary"]["archive_candidates"] == 54
    assert payload["summary"]["displayed_candidates"] == 2
    assert payload["summary"]["buy"] == 2
    assert payload["candidates"][0]["signal_id"] == "ready"


def test_product_ui_writes_clickable_full_passport(tmp_path: Path) -> None:
    canonical = tmp_path / "volume.csv"
    _write_candles(canonical)
    runtime = tmp_path / "runtime"
    dashboard = runtime / "dashboard"
    dashboard.mkdir(parents=True)
    (dashboard / "data.json").write_text(
        json.dumps(_source(canonical), ensure_ascii=False),
        encoding="utf-8",
    )

    index, payload = run_product_ui(runtime)
    page = index.read_text(encoding="utf-8")

    assert payload["schema_version"] == "1.23.1"
    assert "<dialog" in page
    assert "Открыть паспорт" in page
    assert "Smart Money" in page
    assert "Fibonacci" in page
    assert "Объёмы" in page
    assert "Всего накоплено" in page
    assert "BUY / SELL в ленте" in page
    assert "WAITING_NO_PUBLISHABLE_PASSPORT" not in page


def test_product_ui_escapes_detail_text() -> None:
    candidate = _candidate(
        "unsafe",
        "REJECTED",
        symbol="<script>alert(1)</script>",
    )
    candidate["scenario"] = "<img src=x onerror=alert(1)>"
    payload = {
        "updated_at": "2026-08-06T05:00:00+00:00",
        "runtime": {"state": "WAITING_NO_NEW_CLOSED_BARS", "account_login": "1"},
        "factory": {"state": "WAITING_NO_PUBLISHABLE_PASSPORT"},
        "bridge": {"state": "WAITING_NO_PUBLISHABLE_PASSPORT"},
        "latest_decision": {},
        "summary": {
            "archive_candidates": 1,
            "displayed_candidates": 1,
            "active_candidates": 1,
            "fresh_factory": 0,
            "publishable": 0,
            "completed_outcomes": 0,
            "buy": 1,
            "sell": 0,
        },
        "candidates": [candidate],
    }

    page = render(payload)

    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    assert "<img src=x onerror=alert(1)>" not in page
