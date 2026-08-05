param(
    [Parameter(Mandatory=$false)]
    [long]$AOAccount = 37365712,

    [Parameter(Mandatory=$false)]
    [long]$MultiAccount = 37353316,

    [Parameter(Mandatory=$false)]
    [string[]]$MultiMagic = @("8035", "8"),

    [Parameter(Mandatory=$false)]
    [int]$StaleMinutes = 15,

    [Parameter(Mandatory=$false)]
    [int]$LegWarning = 6,

    [Parameter(Mandatory=$false)]
    [int]$AgeWarningHours = 72,

    [Parameter(Mandatory=$false)]
    [int]$AgeCriticalHours = 168
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo

$logDir = Join-Path $repo "data\control_center_v1_15\logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$logFile = Join-Path $logDir "control_center_watch.log"
$mutex = New-Object System.Threading.Mutex($false, "Global\TradeMindAI_ControlCenter_v115")

if (-not $mutex.WaitOne(0)) {
    $line = "$(Get-Date -Format o) Previous Control Center run is still active. Skipped."
    Add-Content -Path $logFile -Value $line -Encoding UTF8
    Write-Host $line
    $mutex.Dispose()
    exit 0
}

try {
    $started = Get-Date
    Add-Content -Path $logFile -Value "`n=== $($started.ToString('o')) RUN START ===" -Encoding UTF8

    $runner = Join-Path $PSScriptRoot "run_v115_robot_control_center.ps1"
    & $runner `
        -AOAccount $AOAccount `
        -MultiAccount $MultiAccount `
        -MultiMagic $MultiMagic *>&1 |
        Tee-Object -FilePath $logFile -Append

    $python = Join-Path $repo ".venv\Scripts\python.exe"
    if (!(Test-Path $python)) {
        throw "Python environment not found: $python"
    }

    & $python -m trademind.control_center_watchdog `
        --control-status (Join-Path $repo "data\control_center_v1_15\status.json") `
        --stale-minutes $StaleMinutes `
        --leg-warning $LegWarning `
        --age-warning-hours $AgeWarningHours `
        --age-critical-hours $AgeCriticalHours *>&1 |
        Tee-Object -FilePath $logFile -Append

    if ($LASTEXITCODE -ne 0) {
        throw "TradeMind Control Center watchdog failed"
    }

    $finished = Get-Date
    $elapsed = [math]::Round(($finished - $started).TotalSeconds, 1)
    Add-Content -Path $logFile -Value "=== $($finished.ToString('o')) RUN OK ${elapsed}s ===" -Encoding UTF8
}
catch {
    $message = "$(Get-Date -Format o) RUN FAILED: $($_.Exception.Message)"
    Add-Content -Path $logFile -Value $message -Encoding UTF8
    Write-Error $message
    exit 1
}
finally {
    try { $mutex.ReleaseMutex() } catch { }
    $mutex.Dispose()
}
