from __future__ import annotations

from trademind.crypto_signal_adapter_v125 import build_candidate
from trademind.product_ui_v124 import crypto_candidate_to_ui
from trademind.product_ui_v125 import render, safety_contract


def candidate_item() -> dict[str, object]:
    row = {
        "decision_id": "BTCUSDT:1000:MTF_FLOW_ALIGNMENT",
        "signal_time": "2026-08-06T10:00:00+00:00",
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
    snapshot = {
        "state": "OK",
        "as_of": row["signal_time"],
        "bar_counts": {"M5": 240, "M15": 80, "H1": 20},
        "timeframes": {
            "H1": {
                "bias": "BULLISH",
                "break": "BULLISH_BOS",
                "break_direction": "BULLISH",
                "break_level": 64800,
            },
            "M15": {
                "bias": "BULLISH",
                "break": "BULLISH_CHOCH",
                "break_direction": "BULLISH",
                "break_level": 64900,
            },
        },
        "liquidity": {
            "ssl_sweep": True,
            "bsl_sweep": False,
            "sweep_type": "SSL_SWEEP",
            "sweep_level": 64600,
            "sweep_depth_atr": 0.4,
        },
        "fvg": {
            "type": "BULLISH_FVG",
            "lower": 64700,
            "upper": 64800,
            "size_atr": 0.3,
        },
        "fibonacci": {
            "retracement": 0.705,
            "ote_low": 0.618,
            "ote_mid": 0.705,
            "ote_high": 0.790,
            "ote_hit": True,
            "level_618": 64920,
            "level_705": 64750,
            "level_790": 64580,
            "impulse_start": 64000,
            "impulse_end": 66400,
            "score": 1.0,
        },
        "volatility": {"atr_m5": 210, "atr_m15": 460, "atr_h1": 900},
        "factor_scores": {
            "structure": 1.0,
            "liquidity": 1.0,
            "fibonacci": 1.0,
            "confirmation": 1.0,
        },
        "factor_reasons": {
            "structure": ["H1 bullish BOS"],
            "liquidity": ["SSL sweep"],
            "fibonacci": ["OTE reached"],
            "confirmation": ["aligned"],
        },
    }
    candidate = build_candidate(row, snapshot)
    payload = {
        **candidate.as_dict(),
        "asset_class": "CRYPTO",
        "venue": "BYBIT",
        "source_gate_status": "CANDIDATE",
        "source_quality_score": 82.0,
    }
    return crypto_candidate_to_ui(payload, {}, None, [])


def test_render_shows_native_crypto_structure_and_ote() -> None:
    item = candidate_item()
    page = render(
        {
            "schema_version": "1.25.0",
            "updated_at": "2026-08-06T10:00:00+00:00",
            "runtime": {"state": "RUN_COMPLETE", "account_login": "77053345"},
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
    assert "Нативная структура" in page
    assert "бычий BOS" in page
    assert "съём ликвидности снизу" in page
    assert "Цена 70,5%" in page
    assert "70,5%" in page
    assert "Будущие свечи" in page
    assert "data-market-filter='CRYPTO'" in page
    assert "OrderSend" not in page


def test_safety_contract_is_read_only() -> None:
    assert safety_contract() == {
        "read_only": True,
        "orders_enabled": False,
        "publication_enabled": False,
        "broker_api_called": False,
        "future_bars_used": False,
    }
