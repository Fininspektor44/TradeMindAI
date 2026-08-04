param(
    [string]$TaskName = "TradeMindAI-v1.10.2-UnifiedWatchdog",
    [ValidateRange(60, 1800)]
    [int]$FreshSeconds = 420,
    [switch]$RunNow
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$runner = Join-Path $projectRoot "scripts\run_v1102_unified_watchdog.ps1"
$statusPath = Join-Path $projectRoot "data\watchdog_v1_10_1\status.json"
$reportPath = Join-Path $projectRoot "data\watchdog_v1_10_1\report.txt"

$runExit = $null
if ($RunNow) {
    & $runner
    $runExit = $LASTEXITCODE
}

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
$taskInfo = if ($task) { Get-ScheduledTaskInfo -TaskName $TaskName } else { $null }
if (-not (Test-Path $statusPath)) {
    throw "Unified watchdog status not found: $statusPath"
}
$status = Get-Content $statusPath -Raw | ConvertFrom-Json
$generatedAt = [DateTimeOffset]::Parse([string]$status.generated_at).ToUniversalTime()
$statusAgeSeconds = [math]::Round(([DateTimeOffset]::UtcNow - $generatedAt).TotalSeconds, 1)
$currentSnapshotHealthy = (
    [string]$status.overall -eq "OK" -and
    [bool]$status.read_only -and
    $statusAgeSeconds -ge 0 -and
    $statusAgeSeconds -le $FreshSeconds
)
$taskRegisteredAndReady = $task -and $task.State -in @("Ready", "Running")
$lastScheduledRunOk = $taskInfo -and $taskInfo.LastTaskResult -eq 0
$scheduledTaskHealthy = $taskRegisteredAndReady -and $lastScheduledRunOk

Write-Host "`n=== TRADEMIND v1.10.2 UNIFIED WATCHDOG STATUS ===" -ForegroundColor Cyan
[pscustomobject]@{
    Overall = if ($currentSnapshotHealthy) { "OK" } else { "WARN" }
    SnapshotOverall = [string]$status.overall
    ReadOnly = [bool]$status.read_only
    StatusAgeSeconds = $statusAgeSeconds
    ScheduledTaskHealth = if ($scheduledTaskHealthy) { "OK" } else { "WARN" }
    TaskState = if ($task) { [string]$task.State } else { "MISSING" }
    LastTaskResult = if ($taskInfo) { $taskInfo.LastTaskResult } else { $null }
    ManualRunExit = $runExit
    EcnFreshStreams = "$($status.ecn.fresh_streams)/$($status.ecn.expected_symbols)"
    BybitSymbols = "$($status.bybit.observed_symbols)/$($status.bybit.expected_symbols)"
    BybitState = $status.bybit.state
    BybitReconnectGrace = $status.bybit.reconnect_grace_active
    BybitLastEventAgeSeconds = $status.bybit.last_event_age_seconds
    BybitCollectorInstances = $status.bybit.collector_instances
    BybitPythonProcesses = $status.bybit.python_processes
    BybitOrdersEnabled = $status.bybit.orders_enabled
    ShadowState = $status.shadow.state
    ShadowTaskState = $status.shadow.task_state
    ShadowM5M15H1 = "$($status.shadow.source_m5_bars)/$($status.shadow.m15_bars)/$($status.shadow.h1_bars)"
    ShadowDecisions = $status.shadow.decisions
    ShadowCandidates = $status.shadow.candidates
    ShadowForwardSignals = $status.shadow.paper_signals
    ShadowCompletedSignals = $status.shadow.completed_signals
    ShadowForwardOnly = $status.shadow.forward_only
    ShadowOrdersEnabled = $status.shadow.orders_enabled
    StatusFile = $statusPath
    ReportFile = $reportPath
} | Format-List

@($status.checks) | Select-Object status,name,message | Format-Table -AutoSize
if (-not $currentSnapshotHealthy) {
    Write-Host "[WARN] Current snapshot is stale, a component failed, or reconnect exceeded grace." -ForegroundColor Yellow
} elseif (-not $scheduledTaskHealthy) {
    Write-Host "[OK] Current ECN, Bybit and Shadow snapshot is healthy and read-only." -ForegroundColor Green
    Write-Host "[WARN] Previous scheduled run returned $($taskInfo.LastTaskResult)." -ForegroundColor Yellow
} elseif ([bool]$status.bybit.reconnect_grace_active) {
    Write-Host "[OK] Short verified Bybit reconnect is inside grace; recent market flow remains healthy." -ForegroundColor Yellow
} else {
    Write-Host "[OK] ECN, Bybit collector and Bybit Shadow are healthy and read-only." -ForegroundColor Green
}
