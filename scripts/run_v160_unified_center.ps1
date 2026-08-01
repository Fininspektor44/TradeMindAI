param(
    [string]$FxObservations = ".\data\fx_research_v1_4_2\observations.csv",
    [string]$OteSignals = ".\data\smc_ote_v1_5\signals.csv",
    [string]$OutputDir = ".\data\unified_signal_center_v1_6",
    [switch]$OpenDashboard
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $projectRoot

$signals = Join-Path $OutputDir "signals.csv"
$states = Join-Path $OutputDir "latest.csv"
$dashboard = Join-Path $OutputDir "dashboard\index.html"

& ".\.venv\Scripts\trademind-unified-center.exe" `
    --fx-observations $FxObservations `
    --ote-signals $OteSignals `
    --signals $signals `
    --states $states `
    --dashboard $dashboard
$exitCode = $LASTEXITCODE

if ($exitCode -ne 0) {
    throw "TradeMind v1.6 Unified Signal Center failed with exit code $exitCode"
}
if ($OpenDashboard -and (Test-Path $dashboard)) {
    Start-Process $dashboard
}
exit 0
