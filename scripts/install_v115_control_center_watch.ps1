param(
    [Parameter(Mandatory=$false)]
    [string]$TaskName = "TradeMindAI-v1.15-ControlCenter",

    [Parameter(Mandatory=$false)]
    [int]$IntervalMinutes = 5,

    [Parameter(Mandatory=$false)]
    [long]$AOAccount = 37365712,

    [Parameter(Mandatory=$false)]
    [long]$MultiAccount = 37353316,

    [Parameter(Mandatory=$false)]
    [string[]]$MultiMagic = @("8035", "8"),

    [switch]$OpenDashboard,

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

$watchScript = Join-Path $PSScriptRoot "run_v115_control_center_watch.ps1"
if (!(Test-Path $watchScript)) {
    throw "Watch script not found: $watchScript"
}

$powershell = Join-Path $PSHOME "powershell.exe"
$magicArgument = ($MultiMagic -join ",")
$taskCommand = "`"$powershell`" -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$watchScript`" -AOAccount $AOAccount -MultiAccount $MultiAccount -MultiMagic $magicArgument"

Write-Host "Installing scheduled task: $TaskName" -ForegroundColor Cyan
Write-Host "Interval: every $IntervalMinutes minutes"
Write-Host "Command: $taskCommand"

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

$dashboard = Join-Path $repo "data\control_center_v1_15\dashboard\index.html"
if ($OpenDashboard) {
    $deadline = (Get-Date).AddMinutes(3)
    while (!(Test-Path $dashboard) -and (Get-Date) -lt $deadline) {
        Start-Sleep -Seconds 2
    }
    if (Test-Path $dashboard) {
        Start-Process $dashboard
    }
    else {
        Write-Warning "Dashboard was not created before timeout: $dashboard"
    }
}

Write-Host "`nInstalled: $TaskName" -ForegroundColor Green
Write-Host "The Control Center will rebuild every $IntervalMinutes minutes."
Write-Host "Dashboard: $dashboard"
Write-Host "Log: $(Join-Path $repo 'data\control_center_v1_15\logs\control_center_watch.log')"
Write-Host "Read-only: no order sending and no strategy-setting changes." -ForegroundColor Green
