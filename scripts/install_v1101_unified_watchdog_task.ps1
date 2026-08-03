param(
    [string]$TaskName = "TradeMindAI-v1.10.1-UnifiedWatchdog",
    [ValidateRange(1, 60)]
    [int]$IntervalMinutes = 5,
    [switch]$RunNow
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$runner = Join-Path $projectRoot "scripts\run_v1101_unified_watchdog.ps1"
$oldTaskName = "TradeMindAI-v1.9.5-UnifiedWatchdog"

if (-not (Test-Path $runner)) {
    throw "Unified watchdog runner not found: $runner"
}
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Run PowerShell as Administrator to install the unified watchdog task."
}

$arguments = "-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$runner`""
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $arguments -WorkingDirectory $projectRoot
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes)
$settings = New-ScheduledTaskSettingsSet `
    -Hidden `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 2)

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Description "TradeMind v1.10.1 read-only unified ECN, Bybit collector and Bybit Shadow watchdog" `
    -Force | Out-Null

$oldTask = Get-ScheduledTask -TaskName $oldTaskName -ErrorAction SilentlyContinue
if ($oldTask) {
    Unregister-ScheduledTask -TaskName $oldTaskName -Confirm:$false
    Write-Host "Removed superseded task: $oldTaskName"
}

Write-Host "Installed task: $TaskName"
Write-Host "Action: hidden read-only ECN + Bybit collector + Bybit Shadow health check"
Write-Host "Interval: every $IntervalMinutes minutes"
Write-Host "Project: $projectRoot"
Write-Host "The watchdog never sends trading orders and never changes MT5 positions."

if ($RunNow) {
    Start-ScheduledTask -TaskName $TaskName
    Start-Sleep -Seconds 8
    $info = Get-ScheduledTaskInfo -TaskName $TaskName
    Write-Host "Task started. LastTaskResult: $($info.LastTaskResult)"
    $statusPath = Join-Path $projectRoot "data\watchdog_v1_10_1\status.json"
    if (Test-Path $statusPath) {
        $status = Get-Content $statusPath -Raw | ConvertFrom-Json
        Write-Host "Unified status: $($status.overall)"
        Write-Host "ECN streams: $($status.ecn.fresh_streams)/$($status.ecn.expected_symbols)"
        Write-Host "Bybit symbols: $($status.bybit.observed_symbols)/$($status.bybit.expected_symbols)"
        Write-Host "Shadow state: $($status.shadow.state)"
        Write-Host "Shadow M5/M15/H1: $($status.shadow.source_m5_bars)/$($status.shadow.m15_bars)/$($status.shadow.h1_bars)"
        Write-Host "Shadow OrdersEnabled: $($status.shadow.orders_enabled)"
    }
}
