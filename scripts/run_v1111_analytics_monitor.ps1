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
    & $python -m trademind.bybit_shadow_monitor `
        --experiment-dir (Join-Path $ProjectRoot "data\bybit_shadow_v1_11") `
        --output-dir (Join-Path $ProjectRoot "data\bybit_shadow_monitor_v1_11_1") `
        --fee-bps-per-side $feeInvariant `
        --slippage-bps-per-side $slippageInvariant
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
