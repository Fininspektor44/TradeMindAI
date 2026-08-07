from __future__ import annotations

import json
from pathlib import Path

from trademind import breakeven_runtime as runtime


def test_runtime_orchestrates_existing_shadow_and_counterfactual(
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
        return {
            "state": "OK",
            "completed_baskets": 12,
            "covered_completed_baskets": 4,
            "losses_avoided_count": 1,
            "winners_cut_count": 2,
            "triggered_without_revisit_count": 1,
            "net_effect_proxy_money": -7.5,
            "unmapped_shadow_epochs": 2,
            "ambiguous_shadow_epochs": 0,
        }

    monkeypatch.setattr(runtime, "run_monitor", fake_monitor)
    monkeypatch.setattr(runtime, "run_counterfactual", fake_counterfactual)

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
    assert status["safety"]["read_only"] is True
    assert status["safety"]["orders_enabled"] is False
    assert calls[0] == ("shadow", positions, shadow_dir)
    assert calls[1][-1] == "37365712"
    assert json.loads(status_path.read_text(encoding="utf-8"))["schema_version"] == "1.30.0"


def test_runtime_propagates_mapping_warning(tmp_path: Path, monkeypatch) -> None:
    positions = tmp_path / "positions.csv"
    deals = tmp_path / "deals.csv"
    positions.write_text("x", encoding="utf-8")
    deals.write_text("x", encoding="utf-8")

    monkeypatch.setattr(
        runtime,
        "run_monitor",
        lambda *_: {"state": "OK"},
    )
    monkeypatch.setattr(
        runtime,
        "run_counterfactual",
        lambda *_, **__: {"state": "WARN_AMBIGUOUS_MAPPING"},
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
