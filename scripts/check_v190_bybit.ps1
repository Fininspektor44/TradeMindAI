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

# A Windows venv pythonw.exe launcher can start the base Python interpreter as a
# child process. Both command lines contain the module name, but together they are
# one collector instance. Count roots of matching parent-child chains, not raw PIDs.
$processes = @(
    Get-CimInstance Win32_Process |
        Where-Object {
            $_.Name -in @("python.exe", "pythonw.exe") -and
            $_.CommandLine -match '(?i)(^|\s)-m\s+trademind\.bybit_fixed20(\s|$)'
        }
)
$matchingPids = @{}
foreach ($process in $processes) {
    $matchingPids[[int]$process.ProcessId] = $true
}
$collectorRoots = @(
    $processes |
        Where-Object { -not $matchingPids.ContainsKey([int]$_.ParentProcessId) }
)
$collectorInstances = $collectorRoots.Count

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
$taskInfo = if ($task) { Get-ScheduledTaskInfo -TaskName $TaskName } else { $null }
$healthy = (
    $task -and
    $task.State -eq "Running" -and
    $collectorInstances -eq 1 -and
    $processes.Count -ge 1 -and
    [string]$status.state -eq "RUNNING" -and
    $statusAgeSeconds -le $FreshSeconds -and
    -not [bool]$status.orders_enabled
)

Write-Host "`n=== BYBIT RUNTIME ===" -ForegroundColor Cyan
[pscustomobject]@{
    Overall = if ($healthy) { "OK" } else { "WARN" }
    TaskState = if ($task) { [string]$task.State } else { "MISSING" }
    LastTaskResult = if ($taskInfo) { $taskInfo.LastTaskResult } else { "" }
    CollectorState = [string]$status.state
    StatusAgeSeconds = $statusAgeSeconds
    CollectorInstances = $collectorInstances
    PythonProcessCount = $processes.Count
    Messages = $status.messages
    BarsWritten = $status.bars_written
    Reconnects = $status.reconnects
    OrdersEnabled = $status.orders_enabled
} | Format-List

if ($processes.Count -gt 0) {
    Write-Host "=== BYBIT PROCESS CHAIN ===" -ForegroundColor DarkCyan
    $processes |
        Sort-Object ParentProcessId,ProcessId |
        Select-Object ProcessId,ParentProcessId,Name,ExecutablePath,CreationDate,CommandLine |
        Format-List
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
    if ($collectorInstances -gt 1) {
        Write-Host "[WARN] Duplicate Bybit collector chains detected: $collectorInstances." -ForegroundColor Yellow
    } elseif ([bool]$status.orders_enabled) {
        Write-Host "[WARN] Bybit collector is not in read-only mode." -ForegroundColor Yellow
    } else {
        Write-Host "[WARN] Bybit collector is not confirmed alive. Reinstall/start the hidden direct-Python task." -ForegroundColor Yellow
    }
}

if ($OpenDashboard -and (Test-Path $dashboard)) {
    Start-Process $dashboard
}
