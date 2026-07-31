param(
    [string]$FxSymbols = "EURUSD,GBPUSD,USDJPY,USDCHF,USDCAD,AUDUSD,NZDUSD",
    [string]$TargetName = "TradeMindAI_VolumeExporter_FX_v1_4.mq5",
    [switch]$Open
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$sourcePath = Join-Path $projectRoot "mt5\TradeMindAI_VolumeExporter_v1_4.mq5"
if (-not (Test-Path $sourcePath)) {
    throw "Canonical exporter not found: $sourcePath"
}

$symbols = $FxSymbols.Split(",") | ForEach-Object { $_.Trim().ToUpperInvariant() } | Where-Object { $_ }
$expected = @("EURUSD", "GBPUSD", "USDJPY", "USDCHF", "USDCAD", "AUDUSD", "NZDUSD")
if ($symbols.Count -ne $expected.Count) {
    throw "FX symbol set must contain exactly seven majors."
}
foreach ($symbol in $expected) {
    if ($symbols -notcontains $symbol) {
        throw "Missing required FX major: $symbol"
    }
}
if (($symbols | Select-Object -Unique).Count -ne $symbols.Count) {
    throw "FX symbol set contains duplicates."
}

$content = Get-Content -Path $sourcePath -Raw -Encoding UTF8
$pattern = 'input string\s+InpSymbols\s*=\s*"[^"]*";'
if (-not [regex]::IsMatch($content, $pattern)) {
    throw "InpSymbols declaration was not found in the canonical exporter."
}
$replacement = 'input string          InpSymbols        = "' + ($symbols -join ',') + '";'
$content = [regex]::Replace($content, $pattern, $replacement, 1)
$content = $content.Replace(
    '#property description "TradeMind AI v1.4 read-only tick and volume intelligence exporter"',
    '#property description "TradeMind AI v1.4 read-only FX majors volume intelligence exporter"'
)

$terminalRoot = Join-Path $env:APPDATA "MetaQuotes\Terminal"
if (-not (Test-Path $terminalRoot)) {
    throw "MetaTrader terminal data root not found: $terminalRoot"
}

$preferred = Get-ChildItem -Path $terminalRoot -Recurse -File -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -in @("TradeMindAI_VolumeExporter_v1_4_FIXED.mq5", "TradeMindAI_VolumeExporter_v1_4.mq5") } |
    Select-Object -ExpandProperty DirectoryName -Unique

if (-not $preferred) {
    $preferred = Get-ChildItem -Path $terminalRoot -Directory -ErrorAction Stop |
        ForEach-Object { Join-Path $_.FullName "MQL5\Experts" } |
        Where-Object { Test-Path $_ }
}
if (-not $preferred) {
    throw "No MetaTrader MQL5\\Experts directories were found."
}

$written = @()
foreach ($expertsDir in $preferred) {
    $targetPath = Join-Path $expertsDir $TargetName
    [System.IO.File]::WriteAllText($targetPath, $content, [System.Text.UTF8Encoding]::new($false))
    $written += $targetPath
    Write-Host "Deployed FX exporter: $targetPath"
}

Write-Host "FX symbols: $($symbols -join ',')"
Write-Host "Targets written: $($written.Count)"
Write-Host "Existing market exporter was not changed."
Write-Host "No orders can be sent by this exporter."

if ($Open -and $written.Count -gt 0) {
    Start-Process $written[0]
}
