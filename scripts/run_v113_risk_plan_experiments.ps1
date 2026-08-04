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
$feeInvariant = $FeeBpsPerSide.ToString([Globalization.CultureInfo]::InvariantCulture)
$slippageInvariant = $SlippageBpsPerSide.ToString([Globalization.CultureInfo]::InvariantCulture)

Push-Location $ProjectRoot
try {
    & $python -m trademind.bybit_risk_plan_experiments `
        --bars (Join-Path $ProjectRoot "data\bybit_v1_9\bybit_bars.csv") `
        --strict-decisions (Join-Path $ProjectRoot "data\bybit_shadow_v1_11\strict_sell\decisions.csv") `
        --output-dir (Join-Path $ProjectRoot "data\bybit_risk_plans_v1_13") `
        --fee-bps-per-side $feeInvariant `
        --slippage-bps-per-side $slippageInvariant
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
