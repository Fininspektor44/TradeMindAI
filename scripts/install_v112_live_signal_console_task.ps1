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
$windowsPrincipal = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $windowsPrincipal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Run PowerShell as Administrator to install the Live Signal Console task."
}

$powerShellExe = Join-Path $PSHOME "powershell.exe"
$arguments = "-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$runner`" -HostAddress `"$HostAddress`" -Port $Port"
$action = New-ScheduledTaskAction `
    -Execute $powerShellExe `
    -Argument $arguments `
    -WorkingDirectory $projectRoot
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $identity.Name
$taskPrincipal = New-ScheduledTaskPrincipal `
    -UserId $identity.Name `
    -LogonType Interactive `
    -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet `
    -Hidden `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit ([TimeSpan]::Zero)

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}
Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Principal $taskPrincipal `
    -Settings $settings `
    -Description "TradeMind v1.12 local read-only MT5 and Bybit live signal console" `
    -Force | Out-Null

Write-Host "Installed task: $TaskName"
Write-Host "Account: $($identity.Name)"
Write-Host "Logon type: Interactive"
Write-Host "Address: http://${HostAddress}:$Port"
Write-Host "Starts at user logon and runs hidden."
Write-Host "Read-only. OrdersEnabled=False."

if ($RunNow) {
    $listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    foreach ($connection in $listener) {
        Stop-Process -Id $connection.OwningProcess -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep -Seconds 2
    Start-ScheduledTask -TaskName $TaskName
    Start-Sleep -Seconds 5
    $info = Get-ScheduledTaskInfo -TaskName $TaskName
    $task = Get-ScheduledTask -TaskName $TaskName
    Write-Host "TaskState: $($task.State)"
    Write-Host "LastTaskResult: $($info.LastTaskResult)"
    Write-Host "Open: http://${HostAddress}:$Port"
    if ($task.State -ne "Running") {
        throw "Live Signal Console task did not enter Running state."
    }
}
