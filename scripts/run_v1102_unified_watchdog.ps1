param(
    [string]$BybitTaskName = "TradeMindAI-v1.9-Bybit",
    [ValidateRange(60, 1800)]
    [int]$BybitFreshSeconds = 120,
    [ValidateRange(30, 900)]
    [int]$ReconnectGraceSeconds = 180
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$legacyRunner = Join-Path $projectRoot "scripts\run_v1101_unified_watchdog.ps1"
$statusPath = Join-Path $projectRoot "data\watchdog_v1_10_1\status.json"
$reportPath = Join-Path $projectRoot "data\watchdog_v1_10_1\report.txt"
$bybitDir = Join-Path $projectRoot "data\bybit_v1_9"
$bybitStatusPath = Join-Path $bybitDir "status.json"
$bybitLatestPath = Join-Path $bybitDir "latest.csv"
$expectedBybitCount = 20
$nowUtc = [DateTimeOffset]::UtcNow

if (-not (Test-Path $legacyRunner)) {
    throw "Base unified watchdog runner not found: $legacyRunner"
}

# Run the complete ECN + Bybit + Shadow audit in an isolated PowerShell process.
# The v1.10.1 runner uses exit codes, so isolation guarantees this wrapper can
# inspect a failed snapshot and distinguish a short reconnect from a real fault.
$legacyOutput = @(
    & powershell.exe `
        -NoProfile `
        -NonInteractive `
        -ExecutionPolicy Bypass `
        -File $legacyRunner 2>&1
)
$legacyExitCode = $LASTEXITCODE
if (-not (Test-Path $statusPath)) {
    $legacyOutput | ForEach-Object { Write-Host $_ }
    throw "Unified watchdog status not found: $statusPath"
}
$status = Get-Content $statusPath -Raw | ConvertFrom-Json

$bybitState = $null
$bybitStatusAge = $null
$lastEventAge = $null
$reconnectGraceActive = $false
$reconnectEvidence = $null

if (Test-Path $bybitStatusPath) {
    try {
        $rawBybit = Get-Content $bybitStatusPath -Raw | ConvertFrom-Json
        $bybitState = [string]$rawBybit.state
        $updatedAt = [DateTimeOffset]::Parse([string]$rawBybit.updated_at).ToUniversalTime()
        $bybitStatusAge = [math]::Round(($nowUtc - $updatedAt).TotalSeconds, 1)
        if (-not [string]::IsNullOrWhiteSpace([string]$rawBybit.last_event_at)) {
            $lastEventAt = [DateTimeOffset]::Parse([string]$rawBybit.last_event_at).ToUniversalTime()
            $lastEventAge = [math]::Round(($nowUtc - $lastEventAt).TotalSeconds, 1)
        }

        $processes = @(
            Get-CimInstance Win32_Process |
                Where-Object {
                    $_.Name -in @("python.exe", "pythonw.exe") -and
                    $_.CommandLine -match '(?i)(^|\s)-m\s+trademind\.bybit_fixed20(\s|$)'
                }
        )
        $matchingPids = @{}
        foreach ($process in $processes) {
            $matchingPids[[int]$process.ProcessId] = $true
        }
        $collectorRoots = @($processes | Where-Object {
            -not $matchingPids.ContainsKey([int]$_.ParentProcessId)
        })
        $bybitTask = Get-ScheduledTask -TaskName $BybitTaskName -ErrorAction SilentlyContinue
        $runtimeHealthy = (
            $bybitTask -and
            $bybitTask.State -eq "Running" -and
            $collectorRoots.Count -eq 1 -and
            $processes.Count -ge 1
        )

        $uniqueSymbols = @()
        if (Test-Path $bybitLatestPath) {
            $uniqueSymbols = @(
                Import-Csv $bybitLatestPath |
                    ForEach-Object { [string]$_.symbol } |
                    Sort-Object -Unique
            )
        }
        $universeHealthy = $uniqueSymbols.Count -eq $expectedBybitCount
        $freshStatus = (
            $bybitStatusAge -ne $null -and
            $bybitStatusAge -ge 0 -and
            $bybitStatusAge -le $BybitFreshSeconds
        )
        $recentMarketEvent = (
            $lastEventAge -ne $null -and
            $lastEventAge -ge 0 -and
            $lastEventAge -le $ReconnectGraceSeconds
        )
        $safeReadOnly = -not [bool]$rawBybit.orders_enabled
        $failedChecks = @($status.checks | Where-Object { [string]$_.status -ne "OK" })
        $onlyStatusFailed = (
            $failedChecks.Count -eq 1 -and
            [string]$failedChecks[0].name -eq "Bybit status"
        )

        $reconnectGraceActive = (
            $legacyExitCode -ne 0 -and
            $onlyStatusFailed -and
            $bybitState -eq "RECONNECTING" -and
            $freshStatus -and
            $recentMarketEvent -and
            $runtimeHealthy -and
            $universeHealthy -and
            $safeReadOnly
        )
        $reconnectEvidence = [pscustomobject]@{
            status_age_seconds = $bybitStatusAge
            last_event_age_seconds = $lastEventAge
            grace_seconds = $ReconnectGraceSeconds
            task_state = if ($bybitTask) { [string]$bybitTask.State } else { "MISSING" }
            collector_instances = $collectorRoots.Count
            python_processes = $processes.Count
            symbols = $uniqueSymbols.Count
            orders_enabled = [bool]$rawBybit.orders_enabled
            reconnects = $rawBybit.reconnects
            error = $rawBybit.error
        }

        if ($reconnectGraceActive) {
            foreach ($check in @($status.checks)) {
                if ([string]$check.name -eq "Bybit status") {
                    $check.status = "OK"
                    $check.message = "state=RECONNECTING transient=True status_age_seconds=$bybitStatusAge last_event_age_seconds=$lastEventAge grace_seconds=$ReconnectGraceSeconds orders_enabled=$($rawBybit.orders_enabled)"
                    $check.details = $reconnectEvidence
                }
            }
            $status.overall = "OK"
        }
    } catch {
        $reconnectEvidence = [pscustomobject]@{ error = $_.Exception.Message }
    }
}

# Add reconnect diagnostics even during normal RUNNING state.
$status.schema_version = "1.10.2"
$status | Add-Member -NotePropertyName watchdog_patch -NotePropertyValue "verified_reconnect_grace" -Force
if ($status.bybit) {
    $status.bybit | Add-Member -NotePropertyName state -NotePropertyValue $bybitState -Force
    $status.bybit | Add-Member -NotePropertyName last_event_age_seconds -NotePropertyValue $lastEventAge -Force
    $status.bybit | Add-Member -NotePropertyName reconnect_grace_seconds -NotePropertyValue $ReconnectGraceSeconds -Force
    $status.bybit | Add-Member -NotePropertyName reconnect_grace_active -NotePropertyValue $reconnectGraceActive -Force
}

$tempStatus = "$statusPath.tmp"
$status | ConvertTo-Json -Depth 10 | Set-Content -Path $tempStatus -Encoding UTF8
Move-Item -Path $tempStatus -Destination $statusPath -Force

$reportLines = @(
    "TradeMind AI v1.10.2 Unified Watchdog",
    "Generated: $($status.generated_at)",
    "Overall: $($status.overall)",
    "Read-only: $($status.read_only)",
    "Bybit state: $bybitState",
    "Bybit reconnect grace active: $reconnectGraceActive",
    "Bybit last event age seconds: $lastEventAge",
    ""
)
foreach ($check in @($status.checks)) {
    $reportLines += "[$($check.status)] $($check.name): $($check.message)"
}
$reportLines | Set-Content -Path $reportPath -Encoding UTF8

Write-Host "`n=== TRADEMIND v1.10.2 UNIFIED WATCHDOG ===" -ForegroundColor Cyan
[pscustomobject]@{
    Overall = [string]$status.overall
    ReadOnly = [bool]$status.read_only
    EcnFreshStreams = "$($status.ecn.fresh_streams)/$($status.ecn.expected_symbols)"
    BybitSymbols = "$($status.bybit.observed_symbols)/$($status.bybit.expected_symbols)"
    BybitState = $bybitState
    BybitReconnectGrace = $reconnectGraceActive
    BybitLastEventAgeSeconds = $lastEventAge
    BybitCollectorInstances = $status.bybit.collector_instances
    BybitPythonProcesses = $status.bybit.python_processes
    BybitOrdersEnabled = $status.bybit.orders_enabled
    ShadowState = $status.shadow.state
    ShadowM5M15H1 = "$($status.shadow.source_m5_bars)/$($status.shadow.m15_bars)/$($status.shadow.h1_bars)"
    ShadowCandidates = $status.shadow.candidates
    ShadowForwardSignals = $status.shadow.paper_signals
    ShadowCompletedSignals = $status.shadow.completed_signals
    ShadowOrdersEnabled = $status.shadow.orders_enabled
    StatusFile = $statusPath
} | Format-List

@($status.checks) | Select-Object status,name,message | Format-Table -AutoSize
if ([string]$status.overall -ne "OK") {
    $legacyOutput | ForEach-Object { Write-Host $_ }
    Write-Host "[ERROR] A component is unhealthy or Bybit reconnect exceeded verified grace." -ForegroundColor Red
    exit 2
}
if ($reconnectGraceActive) {
    Write-Host "[OK] Bybit is in a short verified reconnect; runtime, recent market flow and 20-symbol universe are healthy." -ForegroundColor Yellow
} else {
    Write-Host "[OK] ECN, Bybit collector and Bybit Shadow are healthy and read-only." -ForegroundColor Green
}
exit 0
