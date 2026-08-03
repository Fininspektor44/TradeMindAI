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

$expertDirs = Get-ChildItem -Path $terminalRoot -Directory -ErrorAction Stop |
    ForEach-Object { Join-Path $_.FullName "MQL5\Experts" } |
    Where-Object { Test-Path $_ } |
    Sort-Object -Unique
if (-not $expertDirs) {
    throw "No MetaTrader MQL5\Experts directories were found."
}

$utf8 = [System.Text.UTF8Encoding]::new($false)
$written = @()
foreach ($expertsDir in $expertDirs) {
    $targetPath = Join-Path $expertsDir $TargetName
    [System.IO.File]::WriteAllText($targetPath, $content, $utf8)
    $written += $targetPath
    Write-Host "Deployed universal ECN exporter: $targetPath"
}

Write-Host ""
Write-Host "TradeMind v1.9.4 universal ECN exporter deployed."
Write-Host "Symbols: $($requiredSymbols -join ',')"
Write-Host "Source: ROBO_ECN"
Write-Host "Output folder: Terminal Common Files\TradeMindAI_Volume_v1_4"
Write-Host "Manifest: ecn_manifest.csv"
Write-Host "Cent is not included."
Write-Host "No order functions are present."
Write-Host "Targets written: $($written.Count)"

if ($Open -and $written.Count -gt 0) {
    Start-Process $written[0]
}
