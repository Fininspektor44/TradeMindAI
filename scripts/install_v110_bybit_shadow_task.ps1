param(
    [string]$TaskName = "TradeMindAI-v1.10-BybitShadow",
    [ValidateRange(5, 60)]
    [int]$IntervalMinutes = 5,
    [switch]$RunNow
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$pythonw = Join-Path $projectRoot ".venv\Scripts\pythonw.exe"
$bars = Join-Path $projectRoot "data\bybit_v1_9\bybit_bars.csv"
$outputDir = Join-Path $projectRoot "data\bybit_shadow_v1_10"
$statusPath = Join-Path $outputDir "status.json"

if (-not (Test-Path $pythonw)) { throw "Background Python not found: $pythonw" }
if (-not (Test-Path $bars)) { throw "Bybit M5 source not found: $bars" }
New-Item -ItemType Directory -Path $outputDir -Force | Out-Null

$arguments = "-m trademind.bybit_shadow --bars `"$bars`" --output-dir `"$outputDir`""
$action = New-ScheduledTaskAction -Execute $pythonw -Argument $arguments -WorkingDirectory $projectRoot
$trigger = New-ScheduledTaskTrigger `
    -Once `
    -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes)
$settings = New-ScheduledTaskSettingsSet `
    -Hidden `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 4)

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Description "TradeMind v1.10 forward-only read-only Bybit H1-M15-M5 shadow research" `
    -Force | Out-Null

Write-Host "Installed task: $TaskName"
Write-Host "Interval: every $IntervalMinutes minutes"
Write-Host "Mode: H1 context + M15 confirmation + M5 trigger"
Write-Host "Forward-only paper research. No orders."
if ($RunNow) {
    Start-ScheduledTask -TaskName $TaskName
    Start-Sleep -Seconds 8
    $info = Get-ScheduledTaskInfo -TaskName $TaskName
    Write-Host "LastTaskResult: $($info.LastTaskResult)"
    if (Test-Path $statusPath) {
        $status = Get-Content $statusPath -Raw | ConvertFrom-Json
        Write-Host "Shadow status: $($status.state)"
        Write-Host "M15/H1 bars: $($status.m15_bars)/$($status.h1_bars)"
        Write-Host "Forward signals: $($status.paper_signals)"
        Write-Host "OrdersEnabled: $($status.orders_enabled)"
    }
}
