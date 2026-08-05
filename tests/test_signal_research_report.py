from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from trademind.signal_evidence import OutcomeObservation, similarity_key
from trademind.signal_intelligence import EntryOrder, SignalCandidate, TradePlan
from trademind.signal_research_report import build_report_rows, run_report


NOW = datetime(2026, 8, 5, 13, 30, tzinfo=timezone.utc)


def _candidate(index: int = 0) -> SignalCandidate:
    return SignalCandidate(
        observed_at=NOW + timedelta(minutes=index),
        created_at=NOW + timedelta(minutes=index),
        symbol="EURUSD",
        timeframe="M5",
        setup_family="SMC_OTE_CONTINUATION",
        scenario="report test",
        plan=TradePlan(
            action="BUY",
            entries=(EntryOrder(1.10, 1.0, "OTE", "MARKET"),),
            stop_price=1.095,
            targets=(1.108,),
            invalidation="protected low broken",
        ),
        market_features={
            "structure": {"swing_bias": "BULLISH", "internal_bias": "BULLISH"},
            "liquidity": {"ssl_sweep": True},
            "fibonacci": {"retracement": 0.68},
            "volume": {"rvol_20": 1.5},
            "momentum": {"impulse_atr": 1.3},
            "volatility": {"atr_percentile": 55, "spread_cost_atr": 0.04},
            "confirmation": {"fvg": "BULLISH"},
            "session": {"name": "LONDON"},
        },
        factor_scores={
            "structure": 0.92,
            "liquidity": 0.90,
            "fibonacci": 0.88,
            "volume": 0.84,
            "momentum": 0.82,
            "volatility": 0.80,
            "confirmation": 0.90,
            "session": 0.90,
            "execution": 0.82,
            "portfolio": 0.60,
        },
        factor_reasons={"structure": ("aligned",)},
        provenance=("TEST",),
    )


def _outcomes(candidate: SignalCandidate, count: int = 43) -> list[OutcomeObservation]:
    key = similarity_key(candidate)
    rows: list[OutcomeObservation] = []
    for index in range(count):
        win = index < 34
        rows.append(
            OutcomeObservation(
                signal_id=f"S{index}",
                setup_key=key,
                completed_at=NOW + timedelta(minutes=index),
                outcome="WIN" if win else "LOSS",
                net_r=1.6 if win else -0.9,
            )
        )
    return rows


def test_report_marks_supported_setup_eligible_for_candidate_gate() -> None:
    candidate = _candidate()
    rows = build_report_rows([candidate], _outcomes(candidate), captured_at=NOW)

    assert len(rows) == 1
    row = rows[0]
    assert row["completed"] == 43
    assert row["wilson_lower_95"] > 0.60
    assert row["profit_factor_r"] > 1.20
    assert row["expected_value_r"] > 0
    assert row["research_status"] == "ELIGIBLE_FOR_CANDIDATE_GATE"


def test_report_keeps_small_sample_in_shadow() -> None:
    candidate = _candidate()
    rows = build_report_rows(
        [candidate],
        _outcomes(candidate, count=12),
        captured_at=NOW,
    )

    assert rows[0]["research_status"] == "SHADOW_INSUFFICIENT_SAMPLE"


def test_run_report_writes_csv_json_and_dashboard(tmp_path: Path) -> None:
    candidate = _candidate()
    candidates_path = tmp_path / "candidates.jsonl"
    outcomes_path = tmp_path / "outcomes.jsonl"
    output_dir = tmp_path / "report"
    candidates_path.write_text(
        __import__("json").dumps(candidate.as_dict(), ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    outcomes_path.write_text(
        "\n".join(
            __import__("json").dumps(row.as_dict(), ensure_ascii=False)
            for row in _outcomes(candidate)
        )
        + "\n",
        encoding="utf-8",
    )

    status = run_report(candidates_path, outcomes_path, output_dir, now=NOW)

    assert status["state"] == "OK"
    assert status["eligible_groups"] == 1
    assert (output_dir / "setup_evidence.csv").is_file()
    assert (output_dir / "status.json").is_file()
    dashboard = output_dir / "dashboard" / "index.html"
    assert dashboard.is_file()
    assert "ELIGIBLE_FOR_CANDIDATE_GATE" in dashboard.read_text(encoding="utf-8")
