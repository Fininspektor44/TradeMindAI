param(
    [string]$TaskName = "TradeMindAI Daily Research",
    [string]$DailyTime = "23:55",
    [switch]$RunNow
)

$ErrorActionPreference = "Stop"
$projectPath = (Resolve-Path (Split-Path -Parent $PSScriptRoot)).Path
$reportScript = Join-Path $projectPath "scripts\generate_daily_research_report.ps1"

if (-not (Test-Path $reportScript -PathType Leaf)) {
    throw "Daily report script not found: $reportScript"
}

try {
    $triggerTime = [datetime]::ParseExact(
        $DailyTime,
        "HH:mm",
        [System.Globalization.CultureInfo]::InvariantCulture
    )
}
catch {
    throw "DailyTime must use HH:mm format, for example 23:55."
}

$powershell = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
$arguments = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$reportScript`""
$action = New-ScheduledTaskAction `
    -Execute $powershell `
    -Argument $arguments `
    -WorkingDirectory $projectPath
$trigger = New-ScheduledTaskTrigger -Daily -At $triggerTime
$currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$principal = New-ScheduledTaskPrincipal `
    -UserId $currentUser `
    -LogonType Interactive `
    -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Principal $principal `
    -Settings $settings `
    -Description "Generate TradeMind ECN data-health and SMC research reports" `
    -Force | Out-Null

Write-Host "Scheduled task installed: $TaskName"
Write-Host "Daily time: $DailyTime"
Write-Host "Script: $reportScript"

if ($RunNow) {
    Start-ScheduledTask -TaskName $TaskName
    Write-Host "Scheduled task started for immediate validation."
}
