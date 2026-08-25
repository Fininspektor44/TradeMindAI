from __future__ import annotations

from trademind.product_ui_v1232_entry import (
    _localize_candidate,
    render,
    translate_explanation,
)


def _candidate() -> dict[str, object]:
    return {
        "signal_id": "sig-russian-1",
        "created_at": "2026-08-06T07:30:00+00:00",
        "symbol": "NZDUSD",
        "action": "SELL",
        "state": "PENDING_GATE",
        "setup_family": "MULTIFACTOR_MARKET_SETUP",
        "scenario": (
            "valid SMC impulse and OTE geometry | H1 aligned | BULLISH_BOS; "
            "ALL_SIGNALS, LOW_SPREAD, NORMAL_VOLUME, STRUCTURE_CONFLICT"
        ),
        "quality_score": None,
        "conservative_probability": None,
        "expected_value_r": None,
        "historical_sample": 0,
        "reasons": ["insufficient historical sample"],
        "plan": {
            "average_entry": 0.58787,
            "stop_price": 0.58830,
            "targets": [0.58653, 0.58618],
            "first_target_rr": 1.5,
            "entries": [
                {
                    "price": 0.58727,
                    "weight": 0.4,
                    "rationale": "Market confirmation entry from the research signal close",
                },
                {
                    "price": 0.58787,
                    "weight": 0.35,
                    "rationale": "Fibonacci/OTE 70.5% retracement",
                },
            ],
            "invalidation": (
                "Protected swing high breaks after the liquidity/OTE setup"
            ),
            "target_rationale": [
                "Prior/external low or 1.5R",
                "External liquidity or 2R",
            ],
        },
        "market": {
            "structure": {
                "swing_bias": "BEARISH",
                "swing_break": "NONE",
                "internal_break": "NONE",
            },
            "liquidity": {"ssl_sweep": False, "bsl_sweep": False},
            "fibonacci": {
                "retracement": 0.244,
                "ote_low": 0.618,
                "ote_mid": 0.705,
                "ote_high": 0.79,
            },
            "volume": {
                "rvol_20": 0.90,
                "percentile": 43.0,
                "imbalance": -0.01,
                "tick_rate_ratio": 0.83,
            },
            "momentum": {"impulse_atr": 1.7, "body_efficiency_ratio_20": 1.1},
            "volatility": {"atr": 0.00021, "spread_cost_atr": 0.079},
            "confirmation": {"fvg": "NONE"},
        },
        "factor_scores": {
            "structure": 0.30,
            "liquidity": 0.0,
            "fibonacci": 0.25,
            "volume": 0.18,
            "momentum": 0.91,
            "volatility": 0.67,
            "confirmation": 0.0,
            "session": 0.90,
            "execution": 0.40,
            "portfolio": 0.50,
        },
        "factor_reasons": {
            "structure": ["swing_bias=BEARISH · internal_bias=BULLISH"],
            "confirmation": ["fvg=NONE · break_confirmed=0"],
            "portfolio": ["portfolio correlation feed not connected yet"],
        },
        "checks": {},
        "candles": [],
    }


def _payload(candidate: dict[str, object]) -> dict[str, object]:
    return {
        "schema_version": "1.23.2",
        "updated_at": "2026-08-06T07:35:00+00:00",
        "runtime": {
            "state": "WAITING_NO_NEW_CLOSED_BARS",
            "risk_state": None,
            "account_login": "77053345",
        },
        "factory": {
            "state": "WAITING_NO_PUBLISHABLE_PASSPORT",
            "fresh": 1,
            "publishable": 0,
        },
        "bridge": {"state": "WAITING_NO_PUBLISHABLE_PASSPORT"},
        "latest_decision": {},
        "summary": {
            "archive_candidates": 54,
            "displayed_candidates": 1,
            "active_candidates": 1,
            "fresh_factory": 1,
            "publishable": 0,
            "completed_outcomes": 0,
            "buy": 0,
            "sell": 1,
        },
        "candidates": [candidate],
    }


def test_translates_ote_sentence_and_internal_codes() -> None:
    translated = translate_explanation(
        "valid SMC impulse and OTE geometry | H1 aligned | BULLISH_BOS; "
        "ALL_SIGNALS, LOW_SPREAD, "
        "NORMAL_VOLUME, STRUCTURE_CONFLICT"
    )

    assert "OTE" in translated
    assert "BULLISH" not in translated
    assert "основные сигнальные условия соблюдены" in translated
    assert "конфликт структуры" in translated
    assert "STRUCTURE_CONFLICT" not in translated


def test_translates_metric_diagnostics() -> None:
    translated = translate_explanation(
        "swing_bias=BEARISH · internal_bias=BULLISH · break_confirmed=0"
    )

    assert "Старший уклон: медвежий" in translated
    assert "Внутренняя структура: бычий" in translated
    assert "Слом структуры подтверждён: нет" in translated
    assert "swing_bias" not in translated


def test_render_hides_english_explanations_and_raw_codes() -> None:
    candidate = _localize_candidate(_candidate())
    page = render(_payload(candidate))

    assert "OTE" in page
    assert "Старший уклон" in page
    assert "Съём ликвидности снизу" in page
    assert "Фильтр публикации" in page
    assert "Данные о корреляции портфеля пока не подключены" in page
    assert "BULLISH_BOS" not in page
    assert "STRUCTURE_CONFLICT" not in page
    assert "Swing bias" not in page
    assert "Publication gate" not in page


def test_localization_still_escapes_market_text() -> None:
    candidate = _candidate()
    candidate["scenario"] = "<script>alert(1)</script> | STRUCTURE_CONFLICT"
    page = render(_payload(_localize_candidate(candidate)))

    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page
