param(
    [string]$SourceDir = "$env:APPDATA\MetaQuotes\Terminal\Common\Files\TradeMindAI_Volume_v1_4",
    [string]$Volume = ".\data\volume_v1_4\volume_bars.csv",
    [string]$ResearchDir = ".\data\fx_research_v1_4_2",
    [string]$OutputDir = ".\data\watchdog_v1_4_4",
    [string[]]$TaskNames = @(
        "TradeMindAI-v1.4-VolumeCollector",
        "TradeMindAI-v1.4.2-FXResearch"
    ),
    [ValidateRange(5, 240)]
    [int]$SourceMaxAgeMinutes = 20,
    [ValidateRange(5, 240)]
    [int]$DerivedMaxAgeMinutes = 20,
    [switch]$NotifyOnError,
    [switch]$OpenReport
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $projectRoot

$outputPath = Join-Path $projectRoot $OutputDir
New-Item -ItemType Directory -Force -Path $outputPath | Out-Null
$taskSnapshot = Join-Path $outputPath "tasks.json"
$statusPath = Join-Path $outputPath "status.json"
$reportPath = Join-Path $outputPath "report.txt"
$htmlPath = Join-Path $outputPath "index.html"
$alertPath = Join-Path $outputPath "ALERT.txt"

$taskRows = @()
foreach ($taskName in $TaskNames) {
    try {
        $task = Get-ScheduledTask -TaskName $taskName -ErrorAction Stop
        $info = Get-ScheduledTaskInfo -TaskName $taskName -ErrorAction Stop
        $taskRows += [pscustomobject]@{
            name = $taskName
            exists = $true
            enabled = ($task.State.ToString() -ne "Disabled")
            state = $task.State.ToString()
            last_task_result = [int64]$info.LastTaskResult
            last_run_time = $info.LastRunTime.ToString("o")
            next_run_time = $info.NextRunTime.ToString("o")
        }
    }
    catch {
        $taskRows += [pscustomobject]@{
            name = $taskName
            exists = $false
            enabled = $false
            state = "MISSING"
            last_task_result = -1
            last_run_time = ""
            next_run_time = ""
            error = $_.Exception.Message
        }
    }
}
$taskRows | ConvertTo-Json -Depth 4 | Set-Content -Path $taskSnapshot -Encoding utf8

$observations = Join-Path $ResearchDir "observations.csv"
$states = Join-Path $ResearchDir "latest.csv"
$dashboard = Join-Path $ResearchDir "dashboard\index.html"

& ".\.venv\Scripts\trademind-watchdog.exe" `
    --source-dir $SourceDir `
    --volume $Volume `
    --observations $observations `
    --states $states `
    --dashboard $dashboard `
    --task-snapshot $taskSnapshot `
    --status $statusPath `
    --report $reportPath `
    --html $htmlPath `
    --source-max-age-minutes $SourceMaxAgeMinutes `
    --derived-max-age-minutes $DerivedMaxAgeMinutes
$watchdogExit = $LASTEXITCODE

if (Test-Path $statusPath) {
    $status = Get-Content $statusPath -Raw | ConvertFrom-Json
    if ($status.overall_status -eq "ERROR") {
        Copy-Item -Force $reportPath $alertPath
    }
    elseif (Test-Path $alertPath) {
        Remove-Item -Force $alertPath
    }

    if ($NotifyOnError -and $status.notify_required) {
        try {
            Add-Type -AssemblyName System.Windows.Forms
            Add-Type -AssemblyName System.Drawing
            $notify = New-Object System.Windows.Forms.NotifyIcon
            $notify.Icon = [System.Drawing.SystemIcons]::Error
            $notify.BalloonTipIcon = [System.Windows.Forms.ToolTipIcon]::Error
            $notify.BalloonTipTitle = "TradeMind Watchdog: ERROR"
            $failed = @($status.checks | Where-Object { $_.status -eq "ERROR" } | Select-Object -First 3)
            $notify.BalloonTipText = (($failed | ForEach-Object { $_.name + ": " + $_.message }) -join "`n")
            $notify.Visible = $true
            $notify.ShowBalloonTip(10000)
            Start-Sleep -Seconds 11
            $notify.Dispose()
        }
        catch {
            Write-Warning "Desktop notification failed: $($_.Exception.Message)"
        }
    }
}

if ($OpenReport -and (Test-Path $htmlPath)) {
    Start-Process $htmlPath
}
exit $watchdogExit
