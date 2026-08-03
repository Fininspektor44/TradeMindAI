param(
    [string]$TaskName = "TradeMindAI-v1.9-Bybit",
    [ValidateRange(30, 900)]
    [int]$FreshSeconds = 120,
    [switch]$OpenDashboard
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$outputDir = Join-Path $projectRoot "data\bybit_v1_9"
$statusPath = Join-Path $outputDir "status.json"
$universePath = Join-Path $outputDir "universe.csv"
$latestPath = Join-Path $outputDir "latest.csv"
$dashboard = Join-Path $outputDir "dashboard\index.html"

if (-not (Test-Path $statusPath)) {
    throw "Bybit status not found: $statusPath"
}

$status = Get-Content $statusPath -Raw | ConvertFrom-Json
$updatedAt = [DateTimeOffset]::Parse([string]$status.updated_at).ToUniversalTime()
$statusAgeSeconds = [math]::Round(([DateTimeOffset]::UtcNow - $updatedAt).TotalSeconds, 1)
$processes = @(
    Get-CimInstance Win32_Process |
        Where-Object { $_.CommandLine -match "trademind\.bybit_fixed20" }
)
$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
$taskInfo = if ($task) { Get-ScheduledTaskInfo -TaskName $TaskName } else { $null }
$healthy = (
    $task -and
    $task.State -eq "Running" -and
    $processes.Count -ge 1 -and
    [string]$status.state -eq "RUNNING" -and
    $statusAgeSeconds -le $FreshSeconds
)

Write-Host "`n=== BYBIT RUNTIME ===" -ForegroundColor Cyan
[pscustomobject]@{
    Overall = if ($healthy) { "OK" } else { "WARN" }
    TaskState = if ($task) { [string]$task.State } else { "MISSING" }
    LastTaskResult = if ($taskInfo) { $taskInfo.LastTaskResult } else { "" }
    CollectorState = [string]$status.state
    StatusAgeSeconds = $statusAgeSeconds
    ProcessCount = $processes.Count
    Messages = $status.messages
    BarsWritten = $status.bars_written
    Reconnects = $status.reconnects
    OrdersEnabled = $status.orders_enabled
} | Format-List

if ($processes.Count -gt 0) {
    Write-Host "=== BYBIT PROCESS ===" -ForegroundColor DarkCyan
    $processes | Select-Object ProcessId,CreationDate,CommandLine | Format-List
}

if (Test-Path $universePath) {
    Write-Host "=== FIXED BYBIT UNIVERSE ===" -ForegroundColor Yellow
    Import-Csv $universePath |
        Select-Object rank,symbol,turnover24h,lastPrice,price24hPcnt |
        Format-Table -AutoSize
}

if (Test-Path $latestPath) {
    Write-Host "=== LATEST CLOSED M5 BARS ===" -ForegroundColor Green
    Import-Csv $latestPath |
        Select-Object symbol,close,trade_count,delta_turnover,cvd_turnover,book_imbalance_10,spread_bps,funding_rate,open_interest_value,received_at |
        Format-Table -AutoSize
}

if (-not $healthy) {
    Write-Host "[WARN] Bybit collector is not confirmed alive. Reinstall/start the direct-Python task." -ForegroundColor Yellow
}

if ($OpenDashboard -and (Test-Path $dashboard)) {
    Start-Process $dashboard
}
