from pathlib import Path

from trademind import product_ui_v125 as ui


def test_risk_safe_crypto_plan_removes_allocation_fields() -> None:
    plan = {
        "average_entry": 55.524,
        "entries": [
            {
                "price": 55.524,
                "allocation": 1.0,
                "weight": 1.0,
                "rationale": "M5 триггер после согласования H1",
            }
        ],
    }

    safe = ui._risk_safe_crypto_plan(plan)
    entry = safe["entries"][0]

    assert safe["position_sizing_available"] is False
    assert safe["position_sizing_state"] == "BYBIT_RISK_MANAGER_NOT_CONNECTED"
    assert "allocation" not in entry
    assert "weight" not in entry


def test_crypto_entries_show_no_false_hundred_percent() -> None:
    plan = ui._risk_safe_crypto_plan(
        {
            "average_entry": 55.524,
            "entries": [
                {
                    "price": 55.524,
                    "allocation": 1.0,
                    "rationale": "M5 триггер после согласования H1",
                }
            ],
        }
    )

    rendered = ui._entries_html(plan)

    assert "100%" not in rendered
    assert "Размер позиции не рассчитан" in rendered
    assert "Bybit Risk Manager не подключён" in rendered
    assert "M5 триггер после согласования H1" in rendered


def test_crypto_risk_panel_ignores_forex_sizing_decision() -> None:
    candidate = {"asset_class": "CRYPTO", "state": "PUBLISHABLE"}
    decision = {
        "trader_summary": {
            "decision": "ALLOW",
            "actual_risk_pct": 1.0,
            "total_volume": 999.0,
            "margin_required": 1000.0,
        }
    }

    rendered = ui._risk_candidate_html(candidate, decision)

    assert "Bybit Risk Manager не подключён" in rendered
    assert "Размер позиции" in rendered
    assert "999" not in rendered
    assert "Лот" not in rendered


def test_build_payload_marks_only_crypto_plan_as_unsized(monkeypatch) -> None:
    source = {
        "schema_version": "1.24.0",
        "summary": {},
        "candidates": [
            {
                "asset_class": "FOREX",
                "plan": {"entries": [{"price": 1.1, "weight": 1.0}]},
            },
            {
                "asset_class": "CRYPTO",
                "plan": {"entries": [{"price": 55.5, "weight": 1.0}]},
            },
        ],
    }

    monkeypatch.setattr(ui.previous, "build_payload", lambda *args, **kwargs: source)
    payload = ui.build_payload(
        {},
        None,
        Path("crypto"),
        Path("bars.csv"),
        fx_limit=1,
        crypto_limit=1,
        candle_limit=1,
    )

    forex, crypto = payload["candidates"]
    assert forex["plan"]["entries"][0]["weight"] == 1.0
    assert crypto["plan"]["position_sizing_available"] is False
    assert "weight" not in crypto["plan"]["entries"][0]
    assert payload["schema_version"] == "1.25.2"
