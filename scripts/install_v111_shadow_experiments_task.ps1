param(
    [string]$TaskName = "TradeMindAI-v1.11-ShadowExperiments",
    [ValidateRange(1, 60)]
    [int]$IntervalMinutes = 5,
    [switch]$RunNow
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$runner = Join-Path $projectRoot "scripts\run_v111_shadow_experiments.ps1"

if (-not (Test-Path $runner)) {
    throw "Experiment runner not found: $runner"
}
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Run PowerShell as Administrator to install the experiment task."
}

$arguments = "-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$runner`" -ProjectRoot `"$projectRoot`""
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $arguments -WorkingDirectory $projectRoot
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes)
$settings = New-ScheduledTaskSettingsSet `
    -Hidden `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 3)

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Description "TradeMind v1.11 equal-start CONTROL vs BUY_ONLY vs STRICT_SELL shadow experiment" `
    -Force | Out-Null

Write-Host "Installed task: $TaskName"
Write-Host "Interval: every $IntervalMinutes minutes"
Write-Host "Arms: CONTROL + BUY_ONLY + STRICT_SELL"
Write-Host "The existing v1.10 Shadow task and journal are not changed."
Write-Host "Forward-only and read-only. No orders."

if ($RunNow) {
    Start-ScheduledTask -TaskName $TaskName
    Start-Sleep -Seconds 12
    $info = Get-ScheduledTaskInfo -TaskName $TaskName
    Write-Host "LastTaskResult: $($info.LastTaskResult)"
    $statusPath = Join-Path $projectRoot "data\bybit_shadow_v1_11\status.json"
    if (Test-Path $statusPath) {
        $status = Get-Content $statusPath -Raw | ConvertFrom-Json
        Write-Host "Experiment state: $($status.state)"
        Write-Host "Equal-start: $($status.started_at)"
        foreach ($arm in @("CONTROL", "BUY_ONLY", "STRICT_SELL")) {
            $item = $status.arms.$arm
            Write-Host "$arm signals/completed: $($item.signals)/$($item.completed)"
        }
        Write-Host "OrdersEnabled: $($status.orders_enabled)"
    }
}
