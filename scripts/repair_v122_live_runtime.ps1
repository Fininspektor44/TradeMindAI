param(
    [Parameter(Mandatory=$false)]
    [string]$Login = "37365712",

    [Parameter(Mandatory=$false)]
    [int]$ServerUTCOffsetHours = 3,

    [switch]$OpenDashboard
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo

$taskName = "TradeMindAI-v1.21-LiveSignalRuntime"
$runtimeRoot = Join-Path $repo "data\live_signal_runtime_v1"
$quarantineRoot = Join-Path $repo "data\quarantine"
$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$archivePath = Join-Path $quarantineRoot "live_signal_runtime_v1_contaminated_$stamp"
$task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
$taskWasEnabled = $false

try {
    if ($task) {
        $taskWasEnabled = $task.State -ne "Disabled"
        Disable-ScheduledTask -InputObject $task | Out-Null
        Stop-ScheduledTask -InputObject $task -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 2
    }

    if (Test-Path $runtimeRoot) {
        New-Item -ItemType Directory -Path $quarantineRoot -Force | Out-Null
        $moved = $false
        for ($attempt = 1; $attempt -le 10 -and -not $moved; $attempt++) {
            try {
                Move-Item -Path $runtimeRoot -Destination $archivePath -Force
                $moved = $true
            }
            catch {
                if ($attempt -eq 10) {
                    throw
                }
                Start-Sleep -Seconds 1
            }
        }
        Write-Host "Contaminated live archive quarantined:" -ForegroundColor Yellow
        Write-Host $archivePath -ForegroundColor Yellow
    }

    New-Item -ItemType Directory -Path $runtimeRoot -Force | Out-Null
    $runner = Join-Path $PSScriptRoot "run_v121_live_signal_runtime.ps1"
    $arguments = @(
        "-NoProfile",
        "-ExecutionPolicy", "Bypass",
        "-File", $runner,
        "-Login", $Login,
        "-ServerUTCOffsetHours", $ServerUTCOffsetHours
    )
    if ($OpenDashboard) {
        $arguments += "-OpenDashboard"
    }
    & powershell.exe @arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Clean v1.22.1 runtime bootstrap failed"
    }

    Write-Host "`nLive runtime repaired with strict per-symbol watermarks." -ForegroundColor Green
    Write-Host "Historical v1.16 archive was not changed." -ForegroundColor Green
    Write-Host "Orders OFF. Publication OFF. Broker API not called." -ForegroundColor Green
}
finally {
    if ($task -and $taskWasEnabled) {
        Enable-ScheduledTask -InputObject $task | Out-Null
    }
}
