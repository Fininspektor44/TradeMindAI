param(
    [string]$TaskName = "TradeMindAI-v1.4.2-FXResearch",
    [ValidateRange(5, 60)]
    [int]$EveryMinutes = 5,
    [ValidateRange(-14, 14)]
    [int]$ServerUtcOffsetHours = 0,
    [switch]$RunNow
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$runner = Join-Path $projectRoot "scripts\run_v142_fx_research.ps1"
if (-not (Test-Path $runner)) {
    throw "Runner not found: $runner"
}

$arguments =
    "-NoProfile -ExecutionPolicy Bypass -File `"$runner`" " +
    "-ServerUtcOffsetHours $ServerUtcOffsetHours"
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
    -Description "TradeMind v1.4.2 read-only FX SMC and volume research" `
    -Force | Out-Null

Write-Host "Installed task: $TaskName"
Write-Host "Interval: $EveryMinutes minutes"
Write-Host "Server UTC offset: $ServerUtcOffsetHours hours"
Write-Host "Project: $projectRoot"
Write-Host "No orders can be sent by this research task."
if ($RunNow) {
    Start-ScheduledTask -TaskName $TaskName
    Write-Host "Task started."
}
