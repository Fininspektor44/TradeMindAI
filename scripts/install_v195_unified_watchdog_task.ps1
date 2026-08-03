param(
    [string]$TaskName = "TradeMindAI-v1.9.5-UnifiedWatchdog",
    [ValidateRange(1, 60)]
    [int]$IntervalMinutes = 5,
    [switch]$RunNow
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$runner = Join-Path $projectRoot "scripts\run_v195_unified_watchdog.ps1"

if (-not (Test-Path $runner)) {
    throw "Unified watchdog runner not found: $runner"
}

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Run PowerShell as Administrator to install the watchdog task."
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
    -Description "TradeMind v1.9.5 read-only unified ECN and Bybit watchdog" `
    -Force | Out-Null

Write-Host "Installed task: $TaskName"
Write-Host "Action: hidden read-only ECN + Bybit health check"
Write-Host "Interval: every $IntervalMinutes minutes"
Write-Host "Project: $projectRoot"
Write-Host "The watchdog never sends trading orders and never changes MT5 positions."

if ($RunNow) {
    Start-ScheduledTask -TaskName $TaskName
    Start-Sleep -Seconds 8
    $info = Get-ScheduledTaskInfo -TaskName $TaskName
    Write-Host "Task started. LastTaskResult: $($info.LastTaskResult)"
    $statusPath = Join-Path $projectRoot "data\watchdog_v1_9_5\status.json"
    if (Test-Path $statusPath) {
        $status = Get-Content $statusPath -Raw | ConvertFrom-Json
        Write-Host "Unified status: $($status.overall)"
    }
}
