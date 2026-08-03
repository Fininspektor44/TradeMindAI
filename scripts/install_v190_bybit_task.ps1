param(
    [string]$TaskName = "TradeMindAI-v1.9-Bybit",
    [ValidateRange(2, 50)]
    [int]$TopN = 10,
    [double]$MinTurnover = 5000000,
    [ValidateRange(0.25, 24)]
    [double]$RefreshHours = 6,
    [switch]$RunNow
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$runner = Join-Path $projectRoot "scripts\run_v190_bybit.ps1"
if (-not (Test-Path $runner)) {
    throw "Runner not found: $runner"
}

$arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$runner`" -TopN $TopN -MinTurnover $MinTurnover -RefreshHours $RefreshHours"
$action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument $arguments `
    -WorkingDirectory $projectRoot

$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 0)

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Description "TradeMind v1.9 public read-only Bybit top-10 market intelligence" `
    -Force | Out-Null

Write-Host "Installed task: $TaskName"
Write-Host "Trigger: user logon"
Write-Host "Restart on failure: every 1 minute"
Write-Host "Universe size: $TopN"
Write-Host "Project: $projectRoot"
Write-Host "No API key, account access or order function is used."
if ($RunNow) {
    Start-ScheduledTask -TaskName $TaskName
    Write-Host "Task started."
}
