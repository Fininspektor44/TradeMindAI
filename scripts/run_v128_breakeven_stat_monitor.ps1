param(
    [Parameter(Mandatory=$true)]
    [string]$Login,

    [Parameter(Mandatory=$false)]
    [string]$CommonFilesRoot = "",

    [Parameter(Mandatory=$false)]
    [string]$OutputDir = "",

    [switch]$RunTests
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo

$python = Join-Path $repo ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    throw "Python environment not found: $python"
}

if ([string]::IsNullOrWhiteSpace($CommonFilesRoot)) {
    $CommonFilesRoot = Join-Path $env:APPDATA "MetaQuotes\Terminal\Common\Files\TradeMindAI"
}
if ([string]::IsNullOrWhiteSpace($OutputDir)) {
    $OutputDir = Join-Path $repo "data\breakeven_stat_monitor_v1\$Login"
}

$positionsCsv = Join-Path $CommonFilesRoot "mt5_risk_positions_utc_$Login.csv"
if (-not (Test-Path $positionsCsv)) {
    throw "Required read-only MT5 position snapshot not found: $positionsCsv"
}

if ($RunTests) {
    & $python -m pytest -q ".\tests\test_breakeven_stat_monitor.py"
    if ($LASTEXITCODE -ne 0) {
        throw "BreakEven statistics monitor tests failed"
    }
}

& $python -m trademind.breakeven_stat_monitor `
    --positions-csv $positionsCsv `
    --output-dir $OutputDir
if ($LASTEXITCODE -ne 0) {
    throw "BreakEven statistics monitor execution failed"
}

Write-Host "`nBreakEven statistics: $OutputDir" -ForegroundColor Cyan
Write-Host "1R shadow trigger. READ-ONLY. Orders OFF. No position modification." -ForegroundColor Green
