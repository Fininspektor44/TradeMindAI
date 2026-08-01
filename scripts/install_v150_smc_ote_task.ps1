param(
    [string]$TaskName = "TradeMindAI-v1.5-SMC-OTE",
    [ValidateRange(5, 60)]
    [int]$EveryMinutes = 5,
    [ValidateRange(-14, 14)]
    [int]$ServerUtcOffsetHours = 3,
    [switch]$RunNow
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$runner = Join-Path $projectRoot "scripts\run_v150_smc_ote.ps1"
if (-not (Test-Path $runner)) {
    throw "Runner not found: $runner"
}

$arguments = (
    "-NoProfile -ExecutionPolicy Bypass -File `"$runner`" " +
    "-ServerUtcOffsetHours $ServerUtcOffsetHours"
)
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
    -Description "TradeMind v1.5 read-only SMC and Fibonacci OTE shadow research" `
    -Force | Out-Null

Write-Host "Installed task: $TaskName"
Write-Host "Interval: $EveryMinutes minutes"
Write-Host "Server UTC offset: $ServerUtcOffsetHours hours"
Write-Host "Project: $projectRoot"
Write-Host "No MT5 chart or order function is used by this task."
if ($RunNow) {
    Start-ScheduledTask -TaskName $TaskName
    Write-Host "Task started."
}
