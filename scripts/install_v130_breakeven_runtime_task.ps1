param(
    [Parameter(Mandatory=$true)]
    [string]$Login,

    [Parameter(Mandatory=$false)]
    [string]$TaskName = "TradeMindAI-v1.30-BreakEvenRuntime",

    [Parameter(Mandatory=$false)]
    [string]$LegacyTaskName = "TradeMindAI-v1.28-BreakEvenStats"
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
$runner = Join-Path $PSScriptRoot "run_v130_breakeven_runtime.ps1"
if (-not (Test-Path $runner -PathType Leaf)) {
    throw "Runtime runner not found: $runner"
}

# Verify the complete read-only pipeline before changing Task Scheduler.
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File $runner -Login $Login
if ($LASTEXITCODE -ne 0) {
    throw "Initial v1.30 runtime verification failed. Legacy task was not changed."
}

$argument = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$runner`" -Login `"$Login`""
$action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument $argument `
    -WorkingDirectory $repo
$trigger = New-ScheduledTaskTrigger `
    -Once `
    -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes 1)

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Description "TradeMind read-only BE shadow statistics and counterfactual runtime" `
    -Force | Out-Null

$legacy = Get-ScheduledTask -TaskName $LegacyTaskName -ErrorAction SilentlyContinue
if ($null -ne $legacy -and $legacy.State -ne "Disabled") {
    Disable-ScheduledTask -TaskName $LegacyTaskName | Out-Null
    Write-Host "Legacy task disabled: $LegacyTaskName" -ForegroundColor Yellow
}

Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 3
$info = Get-ScheduledTask -TaskName $TaskName | Get-ScheduledTaskInfo
$info | Select-Object LastRunTime,NextRunTime,LastTaskResult,NumberOfMissedRuns

Write-Host "`nInstalled: $TaskName" -ForegroundColor Green
Write-Host "Interval: 1 minute" -ForegroundColor Green
Write-Host "READ-ONLY. No order or robot/exporter modification." -ForegroundColor Green
