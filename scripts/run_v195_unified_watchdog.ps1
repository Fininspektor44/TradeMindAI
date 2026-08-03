param(
    [string]$BybitTaskName = "TradeMindAI-v1.9-Bybit",
    [ValidateRange(60, 1800)]
    [int]$BybitFreshSeconds = 120,
    [ValidateRange(300, 3600)]
    [int]$EcnFreshSeconds = 600
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$commonDir = Join-Path $env:APPDATA "MetaQuotes\Terminal\Common\Files\TradeMindAI_Volume_v1_4"
$ecnManifestPath = Join-Path $commonDir "ecn_manifest.csv"
$bybitDir = Join-Path $projectRoot "data\bybit_v1_9"
$bybitStatusPath = Join-Path $bybitDir "status.json"
$bybitLatestPath = Join-Path $bybitDir "latest.csv"
$outputDir = Join-Path $projectRoot "data\watchdog_v1_9_5"
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
        Add-HealthCheck \
            -Name "ECN manifest" \
            -Ok ($missingManifestSymbols.Count -eq 0 -and $resolvedRows.Count -eq $expectedEcn.Count) \
            -Message "resolved=$($resolvedRows.Count) expected=$($expectedEcn.Count)" \
            -Details ([pscustomobject]@{ missing = $missingManifestSymbols; path = $ecnManifestPath })
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
    $fresh = $exists -and $size -gt 0 -and $ageSeconds -ge 0 -and $ageSeconds -le $EcnFreshSeconds
    $ecnFileDetails += [pscustomobject]@{
        symbol = $symbol
        exists = $exists
        age_seconds = $ageSeconds
        size = $size
        fresh = $fresh
    }
}
$staleEcn = @($ecnFileDetails | Where-Object { -not $_.fresh })
$maxEcnAge = @($ecnFileDetails | Where-Object { $_.age_seconds -ne $null } | Measure-Object age_seconds -Maximum).Maximum
Add-HealthCheck \
    -Name "ECN M5 streams" \
    -Ok ($staleEcn.Count -eq 0) \
    -Message "fresh=$($expectedEcn.Count - $staleEcn.Count)/$($expectedEcn.Count) max_age_seconds=$maxEcnAge" \
    -Details $ecnFileDetails

# Bybit status, scheduled task and one parent-child collector chain.
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
        Add-HealthCheck \
            -Name "Bybit status" \
            -Ok $bybitStateOk \
            -Message "state=$($bybitStatus.state) age_seconds=$bybitStatusAge orders_enabled=$($bybitStatus.orders_enabled)" \
            -Details ([pscustomobject]@{
                messages = $bybitStatus.messages
                bars_written = $bybitStatus.bars_written
                reconnects = $bybitStatus.reconnects
                path = $bybitStatusPath
            })
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
$task = Get-ScheduledTask -TaskName $BybitTaskName -ErrorAction SilentlyContinue
$taskInfo = if ($task) { Get-ScheduledTaskInfo -TaskName $BybitTaskName } else { $null }
$runtimeOk = $task -and $task.State -eq "Running" -and $collectorInstances -eq 1 -and $processes.Count -ge 1
Add-HealthCheck \
    -Name "Bybit runtime" \
    -Ok $runtimeOk \
    -Message "task=$(if($task){$task.State}else{'MISSING'}) instances=$collectorInstances python_processes=$($processes.Count)" \
    -Details ([pscustomobject]@{
        last_task_result = if ($taskInfo) { $taskInfo.LastTaskResult } else { $null }
        roots = @($collectorRoots | Select-Object ProcessId,ParentProcessId,Name,ExecutablePath)
        processes = @($processes | Select-Object ProcessId,ParentProcessId,Name,ExecutablePath)
    })

$bybitRows = @()
if (Test-Path $bybitLatestPath) {
    try {
        $bybitRows = @(Import-Csv $bybitLatestPath)
        $uniqueSymbols = @($bybitRows | ForEach-Object { [string]$_.symbol } | Sort-Object -Unique)
        Add-HealthCheck \
            -Name "Bybit M5 universe" \
            -Ok ($uniqueSymbols.Count -eq $expectedBybitCount) \
            -Message "symbols=$($uniqueSymbols.Count) expected=$expectedBybitCount" \
            -Details ([pscustomobject]@{ symbols = $uniqueSymbols; path = $bybitLatestPath })
    } catch {
        Add-HealthCheck -Name "Bybit M5 universe" -Ok $false -Message $_.Exception.Message -Details $bybitLatestPath
    }
} else {
    Add-HealthCheck -Name "Bybit M5 universe" -Ok $false -Message "missing file" -Details $bybitLatestPath
}

$failedChecks = @($checks | Where-Object { $_.status -ne "OK" })
$overall = if ($failedChecks.Count -eq 0) { "OK" } else { "ERROR" }
$snapshot = [ordered]@{
    schema_version = "1.9.5"
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
        observed_symbols = @($bybitRows | ForEach-Object { [string]$_.symbol } | Sort-Object -Unique).Count
        collector_instances = $collectorInstances
        python_processes = $processes.Count
        status_age_seconds = $bybitStatusAge
        orders_enabled = if ($bybitStatus) { [bool]$bybitStatus.orders_enabled } else { $null }
    }
    checks = $checks
}

New-Item -ItemType Directory -Path $outputDir -Force | Out-Null
$tempStatus = "$statusPath.tmp"
$snapshot | ConvertTo-Json -Depth 8 | Set-Content -Path $tempStatus -Encoding UTF8
Move-Item -Path $tempStatus -Destination $statusPath -Force

$reportLines = @(
    "TradeMind AI v1.9.5 Unified Watchdog",
    "Generated: $($snapshot.generated_at)",
    "Overall: $overall",
    "Read-only: True",
    "",
    "ECN fresh streams: $($snapshot.ecn.fresh_streams)/$($snapshot.ecn.expected_symbols)",
    "Bybit symbols: $($snapshot.bybit.observed_symbols)/$($snapshot.bybit.expected_symbols)",
    "Bybit collector instances: $collectorInstances",
    "Bybit Python processes: $($processes.Count)",
    ""
)
foreach ($check in $checks) {
    $reportLines += "[$($check.status)] $($check.name): $($check.message)"
}
$reportLines | Set-Content -Path $reportPath -Encoding UTF8

Write-Host "`n=== TRADEMIND v1.9.5 UNIFIED WATCHDOG ===" -ForegroundColor Cyan
[pscustomobject]@{
    Overall = $overall
    ReadOnly = $true
    EcnFreshStreams = "$($snapshot.ecn.fresh_streams)/$($snapshot.ecn.expected_symbols)"
    BybitSymbols = "$($snapshot.bybit.observed_symbols)/$($snapshot.bybit.expected_symbols)"
    BybitCollectorInstances = $collectorInstances
    BybitPythonProcesses = $processes.Count
    BybitOrdersEnabled = $snapshot.bybit.orders_enabled
    StatusFile = $statusPath
} | Format-List

$checks | Select-Object status,name,message | Format-Table -AutoSize
if ($overall -ne "OK") {
    Write-Host "[ERROR] Unified watchdog found $($failedChecks.Count) failed checks." -ForegroundColor Red
    exit 2
}
Write-Host "[OK] ECN and Bybit are healthy and read-only." -ForegroundColor Green
exit 0
