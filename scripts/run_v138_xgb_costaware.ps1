param(
    [switch]$Refresh
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    throw "Python venv not found: $Python"
}

$env:PYTHONPATH = Join-Path $ProjectRoot "src"

Write-Host "TradeMind v1.38 XGBoost cost-aware reproduction" -ForegroundColor Cyan
Write-Host "READ-ONLY. No orders. Public Binance archive only." -ForegroundColor Green

# Windows PowerShell 5.1 turns native-process stderr into ErrorRecords.  With
# $ErrorActionPreference='Stop', an expected failed import probe can terminate
# the script before we get a chance to install the missing packages.  Probe
# under Continue, capture the real process exit code, then restore Stop.
$SavedErrorActionPreference = $ErrorActionPreference
$ErrorActionPreference = "Continue"
& $Python -c "import numpy,pandas,xgboost" *> $null
$DepsExitCode = $LASTEXITCODE
$ErrorActionPreference = $SavedErrorActionPreference

if ($DepsExitCode -ne 0) {
    Write-Host "Installing research dependencies: numpy pandas xgboost" -ForegroundColor Yellow
    & $Python -m pip install "numpy>=1.26,<3" "pandas>=2.1,<3" "xgboost>=2.1,<4"
    if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed" }

    # Verify the actual imports after installation and fail with the real
    # traceback if the environment is still incompatible.
    & $Python -c "import numpy,pandas,xgboost; print('deps OK:', numpy.__version__, pandas.__version__, xgboost.__version__)"
    if ($LASTEXITCODE -ne 0) { throw "Research dependencies are installed but cannot be imported" }
}

$Arguments = @(
    "-m", "trademind.xgb_costaware_v138",
    "--output-dir", (Join-Path $ProjectRoot "data\xgb_costaware_v138"),
    "--start", "2017-12-01T00:00:00+00:00",
    "--end", "2026-08-01T00:00:00+00:00",
    "--lambda-value", "2.0"
)
if ($Refresh) { $Arguments += "--refresh" }

& $Python @Arguments
exit $LASTEXITCODE
