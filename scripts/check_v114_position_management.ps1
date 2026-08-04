param(
    [string]$TaskName = "TradeMindAI-v1.14-PositionManagement",
    [ValidateRange(60, 1800)]
    [int]$FreshSeconds = 600,
    [switch]$RunNow,
    [switch]$OpenDashboard
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$runner = Join-Path $projectRoot "scripts\run_v114_position_management.ps1"
$statusPath = Join-Path $projectRoot "data\bybit_position_management_v1_14\status.json"
$dashboard = Join-Path $projectRoot "data\bybit_position_management_v1_14\dashboard\index.html"
$manualExit = $null
if ($RunNow) {
    & powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $runner -ProjectRoot $projectRoot
    $manualExit = $LASTEXITCODE
}
if (-not (Test-Path $statusPath)) {
    throw "Position-management status not found: $statusPath"
}
$status = Get-Content $statusPath -Raw | ConvertFrom-Json
$updated = [DateTimeOffset]::Parse([string]$status.updated_at).ToUniversalTime()
$age = [math]::Round(([DateTimeOffset]::UtcNow - $updated).TotalSeconds, 1)
$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
$taskInfo = if ($task) { Get-ScheduledTaskInfo -TaskName $TaskName } else { $null }
$expectedArms = 12
$actualArms = @($status.arms.PSObject.Properties).Count
$healthy = (
    [string]$status.state -eq "OK" -and
    [string]$status.mode -eq "FORWARD" -and
    [bool]$status.forward_only -and
    -not [bool]$status.historical_only -and
    [bool]$status.equal_start -and
    -not [bool]$status.orders_enabled -and
    -not [bool]$status.logic_changed -and
    -not [bool]$status.source_journals_modified -and
    $actualArms -eq $expectedArms -and
    $age -ge -5 -and $age -le $FreshSeconds -and
    $task -and $task.State -in @("Ready", "Running") -and
    $taskInfo -and $taskInfo.LastTaskResult -eq 0
)

Write-Host "`n=== TRADEMIND v1.14 POSITION MANAGEMENT FORWARD ===" -ForegroundColor Cyan
[pscustomobject]@{
    Overall = if ($healthy) { "OK" } else { "WARN" }
    State = [string]$status.state
    Mode = [string]$status.mode
    StatusAgeSeconds = $age
    TaskState = if ($task) { [string]$task.State } else { "MISSING" }
    LastTaskResult = if ($taskInfo) { $taskInfo.LastTaskResult } else { $null }
    ManualRunExit = $manualExit
    ExperimentStarted = [string]$status.cutoff
    SourceCandidates = $status.source_candidates
    Arms = $actualArms
    ForwardOnly = [bool]$status.forward_only
    OrdersEnabled = [bool]$status.orders_enabled
    LogicChanged = [bool]$status.logic_changed
    SourceJournalsModified = [bool]$status.source_journals_modified
    Dashboard = $dashboard
} | Format-List

$rows = foreach ($property in $status.arms.PSObject.Properties) {
    $item = $property.Value
    [pscustomobject]@{
        Arm = $property.Name
        Signals = $item.signals
        Completed = $item.completed
        Part1 = $item.partial_1_hits
        BE = $item.be_exits
        Trail = $item.trail_exits
        GrossR = [math]::Round([double]$item.gross_total_r, 4)
        CostR = [math]::Round([double]$item.estimated_cost_r, 4)
        NetR = [math]::Round([double]$item.net_total_r, 4)
        NetPF = [math]::Round([double]$item.net_profit_factor, 3)
        NetDD = [math]::Round([double]$item.net_max_drawdown_r, 3)
    }
}
$rows | Sort-Object NetR -Descending | Format-Table -AutoSize

if ($OpenDashboard -and (Test-Path $dashboard)) {
    Start-Process $dashboard
}
if ($healthy) {
    Write-Host "[OK] Twelve forward management arms are fresh, isolated and read-only." -ForegroundColor Green
} else {
    Write-Host "[WARN] Task, freshness, schema or read-only contract failed." -ForegroundColor Yellow
}
