param(
    [switch]$RunNow,
    [switch]$OpenDashboard
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$runner = Join-Path $projectRoot "scripts\run_v114_position_management_backfill.ps1"
$statusPath = Join-Path $projectRoot "data\bybit_position_management_backfill_v1_14\status.json"
$dashboard = Join-Path $projectRoot "data\bybit_position_management_backfill_v1_14\dashboard\index.html"
$manualExit = $null
if ($RunNow) {
    & powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $runner -ProjectRoot $projectRoot
    $manualExit = $LASTEXITCODE
}
if (-not (Test-Path $statusPath)) {
    throw "Position-management backfill status not found: $statusPath"
}
$status = Get-Content $statusPath -Raw | ConvertFrom-Json
$actualArms = @($status.arms.PSObject.Properties).Count
$healthy = (
    [string]$status.state -eq "OK" -and
    [string]$status.mode -eq "BACKFILL" -and
    [bool]$status.historical_only -and
    -not [bool]$status.forward_only -and
    [bool]$status.equal_start -and
    -not [bool]$status.orders_enabled -and
    -not [bool]$status.logic_changed -and
    -not [bool]$status.source_journals_modified -and
    $actualArms -eq 12 -and
    ($null -eq $manualExit -or $manualExit -eq 0)
)

Write-Host "`n=== TRADEMIND v1.14 POSITION MANAGEMENT BACKFILL ===" -ForegroundColor Cyan
[pscustomobject]@{
    Overall = if ($healthy) { "OK" } else { "WARN" }
    State = [string]$status.state
    Mode = [string]$status.mode
    HistoricalOnly = [bool]$status.historical_only
    SourceCandidates = $status.source_candidates
    CutoffExclusive = [string]$status.cutoff
    Arms = $actualArms
    ManualRunExit = $manualExit
    OrdersEnabled = [bool]$status.orders_enabled
    LogicChanged = [bool]$status.logic_changed
    SourceJournalsModified = [bool]$status.source_journals_modified
    SameBarRule = [string]$status.same_bar_rule
    Dashboard = $dashboard
} | Format-List

$rows = foreach ($property in $status.arms.PSObject.Properties) {
    $item = $property.Value
    [pscustomobject]@{
        Arm = $property.Name
        Signals = $item.signals
        Completed = $item.completed
        Part1 = $item.partial_1_hits
        Part2 = $item.partial_2_hits
        BE = $item.be_exits
        Trail = $item.trail_exits
        GrossR = [math]::Round([double]$item.gross_total_r, 4)
        CostR = [math]::Round([double]$item.estimated_cost_r, 4)
        NetR = [math]::Round([double]$item.net_total_r, 4)
        NetAvg = [math]::Round([double]$item.net_average_r, 4)
        NetPF = [math]::Round([double]$item.net_profit_factor, 3)
        NetDD = [math]::Round([double]$item.net_max_drawdown_r, 3)
    }
}
$rows | Sort-Object NetR -Descending | Format-Table -AutoSize

if ($OpenDashboard -and (Test-Path $dashboard)) {
    Start-Process $dashboard
}
if ($healthy) {
    Write-Host "[OK] Historical management backfill is isolated, read-only and separate from forward." -ForegroundColor Green
} else {
    Write-Host "[WARN] Backfill schema, mode or read-only contract failed." -ForegroundColor Yellow
}
