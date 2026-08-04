from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "scripts" / "report_v110_bybit_shadow_performance.ps1"
CHECKER = ROOT / "scripts" / "check_v1101_unified_watchdog.ps1"


def test_report_exposes_forward_profitability_metrics_without_orders() -> None:
    text = REPORT.read_text(encoding="utf-8")

    assert "signals.csv" in text
    assert "ForwardSignals" in text
    assert "Completed" in text
    assert "Open" in text
    assert "Wins" in text
    assert "Losses" in text
    assert "Timeouts" in text
    assert "WinRatePercent" in text
    assert "TotalR" in text
    assert "AverageR" in text
    assert "ProfitFactor" in text
    assert "MaxDrawdownR" in text
    assert "Results by symbol and direction" in text
    assert "R is a normalized risk unit, not dollars" in text
    assert "OrdersEnabled = $false" in text
    for forbidden in (
        "OrderSend",
        "CTrade",
        "place_order",
        "create_order",
        "Start-ScheduledTask",
        "Stop-Process",
    ):
        assert forbidden not in text


def test_checker_does_not_turn_fresh_ok_snapshot_into_warn_from_old_task_result() -> None:
    text = CHECKER.read_text(encoding="utf-8")

    assert "$currentSnapshotHealthy" in text
    assert "$scheduledTaskHealthy" in text
    assert 'Overall = if ($currentSnapshotHealthy) { "OK" } else { "WARN" }' in text
    assert "ScheduledTaskHealth" in text
    assert "previous scheduled watchdog run" in text
    healthy_block = text.split("$currentSnapshotHealthy = (", 1)[1].split(")\n$taskRegisteredAndReady", 1)[0]
    assert "LastTaskResult" not in healthy_block
