from __future__ import annotations

import csv
from pathlib import Path

from trademind.fx_dashboard import collect_snapshot, render_dashboard, write_dashboard


def _write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _observation(symbol: str, action: str, outcome: str) -> dict[str, str]:
    return {
        "signal_time": "2026-07-31T12:00:00+00:00",
        "symbol": symbol,
        "action": action,
        "outcome_12": outcome,
        "rvol_20": "1.25",
        "tick_rate_per_sec": "2.5",
        "spread_mean_points": "2.2",
    }


def _state(status: str, label: str, avg: str, reasons: str) -> dict[str, str]:
    return {
        "symbol": "EURUSD",
        "label": label,
        "session": "LONDON",
        "action": "BUY",
        "horizon": "6",
        "observations": "45",
        "trades": "40",
        "trading_days": "12",
        "status": status,
        "win_rate": "62.5",
        "profit_factor_atr": "1.7",
        "avg_net_atr": avg,
        "early_avg_net_atr": "0.08",
        "late_avg_net_atr": "0.11",
        "max_drawdown_atr": "4.2",
        "max_loss_streak": "4",
        "q_value": "0.08",
        "reasons": reasons,
    }


def test_dashboard_collects_pair_and_status_summaries(tmp_path: Path) -> None:
    observations = tmp_path / "observations.csv"
    states = tmp_path / "latest.csv"
    _write_csv(
        observations,
        list(_observation("EURUSD", "BUY", "WIN")),
        [
            _observation("EURUSD", "BUY", "WIN"),
            _observation("EURUSD", "SELL", "LOSS"),
        ],
    )
    rows = [
        _state("RESEARCH_CANDIDATE", "HIGH_RVOL", "0.10", "stable halves"),
        _state("UNSTABLE", "SPREAD_EXPANDING", "-0.05", "negative late half"),
    ]
    _write_csv(states, list(rows[0]), rows)

    snapshot = collect_snapshot(observations, states)

    assert len(snapshot.observations) == 2
    assert snapshot.status_counts == {"RESEARCH_CANDIDATE": 1, "UNSTABLE": 1}
    assert snapshot.pairs[0].symbol == "EURUSD"
    assert snapshot.pairs[0].completed_h12 == 2
    assert snapshot.pairs[0].buy_count == 1
    assert snapshot.pairs[0].sell_count == 1


def test_dashboard_is_standalone_and_escapes_source_text(tmp_path: Path) -> None:
    observations = tmp_path / "observations.csv"
    states = tmp_path / "latest.csv"
    _write_csv(
        observations,
        list(_observation("EURUSD", "BUY", "WIN")),
        [_observation("EURUSD", "BUY", "WIN")],
    )
    dangerous = '<script>alert("x")</script>'
    state = _state("RESEARCH_CANDIDATE", "HIGH_RVOL", "0.10", dangerous)
    _write_csv(states, list(state), [state])

    snapshot = collect_snapshot(observations, states)
    markup = render_dashboard(snapshot)
    output = write_dashboard(snapshot, tmp_path / "dashboard" / "index.html")

    assert output.is_file()
    assert "TradeMind AI v1.4.3 FX Research Dashboard" in markup
    assert dangerous not in markup
    assert "&lt;script&gt;" in markup
    assert "https://" not in markup
    assert "<table>" in markup


def test_fx_dashboard_contract_is_read_only() -> None:
    source = Path("src/trademind/fx_dashboard.py").read_text(encoding="utf-8")
    runner = Path("scripts/run_v142_fx_research.ps1").read_text(encoding="utf-8")
    text = source + runner
    forbidden = (
        "CTrade",
        "OrderSend(",
        ".Buy(",
        ".Sell(",
        "PositionClose(",
        "TRADE_ACTION_DEAL",
    )
    assert all(token not in text for token in forbidden)
    assert "trademind-fx-dashboard.exe" in runner
