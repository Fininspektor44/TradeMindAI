param(
    [string]$TaskName = "TradeMindAI-v1.13-RiskPlanExperiments",
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
$runner = Join-Path $projectRoot "scripts\run_v113_risk_plan_experiments.ps1"
if (-not (Test-Path $runner)) {
    throw "Risk-plan runner not found: $runner"
}
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Run PowerShell as Administrator to install the risk-plan experiment task."
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
    -Description "TradeMind v1.13 forward-only read-only stop and target plan experiments" `
    -Force | Out-Null

Write-Host "Installed task: $TaskName"
Write-Host "Interval: every $IntervalMinutes minutes"
Write-Host "Arms: BASE_STRICT, WIDE15_R15, WIDE15_R20, WIDE20_R15, WIDE20_R20, STRUCTURE_R15, STRUCTURE_LIQ"
Write-Host "Entry logic: existing STRICT_SELL, unchanged"
Write-Host "Same money risk: wider stop means smaller theoretical size"
Write-Host "Read-only shadow experiment. No orders."

if ($RunNow) {
    Start-ScheduledTask -TaskName $TaskName
    Start-Sleep -Seconds 12
    $info = Get-ScheduledTaskInfo -TaskName $TaskName
    Write-Host "LastTaskResult: $($info.LastTaskResult)"
    $statusPath = Join-Path $projectRoot "data\bybit_risk_plans_v1_13\status.json"
    if (Test-Path $statusPath) {
        $status = Get-Content $statusPath -Raw | ConvertFrom-Json
        Write-Host "Experiment state: $($status.state)"
        Write-Host "Started: $($status.started_at)"
        foreach ($arm in @(
            "BASE_STRICT", "WIDE15_R15", "WIDE15_R20", "WIDE20_R15",
            "WIDE20_R20", "STRUCTURE_R15", "STRUCTURE_LIQ"
        )) {
            $item = $status.arms.$arm
            Write-Host "$arm signals/completed/gross/net: $($item.signals)/$($item.completed)/$([math]::Round([double]$item.gross_total_r,4))/$([math]::Round([double]$item.net_total_r,4))"
        }
        Write-Host "OrdersEnabled: $($status.orders_enabled)"
        Write-Host "LogicChanged: $($status.logic_changed)"
    }
}
