from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts" / "run_v1102_unified_watchdog.ps1"
CHECKER = ROOT / "scripts" / "check_v1102_unified_watchdog.ps1"
INSTALLER = ROOT / "scripts" / "install_v1102_unified_watchdog_task.ps1"


def test_reconnect_grace_requires_all_independent_health_evidence() -> None:
    text = RUNNER.read_text(encoding="utf-8")

    assert '$status.schema_version = "1.10.2"' in text
    assert '$bybitState -eq "RECONNECTING"' in text
    assert "$failedChecks.Count -eq 1" in text
    assert '[string]$failedChecks[0].name -eq "Bybit status"' in text
    assert "$bybitStatusAge -le $BybitFreshSeconds" in text
    assert "$lastEventAge -le $ReconnectGraceSeconds" in text
    assert '$bybitTask.State -eq "Running"' in text
    assert "$collectorRoots.Count -eq 1" in text
    assert "$processes.Count -ge 1" in text
    assert "$uniqueSymbols.Count -eq $expectedBybitCount" in text
    assert "-not [bool]$rawBybit.orders_enabled" in text
    assert "$status.overall = \"OK\"" in text


def test_reconnect_grace_does_not_restart_or_trade() -> None:
    text = RUNNER.read_text(encoding="utf-8")
    forbidden = (
        "OrderSend",
        "OrderSendAsync",
        "CTrade",
        "trade.Buy",
        "trade.Sell",
        "PositionClose",
        "Start-ScheduledTask -TaskName $BybitTaskName",
        "Stop-ScheduledTask",
        "Stop-Process",
    )
    assert not any(token in text for token in forbidden)
    assert "orders_enabled" in text
    assert "read-only" in text.lower()


def test_checker_exposes_reconnect_state_and_market_event_age() -> None:
    text = CHECKER.read_text(encoding="utf-8")

    assert "BybitState" in text
    assert "BybitReconnectGrace" in text
    assert "BybitLastEventAgeSeconds" in text
    assert "ShadowForwardSignals" in text
    assert "ShadowCompletedSignals" in text
    assert "$statusAgeSeconds -le $FreshSeconds" in text


def test_installer_replaces_v1101_task_and_keeps_five_minute_schedule() -> None:
    text = INSTALLER.read_text(encoding="utf-8")

    assert '"TradeMindAI-v1.10.2-UnifiedWatchdog"' in text
    assert '"TradeMindAI-v1.10.1-UnifiedWatchdog"' in text
    assert "Unregister-ScheduledTask" in text
    assert "run_v1102_unified_watchdog.ps1" in text
    assert "IntervalMinutes = 5" in text
    assert "-WindowStyle Hidden" in text
    assert "-MultipleInstances IgnoreNew" in text
    assert "No trading orders" in text
