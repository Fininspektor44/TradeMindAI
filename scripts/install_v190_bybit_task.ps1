param(
    [string]$TaskName = "TradeMindAI-v1.9-Bybit",
    [string]$Symbols = "BTCUSDT,ETHUSDT,UNIUSDT,JTOUSDT,SOLUSDT,BZUSDT,NEARUSDT,AKEUSDT,ONDOUSDT,POPCATUSDT,XMRUSDT,MYXUSDT,AAVEUSDT,ZECUSDT,HYPEUSDT,LDOUSDT,PUMPFUNUSDT,GRASSUSDT,XAUTUSDT,1000PEPEUSDT",
    [ValidateRange(0.25, 24)]
    [double]$RefreshHours = 6,
    [switch]$RunNow
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
$outputDir = Join-Path $projectRoot "data\bybit_v1_9"

if (-not (Test-Path $python)) {
    throw "Python environment not found: $python"
}
New-Item -ItemType Directory -Path $outputDir -Force | Out-Null

# Run Python directly. The previous nested PowerShell runner could inherit a console
# control event and terminate with 0xC000013A even while status.json still said RUNNING.
$arguments = "-m trademind.bybit_fixed20 --output-dir `"$outputDir`" --symbols `"$Symbols`" --refresh-hours $RefreshHours"
$action = New-ScheduledTaskAction `
    -Execute $python `
    -Argument $arguments `
    -WorkingDirectory $projectRoot

$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 0)

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing -and $existing.State -eq "Running") {
    Stop-ScheduledTask -TaskName $TaskName
    Start-Sleep -Seconds 2
}

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Description "TradeMind v1.9.2 public read-only Bybit fixed 20-symbol intelligence" `
    -Force | Out-Null

$symbolCount = @($Symbols.Split(",") | Where-Object { $_.Trim() }).Count
Write-Host "Installed task: $TaskName"
Write-Host "Action: direct Python process"
Write-Host "Trigger: user logon"
Write-Host "Restart on failure: every 1 minute"
Write-Host "Universe size: $symbolCount"
Write-Host "Symbols: $Symbols"
Write-Host "Project: $projectRoot"
Write-Host "No API key, account access or order function is used."
if ($RunNow) {
    Start-ScheduledTask -TaskName $TaskName
    Write-Host "Task started."
}
