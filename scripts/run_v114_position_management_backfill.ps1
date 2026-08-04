param(
    [string]$ProjectRoot = "",
    [ValidateRange(0, 100)]
    [double]$FeeBpsPerSide = 5.5,
    [ValidateRange(0, 100)]
    [double]$SlippageBpsPerSide = 1.0
)

$ErrorActionPreference = "Stop"
if ([string]::IsNullOrWhiteSpace($ProjectRoot)) {
    $ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
}
$python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    throw "Project Python not found: $python"
}
$forwardMeta = Join-Path $ProjectRoot "data\bybit_position_management_v1_14\experiment_meta.json"
if (-not (Test-Path $forwardMeta)) {
    throw "Run the v1.14 forward experiment first. Metadata not found: $forwardMeta"
}
$feeInvariant = $FeeBpsPerSide.ToString([Globalization.CultureInfo]::InvariantCulture)
$slippageInvariant = $SlippageBpsPerSide.ToString([Globalization.CultureInfo]::InvariantCulture)

Push-Location $ProjectRoot
try {
    & $python -m trademind.bybit_position_management `
        --mode BACKFILL `
        --bars (Join-Path $ProjectRoot "data\bybit_v1_9\bybit_bars.csv") `
        --strict-decisions (Join-Path $ProjectRoot "data\bybit_shadow_v1_11\strict_sell\decisions.csv") `
        --forward-meta $forwardMeta `
        --output-dir (Join-Path $ProjectRoot "data\bybit_position_management_backfill_v1_14") `
        --fee-bps-per-side $feeInvariant `
        --slippage-bps-per-side $slippageInvariant
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
