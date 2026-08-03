from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "scripts" / "install_v190_bybit_task.ps1"
CHECKER = ROOT / "scripts" / "check_v190_bybit.ps1"


def test_installer_runs_hidden_python_directly() -> None:
    text = INSTALLER.read_text(encoding="utf-8")

    assert 'Join-Path $projectRoot ".venv\\Scripts\\pythonw.exe"' in text
    assert "-Execute $pythonw" in text
    assert "-m trademind.bybit_fixed20" in text
    assert "-Hidden" in text
    assert "-ExecutionTimeLimit (New-TimeSpan -Seconds 0)" in text
    assert "-RestartCount 999" in text
    assert '-Execute "powershell.exe"' not in text
    assert 'Join-Path $projectRoot ".venv\\Scripts\\python.exe"' not in text
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
    assert "Task started in background." in text


def test_checker_counts_one_parent_child_python_chain_as_one_instance() -> None:
    text = CHECKER.read_text(encoding="utf-8")

    assert "Get-CimInstance Win32_Process" in text
    assert '$_.Name -in @("python.exe", "pythonw.exe")' in text
    assert 'trademind\\.bybit_fixed20' in text
    assert "$matchingPids" in text
    assert "ParentProcessId" in text
    assert "$collectorRoots" in text
    assert "$collectorInstances = $collectorRoots.Count" in text
    assert "$collectorInstances -eq 1" in text
    assert "$processes.Count -ge 1" in text
    assert "CollectorInstances" in text
    assert "PythonProcessCount" in text
    assert "Duplicate Bybit collector chains detected" in text
    assert "$statusAgeSeconds -le $FreshSeconds" in text
    assert '$task.State -eq "Running"' in text
    assert "OrdersEnabled" in text
    assert "-not [bool]$status.orders_enabled" in text
