from __future__ import annotations

import json
from pathlib import Path

from trademind import breakeven_runtime as runtime


def test_runtime_orchestrates_shadow_counterfactual_and_report(
    tmp_path: Path,
    monkeypatch,
) -> None:
    positions = tmp_path / "positions.csv"
    deals = tmp_path / "deals.csv"
    positions.write_text("positions", encoding="utf-8")
    deals.write_text("deals", encoding="utf-8")
    shadow_dir = tmp_path / "shadow"
    counter_dir = tmp_path / "counter"
    status_path = tmp_path / "runtime" / "status.json"

    calls: list[tuple] = []

    def fake_monitor(path: Path, output: Path) -> dict:
        calls.append(("shadow", path, output))
        output.mkdir(parents=True, exist_ok=True)
        (output / "state.json").write_text('{"epochs": {}}', encoding="utf-8")
        return {
            "state": "OK",
            "trackable_basket_epochs": 6,
            "open_trackable_epochs": 5,
            "be_triggered_epochs": 2,
            "be_revisited_after_trigger_epochs": 1,
        }

    def fake_counterfactual(
        state: Path,
        path: Path,
        output: Path,
        *,
        login: str,
    ) -> dict:
        calls.append(("counterfactual", state, path, output, login))
        output.mkdir(parents=True, exist_ok=True)
        (output / "basket_be_counterfactual.csv").write_text("effect_class\n", encoding="utf-8")
        return {
            "state": "OK",
            "completed_baskets": 12,
            "covered_completed_baskets": 4,
            "affected_by_shadow_be_baskets": 1,
            "losses_avoided_count": 1,
            "winners_cut_count": 2,
            "triggered_without_revisit_count": 1,
            "loss_avoided_proxy_money": 10.0,
            "opportunity_cost_proxy_money": 17.5,
            "net_effect_proxy_money": -7.5,
            "unmapped_shadow_epochs": 2,
            "ambiguous_shadow_epochs": 0,
        }

    def fake_report(runtime_status, counter_status, counter_csv: Path, output: Path) -> dict:
        calls.append(("report", counter_csv, output))
        output.mkdir(parents=True, exist_ok=True)
        (output / "index.html").write_text("report", encoding="utf-8")
        (output / "summary.json").write_text("{}", encoding="utf-8")
        return {
            "review_state": "COLLECTING_EVIDENCE",
            "sample": {"coverage_ratio": 1 / 3, "affected_by_shadow_be_baskets": 1},
        }

    monkeypatch.setattr(runtime, "run_monitor", fake_monitor)
    monkeypatch.setattr(runtime, "run_counterfactual", fake_counterfactual)
    monkeypatch.setattr(runtime, "generate_report", fake_report)

    status = runtime.run_runtime(
        positions,
        deals,
        shadow_dir,
        counter_dir,
        status_path,
        login="37365712",
    )

    assert status["state"] == "OK"
    assert status["shadow"]["be_triggered_epochs"] == 2
    assert status["counterfactual"]["covered_completed_baskets"] == 4
    assert status["counterfactual"]["net_effect_proxy_money"] == -7.5
    assert status["report"]["review_state"] == "COLLECTING_EVIDENCE"
    assert status["safety"]["read_only"] is True
    assert status["safety"]["orders_enabled"] is False
    assert calls[0] == ("shadow", positions, shadow_dir)
    assert calls[1][-1] == "37365712"
    assert calls[2][0] == "report"
    assert json.loads(status_path.read_text(encoding="utf-8"))["schema_version"] == "1.31.0"


def test_runtime_propagates_mapping_warning(tmp_path: Path, monkeypatch) -> None:
    positions = tmp_path / "positions.csv"
    deals = tmp_path / "deals.csv"
    positions.write_text("x", encoding="utf-8")
    deals.write_text("x", encoding="utf-8")

    monkeypatch.setattr(runtime, "run_monitor", lambda *_: {"state": "OK"})

    def fake_counter(*_, **__) -> dict:
        counter_dir = _[2]
        counter_dir.mkdir(parents=True, exist_ok=True)
        (counter_dir / "basket_be_counterfactual.csv").write_text(
            "effect_class\n", encoding="utf-8"
        )
        return {"state": "WARN_AMBIGUOUS_MAPPING"}

    monkeypatch.setattr(runtime, "run_counterfactual", fake_counter)
    monkeypatch.setattr(
        runtime,
        "generate_report",
        lambda *_, **__: {
            "review_state": "COLLECTING_EVIDENCE",
            "sample": {"coverage_ratio": 0.0, "affected_by_shadow_be_baskets": 0},
        },
    )

    status = runtime.run_runtime(
        positions,
        deals,
        tmp_path / "shadow",
        tmp_path / "counter",
        tmp_path / "status.json",
        login="37365712",
    )
    assert status["state"] == "WARN_AMBIGUOUS_MAPPING"


def test_error_status_remains_read_only(tmp_path: Path) -> None:
    path = tmp_path / "status.json"
    runtime._write_error_status(path, "37365712", ValueError("missing snapshot"))
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["state"] == "ERROR"
    assert payload["error_type"] == "ValueError"
    assert payload["safety"]["read_only"] is True
    assert payload["safety"]["orders_enabled"] is False


def test_source_contains_no_execution_api() -> None:
    source = Path(runtime.__file__).read_text(encoding="utf-8")
    for token in ("MetaTrader5", "OrderSend", "PositionModify", "PositionClose"):
        assert token not in source
