param(
    [string]$TaskName = "TradeMindAI-v1.10.2-UnifiedWatchdog",
    [ValidateRange(1, 60)]
    [int]$IntervalMinutes = 5,
    [switch]$RunNow
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$runner = Join-Path $projectRoot "scripts\run_v1102_unified_watchdog.ps1"
$oldTaskNames = @(
    "TradeMindAI-v1.9.5-UnifiedWatchdog",
    "TradeMindAI-v1.10.1-UnifiedWatchdog"
)

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
    -Description "TradeMind v1.10.2 read-only watchdog with verified Bybit reconnect grace" `
    -Force | Out-Null

foreach ($oldTaskName in $oldTaskNames) {
    if ($oldTaskName -eq $TaskName) { continue }
    $oldTask = Get-ScheduledTask -TaskName $oldTaskName -ErrorAction SilentlyContinue
    if ($oldTask) {
        Unregister-ScheduledTask -TaskName $oldTaskName -Confirm:$false
        Write-Host "Removed superseded task: $oldTaskName"
    }
}

Write-Host "Installed task: $TaskName"
Write-Host "Interval: every $IntervalMinutes minutes"
Write-Host "Mode: ECN + Bybit + Shadow, verified reconnect grace 180 seconds"
Write-Host "Read-only. No trading orders and no MT5 position changes."

if ($RunNow) {
    Start-ScheduledTask -TaskName $TaskName
    Start-Sleep -Seconds 10
    $info = Get-ScheduledTaskInfo -TaskName $TaskName
    Write-Host "LastTaskResult: $($info.LastTaskResult)"
    $statusPath = Join-Path $projectRoot "data\watchdog_v1_10_1\status.json"
    if (Test-Path $statusPath) {
        $status = Get-Content $statusPath -Raw | ConvertFrom-Json
        Write-Host "Unified status: $($status.overall)"
        Write-Host "Bybit state: $($status.bybit.state)"
        Write-Host "Reconnect grace active: $($status.bybit.reconnect_grace_active)"
        Write-Host "Bybit last event age: $($status.bybit.last_event_age_seconds)"
        Write-Host "Shadow signals/completed: $($status.shadow.paper_signals)/$($status.shadow.completed_signals)"
    }
}
