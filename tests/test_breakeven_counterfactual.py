from __future__ import annotations

import csv
import json
from pathlib import Path

from trademind import breakeven_counterfactual as resolver

DEAL_FIELDS = [
    "ticket",
    "position_id",
    "time_msc",
    "symbol",
    "magic",
    "deal_type",
    "entry",
    "volume",
    "price",
    "profit",
    "commission",
    "swap",
    "fee",
    "reason",
    "comment",
    "robot",
]


def write_deals(path: Path) -> None:
    rows = [
        {
            "ticket": "1",
            "position_id": "501",
            "time_msc": "1780000000000",
            "symbol": "EURUSD",
            "magic": "777270003",
            "deal_type": "BUY",
            "entry": "IN",
            "volume": "0.10",
            "price": "1.1000",
            "profit": "0",
            "commission": "-1",
            "swap": "0",
            "fee": "0",
            "reason": "EXPERT",
            "comment": "open",
            "robot": "AOExtremum",
        },
        {
            "ticket": "2",
            "position_id": "501",
            "time_msc": "1780000180000",
            "symbol": "EURUSD",
            "magic": "777270003",
            "deal_type": "SELL",
            "entry": "OUT",
            "volume": "0.10",
            "price": "1.1200",
            "profit": "50",
            "commission": "-1",
            "swap": "0",
            "fee": "0",
            "reason": "TP",
            "comment": "close",
            "robot": "AOExtremum",
        },
        {
            "ticket": "3",
            "position_id": "601",
            "time_msc": "1780000300000",
            "symbol": "USDJPY",
            "magic": "777270004",
            "deal_type": "SELL",
            "entry": "IN",
            "volume": "0.20",
            "price": "150.000",
            "profit": "0",
            "commission": "-1",
            "swap": "0",
            "fee": "0",
            "reason": "EXPERT",
            "comment": "open",
            "robot": "AOExtremum",
        },
        {
            "ticket": "4",
            "position_id": "601",
            "time_msc": "1780000480000",
            "symbol": "USDJPY",
            "magic": "777270004",
            "deal_type": "BUY",
            "entry": "OUT",
            "volume": "0.20",
            "price": "150.500",
            "profit": "-30",
            "commission": "-1",
            "swap": "0",
            "fee": "0",
            "reason": "SL",
            "comment": "close",
            "robot": "AOExtremum",
        },
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=DEAL_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def epoch(*, login: str, magic: str, symbol: str, side: str, position_id: str, seen: str, triggered: bool, revisited: bool) -> dict:
    return {
        "account_login": login,
        "magic": magic,
        "symbol": symbol,
        "side": side,
        "position_ids": [position_id],
        "first_seen_at": seen,
        "be_triggered": triggered,
        "be_revisited": revisited,
    }


def write_state(path: Path) -> None:
    payload = {
        "schema_version": "1.28.0",
        "epochs": {
            "winner": epoch(
                login="77053345",
                magic="777270003",
                symbol="EURUSD",
                side="BUY",
                position_id="501",
                seen="2026-05-28T20:27:40+00:00",
                triggered=True,
                revisited=True,
            ),
            "loser": epoch(
                login="77053345",
                magic="777270004",
                symbol="USDJPY",
                side="SELL",
                position_id="601",
                seen="2026-05-28T20:32:40+00:00",
                triggered=True,
                revisited=True,
            ),
        },
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def read_report(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def test_resolver_classifies_winner_cut_and_loss_avoided(tmp_path: Path) -> None:
    deals = tmp_path / "deals.csv"
    state = tmp_path / "state.json"
    out = tmp_path / "out"
    write_deals(deals)
    write_state(state)

    status = resolver.run_counterfactual(state, deals, out, login="77053345")
    assert status["covered_completed_baskets"] == 2
    assert status["losses_avoided_count"] == 1
    assert status["winners_cut_count"] == 1

    rows = read_report(out / "basket_be_counterfactual.csv")
    by_symbol = {row["symbol"]: row for row in rows}
    assert by_symbol["EURUSD"]["effect_class"] == "WINNER_CUT_BY_BE"
    assert float(by_symbol["EURUSD"]["opportunity_cost_proxy_money"]) == 48.0
    assert by_symbol["USDJPY"]["effect_class"] == "LOSS_AVOIDED_BY_BE"
    assert float(by_symbol["USDJPY"]["loss_avoided_proxy_money"]) == 32.0
    assert status["net_effect_proxy_money"] == -16.0


def test_epoch_from_other_login_is_not_used(tmp_path: Path) -> None:
    deals = tmp_path / "deals.csv"
    state = tmp_path / "state.json"
    out = tmp_path / "out"
    write_deals(deals)
    payload = {
        "epochs": {
            "other": epoch(
                login="999",
                magic="777270003",
                symbol="EURUSD",
                side="BUY",
                position_id="501",
                seen="2026-05-28T20:27:40+00:00",
                triggered=True,
                revisited=True,
            )
        }
    }
    state.write_text(json.dumps(payload), encoding="utf-8")

    status = resolver.run_counterfactual(state, deals, out, login="77053345")
    assert status["covered_completed_baskets"] == 0
    assert all(
        row["effect_class"] == "NO_SHADOW_COVERAGE"
        for row in read_report(out / "basket_be_counterfactual.csv")
    )


def test_trigger_without_revisit_does_not_claim_effect(tmp_path: Path) -> None:
    deals = tmp_path / "deals.csv"
    state = tmp_path / "state.json"
    out = tmp_path / "out"
    write_deals(deals)
    payload = {
        "epochs": {
            "winner": epoch(
                login="77053345",
                magic="777270003",
                symbol="EURUSD",
                side="BUY",
                position_id="501",
                seen="2026-05-28T20:27:40+00:00",
                triggered=True,
                revisited=False,
            )
        }
    }
    state.write_text(json.dumps(payload), encoding="utf-8")

    status = resolver.run_counterfactual(state, deals, out, login="77053345")
    rows = read_report(out / "basket_be_counterfactual.csv")
    eurusd = next(row for row in rows if row["symbol"] == "EURUSD")
    assert eurusd["effect_class"] == "TRIGGERED_NO_REVISIT"
    assert float(eurusd["net_effect_proxy_money"]) == 0.0
    assert status["triggered_without_revisit_count"] == 1


def test_safety_contract(tmp_path: Path) -> None:
    deals = tmp_path / "deals.csv"
    state = tmp_path / "state.json"
    out = tmp_path / "out"
    write_deals(deals)
    write_state(state)
    status = resolver.run_counterfactual(state, deals, out, login="77053345")
    assert status["safety"] == {
        "read_only": True,
        "shadow_only": True,
        "orders_enabled": False,
        "position_modify_called": False,
        "broker_api_called": False,
        "source_deals_modified": False,
        "robot_settings_modified": False,
    }


def test_source_has_no_execution_imports() -> None:
    source = Path(resolver.__file__).read_text(encoding="utf-8")
    for token in ("MetaTrader5", "OrderSend", "PositionModify", "PositionClose"):
        assert token not in source
