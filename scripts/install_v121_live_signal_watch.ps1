param(
    [Parameter(Mandatory=$false)]
    [string]$TaskName = "TradeMindAI-v1.21-LiveSignalRuntime",

    [Parameter(Mandatory=$false)]
    [int]$IntervalMinutes = 1,

    [Parameter(Mandatory=$false)]
    [string]$Login = "37365712",

    [Parameter(Mandatory=$false)]
    [int]$ServerUTCOffsetHours = 3,

    [switch]$Remove
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo

if ($Remove) {
    & schtasks.exe /Delete /TN $TaskName /F | Out-Host
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to remove scheduled task: $TaskName"
    }
    Write-Host "Removed scheduled task: $TaskName" -ForegroundColor Green
    exit 0
}

if ($IntervalMinutes -lt 1) {
    throw "IntervalMinutes must be at least 1"
}
if ($ServerUTCOffsetHours -lt -14 -or $ServerUTCOffsetHours -gt 14) {
    throw "ServerUTCOffsetHours must be between -14 and 14"
}

$watchScript = Join-Path $PSScriptRoot "run_v121_live_signal_watch.ps1"
if (-not (Test-Path $watchScript)) {
    throw "Watch script not found: $watchScript"
}

$powershell = Join-Path $PSHOME "powershell.exe"
$taskCommand = "`"$powershell`" -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$watchScript`" -Login `"$Login`" -ServerUTCOffsetHours $ServerUTCOffsetHours"

Write-Host "Installing scheduled task: $TaskName" -ForegroundColor Cyan
Write-Host "Interval: every $IntervalMinutes minute(s)"
Write-Host "Account: $Login"
Write-Host "Broker server UTC offset: $ServerUTCOffsetHours"

& schtasks.exe /Create `
    /TN $TaskName `
    /TR $taskCommand `
    /SC MINUTE `
    /MO $IntervalMinutes `
    /RL HIGHEST `
    /F | Out-Host

if ($LASTEXITCODE -ne 0) {
    throw "Failed to create scheduled task: $TaskName"
}

& schtasks.exe /Run /TN $TaskName | Out-Host
if ($LASTEXITCODE -ne 0) {
    throw "Scheduled task was installed but could not be started: $TaskName"
}

Write-Host "`nInstalled: $TaskName" -ForegroundColor Green
Write-Host "The runtime checks every $IntervalMinutes minute(s), but processes only a new closed M5 bar."
Write-Host "Status: $(Join-Path $repo 'data\live_signal_runtime_v1\status.json')"
Write-Host "Log: $(Join-Path $repo 'data\live_signal_runtime_v1\logs\live_signal_watch.log')"
Write-Host "Read-only. Orders OFF. Publication OFF." -ForegroundColor Green
