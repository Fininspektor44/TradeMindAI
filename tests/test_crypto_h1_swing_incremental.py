from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path

from trademind import crypto_h1_swing_incremental as runtime
from trademind.signal_intelligence import EntryOrder, SignalCandidate, TradePlan


def write_decisions(path: Path) -> None:
    rows = [
        {
            "decision_id": f"d{index}",
            "signal_time": datetime(2026, 8, 6, 10 + index, tzinfo=timezone.utc).isoformat(),
            "symbol": "BTCUSDT",
            "action": "BUY",
        }
        for index in range(1, 4)
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)


def candidate_for(row):
    observed = datetime.fromisoformat(str(row["signal_time"]))
    return SignalCandidate(
        observed_at=observed,
        created_at=observed,
        symbol="BTCUSDT",
        timeframe="M5",
        setup_family="CRYPTO_H1_SWING_M5_VOLUME_BREAKOUT",
        scenario="test opportunity",
        plan=TradePlan(
            action="BUY",
            entries=(EntryOrder(100.0, 1.0, "test breakout", "STOP"),),
            stop_price=95.0,
            targets=(110.0,),
            invalidation="test stop",
        ),
        market_features={
            "structure": {"swing_bias": "BULLISH"},
            "volume": {"m5_volume_ratio_20": 1.3},
            "confirmation": {"close_confirmed": True},
        },
        factor_scores={"structure": 1.0, "volume": 0.8, "confirmation": 1.0},
        factor_reasons={
            "structure": ("aligned",),
            "volume": ("confirmed",),
            "confirmation": ("breakout",),
        },
        provenance=("TEST",),
    )


def test_incremental_archives_candidates_and_rejections_without_repeating(
    tmp_path, monkeypatch
) -> None:
    decisions = tmp_path / "decisions.csv"
    bars = tmp_path / "bars.csv"
    output = tmp_path / "output"
    write_decisions(decisions)
    bars.write_text("unused\n", encoding="utf-8")

    class FakeEngine:
        @classmethod
        def from_csv(cls, path):
            return cls()

    class FakeFlow:
        @classmethod
        def from_csv(cls, path):
            return cls()

    def fake_evaluate(row, engine, flow):
        source_id = str(row["decision_id"])
        audit = {"source_decision_id": source_id, "as_of": str(row["signal_time"])}
        if source_id == "d3":
            return candidate_for(row), audit, {}
        rejection = {
            "source_decision_id": source_id,
            "as_of": str(row["signal_time"]),
            "reasons": ["FILTERED"],
        }
        return None, audit, rejection

    monkeypatch.setattr(runtime, "MarketStructureEngine", FakeEngine)
    monkeypatch.setattr(runtime, "FlowHistory", FakeFlow)
    monkeypatch.setattr(runtime, "evaluate_row", fake_evaluate)

    first = runtime.run_incremental(decisions, bars, output, batch_size=2)
    second = runtime.run_incremental(decisions, bars, output, batch_size=2)
    third = runtime.run_incremental(decisions, bars, output, batch_size=2)

    assert first.processed_batch == 2
    assert first.eligible_total == 1
    assert first.rejected_total == 1
    assert first.remaining_decisions == 1
    assert second.processed_batch == 1
    assert second.eligible_total == 1
    assert second.rejected_total == 2
    assert second.remaining_decisions == 0
    assert third.processed_batch == 0
    assert third.eligible_total == 1
    assert third.rejected_total == 2
    assert (output / "outcomes.jsonl").read_text(encoding="utf-8") == ""


def test_incremental_safety_contract() -> None:
    assert runtime.safety_contract() == {
        "read_only": True,
        "orders_enabled": False,
        "publication_enabled": False,
        "exchange_api_called": False,
        "future_bars_used": False,
        "source_files_modified": False,
        "account_sizing_calculated": False,
    }
