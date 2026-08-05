param(
    [string]$ProjectRoot = "",
    [string]$LegsPath = "",
    [switch]$OpenDashboard
)

$ErrorActionPreference = "Stop"
if ([string]::IsNullOrWhiteSpace($ProjectRoot)) {
    $ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
}
if ([string]::IsNullOrWhiteSpace($LegsPath)) {
    $LegsPath = Join-Path $ProjectRoot "data\grid_basket_v1_15\basket_legs.csv"
}
$python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    throw "Project Python not found: $python"
}
if (-not (Test-Path $LegsPath)) {
    throw "Grid basket leg CSV not found: $LegsPath"
}
$output = Join-Path $ProjectRoot "data\grid_basket_analytics_v1_15"

Push-Location $ProjectRoot
try {
    $arguments = @(
        "-m", "trademind.grid_basket_analytics",
        "--legs", $LegsPath,
        "--output-dir", $output
    )
    if ($OpenDashboard) {
        $arguments += "--open-dashboard"
    }
    & $python @arguments
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
