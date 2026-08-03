from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts" / "run_v1101_unified_watchdog.ps1"
INSTALLER = ROOT / "scripts" / "install_v1101_unified_watchdog_task.ps1"
CHECKER = ROOT / "scripts" / "check_v1101_unified_watchdog.ps1"

EXPECTED_ECN = (
    "EURUSD",
    "GBPUSD",
    "USDJPY",
    "USDCHF",
    "USDCAD",
    "AUDUSD",
    "NZDUSD",
    "XAUUSD",
    "XAGUSD",
    ".USTECHCash",
    ".US500Cash",
    ".US30Cash",
    "WTI",
    "BRENT",
    "BTCUSD",
    "ETHUSD",
)


def test_runner_checks_ecn_bybit_and_shadow_contracts() -> None:
    text = RUNNER.read_text(encoding="utf-8")

    for symbol in EXPECTED_ECN:
        assert symbol in text
    assert '"ecn_manifest.csv"' in text
    assert '"ROBO_ECN"' in text
    assert "$expectedBybitCount = 20" in text
    assert '"data\\bybit_v1_9"' in text
    assert '"data\\bybit_shadow_v1_10"' in text
    assert '"data\\watchdog_v1_10_1"' in text
    assert 'trademind\\.bybit_fixed20' in text
    assert "ParentProcessId" in text
    assert '"TradeMindAI-v1.10-BybitShadow"' in text
    assert "Bybit Shadow Research" in text
    assert "source_bars" in text
    assert "m15_bars" in text
    assert "h1_bars" in text
    assert "forward_only" in text
    assert "orders_enabled" in text
    assert 'schema_version = "1.10.1"' in text
    assert "read_only = $true" in text


def test_runner_is_diagnostic_only() -> None:
    text = RUNNER.read_text(encoding="utf-8")
    forbidden = (
        "OrderSend",
        "OrderSendAsync",
        "CTrade",
        "trade.Buy",
        "trade.Sell",
        "PositionClose",
        "Start-ScheduledTask -TaskName $BybitTaskName",
        "Start-ScheduledTask -TaskName $ShadowTaskName",
        "Stop-ScheduledTask",
        "Stop-Process",
    )
    assert not any(token in text for token in forbidden)
    assert "read-only" in text.lower()


def test_shadow_health_requires_task_fresh_forward_read_only_data() -> None:
    text = RUNNER.read_text(encoding="utf-8")

    assert '$shadowTask.State -in @("Ready", "Running")' in text
    assert "$shadowTaskInfo.LastTaskResult -eq 0" in text
    assert "$shadowStatusAge -le $ShadowFreshSeconds" in text
    assert "[bool]$shadowStatus.forward_only" in text
    assert "-not [bool]$shadowStatus.orders_enabled" in text
    assert "[int]$shadowStatus.source_bars -gt 0" in text
    assert "[int]$shadowStatus.m15_bars -gt 0" in text
    assert "[int]$shadowStatus.h1_bars -gt 0" in text


def test_installer_replaces_old_watchdog_and_runs_hidden_every_five_minutes() -> None:
    text = INSTALLER.read_text(encoding="utf-8")

    assert '"TradeMindAI-v1.10.1-UnifiedWatchdog"' in text
    assert '"TradeMindAI-v1.9.5-UnifiedWatchdog"' in text
    assert "Unregister-ScheduledTask" in text
    assert "run_v1101_unified_watchdog.ps1" in text
    assert "-WindowStyle Hidden" in text
    assert "-RepetitionInterval" in text
    assert "IntervalMinutes = 5" in text
    assert "-MultipleInstances IgnoreNew" in text
    assert "never sends trading orders" in text
    assert "Bybit Shadow" in text


def test_checker_exposes_all_three_systems() -> None:
    text = CHECKER.read_text(encoding="utf-8")

    assert "SnapshotOverall" in text
    assert "EcnFreshStreams" in text
    assert "BybitSymbols" in text
    assert "BybitCollectorInstances" in text
    assert "BybitPythonProcesses" in text
    assert "BybitOrdersEnabled" in text
    assert "ShadowState" in text
    assert "ShadowTaskState" in text
    assert "ShadowM5M15H1" in text
    assert "ShadowDecisions" in text
    assert "ShadowCandidates" in text
    assert "ShadowForwardSignals" in text
    assert "ShadowCompletedSignals" in text
    assert "ShadowForwardOnly" in text
    assert "ShadowOrdersEnabled" in text
    assert "$statusAgeSeconds -le $FreshSeconds" in text
