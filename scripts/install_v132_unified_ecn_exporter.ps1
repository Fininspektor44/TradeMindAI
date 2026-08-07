param(
    [string]$Login = "77053345"
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$sourceRoot = Join-Path $repoRoot "mt5\exporters"
$mainSource = Join-Path $sourceRoot "TradeMindAI_ECN_UnifiedExporter_v1_32.mq5"
$dependencyNames = @(
    "TradeMindAI_ECN_UniversalVolumeExporter_v1_9_4.mq5",
    "TradeMind_Grid_Deal_Exporter.mq5",
    "TradeMind_MT5_Risk_Snapshot_Exporter.mq5"
)

if (-not (Test-Path $mainSource)) {
    throw "Unified exporter source not found: $mainSource"
}

$terminalRoot = Join-Path $env:APPDATA "MetaQuotes\Terminal"
$candidates = @()

Get-ChildItem $terminalRoot -Directory -ErrorAction Stop |
    Where-Object { $_.Name -ne "Common" } |
    ForEach-Object {
        $dataDir = $_.FullName
        $logsDir = Join-Path $dataDir "logs"
        if (-not (Test-Path $logsDir)) { return }

        $recentLogs = Get-ChildItem $logsDir -File -Filter "*.log" -ErrorAction SilentlyContinue |
            Sort-Object LastWriteTime -Descending |
            Select-Object -First 5
        if (-not $recentLogs) { return }

        $matched = $false
        foreach ($log in $recentLogs) {
            if (Select-String -Path $log.FullName -Pattern $Login -SimpleMatch -Quiet -ErrorAction SilentlyContinue) {
                $matched = $true
                break
            }
        }
        if ($matched) {
            $latestWrite = ($recentLogs | Measure-Object LastWriteTime -Maximum).Maximum
            $candidates += [pscustomobject]@{ DataDir = $dataDir; LastWriteTime = $latestWrite }
        }
    }

if ($candidates.Count -eq 0) {
    throw "No MT5 terminal data directory found for login $Login. Keep that terminal open and retry."
}

$selected = $candidates | Sort-Object LastWriteTime -Descending | Select-Object -First 1
$dataDir = $selected.DataDir
$destination = Join-Path $dataDir "MQL5\Experts\TradeMindAI\UnifiedExporter"
$dependencyDestination = Join-Path $destination "source"
New-Item -ItemType Directory -Path $dependencyDestination -Force | Out-Null

$mainDestination = Join-Path $destination (Split-Path $mainSource -Leaf)
Copy-Item $mainSource $mainDestination -Force
foreach ($name in $dependencyNames) {
    $source = Join-Path (Join-Path $sourceRoot "source") $name
    if (-not (Test-Path $source)) { throw "Dependency not found: $source" }
    Copy-Item $source (Join-Path $dependencyDestination $name) -Force
}

$originFile = Join-Path $dataDir "origin.txt"
if (-not (Test-Path $originFile)) {
    throw "MT5 origin.txt not found: $originFile"
}
$installRoot = (Get-Content $originFile -Raw).Trim()
$metaEditor = Join-Path $installRoot "metaeditor64.exe"
if (-not (Test-Path $metaEditor)) {
    throw "MetaEditor64 not found: $metaEditor"
}

$compileLog = Join-Path $env:TEMP "TradeMind_v132_unified_compile.log"
Remove-Item $compileLog -Force -ErrorAction SilentlyContinue
& $metaEditor "/compile:$mainDestination" "/log:$compileLog" | Out-Null
Start-Sleep -Seconds 2

if (-not (Test-Path $compileLog)) {
    throw "MetaEditor compile log was not created: $compileLog"
}
$compileText = Get-Content $compileLog -Raw
Write-Host $compileText
if ($compileText -notmatch "0 errors") {
    throw "Unified exporter compile failed. See $compileLog"
}

Write-Host "INSTALLED SOURCE: $mainDestination" -ForegroundColor Green
Write-Host "COMPILE: 0 errors" -ForegroundColor Green
Write-Host "LOGIN: $Login" -ForegroundColor Cyan
Write-Host "READ-ONLY: one EA replaces Volume + Risk Snapshot + Deal Exporter." -ForegroundColor Green
Write-Host "Do not remove the three old exporters until the unified EA is attached and fresh output is verified." -ForegroundColor Yellow
