param(
    [string]$ProjectRoot = "",
    [string]$DealsPath = "",
    [switch]$OpenDashboard
)

$ErrorActionPreference = "Stop"
if ([string]::IsNullOrWhiteSpace($ProjectRoot)) {
    $ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
}
if ([string]::IsNullOrWhiteSpace($DealsPath)) {
    $DealsPath = Join-Path $env:APPDATA "MetaQuotes\Terminal\Common\Files\TradeMindAI\grid_deals.csv"
}
if (-not (Test-Path $DealsPath)) {
    throw "MT5 grid deal export not found: $DealsPath"
}
$python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    throw "Project Python not found: $python"
}

$dealDir = Join-Path $ProjectRoot "data\grid_deals_v1_15"
$basketDir = Join-Path $ProjectRoot "data\grid_basket_v1_15"
$analyticsDir = Join-Path $ProjectRoot "data\grid_basket_analytics_v1_15"
New-Item -ItemType Directory -Path $dealDir,$basketDir,$analyticsDir -Force | Out-Null
$localDeals = Join-Path $dealDir "grid_deals.csv"
$legs = Join-Path $basketDir "basket_legs.csv"
Copy-Item -Path $DealsPath -Destination $localDeals -Force

Push-Location $ProjectRoot
try {
    & $python -m trademind.grid_deal_reconstruction --deals $localDeals --output $legs
    if ($LASTEXITCODE -ne 0) {
        throw "Grid deal reconstruction failed with exit code $LASTEXITCODE"
    }
    $arguments = @(
        "-m", "trademind.grid_basket_audit",
        "--legs", $legs,
        "--output-dir", $analyticsDir
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
