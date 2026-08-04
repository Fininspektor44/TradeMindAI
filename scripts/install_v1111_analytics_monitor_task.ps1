param(
    [string]$TaskName = "TradeMindAI-v1.11.1-AnalyticsMonitor",
    [ValidateRange(1, 60)]
    [int]$IntervalMinutes = 5,
    [ValidateRange(0, 100)]
    [double]$FeeBpsPerSide = 5.5,
    [ValidateRange(0, 100)]
    [double]$SlippageBpsPerSide = 1.0,
    [switch]$RunNow
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$runner = Join-Path $projectRoot "scripts\run_v1111_analytics_monitor.ps1"
if (-not (Test-Path $runner)) {
    throw "Analytics runner not found: $runner"
}
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Run PowerShell as Administrator to install the analytics task."
}
$feeInvariant = $FeeBpsPerSide.ToString([Globalization.CultureInfo]::InvariantCulture)
$slippageInvariant = $SlippageBpsPerSide.ToString([Globalization.CultureInfo]::InvariantCulture)

$arguments = "-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$runner`" -ProjectRoot `"$projectRoot`" -FeeBpsPerSide $feeInvariant -SlippageBpsPerSide $slippageInvariant"
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
    -Description "TradeMind v1.11.1 read-only analytics monitor for v1.11 shadow experiments" `
    -Force | Out-Null

Write-Host "Installed task: $TaskName"
Write-Host "Interval: every $IntervalMinutes minutes"
Write-Host "Cost model: fee=$feeInvariant bps/side, slippage=$slippageInvariant bps/side, observed entry spread included"
Write-Host "The CONTROL, BUY_ONLY and STRICT_SELL logic is not changed."
Write-Host "Read-only analytics. No orders."

if ($RunNow) {
    Start-ScheduledTask -TaskName $TaskName
    Start-Sleep -Seconds 12
    $info = Get-ScheduledTaskInfo -TaskName $TaskName
    Write-Host "LastTaskResult: $($info.LastTaskResult)"
    $statusPath = Join-Path $projectRoot "data\bybit_shadow_monitor_v1_11_1\status.json"
    if (Test-Path $statusPath) {
        $status = Get-Content $statusPath -Raw | ConvertFrom-Json
        Write-Host "Monitor state: $($status.state)"
        foreach ($arm in @("CONTROL", "BUY_ONLY", "STRICT_SELL")) {
            $item = $status.arms.$arm
            Write-Host "$arm completed/gross/net: $($item.completed)/$([math]::Round([double]$item.gross_total_r,4))/$([math]::Round([double]$item.net_total_r,4))"
        }
        Write-Host "OrdersEnabled: $($status.orders_enabled)"
        Write-Host "LogicChanged: $($status.logic_changed)"
    }
}
