from __future__ import annotations

from trademind.stats import _confidence_bucket, _metrics, _non_overlapping


def _row(
    time: str,
    *,
    symbol: str = "XAUUSD",
    action: str = "BUY",
    confidence: str = "70",
    outcome: str = "WIN",
    net: str = "2.0",
    atr: str = "4.0",
) -> dict[str, str]:
    return {
        "signal_time": time,
        "symbol": symbol,
        "timeframe": "M5",
        "action": action,
        "confidence": confidence,
        "outcome_3": outcome,
        "net_move_3": net,
        "mfe_3": "3.0",
        "mae_3": "1.0",
        "atr": atr,
    }


def test_non_overlapping_keeps_one_trade_per_horizon() -> None:
    rows = [
        _row("2026-07-14T20:00:00+00:00"),
        _row("2026-07-14T20:05:00+00:00"),
        _row("2026-07-14T20:10:00+00:00"),
        _row("2026-07-14T20:15:00+00:00"),
    ]

    selected = _non_overlapping(rows, horizon=3)

    assert [row["signal_time"] for row in selected] == [
        "2026-07-14T20:00:00+00:00",
        "2026-07-14T20:15:00+00:00",
    ]


def test_non_overlapping_tracks_symbols_independently() -> None:
    rows = [
        _row("2026-07-14T20:00:00+00:00", symbol="XAUUSD"),
        _row("2026-07-14T20:00:00+00:00", symbol="EURUSD"),
        _row("2026-07-14T20:05:00+00:00", symbol="XAUUSD"),
    ]

    selected = _non_overlapping(rows, horizon=3)

    assert [(row["symbol"], row["signal_time"]) for row in selected] == [
        ("XAUUSD", "2026-07-14T20:00:00+00:00"),
        ("EURUSD", "2026-07-14T20:00:00+00:00"),
    ]


def test_metrics_include_atr_normalized_expectancy() -> None:
    rows = [
        _row("2026-07-14T20:00:00+00:00", outcome="WIN", net="2.0", atr="4.0"),
        _row("2026-07-14T20:05:00+00:00", outcome="LOSS", net="-1.0", atr="2.0"),
    ]

    metrics = _metrics(rows, horizon=3)

    assert metrics["win_rate"] == 50.0
    assert metrics["profit_factor"] == 2.0
    assert metrics["avg_net"] == 0.5
    assert metrics["avg_net_atr"] == 0.0


def test_confidence_buckets() -> None:
    assert _confidence_bucket({"confidence": "45"}) == "35-49"
    assert _confidence_bucket({"confidence": "50"}) == "50-69"
    assert _confidence_bucket({"confidence": "70"}) == "70-89"
    assert _confidence_bucket({"confidence": "90"}) == "90-100"
