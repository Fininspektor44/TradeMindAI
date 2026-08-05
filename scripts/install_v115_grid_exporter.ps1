param(
    [string]$ProjectRoot = "",
    [string]$TerminalDataPath = "",
    [switch]$OpenFolder
)

$ErrorActionPreference = "Stop"
if ([string]::IsNullOrWhiteSpace($ProjectRoot)) {
    $ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
}
$source = Join-Path $ProjectRoot "mt5\TradeMind_Grid_Deal_Exporter.mq5"
if (-not (Test-Path $source)) {
    throw "Grid exporter source not found: $source"
}

$destinations = @()
if (-not [string]::IsNullOrWhiteSpace($TerminalDataPath)) {
    $experts = Join-Path $TerminalDataPath "MQL5\Experts"
    if (-not (Test-Path $experts)) {
        throw "MQL5 Experts folder not found: $experts"
    }
    $destinations += $experts
}
else {
    $terminalRoot = Join-Path $env:APPDATA "MetaQuotes\Terminal"
    if (-not (Test-Path $terminalRoot)) {
        throw "MetaTrader terminal data root not found: $terminalRoot"
    }
    $destinations = Get-ChildItem $terminalRoot -Directory | ForEach-Object {
        $experts = Join-Path $_.FullName "MQL5\Experts"
        if (Test-Path $experts) { $experts }
    }
}

if (@($destinations).Count -eq 0) {
    throw "No MT5 MQL5\\Experts folders were found."
}

$installed = @()
foreach ($experts in $destinations) {
    $targetDir = Join-Path $experts "TradeMindAI"
    New-Item -ItemType Directory -Path $targetDir -Force | Out-Null
    $target = Join-Path $targetDir "TradeMind_Grid_Deal_Exporter.mq5"
    Copy-Item -Path $source -Destination $target -Force
    $installed += $target
}

Write-Host "`n=== TRADEMIND v1.15 GRID EXPORTER INSTALL ===" -ForegroundColor Cyan
Write-Host "Copied read-only exporter to:" -ForegroundColor Green
$installed | ForEach-Object { Write-Host "  $_" }
Write-Host "`nNext: open MetaEditor, compile TradeMind_Grid_Deal_Exporter.mq5, then attach it to one separate chart." -ForegroundColor Yellow
Write-Host "The exporter reads deal history only. It contains no order-send or position-modify calls."

if ($OpenFolder) {
    Start-Process explorer.exe -ArgumentList "/select,`"$($installed[0])`""
}
