param(
    [string]$TaskName = "TradeMindAI-v1.10-BybitShadow",
    [ValidateRange(120, 1800)]
    [int]$FreshSeconds = 600,
    [switch]$OpenDashboard
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$outputDir = Join-Path $projectRoot "data\bybit_shadow_v1_10"
$statusPath = Join-Path $outputDir "status.json"
$dashboard = Join-Path $outputDir "dashboard\index.html"

if (-not (Test-Path $statusPath)) { throw "Shadow status not found: $statusPath" }
$status = Get-Content $statusPath -Raw | ConvertFrom-Json
$updated = [DateTimeOffset]::Parse([string]$status.updated_at).ToUniversalTime()
$age = [math]::Round(([DateTimeOffset]::UtcNow - $updated).TotalSeconds, 1)
$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
$info = if ($task) { Get-ScheduledTaskInfo -TaskName $TaskName } else { $null }
$taskOk = $task -and $task.State -in @("Ready", "Running")
$resultOk = $info -and $info.LastTaskResult -eq 0
$healthy = (
    $taskOk -and
    $resultOk -and
    [string]$status.state -eq "OK" -and
    $age -ge 0 -and
    $age -le $FreshSeconds -and
    -not [bool]$status.orders_enabled -and
    [bool]$status.forward_only
)

Write-Host "`n=== TRADEMIND v1.10 BYBIT SHADOW ===" -ForegroundColor Cyan
[pscustomobject]@{
    Overall = if ($healthy) { "OK" } else { "WARN" }
    TaskState = if ($task) { [string]$task.State } else { "MISSING" }
    LastTaskResult = if ($info) { $info.LastTaskResult } else { "" }
    ShadowState = [string]$status.state
    StatusAgeSeconds = $age
    SourceM5Bars = $status.source_bars
    AggregatedM15Bars = $status.m15_bars
    AggregatedH1Bars = $status.h1_bars
    Decisions = $status.decisions
    Candidates = $status.gate_counts.CANDIDATE
    ForwardSignals = $status.paper_signals
    CompletedSignals = $status.completed_paper_signals
    ForwardOnly = $status.forward_only
    OrdersEnabled = $status.orders_enabled
} | Format-List

if (-not $healthy) {
    Write-Host "[WARN] Bybit Shadow Research is not confirmed healthy." -ForegroundColor Yellow
} else {
    Write-Host "[OK] Shadow research is healthy, forward-only and read-only." -ForegroundColor Green
}
if ($OpenDashboard -and (Test-Path $dashboard)) { Start-Process $dashboard }
