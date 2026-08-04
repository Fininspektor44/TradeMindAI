param(
    [string]$ProjectRoot = ""
)

$ErrorActionPreference = "Stop"
if ([string]::IsNullOrWhiteSpace($ProjectRoot)) {
    $ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
}
$python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$bars = Join-Path $ProjectRoot "data\bybit_v1_9\bybit_bars.csv"
$output = Join-Path $ProjectRoot "data\bybit_shadow_v1_11"

if (-not (Test-Path $python)) { throw "Python venv not found: $python" }
if (-not (Test-Path $bars)) { throw "Bybit bars not found: $bars" }

& $python -m trademind.bybit_shadow_experiments --bars $bars --output-dir $output
exit $LASTEXITCODE
