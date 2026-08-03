param(
    [string]$UnifiedDir = ".\data\unified_signal_center_v1_6",
    [string]$OutputDir = ".\data\paper_signal_gate_v1_8",
    [switch]$OpenDashboard
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $projectRoot

$unifiedSignals = Join-Path $UnifiedDir "signals.csv"
$unifiedStates = Join-Path $UnifiedDir "latest.csv"
$dashboard = Join-Path $OutputDir "dashboard\index.html"

& ".\.venv\Scripts\trademind-paper-gate-v18.exe" `
    --unified-signals $unifiedSignals `
    --unified-states $unifiedStates `
    --output-dir $OutputDir
$exitCode = $LASTEXITCODE

if ($OpenDashboard -and (Test-Path $dashboard)) {
    Start-Process $dashboard
}
exit $exitCode
