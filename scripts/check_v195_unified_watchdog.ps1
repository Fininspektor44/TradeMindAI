param(
    [string]$TaskName = "TradeMindAI-v1.9.5-UnifiedWatchdog",
    [ValidateRange(60, 1800)]
    [int]$FreshSeconds = 420,
    [switch]$RunNow
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$runner = Join-Path $projectRoot "scripts\run_v195_unified_watchdog.ps1"
$statusPath = Join-Path $projectRoot "data\watchdog_v1_9_5\status.json"
$reportPath = Join-Path $projectRoot "data\watchdog_v1_9_5\report.txt"

if ($RunNow) {
    & $runner
}

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
$taskInfo = if ($task) { Get-ScheduledTaskInfo -TaskName $TaskName } else { $null }

if (-not (Test-Path $statusPath)) {
    throw "Unified watchdog status not found: $statusPath"
}
$status = Get-Content $statusPath -Raw | ConvertFrom-Json
$generatedAt = [DateTimeOffset]::Parse([string]$status.generated_at).ToUniversalTime()
$statusAgeSeconds = [math]::Round(([DateTimeOffset]::UtcNow - $generatedAt).TotalSeconds, 1)
$healthy = (
    [string]$status.overall -eq "OK" -and
    [bool]$status.read_only -and
    $statusAgeSeconds -ge 0 -and
    $statusAgeSeconds -le $FreshSeconds
)

Write-Host "`n=== TRADEMIND v1.9.5 UNIFIED WATCHDOG STATUS ===" -ForegroundColor Cyan
[pscustomobject]@{
    Overall = if ($healthy) { "OK" } else { "WARN" }
    SnapshotOverall = [string]$status.overall
    ReadOnly = [bool]$status.read_only
    StatusAgeSeconds = $statusAgeSeconds
    TaskState = if ($task) { [string]$task.State } else { "MISSING" }
    LastTaskResult = if ($taskInfo) { $taskInfo.LastTaskResult } else { $null }
    EcnFreshStreams = "$($status.ecn.fresh_streams)/$($status.ecn.expected_symbols)"
    BybitSymbols = "$($status.bybit.observed_symbols)/$($status.bybit.expected_symbols)"
    BybitCollectorInstances = $status.bybit.collector_instances
    BybitPythonProcesses = $status.bybit.python_processes
    BybitOrdersEnabled = $status.bybit.orders_enabled
    StatusFile = $statusPath
    ReportFile = $reportPath
} | Format-List

@($status.checks) | Select-Object status,name,message | Format-Table -AutoSize
if (-not $healthy) {
    Write-Host "[WARN] Unified watchdog is stale or reports a failed check." -ForegroundColor Yellow
}
