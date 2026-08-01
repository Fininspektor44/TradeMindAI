param(
    [string]$TaskName = "TradeMindAI-v1.6-UnifiedCenter",
    [ValidateRange(5, 60)]
    [int]$EveryMinutes = 5,
    [switch]$RunNow
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$runner = Join-Path $projectRoot "scripts\run_v160_unified_center.ps1"
if (-not (Test-Path $runner)) {
    throw "Runner not found: $runner"
}

$arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$runner`""
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
    -Description "TradeMind v1.6 read-only unified signal center" `
    -Force | Out-Null

Write-Host "Installed task: $TaskName"
Write-Host "Interval: $EveryMinutes minutes"
Write-Host "Project: $projectRoot"
Write-Host "Reads FX Research and SMC OTE outputs. No order function is used."
if ($RunNow) {
    Start-ScheduledTask -TaskName $TaskName
    Write-Host "Task started."
}
