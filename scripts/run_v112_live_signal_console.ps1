param(
    [string]$HostAddress = "127.0.0.1",
    [ValidateRange(1024, 65535)]
    [int]$Port = 8765,
    [string]$UnifiedSignals = ".\data\unified_signal_center_v1_6\signals.csv",
    [string]$FxObservations = ".\data\fx_research_v1_4_2\observations.csv",
    [string]$Mt5Status = ".\data\watchdog_v1_10_1\status.json",
    [string]$BybitStatus = ".\data\bybit_shadow_v1_11\status.json",
    [ValidateRange(60, 86400)]
    [int]$StaleAfterSeconds = 600
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $projectRoot
$executable = Join-Path $projectRoot ".venv\Scripts\trademind-live-signals.exe"
if (-not (Test-Path $executable)) {
    throw "TradeMind live signal executable not found: $executable. Reinstall the project in .venv."
}

$arguments = @(
    "--host", $HostAddress,
    "--port", [string]$Port,
    "--unified-signals", $UnifiedSignals,
    "--stale-after-seconds", [string]$StaleAfterSeconds
)
if (Test-Path $FxObservations) {
    $arguments += @("--fx-observations", $FxObservations)
}
if (Test-Path $Mt5Status) {
    $arguments += @("--mt5-status", $Mt5Status)
}
if (Test-Path $BybitStatus) {
    $arguments += @("--bybit-status", $BybitStatus)
}
foreach ($path in @(
    ".\data\bybit_shadow_v1_11\control\signals.csv",
    ".\data\bybit_shadow_v1_11\buy_only\signals.csv",
    ".\data\bybit_shadow_v1_11\strict_sell\signals.csv",
    ".\data\bybit_risk_plans_v1_13\base_strict\signals.csv",
    ".\data\bybit_risk_plans_v1_13\wide15_r15\signals.csv",
    ".\data\bybit_risk_plans_v1_13\wide15_r20\signals.csv",
    ".\data\bybit_risk_plans_v1_13\wide20_r15\signals.csv",
    ".\data\bybit_risk_plans_v1_13\wide20_r20\signals.csv",
    ".\data\bybit_risk_plans_v1_13\structure_r15\signals.csv",
    ".\data\bybit_risk_plans_v1_13\structure_liq\signals.csv"
)) {
    if (Test-Path $path) {
        $arguments += @("--bybit-signals", $path)
    }
}

Write-Host "TradeMind Live Signal Console"
Write-Host "Address: http://${HostAddress}:$Port"
Write-Host "Mode: read-only, OrdersEnabled=False"
& $executable @arguments
exit $LASTEXITCODE
