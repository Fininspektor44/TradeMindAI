param(
    [string]$LegsPath = "",
    [switch]$RunNow,
    [switch]$OpenDashboard
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$runner = Join-Path $projectRoot "scripts\run_v115_grid_basket_analytics.ps1"
$statusPath = Join-Path $projectRoot "data\grid_basket_analytics_v1_15\status.json"
$manualExit = $null
if ($RunNow) {
    $runnerArgs = @("-ProjectRoot", $projectRoot)
    if (-not [string]::IsNullOrWhiteSpace($LegsPath)) {
        $runnerArgs += @("-LegsPath", $LegsPath)
    }
    if ($OpenDashboard) {
        $runnerArgs += "-OpenDashboard"
    }
    & powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $runner @runnerArgs
    $manualExit = $LASTEXITCODE
}
if (-not (Test-Path $statusPath)) {
    throw "Grid basket analytics status not found: $statusPath"
}
$status = Get-Content $statusPath -Raw | ConvertFrom-Json
$healthy = (
    [string]$status.state -eq "OK" -and
    -not [bool]$status.orders_enabled -and
    -not [bool]$status.logic_changed -and
    -not [bool]$status.source_modified -and
    -not [bool]$status.signal_generation_enabled -and
    ($null -eq $manualExit -or $manualExit -eq 0)
)

Write-Host "`n=== TRADEMIND v1.15 GRID BASKET ANALYTICS ===" -ForegroundColor Cyan
[pscustomobject]@{
    Overall = if ($healthy) { "OK" } else { "WARN" }
    State = [string]$status.state
    SourceRows = $status.source_rows
    Baskets = $status.baskets
    Completed = $status.completed_baskets
    Open = $status.open_baskets
    Wins = $status.wins
    Losses = $status.losses
    NetProfit = [math]::Round([double]$status.net_profit, 2)
    ProfitFactor = [math]::Round([double]$status.profit_factor, 3)
    WorstDDMoney = [math]::Round([double]$status.worst_drawdown_money, 2)
    WorstDDPercent = [math]::Round([double]$status.worst_drawdown_pct, 2)
    DrawdownCoverage = if ($null -ne $status.drawdown_coverage) { [math]::Round(100 * [double]$status.drawdown_coverage, 1) } else { 0 }
    MaxLegs = $status.max_legs
    MaxConcurrent = $status.max_concurrent_baskets
    OrdersEnabled = [bool]$status.orders_enabled
    SignalGeneration = [bool]$status.signal_generation_enabled
    SourceModified = [bool]$status.source_modified
    Dashboard = [string]$status.dashboard
} | Format-List

$riskPath = Join-Path $projectRoot "data\grid_basket_analytics_v1_15\risk_by_leg.csv"
if (Test-Path $riskPath) {
    Import-Csv $riskPath | Select-Object `
        leg_no,baskets_reaching_leg,next_leg_rate,stop_exit_rate,average_max_drawdown_money,worst_max_drawdown_money,average_net_profit | Format-Table -AutoSize
}
if ($healthy) {
    Write-Host "[OK] Grid analytics is read-only, signal-free and isolated from trading logic." -ForegroundColor Green
} else {
    Write-Host "[WARN] Grid analytics safety contract failed." -ForegroundColor Yellow
}
