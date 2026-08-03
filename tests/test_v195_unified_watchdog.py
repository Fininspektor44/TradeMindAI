from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts" / "run_v195_unified_watchdog.ps1"
INSTALLER = ROOT / "scripts" / "install_v195_unified_watchdog_task.ps1"
CHECKER = ROOT / "scripts" / "check_v195_unified_watchdog.ps1"

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


def test_runner_checks_complete_ecn_and_bybit_contracts() -> None:
    text = RUNNER.read_text(encoding="utf-8")

    for symbol in EXPECTED_ECN:
        assert symbol in text
    assert '"ecn_manifest.csv"' in text
    assert '"ROBO_ECN"' in text
    assert "$expectedBybitCount = 20" in text
    assert '"data\\bybit_v1_9"' in text
    assert '"data\\watchdog_v1_9_5"' in text
    assert 'trademind\\.bybit_fixed20' in text
    assert "ParentProcessId" in text
    assert "collector_instances" in text
    assert "python_processes" in text
    assert "orders_enabled" in text
    assert "read_only = $true" in text
    assert "ECN M5 streams" in text
    assert "Bybit M5 universe" in text


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
        "Stop-ScheduledTask -TaskName $BybitTaskName",
        "Stop-Process",
    )
    assert not any(token in text for token in forbidden)
    assert "read-only" in text.lower()


def test_installer_runs_hidden_every_five_minutes() -> None:
    text = INSTALLER.read_text(encoding="utf-8")

    assert '"TradeMindAI-v1.9.5-UnifiedWatchdog"' in text
    assert "run_v195_unified_watchdog.ps1" in text
    assert "-WindowStyle Hidden" in text
    assert "-RepetitionInterval" in text
    assert "IntervalMinutes = 5" in text
    assert "-MultipleInstances IgnoreNew" in text
    assert "never sends trading orders" in text


def test_checker_exposes_combined_health_fields() -> None:
    text = CHECKER.read_text(encoding="utf-8")

    assert "SnapshotOverall" in text
    assert "EcnFreshStreams" in text
    assert "BybitSymbols" in text
    assert "BybitCollectorInstances" in text
    assert "BybitPythonProcesses" in text
    assert "BybitOrdersEnabled" in text
    assert "$statusAgeSeconds -le $FreshSeconds" in text
