param(
    [Parameter(Mandatory=$false)]
    [string]$Login = "77053345",

    [Parameter(Mandatory=$false)]
    [int]$ServerUTCOffsetHours = 3,

    [Parameter(Mandatory=$false)]
    [string]$TaskName = "TradeMindAI-v1.32-ECN-LiveSignalRuntime",

    [Parameter(Mandatory=$false)]
    [string]$LegacyTaskName = "TradeMindAI-v1.21-LiveSignalRuntime"
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo

$watcher = Join-Path $PSScriptRoot "run_v121_live_signal_watch.ps1"
if (-not (Test-Path $watcher)) {
    throw "Watcher not found: $watcher"
}

$runtimeRoot = Join-Path $repo "data\live_signal_runtime_ecN_$Login"
New-Item -ItemType Directory -Force -Path $runtimeRoot | Out-Null

$arguments = "-WindowStyle Hidden -NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$watcher`" -Login `"$Login`" -ServerUTCOffsetHours $ServerUTCOffsetHours -RuntimeRoot `"$runtimeRoot`""
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $arguments -WorkingDirectory $repo
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 1)
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Description "TradeMind v1.32 ECN read-only live signal runtime for account $Login" `
    -Force | Out-Null

if ($LegacyTaskName) {
    $legacy = Get-ScheduledTask -TaskName $LegacyTaskName -ErrorAction SilentlyContinue
    if ($legacy) {
        Stop-ScheduledTask -TaskName $LegacyTaskName -ErrorAction SilentlyContinue
        Disable-ScheduledTask -TaskName $LegacyTaskName | Out-Null
        Write-Host "Legacy task disabled: $LegacyTaskName" -ForegroundColor Yellow
    }
}

Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 3

Write-Host "Installed: $TaskName" -ForegroundColor Green
Write-Host "Login: $Login" -ForegroundColor Green
Write-Host "Runtime root: $runtimeRoot" -ForegroundColor Green
Write-Host "Interval: 1 minute" -ForegroundColor Green
Write-Host "READ-ONLY. Orders OFF. Publication OFF." -ForegroundColor Green
Get-ScheduledTask -TaskName $TaskName,$LegacyTaskName -ErrorAction SilentlyContinue | Select-Object TaskName,State
