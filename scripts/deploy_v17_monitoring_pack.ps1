param(
    [string]$MarketTarget = "TradeMindAI_VolumeExporter_v1_4_FIXED.mq5",
    [string]$FxTarget = "TradeMindAI_VolumeExporter_FX_v1_4.mq5",
    [string]$CryptoTarget = "TradeMindAI_CryptoVolumeExporter_v1_7.mq5",
    [switch]$OpenCrypto
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$marketSource = Join-Path $projectRoot "mt5\TradeMindAI_VolumeExporter_v1_4.mq5"
$cryptoSource = Join-Path $projectRoot "mt5\TradeMindAI_CryptoVolumeExporter_v1_7.mq5"
foreach ($path in @($marketSource, $cryptoSource)) {
    if (-not (Test-Path $path)) { throw "Exporter source not found: $path" }
}

$marketContent = Get-Content -Path $marketSource -Raw -Encoding UTF8
$cryptoContent = Get-Content -Path $cryptoSource -Raw -Encoding UTF8
$fxPattern = 'input string\s+InpSymbols\s*=\s*"[^"]*";'
if (-not [regex]::IsMatch($marketContent, $fxPattern)) {
    throw "InpSymbols declaration was not found in the market exporter."
}
$fxReplacement = 'input string          InpSymbols        = "EURUSD,GBPUSD,USDJPY,USDCHF,USDCAD,AUDUSD,NZDUSD";'
$fxContent = [regex]::Replace($marketContent, $fxPattern, $fxReplacement, 1)
$fxContent = $fxContent.Replace(
    '#property description "TradeMind AI v1.4 read-only tick and volume intelligence exporter"',
    '#property description "TradeMind AI v1.4 read-only FX majors volume intelligence exporter"'
)

$terminalRoot = Join-Path $env:APPDATA "MetaQuotes\Terminal"
if (-not (Test-Path $terminalRoot)) { throw "MetaTrader terminal root not found: $terminalRoot" }
$expertDirs = Get-ChildItem -Path $terminalRoot -Directory -ErrorAction Stop |
    ForEach-Object { Join-Path $_.FullName "MQL5\Experts" } |
    Where-Object { Test-Path $_ } |
    Sort-Object -Unique
if (-not $expertDirs) { throw "No MetaTrader MQL5\Experts directories were found." }

$utf8 = [System.Text.UTF8Encoding]::new($false)
$written = @()
foreach ($expertsDir in $expertDirs) {
    $marketPath = Join-Path $expertsDir $MarketTarget
    $fxPath = Join-Path $expertsDir $FxTarget
    $cryptoPath = Join-Path $expertsDir $CryptoTarget
    [System.IO.File]::WriteAllText($marketPath, $marketContent, $utf8)
    [System.IO.File]::WriteAllText($fxPath, $fxContent, $utf8)
    [System.IO.File]::WriteAllText($cryptoPath, $cryptoContent, $utf8)
    $written += [pscustomobject]@{ Market=$marketPath; FX=$fxPath; Crypto=$cryptoPath }
    Write-Host "Deployed monitoring pack to: $expertsDir"
}

Write-Host "Terminal targets: $($written.Count)"
Write-Host "Market exporter: $MarketTarget"
Write-Host "FX exporter: $FxTarget"
Write-Host "Crypto exporter: $CryptoTarget"
Write-Host "Crypto catalog: BTCUSD, ETHUSD, SOLUSD, XRPUSD, LTCUSD, BCHUSD, ADAUSD, DOGEUSD"
Write-Host "All exporters are read-only and write to Terminal Common Files."
Write-Host "Do not run duplicate market/FX exporters in two terminals after migration verification."

if ($OpenCrypto -and $written.Count -gt 0) {
    Start-Process $written[0].Crypto
}
