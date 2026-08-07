param(
    [Parameter(Mandatory=$true)]
    [string]$Login,

    [Parameter(Mandatory=$false)]
    [string]$CommonFilesRoot = "",

    [Parameter(Mandatory=$false)]
    [string]$ShadowOutputDir = "",

    [Parameter(Mandatory=$false)]
    [string]$CounterfactualOutputDir = "",

    [Parameter(Mandatory=$false)]
    [string]$RuntimeStatus = "",

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
if ([string]::IsNullOrWhiteSpace($ShadowOutputDir)) {
    $ShadowOutputDir = Join-Path $repo "data\breakeven_stat_monitor_v1\$Login"
}
if ([string]::IsNullOrWhiteSpace($CounterfactualOutputDir)) {
    $CounterfactualOutputDir = Join-Path $repo "data\breakeven_counterfactual_v1\$Login"
}
if ([string]::IsNullOrWhiteSpace($RuntimeStatus)) {
    $RuntimeStatus = Join-Path $repo "data\breakeven_runtime_v1\$Login\status.json"
}

$positionsCsv = Join-Path $CommonFilesRoot "mt5_risk_positions_utc_$Login.csv"
$dealsCsv = Join-Path $CommonFilesRoot "grid_deals_$Login.csv"

if (-not (Test-Path $positionsCsv -PathType Leaf)) {
    throw "Read-only positions CSV not found: $positionsCsv"
}
if (-not (Test-Path $dealsCsv -PathType Leaf)) {
    throw "Read-only deals CSV not found: $dealsCsv"
}

if ($RunTests) {
    & $python -m pytest -q `
        ".\tests\test_breakeven_stat_monitor.py" `
        ".\tests\test_breakeven_counterfactual.py" `
        ".\tests\test_breakeven_decision_report.py" `
        ".\tests\test_breakeven_runtime.py"
    if ($LASTEXITCODE -ne 0) {
        throw "v1.31 BreakEven runtime tests failed"
    }
}

& $python -m trademind.breakeven_runtime `
    --login $Login `
    --positions-csv $positionsCsv `
    --deals-csv $dealsCsv `
    --shadow-output-dir $ShadowOutputDir `
    --counterfactual-output-dir $CounterfactualOutputDir `
    --status $RuntimeStatus
if ($LASTEXITCODE -ne 0) {
    throw "v1.31 BreakEven runtime execution failed. Inspect: $RuntimeStatus"
}

$status = Get-Content $RuntimeStatus -Raw | ConvertFrom-Json
Write-Host "`nBreakEven runtime status: $RuntimeStatus" -ForegroundColor Cyan
Write-Host "Decision report: $($status.report.index)" -ForegroundColor Cyan
Write-Host "READ-ONLY. Shadow statistics only. Orders OFF. Robot/exporter unchanged." `
    -ForegroundColor Green
