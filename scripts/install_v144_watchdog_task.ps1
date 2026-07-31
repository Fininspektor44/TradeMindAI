param(
    [string]$TaskName = "TradeMindAI-v1.4.4-Watchdog",
    [ValidateRange(5, 60)]
    [int]$EveryMinutes = 15,
    [switch]$RunNow
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$runner = Join-Path $projectRoot "scripts\run_v144_watchdog.ps1"
if (-not (Test-Path $runner)) {
    throw "Runner not found: $runner"
}

$arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$runner`" -NotifyOnError"
$action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument $arguments `
    -WorkingDirectory $projectRoot

$trigger = New-ScheduledTaskTrigger `
    -Once `
    -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes $EveryMinutes) `
    -RepetitionDuration (New-TimeSpan -Days 3650)

$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Description "TradeMind v1.4.4 read-only 24/7 pipeline watchdog" `
    -Force | Out-Null

Write-Host "Installed task: $TaskName"
Write-Host "Interval: $EveryMinutes minutes"
Write-Host "Project: $projectRoot"
Write-Host "Checks MT5 streams, collectors, research files and dashboard."
Write-Host "No orders can be sent by this watchdog."
if ($RunNow) {
    Start-ScheduledTask -TaskName $TaskName
    Write-Host "Task started."
}
