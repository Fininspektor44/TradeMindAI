param(
    [string]$TaskName = "TradeMindAI-v1.12-LiveSignalConsole",
    [string]$HostAddress = "127.0.0.1",
    [ValidateRange(1024, 65535)]
    [int]$Port = 8765,
    [switch]$RunNow
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$hiddenRunner = Join-Path $projectRoot "scripts\run_v112_live_signal_console_hidden.pyw"
$pythonwExe = Join-Path $projectRoot ".venv\Scripts\pythonw.exe"
$logPath = Join-Path $projectRoot "logs\live_signal_console_v1_12.log"

if (-not (Test-Path $hiddenRunner)) {
    throw "Windowless Live Signal Console runner not found: $hiddenRunner"
}
if (-not (Test-Path $pythonwExe)) {
    throw "pythonw.exe not found in the project virtual environment: $pythonwExe"
}

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$windowsPrincipal = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $windowsPrincipal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Run PowerShell as Administrator to install the Live Signal Console task."
}

$arguments = "`"$hiddenRunner`" --host `"$HostAddress`" --port $Port"
$action = New-ScheduledTaskAction `
    -Execute $pythonwExe `
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
Write-Host "Engine: pythonw.exe (no console window)"
Write-Host "Address: http://${HostAddress}:$Port"
Write-Host "Log: $logPath"
Write-Host "Starts at user logon and runs without a visible terminal."
Write-Host "Read-only. OrdersEnabled=False."

if ($RunNow) {
    $listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    foreach ($connection in $listener) {
        Stop-Process -Id $connection.OwningProcess -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep -Seconds 2
    Start-ScheduledTask -TaskName $TaskName
    Start-Sleep -Seconds 8
    $info = Get-ScheduledTaskInfo -TaskName $TaskName
    $task = Get-ScheduledTask -TaskName $TaskName
    Write-Host "TaskState: $($task.State)"
    Write-Host "LastTaskResult: $($info.LastTaskResult)"
    Write-Host "Open: http://${HostAddress}:$Port"
    if ($task.State -ne "Running") {
        if (Test-Path $logPath) {
            Write-Host "Last log lines:"
            Get-Content $logPath -Tail 20
        }
        throw "Live Signal Console task did not enter Running state."
    }
}
