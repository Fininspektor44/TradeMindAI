param(
    [string]$TaskName = "TradeMindAI-v1.13-RiskPlanExperiments",
    [ValidateRange(60, 1800)]
    [int]$FreshSeconds = 600,
    [switch]$RunNow,
    [switch]$OpenDashboard
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$runner = Join-Path $projectRoot "scripts\run_v113_risk_plan_experiments.ps1"
$statusPath = Join-Path $projectRoot "data\bybit_risk_plans_v1_13\status.json"
$dashboard = Join-Path $projectRoot "data\bybit_risk_plans_v1_13\dashboard\index.html"
$manualExit = $null
if ($RunNow) {
    & powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $runner -ProjectRoot $projectRoot
    $manualExit = $LASTEXITCODE
}
if (-not (Test-Path $statusPath)) {
    throw "Risk-plan status not found: $statusPath"
}
$status = Get-Content $statusPath -Raw | ConvertFrom-Json
$updated = [DateTimeOffset]::Parse([string]$status.updated_at).ToUniversalTime()
$age = [math]::Round(([DateTimeOffset]::UtcNow - $updated).TotalSeconds, 1)
$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
$taskInfo = if ($task) { Get-ScheduledTaskInfo -TaskName $TaskName } else { $null }
$arms = @(
    "BASE_STRICT", "WIDE15_R15", "WIDE15_R20", "WIDE20_R15",
    "WIDE20_R20", "STRUCTURE_R15", "STRUCTURE_LIQ"
)
$armsPresent = @($arms | Where-Object { $null -ne $status.arms.$_ }).Count -eq $arms.Count
$healthy = (
    [string]$status.state -eq "OK" -and
    [bool]$status.forward_only -and
    [bool]$status.equal_start -and
    -not [bool]$status.orders_enabled -and
    -not [bool]$status.logic_changed -and
    $age -ge -5 -and $age -le $FreshSeconds -and
    $armsPresent -and
    $task -and $task.State -in @("Ready", "Running") -and
    $taskInfo -and $taskInfo.LastTaskResult -eq 0
)

Write-Host "`n=== TRADEMIND v1.13 RISK PLAN EXPERIMENTS ===" -ForegroundColor Cyan
[pscustomobject]@{
    Overall = if ($healthy) { "OK" } else { "WARN" }
    State = [string]$status.state
    StatusAgeSeconds = $age
    TaskState = if ($task) { [string]$task.State } else { "MISSING" }
    LastTaskResult = if ($taskInfo) { $taskInfo.LastTaskResult } else { $null }
    ManualRunExit = $manualExit
    ExperimentStarted = [string]$status.started_at
    ForwardOnly = [bool]$status.forward_only
    EqualStart = [bool]$status.equal_start
    OrdersEnabled = [bool]$status.orders_enabled
    LogicChanged = [bool]$status.logic_changed
    Dashboard = $dashboard
} | Format-List

$rows = foreach ($arm in $arms) {
    $item = $status.arms.$arm
    [pscustomobject]@{
        Arm = $arm
        Signals = $item.signals
        Completed = $item.completed
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
    Write-Host "[OK] Seven equal-start risk plans are fresh, forward-only and read-only." -ForegroundColor Green
} else {
    Write-Host "[WARN] Task, freshness, schema or read-only contract failed." -ForegroundColor Yellow
}
