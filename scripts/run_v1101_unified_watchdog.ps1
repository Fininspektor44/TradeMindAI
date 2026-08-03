param(
    [string]$BybitTaskName = "TradeMindAI-v1.9-Bybit",
    [string]$ShadowTaskName = "TradeMindAI-v1.10-BybitShadow",
    [ValidateRange(60, 1800)]
    [int]$BybitFreshSeconds = 120,
    [ValidateRange(300, 3600)]
    [int]$EcnFreshSeconds = 600,
    [ValidateRange(300, 1800)]
    [int]$ShadowFreshSeconds = 600
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$commonDir = Join-Path $env:APPDATA "MetaQuotes\Terminal\Common\Files\TradeMindAI_Volume_v1_4"
$ecnManifestPath = Join-Path $commonDir "ecn_manifest.csv"
$bybitDir = Join-Path $projectRoot "data\bybit_v1_9"
$bybitStatusPath = Join-Path $bybitDir "status.json"
$bybitLatestPath = Join-Path $bybitDir "latest.csv"
$shadowDir = Join-Path $projectRoot "data\bybit_shadow_v1_10"
$shadowStatusPath = Join-Path $shadowDir "status.json"
$outputDir = Join-Path $projectRoot "data\watchdog_v1_10_1"
$statusPath = Join-Path $outputDir "status.json"
$reportPath = Join-Path $outputDir "report.txt"

$expectedEcn = @(
    "EURUSD", "GBPUSD", "USDJPY", "USDCHF", "USDCAD", "AUDUSD", "NZDUSD",
    "XAUUSD", "XAGUSD", ".USTECHCash", ".US500Cash", ".US30Cash", "WTI", "BRENT",
    "BTCUSD", "ETHUSD"
)
$expectedBybitCount = 20
$nowUtc = [DateTimeOffset]::UtcNow
$checks = [System.Collections.Generic.List[object]]::new()

function Add-HealthCheck {
    param(
        [string]$Name,
        [bool]$Ok,
        [string]$Message,
        [object]$Details = $null
    )
    $checks.Add([pscustomobject]@{
        name = $Name
        status = if ($Ok) { "OK" } else { "ERROR" }
        message = $Message
        details = $Details
    }) | Out-Null
}

# ECN manifest and all 16 output streams.
$ecnRows = @()
if (Test-Path $ecnManifestPath) {
    try {
        $ecnRows = @(Import-Csv $ecnManifestPath)
        $resolvedRows = @($ecnRows | Where-Object {
            [string]$_.status -eq "RESOLVED" -and [string]$_.source_id -eq "ROBO_ECN"
        })
        $resolvedSymbols = @($resolvedRows | ForEach-Object { [string]$_.canonical_symbol })
        $missingManifestSymbols = @($expectedEcn | Where-Object { $_ -notin $resolvedSymbols })
        $manifestOk = $missingManifestSymbols.Count -eq 0 -and $resolvedRows.Count -eq $expectedEcn.Count
        Add-HealthCheck -Name "ECN manifest" -Ok $manifestOk -Message "resolved=$($resolvedRows.Count) expected=$($expectedEcn.Count)" -Details ([pscustomobject]@{ missing = $missingManifestSymbols; path = $ecnManifestPath })
    } catch {
        Add-HealthCheck -Name "ECN manifest" -Ok $false -Message $_.Exception.Message -Details $ecnManifestPath
    }
} else {
    Add-HealthCheck -Name "ECN manifest" -Ok $false -Message "missing file" -Details $ecnManifestPath
}

$ecnFileDetails = @()
foreach ($symbol in $expectedEcn) {
    $safe = $symbol -replace '\.', '_'
    $filePath = Join-Path $commonDir ("volume_{0}_M5.csv" -f $safe)
    $exists = Test-Path $filePath
    $ageSeconds = $null
    $size = 0
    if ($exists) {
        $item = Get-Item $filePath
        $size = [int64]$item.Length
        $ageSeconds = [math]::Round(($nowUtc - [DateTimeOffset]$item.LastWriteTimeUtc).TotalSeconds, 1)
    }
    $fresh = $exists -and $size -gt 0 -and $ageSeconds -ne $null -and $ageSeconds -ge 0 -and $ageSeconds -le $EcnFreshSeconds
    $ecnFileDetails += [pscustomobject]@{
        symbol = $symbol
        exists = $exists
        age_seconds = $ageSeconds
        size = $size
        fresh = $fresh
    }
}
$staleEcn = @($ecnFileDetails | Where-Object { -not $_.fresh })
$ages = @($ecnFileDetails | Where-Object { $_.age_seconds -ne $null } | ForEach-Object { [double]$_.age_seconds })
$maxEcnAge = if ($ages.Count -gt 0) { [math]::Round(($ages | Measure-Object -Maximum).Maximum, 1) } else { $null }
Add-HealthCheck -Name "ECN M5 streams" -Ok ($staleEcn.Count -eq 0) -Message "fresh=$($expectedEcn.Count - $staleEcn.Count)/$($expectedEcn.Count) max_age_seconds=$maxEcnAge" -Details $ecnFileDetails

# Bybit collector status, scheduled task and one parent-child process chain.
$bybitStatus = $null
$bybitStatusAge = $null
if (Test-Path $bybitStatusPath) {
    try {
        $bybitStatus = Get-Content $bybitStatusPath -Raw | ConvertFrom-Json
        $updatedAt = [DateTimeOffset]::Parse([string]$bybitStatus.updated_at).ToUniversalTime()
        $bybitStatusAge = [math]::Round(($nowUtc - $updatedAt).TotalSeconds, 1)
        $bybitStateOk = (
            [string]$bybitStatus.state -eq "RUNNING" -and
            $bybitStatusAge -ge 0 -and
            $bybitStatusAge -le $BybitFreshSeconds -and
            -not [bool]$bybitStatus.orders_enabled
        )
        $bybitStatusDetails = [pscustomobject]@{
            messages = $bybitStatus.messages
            bars_written = $bybitStatus.bars_written
            reconnects = $bybitStatus.reconnects
            path = $bybitStatusPath
        }
        Add-HealthCheck -Name "Bybit status" -Ok $bybitStateOk -Message "state=$($bybitStatus.state) age_seconds=$bybitStatusAge orders_enabled=$($bybitStatus.orders_enabled)" -Details $bybitStatusDetails
    } catch {
        Add-HealthCheck -Name "Bybit status" -Ok $false -Message $_.Exception.Message -Details $bybitStatusPath
    }
} else {
    Add-HealthCheck -Name "Bybit status" -Ok $false -Message "missing file" -Details $bybitStatusPath
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
$collectorInstances = $collectorRoots.Count
$bybitTask = Get-ScheduledTask -TaskName $BybitTaskName -ErrorAction SilentlyContinue
$bybitTaskInfo = if ($bybitTask) { Get-ScheduledTaskInfo -TaskName $BybitTaskName } else { $null }
$runtimeOk = $bybitTask -and $bybitTask.State -eq "Running" -and $collectorInstances -eq 1 -and $processes.Count -ge 1
$runtimeDetails = [pscustomobject]@{
    last_task_result = if ($bybitTaskInfo) { $bybitTaskInfo.LastTaskResult } else { $null }
    roots = @($collectorRoots | Select-Object ProcessId,ParentProcessId,Name,ExecutablePath)
    processes = @($processes | Select-Object ProcessId,ParentProcessId,Name,ExecutablePath)
}
$bybitTaskState = if ($bybitTask) { [string]$bybitTask.State } else { "MISSING" }
Add-HealthCheck -Name "Bybit runtime" -Ok $runtimeOk -Message "task=$bybitTaskState instances=$collectorInstances python_processes=$($processes.Count)" -Details $runtimeDetails

$bybitRows = @()
$uniqueBybitSymbols = @()
if (Test-Path $bybitLatestPath) {
    try {
        $bybitRows = @(Import-Csv $bybitLatestPath)
        $uniqueBybitSymbols = @($bybitRows | ForEach-Object { [string]$_.symbol } | Sort-Object -Unique)
        Add-HealthCheck -Name "Bybit M5 universe" -Ok ($uniqueBybitSymbols.Count -eq $expectedBybitCount) -Message "symbols=$($uniqueBybitSymbols.Count) expected=$expectedBybitCount" -Details ([pscustomobject]@{ symbols = $uniqueBybitSymbols; path = $bybitLatestPath })
    } catch {
        Add-HealthCheck -Name "Bybit M5 universe" -Ok $false -Message $_.Exception.Message -Details $bybitLatestPath
    }
} else {
    Add-HealthCheck -Name "Bybit M5 universe" -Ok $false -Message "missing file" -Details $bybitLatestPath
}

# Bybit Shadow Research task and status. The task is periodic, so Ready is healthy.
$shadowStatus = $null
$shadowStatusAge = $null
$shadowTask = Get-ScheduledTask -TaskName $ShadowTaskName -ErrorAction SilentlyContinue
$shadowTaskInfo = if ($shadowTask) { Get-ScheduledTaskInfo -TaskName $ShadowTaskName } else { $null }
$shadowTaskState = if ($shadowTask) { [string]$shadowTask.State } else { "MISSING" }
if (Test-Path $shadowStatusPath) {
    try {
        $shadowStatus = Get-Content $shadowStatusPath -Raw | ConvertFrom-Json
        $shadowUpdated = [DateTimeOffset]::Parse([string]$shadowStatus.updated_at).ToUniversalTime()
        $shadowStatusAge = [math]::Round(($nowUtc - $shadowUpdated).TotalSeconds, 1)
        $shadowTaskOk = $shadowTask -and $shadowTask.State -in @("Ready", "Running")
        $shadowResultOk = $shadowTaskInfo -and $shadowTaskInfo.LastTaskResult -eq 0
        $shadowDataOk = (
            [int]$shadowStatus.source_bars -gt 0 -and
            [int]$shadowStatus.m15_bars -gt 0 -and
            [int]$shadowStatus.h1_bars -gt 0
        )
        $shadowOk = (
            $shadowTaskOk -and
            $shadowResultOk -and
            [string]$shadowStatus.state -eq "OK" -and
            $shadowStatusAge -ge 0 -and
            $shadowStatusAge -le $ShadowFreshSeconds -and
            [bool]$shadowStatus.forward_only -and
            -not [bool]$shadowStatus.orders_enabled -and
            $shadowDataOk
        )
        $shadowDetails = [pscustomobject]@{
            last_task_result = if ($shadowTaskInfo) { $shadowTaskInfo.LastTaskResult } else { $null }
            source_m5_bars = $shadowStatus.source_bars
            m15_bars = $shadowStatus.m15_bars
            h1_bars = $shadowStatus.h1_bars
            decisions = $shadowStatus.decisions
            candidates = $shadowStatus.gate_counts.CANDIDATE
            paper_signals = $shadowStatus.paper_signals
            completed_signals = $shadowStatus.completed_paper_signals
            forward_only = $shadowStatus.forward_only
            orders_enabled = $shadowStatus.orders_enabled
            path = $shadowStatusPath
        }
        Add-HealthCheck -Name "Bybit Shadow Research" -Ok $shadowOk -Message "task=$shadowTaskState state=$($shadowStatus.state) age_seconds=$shadowStatusAge M5/M15/H1=$($shadowStatus.source_bars)/$($shadowStatus.m15_bars)/$($shadowStatus.h1_bars) forward_only=$($shadowStatus.forward_only) orders_enabled=$($shadowStatus.orders_enabled)" -Details $shadowDetails
    } catch {
        Add-HealthCheck -Name "Bybit Shadow Research" -Ok $false -Message $_.Exception.Message -Details $shadowStatusPath
    }
} else {
    Add-HealthCheck -Name "Bybit Shadow Research" -Ok $false -Message "missing file" -Details $shadowStatusPath
}

$failedChecks = @($checks | Where-Object { $_.status -ne "OK" })
$overall = if ($failedChecks.Count -eq 0) { "OK" } else { "ERROR" }
$snapshot = [ordered]@{
    schema_version = "1.10.1"
    generated_at = $nowUtc.ToString("o")
    overall = $overall
    read_only = $true
    ecn = [ordered]@{
        expected_symbols = $expectedEcn.Count
        fresh_streams = $expectedEcn.Count - $staleEcn.Count
        maximum_age_seconds = $maxEcnAge
    }
    bybit = [ordered]@{
        expected_symbols = $expectedBybitCount
        observed_symbols = $uniqueBybitSymbols.Count
        collector_instances = $collectorInstances
        python_processes = $processes.Count
        status_age_seconds = $bybitStatusAge
        orders_enabled = if ($bybitStatus) { [bool]$bybitStatus.orders_enabled } else { $null }
    }
    shadow = [ordered]@{
        task_state = $shadowTaskState
        last_task_result = if ($shadowTaskInfo) { $shadowTaskInfo.LastTaskResult } else { $null }
        state = if ($shadowStatus) { [string]$shadowStatus.state } else { $null }
        status_age_seconds = $shadowStatusAge
        source_m5_bars = if ($shadowStatus) { [int]$shadowStatus.source_bars } else { 0 }
        m15_bars = if ($shadowStatus) { [int]$shadowStatus.m15_bars } else { 0 }
        h1_bars = if ($shadowStatus) { [int]$shadowStatus.h1_bars } else { 0 }
        decisions = if ($shadowStatus) { [int]$shadowStatus.decisions } else { 0 }
        candidates = if ($shadowStatus) { [int]$shadowStatus.gate_counts.CANDIDATE } else { 0 }
        paper_signals = if ($shadowStatus) { [int]$shadowStatus.paper_signals } else { 0 }
        completed_signals = if ($shadowStatus) { [int]$shadowStatus.completed_paper_signals } else { 0 }
        forward_only = if ($shadowStatus) { [bool]$shadowStatus.forward_only } else { $null }
        orders_enabled = if ($shadowStatus) { [bool]$shadowStatus.orders_enabled } else { $null }
    }
    checks = $checks
}

New-Item -ItemType Directory -Path $outputDir -Force | Out-Null
$tempStatus = "$statusPath.tmp"
$snapshot | ConvertTo-Json -Depth 8 | Set-Content -Path $tempStatus -Encoding UTF8
Move-Item -Path $tempStatus -Destination $statusPath -Force

$reportLines = @(
    "TradeMind AI v1.10.1 Unified Watchdog",
    "Generated: $($snapshot.generated_at)",
    "Overall: $overall",
    "Read-only: True",
    "",
    "ECN fresh streams: $($snapshot.ecn.fresh_streams)/$($snapshot.ecn.expected_symbols)",
    "Bybit symbols: $($snapshot.bybit.observed_symbols)/$($snapshot.bybit.expected_symbols)",
    "Bybit collector instances: $collectorInstances",
    "Bybit Python processes: $($processes.Count)",
    "Shadow state: $($snapshot.shadow.state)",
    "Shadow M5/M15/H1: $($snapshot.shadow.source_m5_bars)/$($snapshot.shadow.m15_bars)/$($snapshot.shadow.h1_bars)",
    "Shadow forward signals: $($snapshot.shadow.paper_signals)",
    ""
)
foreach ($check in $checks) {
    $reportLines += "[$($check.status)] $($check.name): $($check.message)"
}
$reportLines | Set-Content -Path $reportPath -Encoding UTF8

Write-Host "`n=== TRADEMIND v1.10.1 UNIFIED WATCHDOG ===" -ForegroundColor Cyan
[pscustomobject]@{
    Overall = $overall
    ReadOnly = $true
    EcnFreshStreams = "$($snapshot.ecn.fresh_streams)/$($snapshot.ecn.expected_symbols)"
    BybitSymbols = "$($snapshot.bybit.observed_symbols)/$($snapshot.bybit.expected_symbols)"
    BybitCollectorInstances = $collectorInstances
    BybitPythonProcesses = $processes.Count
    BybitOrdersEnabled = $snapshot.bybit.orders_enabled
    ShadowState = $snapshot.shadow.state
    ShadowTaskState = $snapshot.shadow.task_state
    ShadowM5M15H1 = "$($snapshot.shadow.source_m5_bars)/$($snapshot.shadow.m15_bars)/$($snapshot.shadow.h1_bars)"
    ShadowCandidates = $snapshot.shadow.candidates
    ShadowForwardSignals = $snapshot.shadow.paper_signals
    ShadowForwardOnly = $snapshot.shadow.forward_only
    ShadowOrdersEnabled = $snapshot.shadow.orders_enabled
    StatusFile = $statusPath
} | Format-List

$checks | Select-Object status,name,message | Format-Table -AutoSize
if ($overall -ne "OK") {
    Write-Host "[ERROR] Unified watchdog found $($failedChecks.Count) failed checks." -ForegroundColor Red
    exit 2
}
Write-Host "[OK] ECN, Bybit collector and Bybit Shadow are healthy and read-only." -ForegroundColor Green
exit 0
