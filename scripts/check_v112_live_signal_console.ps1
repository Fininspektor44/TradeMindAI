param(
    [string]$TaskName = "TradeMindAI-v1.12-LiveSignalConsole",
    [string]$HostAddress = "127.0.0.1",
    [ValidateRange(1024, 65535)]
    [int]$Port = 8765,
    [switch]$Open
)

$ErrorActionPreference = "Stop"
$baseUrl = "http://${HostAddress}:$Port"
$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
$taskInfo = if ($task) { Get-ScheduledTaskInfo -TaskName $TaskName } else { $null }

try {
    $health = Invoke-RestMethod -Method Get -Uri "$baseUrl/api/health" -TimeoutSec 5
    $page = Invoke-WebRequest -Method Get -Uri "$baseUrl/" -TimeoutSec 5 -UseBasicParsing
} catch {
    Write-Host "[ERROR] Live Signal Console is not reachable: $($_.Exception.Message)" -ForegroundColor Red
    exit 2
}

$pageOk = $page.StatusCode -eq 200 -and $page.Content -match "TradeMind Live Signal Console"
$safetyOk = [bool]$health.read_only -and -not [bool]$health.orders_enabled
$taskOk = $task -and $task.State -in @("Ready", "Running")
$dataState = [string]$health.state
$overall = if ($pageOk -and $safetyOk -and $taskOk -and $dataState -eq "OK") {
    "OK"
} elseif ($pageOk -and $safetyOk -and $taskOk) {
    "WARN"
} else {
    "ERROR"
}

Write-Host "`n=== TRADEMIND v1.12 LIVE SIGNAL CONSOLE ===" -ForegroundColor Cyan
[pscustomobject]@{
    Overall = $overall
    Address = $baseUrl
    ApiState = $dataState
    ReadOnly = [bool]$health.read_only
    OrdersEnabled = [bool]$health.orders_enabled
    Signals = $health.signals
    StaleSignals = $health.stale_signals
    Errors = @($health.errors).Count
    PageHealthy = $pageOk
    TaskState = if ($task) { [string]$task.State } else { "MISSING" }
    LastTaskResult = if ($taskInfo) { $taskInfo.LastTaskResult } else { $null }
    LoadedAt = $health.loaded_at
} | Format-List

if ($Open -and $pageOk) {
    Start-Process $baseUrl
}
if (-not $safetyOk) {
    Write-Host "[ERROR] Read-only safety contract failed." -ForegroundColor Red
    exit 2
}
if (-not $pageOk -or -not $taskOk) {
    Write-Host "[ERROR] Page or scheduled task is not healthy." -ForegroundColor Red
    exit 2
}
if ($dataState -ne "OK") {
    Write-Host "[WARN] Console is running safely, but data is empty, stale or has source errors." -ForegroundColor Yellow
    exit 0
}
Write-Host "[OK] Live console, API, task and read-only contract are healthy." -ForegroundColor Green
exit 0
