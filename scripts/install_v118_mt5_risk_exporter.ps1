param(
    [string]$ProjectRoot = "",
    [string]$TerminalDataPath = "",
    [switch]$OpenFolder
)

$ErrorActionPreference = "Stop"
if ([string]::IsNullOrWhiteSpace($ProjectRoot)) {
    $ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
}

$source = Join-Path $ProjectRoot "mt5\TradeMind_MT5_Risk_Snapshot_Exporter.mq5"
if (-not (Test-Path $source)) {
    throw "MT5 risk exporter source not found: $source"
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
    $target = Join-Path $targetDir "TradeMind_MT5_Risk_Snapshot_Exporter.mq5"
    Copy-Item -Path $source -Destination $target -Force
    $installed += $target
}

Write-Host "`n=== TRADEMIND v1.18 MT5 RISK EXPORTER INSTALL ===" -ForegroundColor Cyan
Write-Host "Copied read-only exporter to:" -ForegroundColor Green
$installed | ForEach-Object { Write-Host "  $_" }
Write-Host "`nNext: compile TradeMind_MT5_Risk_Snapshot_Exporter.mq5 in MetaEditor and attach it to one chart on each account." -ForegroundColor Yellow
Write-Host "Keep InpOutputFolder=TradeMindAI. The EA reads the account, positions and Market Watch only."
Write-Host "It contains no order-send, close or modify calls." -ForegroundColor Green

if ($OpenFolder) {
    Start-Process explorer.exe -ArgumentList "/select,`"$($installed[0])`""
}
