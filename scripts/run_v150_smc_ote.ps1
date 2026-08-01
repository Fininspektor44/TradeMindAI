param(
    [ValidateRange(-14, 14)]
    [int]$ServerUtcOffsetHours = 3,
    [string]$Volume = ".\data\volume_v1_4\volume_bars.csv",
    [string]$OutputDir = ".\data\smc_ote_v1_5",
    [switch]$OpenDashboard
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $projectRoot

$signals = Join-Path $OutputDir "signals.csv"
$states = Join-Path $OutputDir "latest.csv"
$dashboard = Join-Path $OutputDir "dashboard\index.html"

& ".\.venv\Scripts\trademind-smc-ote.exe" `
    --volume $Volume `
    --signals $signals `
    --states $states `
    --dashboard $dashboard `
    --server-utc-offset-hours $ServerUtcOffsetHours
$exitCode = $LASTEXITCODE

if ($exitCode -ne 0) {
    throw "TradeMind v1.5 SMC OTE failed with exit code $exitCode"
}
if ($OpenDashboard -and (Test-Path $dashboard)) {
    Start-Process $dashboard
}
exit 0
