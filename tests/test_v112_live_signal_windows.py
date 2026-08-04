from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts" / "run_v112_live_signal_console.ps1"
INSTALLER = ROOT / "scripts" / "install_v112_live_signal_console_task.ps1"
CHECKER = ROOT / "scripts" / "check_v112_live_signal_console.ps1"
PYPROJECT = ROOT / "pyproject.toml"


def test_v112_cli_and_runner_use_existing_mt5_and_bybit_journals() -> None:
    runner = RUNNER.read_text(encoding="utf-8")
    pyproject = PYPROJECT.read_text(encoding="utf-8")

    assert 'version = "1.12.0"' in pyproject
    assert 'trademind-live-signals = "trademind.live_signal_server:main"' in pyproject
    assert ".venv\\Scripts\\trademind-live-signals.exe" in runner
    assert "unified_signal_center_v1_6\\signals.csv" in runner
    assert "bybit_shadow_v1_11\\control\\signals.csv" in runner
    assert "bybit_shadow_v1_11\\buy_only\\signals.csv" in runner
    assert "bybit_shadow_v1_11\\strict_sell\\signals.csv" in runner
    assert "OrdersEnabled=False" in runner


def test_installer_starts_hidden_single_instance_task_at_logon() -> None:
    text = INSTALLER.read_text(encoding="utf-8")

    assert '"TradeMindAI-v1.12-LiveSignalConsole"' in text
    assert "New-ScheduledTaskTrigger -AtLogOn" in text
    assert "-WindowStyle Hidden" in text
    assert "-MultipleInstances IgnoreNew" in text
    assert "-ExecutionTimeLimit ([TimeSpan]::Zero)" in text
    assert "Read-only. OrdersEnabled=False." in text


def test_checker_verifies_page_api_task_and_read_only_contract() -> None:
    text = CHECKER.read_text(encoding="utf-8")

    assert "/api/health" in text
    assert "TradeMind Live Signal Console" in text
    assert "read_only" in text
    assert "orders_enabled" in text
    assert "Get-ScheduledTask" in text
    assert "StaleSignals" in text
    assert "Start-Process $baseUrl" in text


def test_windows_scripts_do_not_contain_trading_functions() -> None:
    text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (RUNNER, INSTALLER, CHECKER)
    )
    forbidden = (
        "OrderSend",
        "OrderSendAsync",
        "CTrade",
        "trade.Buy",
        "trade.Sell",
        "PositionClose",
        "place_order",
        "create_order",
    )
    assert not any(token in text for token in forbidden)
