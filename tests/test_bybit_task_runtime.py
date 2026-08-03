from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "scripts" / "install_v190_bybit_task.ps1"
CHECKER = ROOT / "scripts" / "check_v190_bybit.ps1"


def test_installer_runs_python_directly() -> None:
    text = INSTALLER.read_text(encoding="utf-8")

    assert 'Join-Path $projectRoot ".venv\\Scripts\\python.exe"' in text
    assert "-Execute $python" in text
    assert "-m trademind.bybit_fixed20" in text
    assert "-ExecutionTimeLimit (New-TimeSpan -Seconds 0)" in text
    assert "-RestartCount 999" in text
    assert '-Execute "powershell.exe"' not in text
    assert "run_v190_bybit.ps1" not in text


def test_installer_keeps_fixed_20_universe() -> None:
    text = INSTALLER.read_text(encoding="utf-8")
    symbols = (
        "BTCUSDT,ETHUSDT,UNIUSDT,JTOUSDT,SOLUSDT,BZUSDT,NEARUSDT,AKEUSDT,"
        "ONDOUSDT,POPCATUSDT,XMRUSDT,MYXUSDT,AAVEUSDT,ZECUSDT,HYPEUSDT,"
        "LDOUSDT,PUMPFUNUSDT,GRASSUSDT,XAUTUSDT,1000PEPEUSDT"
    )

    assert symbols in text
    assert "No API key, account access or order function is used." in text


def test_checker_requires_live_process_and_fresh_status() -> None:
    text = CHECKER.read_text(encoding="utf-8")

    assert "Get-CimInstance Win32_Process" in text
    assert 'trademind\\.bybit_fixed20' in text
    assert "$statusAgeSeconds -le $FreshSeconds" in text
    assert '$task.State -eq "Running"' in text
    assert "$processes.Count -ge 1" in text
    assert "OrdersEnabled" in text
