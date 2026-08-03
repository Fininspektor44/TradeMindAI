param(
    [switch]$OpenDashboard
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
$bars = Join-Path $projectRoot "data\bybit_v1_9\bybit_bars.csv"
$outputDir = Join-Path $projectRoot "data\bybit_shadow_v1_10"
$dashboard = Join-Path $outputDir "dashboard\index.html"

if (-not (Test-Path $python)) { throw "Python environment not found: $python" }
if (-not (Test-Path $bars)) { throw "Bybit M5 source not found: $bars" }

& $python -m trademind.bybit_shadow --bars $bars --output-dir $outputDir
if ($LASTEXITCODE -ne 0) { throw "Bybit Shadow Research failed with exit code $LASTEXITCODE" }

if ($OpenDashboard -and (Test-Path $dashboard)) {
    Start-Process $dashboard
}
