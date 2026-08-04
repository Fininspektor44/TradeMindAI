param(
    [string]$TaskName = "TradeMindAI-v1.11-ShadowExperiments",
    [ValidateRange(60, 1800)]
    [int]$FreshSeconds = 600,
    [switch]$RunNow,
    [switch]$OpenDashboard
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$runner = Join-Path $projectRoot "scripts\run_v111_shadow_experiments.ps1"
$statusPath = Join-Path $projectRoot "data\bybit_shadow_v1_11\status.json"
$dashboard = Join-Path $projectRoot "data\bybit_shadow_v1_11\dashboard\index.html"

$manualExit = $null
if ($RunNow) {
    & powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $runner -ProjectRoot $projectRoot
    $manualExit = $LASTEXITCODE
}
if (-not (Test-Path $statusPath)) {
    throw "Experiment status not found: $statusPath"
}
$status = Get-Content $statusPath -Raw | ConvertFrom-Json
$updated = [DateTimeOffset]::Parse([string]$status.updated_at).ToUniversalTime()
$age = [math]::Round(([DateTimeOffset]::UtcNow - $updated).TotalSeconds, 1)
$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
$taskInfo = if ($task) { Get-ScheduledTaskInfo -TaskName $TaskName } else { $null }
$arms = @("CONTROL", "BUY_ONLY", "STRICT_SELL")
$armsPresent = @($arms | Where-Object { $null -ne $status.arms.$_ }).Count -eq $arms.Count
$healthy = (
    [string]$status.state -eq "OK" -and
    [bool]$status.forward_only -and
    -not [bool]$status.orders_enabled -and
    $age -ge 0 -and $age -le $FreshSeconds -and
    $armsPresent -and
    $task -and $task.State -in @("Ready", "Running") -and
    $taskInfo -and $taskInfo.LastTaskResult -eq 0
)

Write-Host "`n=== TRADEMIND v1.11 SHADOW EXPERIMENTS ===" -ForegroundColor Cyan
[pscustomobject]@{
    Overall = if ($healthy) { "OK" } else { "WARN" }
    State = [string]$status.state
    StatusAgeSeconds = $age
    TaskState = if ($task) { [string]$task.State } else { "MISSING" }
    LastTaskResult = if ($taskInfo) { $taskInfo.LastTaskResult } else { $null }
    ManualRunExit = $manualExit
    EqualStart = [string]$status.started_at
    M5M15H1 = "$($status.source_bars)/$($status.m15_bars)/$($status.h1_bars)"
    ForwardOnly = [bool]$status.forward_only
    OrdersEnabled = [bool]$status.orders_enabled
    StatusFile = $statusPath
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
        WinRatePct = [math]::Round(100.0 * [double]$item.win_rate, 2)
        TotalR = [math]::Round([double]$item.total_r, 4)
        AverageR = [math]::Round([double]$item.average_r, 4)
        ProfitFactor = [math]::Round([double]$item.profit_factor, 3)
        MaxDrawdownR = [math]::Round([double]$item.max_drawdown_r, 3)
    }
}
$rows | Format-Table -AutoSize

if ($OpenDashboard -and (Test-Path $dashboard)) {
    Start-Process $dashboard
}
if ($healthy) {
    Write-Host "[OK] All three equal-start experiment arms are healthy, forward-only and read-only." -ForegroundColor Green
} else {
    Write-Host "[WARN] Experiment task, freshness, arm schema or read-only contract failed." -ForegroundColor Yellow
}
