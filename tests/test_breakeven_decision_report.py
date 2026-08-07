from __future__ import annotations

import csv
from pathlib import Path

from trademind import breakeven_decision_report as report


def rows() -> list[dict[str, object]]:
    return [
        {
            "basket_closed_at": "2026-08-07T12:00:00+00:00",
            "symbol": "EURUSD",
            "side": "BUY",
            "mapped_shadow_epochs": "1",
            "effect_class": "WINNER_CUT_BY_BE",
            "actual_net_profit": "50",
            "net_effect_proxy_money": "-50",
        },
        {
            "basket_closed_at": "2026-08-07T13:00:00+00:00",
            "symbol": "USDJPY",
            "side": "SELL",
            "mapped_shadow_epochs": "1",
            "effect_class": "LOSS_AVOIDED_BY_BE",
            "actual_net_profit": "-30",
            "net_effect_proxy_money": "30",
        },
    ]


def test_summary_stays_collecting_below_sample_threshold() -> None:
    runtime = {"login": "37365712", "updated_at": "2026-08-07T15:00:00+00:00"}
    counter = {
        "completed_baskets": 10,
        "covered_completed_baskets": 9,
        "affected_by_shadow_be_baskets": 2,
        "losses_avoided_count": 1,
        "winners_cut_count": 1,
        "loss_avoided_proxy_money": 30,
        "opportunity_cost_proxy_money": 50,
        "net_effect_proxy_money": -20,
    }
    summary = report.build_summary(runtime, counter, rows())
    assert summary["review_state"] == "COLLECTING_EVIDENCE"
    assert summary["sample"]["coverage_ratio"] == 0.9
    assert summary["effect"]["net_effect_proxy_money"] == -20.0
    assert summary["classes"] == {"LOSS_AVOIDED_BY_BE": 1, "WINNER_CUT_BY_BE": 1}


def test_summary_becomes_ready_only_with_coverage_and_sample() -> None:
    runtime = {"login": "37365712"}
    counter = {
        "completed_baskets": 40,
        "covered_completed_baskets": 34,
        "affected_by_shadow_be_baskets": 30,
    }
    summary = report.build_summary(runtime, counter, [])
    assert summary["review_state"] == "READY_FOR_HUMAN_REVIEW"


def test_generate_report_writes_json_and_html(tmp_path: Path) -> None:
    csv_path = tmp_path / "basket_be_counterfactual.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows()[0]))
        writer.writeheader()
        writer.writerows(rows())
    out = tmp_path / "report"
    runtime = {"login": "37365712"}
    counter = {
        "completed_baskets": 2,
        "covered_completed_baskets": 2,
        "affected_by_shadow_be_baskets": 2,
        "losses_avoided_count": 1,
        "winners_cut_count": 1,
        "loss_avoided_proxy_money": 30,
        "opportunity_cost_proxy_money": 50,
        "net_effect_proxy_money": -20,
    }
    summary = report.generate_report(runtime, counter, csv_path, out)
    assert summary["state"] == "OK"
    assert (out / "summary.json").is_file()
    html = (out / "index.html").read_text(encoding="utf-8")
    assert "TradeMind v1.31" in html
    assert "READ-ONLY" in html
    assert "EURUSD" in html


def test_source_has_no_execution_tokens() -> None:
    source = Path(report.__file__).read_text(encoding="utf-8")
    for token in ("MetaTrader5", "OrderSend", "PositionModify", "PositionClose"):
        assert token not in source
