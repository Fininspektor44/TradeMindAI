param(
    [string]$TaskName = "TradeMindAI-v1.12-LiveSignalConsole",
    [string]$HostAddress = "127.0.0.1",
    [ValidateRange(1024, 65535)]
    [int]$Port = 8765,
    [switch]$RunNow
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$runner = Join-Path $projectRoot "scripts\run_v112_live_signal_console.ps1"
if (-not (Test-Path $runner)) {
    throw "Live console runner not found: $runner"
}

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Run PowerShell as Administrator to install the Live Signal Console task."
}

$arguments = "-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$runner`" -HostAddress `"$HostAddress`" -Port $Port"
$action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument $arguments `
    -WorkingDirectory $projectRoot
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet `
    -Hidden `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit ([TimeSpan]::Zero)

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Description "TradeMind v1.12 local read-only MT5 and Bybit live signal console" `
    -Force | Out-Null

Write-Host "Installed task: $TaskName"
Write-Host "Address: http://${HostAddress}:$Port"
Write-Host "Starts at user logon and runs hidden."
Write-Host "Read-only. OrdersEnabled=False."

if ($RunNow) {
    $existing = Get-ScheduledTask -TaskName $TaskName
    if ($existing.State -eq "Running") {
        Stop-ScheduledTask -TaskName $TaskName
        Start-Sleep -Seconds 2
    }
    Start-ScheduledTask -TaskName $TaskName
    Start-Sleep -Seconds 4
    $info = Get-ScheduledTaskInfo -TaskName $TaskName
    Write-Host "LastTaskResult: $($info.LastTaskResult)"
    Write-Host "Open: http://${HostAddress}:$Port"
}
