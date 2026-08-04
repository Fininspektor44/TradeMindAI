param(
    [switch]$RunNow,
    [switch]$OpenDashboard
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$runner = Join-Path $projectRoot "scripts\run_v1131_risk_plan_backfill.ps1"
$statusPath = Join-Path $projectRoot "data\bybit_risk_plans_backfill_v1_13_1\status.json"
$dashboard = Join-Path $projectRoot "data\bybit_risk_plans_backfill_v1_13_1\dashboard\index.html"
$manualExit = $null
if ($RunNow) {
    & powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $runner -ProjectRoot $projectRoot
    $manualExit = $LASTEXITCODE
}
if (-not (Test-Path $statusPath)) {
    throw "Risk-plan backfill status not found: $statusPath"
}
$status = Get-Content $statusPath -Raw | ConvertFrom-Json
$arms = @(
    "BASE_STRICT", "WIDE15_R15", "WIDE15_R20", "WIDE20_R15",
    "WIDE20_R20", "STRUCTURE_R15", "STRUCTURE_LIQ"
)
$armsPresent = @($arms | Where-Object { $null -ne $status.arms.$_ }).Count -eq $arms.Count
$healthy = (
    [string]$status.state -eq "OK" -and
    [string]$status.mode -eq "BACKFILL" -and
    [bool]$status.historical_only -and
    -not [bool]$status.orders_enabled -and
    -not [bool]$status.logic_changed -and
    -not [bool]$status.forward_journals_modified -and
    $armsPresent
)

Write-Host "`n=== TRADEMIND v1.13.1 RISK PLAN BACKFILL ===" -ForegroundColor Cyan
[pscustomobject]@{
    Overall = if ($healthy) { "OK" } else { "WARN" }
    State = [string]$status.state
    Mode = [string]$status.mode
    HistoricalOnly = [bool]$status.historical_only
    SourceCandidates = [int]$status.source_candidates
    SourceStarted = [string]$status.source_started_at
    SourceEnded = [string]$status.source_ended_at
    CutoffExclusive = [string]$status.cutoff_exclusive
    ManualRunExit = $manualExit
    OrdersEnabled = [bool]$status.orders_enabled
    LogicChanged = [bool]$status.logic_changed
    ForwardJournalsModified = [bool]$status.forward_journals_modified
    SameBarRule = [string]$status.same_bar_rule
    Dashboard = $dashboard
} | Format-List

$rows = foreach ($arm in $arms) {
    $item = $status.arms.$arm
    [pscustomobject]@{
        Arm = $arm
        Signals = $item.signals
        Completed = $item.completed
        Open = $item.open
        Wins = $item.wins
        Losses = $item.losses
        Timeouts = $item.timeouts
        GrossR = [math]::Round([double]$item.gross_total_r, 4)
        CostR = [math]::Round([double]$item.estimated_cost_r, 4)
        NetR = [math]::Round([double]$item.net_total_r, 4)
        NetAvgR = [math]::Round([double]$item.net_average_r, 4)
        NetPF = [math]::Round([double]$item.net_profit_factor, 3)
        NetDD = [math]::Round([double]$item.net_max_drawdown_r, 3)
        SizeFactor = [math]::Round([double]$item.average_position_size_factor, 3)
    }
}
$rows | Format-Table -AutoSize

if ($OpenDashboard -and (Test-Path $dashboard)) {
    Start-Process $dashboard
}
if ($healthy) {
    Write-Host "[OK] Historical backfill is isolated, read-only and separate from forward results." -ForegroundColor Green
} else {
    Write-Host "[WARN] Backfill schema or read-only isolation contract failed." -ForegroundColor Yellow
}
