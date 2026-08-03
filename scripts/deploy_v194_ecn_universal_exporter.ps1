param(
    [string]$TargetName = "TradeMindAI_ECN_UniversalVolumeExporter_v1_9_4.mq5",
    [switch]$Open
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$sourcePath = Join-Path $projectRoot "mt5\TradeMindAI_ECN_UniversalVolumeExporter_v1_9_4.mq5"
if (-not (Test-Path $sourcePath)) {
    throw "Universal ECN exporter source not found: $sourcePath"
}

$content = Get-Content -Path $sourcePath -Raw -Encoding UTF8
$requiredSymbols = @(
    "EURUSD", "GBPUSD", "USDJPY", "USDCHF", "USDCAD", "AUDUSD", "NZDUSD",
    "XAUUSD", "XAGUSD", ".USTECHCash", ".US500Cash", ".US30Cash", "WTI", "BRENT",
    "BTCUSD", "ETHUSD"
)
foreach ($symbol in $requiredSymbols) {
    if (-not $content.Contains($symbol)) {
        throw "Universal ECN exporter is missing required symbol: $symbol"
    }
}
if ($content.Contains("ROBO_CENT") -or $content.Contains("crypto_manifest_cent")) {
    throw "Cent configuration must not be present in the universal ECN exporter."
}
if (-not $content.Contains('source=ROBO_ECN')) {
    throw "ROBO_ECN source marker is missing."
}
if ($content -match '(?i)\b(OrderSend|OrderSendAsync|CTrade|trade\.Buy|trade\.Sell)\b') {
    throw "Trading function detected. Deployment stopped."
}

$terminalRoot = Join-Path $env:APPDATA "MetaQuotes\Terminal"
if (-not (Test-Path $terminalRoot)) {
    throw "MetaTrader terminal data root not found: $terminalRoot"
}

# Prefer the Advisors folder that already contains the old ECN exporter.
# This identifies the ECN terminal instead of writing only to an arbitrary Experts root.
$oldEcnMarkers = @(
    Get-ChildItem -Path $terminalRoot -Recurse -File -ErrorAction SilentlyContinue |
        Where-Object {
            $_.Extension -in @(".mq5", ".ex5") -and
            $_.BaseName -match '(?i)TradeMind.*ECN.*Exporter' -and
            $_.BaseName -notmatch '(?i)UniversalVolumeExporter'
        }
)
$targetDirs = @(
    $oldEcnMarkers |
        ForEach-Object { $_.Directory.FullName } |
        Where-Object { $_ -match '\\MQL5\\Experts(\\Advisors)?$' } |
        Sort-Object -Unique
)

if (-not $targetDirs) {
    $targetDirs = @(
        Get-ChildItem -Path $terminalRoot -Directory -ErrorAction Stop |
            ForEach-Object { Join-Path $_.FullName "MQL5\Experts\Advisors" } |
            Where-Object { Test-Path $_ } |
            Sort-Object -Unique
    )
}
if (-not $targetDirs) {
    throw "No MetaTrader MQL5\Experts\Advisors directories were found."
}

$utf8 = [System.Text.UTF8Encoding]::new($false)
$written = @()
foreach ($targetDir in $targetDirs) {
    New-Item -ItemType Directory -Path $targetDir -Force | Out-Null
    $targetPath = Join-Path $targetDir $TargetName
    [System.IO.File]::WriteAllText($targetPath, $content, $utf8)
    $written += $targetPath
    Write-Host "Deployed universal ECN source: $targetPath"
}

# The user has already compiled the file once. Find that EX5 and copy it next to
# the existing ECN advisor so Navigator can see it immediately.
$compiledName = [System.IO.Path]::ChangeExtension($TargetName, ".ex5")
$compiledSource = Get-ChildItem -Path $terminalRoot -Recurse -File -Filter $compiledName -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTime -Descending |
    Select-Object -First 1
$compiledTargets = @()
if ($compiledSource) {
    foreach ($targetDir in $targetDirs) {
        $targetEx5 = Join-Path $targetDir $compiledName
        if ($compiledSource.FullName -ne $targetEx5) {
            Copy-Item -Path $compiledSource.FullName -Destination $targetEx5 -Force
        }
        $compiledTargets += $targetEx5
        Write-Host "Installed compiled ECN exporter: $targetEx5"
    }
}

Write-Host ""
Write-Host "TradeMind v1.9.4 universal ECN exporter deployed."
Write-Host "Symbols: $($requiredSymbols -join ',')"
Write-Host "Source: ROBO_ECN"
Write-Host "Output folder: Terminal Common Files\TradeMindAI_Volume_v1_4"
Write-Host "Manifest: ecn_manifest.csv"
Write-Host "Cent is not included."
Write-Host "No order functions are present."
Write-Host "ECN target folders: $($targetDirs.Count)"
Write-Host "Compiled files installed: $($compiledTargets.Count)"

if ($Open -and $written.Count -gt 0) {
    if (-not $compiledSource) {
        Start-Process $written[0]
        Write-Host "Compiled EX5 was not found, so the ECN source was opened for compilation."
    } else {
        Start-Process explorer.exe -ArgumentList "/select,`"$($compiledTargets[0])`""
    }
}
