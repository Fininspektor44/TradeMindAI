from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from trademind.live_signal_dashboard import DASHBOARD_OK, run_live_dashboard
from trademind.signal_intelligence import EntryOrder, SignalCandidate, TradePlan


NOW = datetime(2026, 8, 6, 4, 0, tzinfo=timezone.utc)


def _candidate() -> SignalCandidate:
    return SignalCandidate(
        observed_at=datetime(2026, 8, 6, 3, 55, tzinfo=timezone.utc),
        created_at=datetime(2026, 8, 6, 3, 55, tzinfo=timezone.utc),
        symbol="EURUSD",
        timeframe="M5",
        setup_family="SMC_OTE_LIQUIDITY_REVERSAL",
        scenario="SSL sweep, bullish CHoCH and OTE confirmation <script>alert(1)</script>",
        plan=TradePlan(
            action="BUY",
            entries=(
                EntryOrder(
                    price=1.1010,
                    allocation=0.5,
                    rationale="Market confirmation",
                    order_type="MARKET",
                ),
                EntryOrder(
                    price=1.1000,
                    allocation=0.5,
                    rationale="OTE 70.5%",
                ),
            ),
            stop_price=1.0970,
            targets=(1.1070, 1.1110),
            invalidation="Protected low breaks",
        ),
        market_features={
            "structure": {
                "swing_bias": "BULLISH",
                "swing_break": "CHOCH",
                "internal_bias": "BULLISH",
                "internal_break": "BOS",
                "protected_low": 1.0980,
                "protected_high": 1.1080,
            },
            "liquidity": {
                "bsl_sweep": False,
                "ssl_sweep": True,
                "ssl_sweep_depth_atr": 0.35,
            },
            "fibonacci": {
                "retracement": 0.705,
                "ote_low": 0.618,
                "ote_mid": 0.705,
                "ote_high": 0.79,
            },
            "volume": {
                "rvol_20": 1.6,
                "volume_percentile_100": 88.0,
                "direction_imbalance": 0.18,
                "tick_rate_ratio_20": 1.4,
            },
            "momentum": {
                "impulse_atr": 1.8,
                "body_efficiency_ratio_20": 1.3,
            },
            "volatility": {
                "atr": 0.0012,
                "spread_cost_atr": 0.04,
                "spread_ratio_20": 0.9,
            },
            "confirmation": {"fvg": "BULLISH", "fvg_size_atr": 0.4},
            "session": {"name": "LONDON_NY_OVERLAP"},
            "execution": {"point": 0.00001, "spread_mean_points": 8.0},
        },
        factor_scores={
            "structure": 0.9,
            "liquidity": 0.9,
            "fibonacci": 1.0,
            "volume": 0.8,
            "momentum": 0.75,
            "volatility": 0.8,
            "confirmation": 0.9,
            "session": 1.0,
            "execution": 0.85,
            "portfolio": 0.5,
        },
        factor_reasons={
            "structure": ("bullish CHoCH",),
            "liquidity": ("SSL swept",),
        },
        provenance=("TEST_MARKET_DATA",),
    )


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_dashboard_renders_candidate_gate_and_safety(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    candidate = _candidate()
    root.mkdir(parents=True)
    (root / "candidates.jsonl").write_text(
        json.dumps(candidate.as_dict(), ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    (root / "outcomes.jsonl").write_text("", encoding="utf-8")
    runtime_status = {
        "state": "RUN_COMPLETE",
        "updated_at": NOW.isoformat(),
        "account_login": "77053345",
        "closed_fx_m5_rows": 9100,
        "latest_closed_bar_at": "2026-08-06T03:55:00+00:00",
        "risk_state": None,
    }
    _write_json(
        root / "factory" / "status.json",
        {
            "state": "WAITING_NO_PUBLISHABLE_PASSPORT",
            "fresh_candidates": 1,
            "publishable": 0,
        },
    )
    _write_json(
        root / "factory" / "evaluations.json",
        {
            "evaluations": [
                {
                    "signal_id": candidate.signal_id,
                    "state": "SHADOW_ONLY",
                    "quality_score": 78.5,
                    "conservative_probability": 0.61,
                    "expected_value_r": 0.12,
                    "historical_sample": 18,
                    "checks": {"sample": False, "quality": True},
                    "reasons": ["historical sample 18 is below minimum 30"],
                }
            ]
        },
    )
    _write_json(
        root / "bridge" / "77053345" / "status.json",
        {"state": "WAITING_NO_PUBLISHABLE_PASSPORT"},
    )

    result = run_live_dashboard(
        runtime_root=root,
        login="77053345",
        runtime_status=runtime_status,
        now=NOW,
    )

    assert result.status["state"] == DASHBOARD_OK
    content = result.dashboard_path.read_text(encoding="utf-8")
    assert "TradeMind Live Signal Dashboard v1.22" in content
    assert "EURUSD" in content
    assert "SHADOW_ONLY" in content
    assert "historical sample 18 is below minimum 30" in content
    assert "READ-ONLY" in content
    assert "<script>alert(1)</script>" not in content
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in content

    data = json.loads(result.data_path.read_text(encoding="utf-8"))
    assert data["summary"]["candidates"] == 1
    assert data["safety"]["dashboard_recalculates_gate"] is False
    assert data["candidates"][0]["market"]["fibonacci"]["retracement"] == 0.705


def test_dashboard_handles_empty_runtime(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    root.mkdir(parents=True)
    result = run_live_dashboard(
        runtime_root=root,
        login="77053345",
        runtime_status={"state": "WAITING_SOURCE_EMPTY"},
        now=NOW,
    )
    assert result.status["displayed_candidates"] == 0
    assert "Live-кандидатов пока нет" in result.dashboard_path.read_text(encoding="utf-8")
