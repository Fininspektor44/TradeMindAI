param(
    [string]$TaskName = "TradeMindAI-v1.14-PositionManagement",
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
$runner = Join-Path $projectRoot "scripts\run_v114_position_management.ps1"
if (-not (Test-Path $runner)) {
    throw "Position-management runner not found: $runner"
}
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Run PowerShell as Administrator to install the position-management task."
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
    -Description "TradeMind v1.14 forward-only read-only partial BE and trail experiments" `
    -Force | Out-Null

Write-Host "Installed task: $TaskName"
Write-Host "Interval: every $IntervalMinutes minutes"
Write-Host "Risk plans: BASE_STRICT, WIDE20_R15"
Write-Host "Management: FULL_TP, PART50_BE, PART50_RUNNER, BE_TRAIL, PART_TRAIL, THREE_STAGE"
Write-Host "Read-only shadow experiment. Source journals unchanged. No orders."

if ($RunNow) {
    Start-ScheduledTask -TaskName $TaskName
    Start-Sleep -Seconds 12
    $info = Get-ScheduledTaskInfo -TaskName $TaskName
    Write-Host "LastTaskResult: $($info.LastTaskResult)"
    $statusPath = Join-Path $projectRoot "data\bybit_position_management_v1_14\status.json"
    if (Test-Path $statusPath) {
        $status = Get-Content $statusPath -Raw | ConvertFrom-Json
        Write-Host "Experiment state: $($status.state)"
        Write-Host "Mode: $($status.mode)"
        Write-Host "Started: $($status.cutoff)"
        Write-Host "Source candidates: $($status.source_candidates)"
        Write-Host "Arms: $(@($status.arms.PSObject.Properties).Count)"
        Write-Host "OrdersEnabled: $($status.orders_enabled)"
        Write-Host "LogicChanged: $($status.logic_changed)"
        Write-Host "SourceJournalsModified: $($status.source_journals_modified)"
    }
}
