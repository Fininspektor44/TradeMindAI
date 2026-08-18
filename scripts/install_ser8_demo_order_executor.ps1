param(
    [string]$ProjectRoot = "",
    [string]$TerminalDataPath = "",
    [switch]$OpenFolder
)

$ErrorActionPreference = "Stop"
if ([string]::IsNullOrWhiteSpace($ProjectRoot)) {
    $ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
}

$source = Join-Path $ProjectRoot "mt5\TradeMind_Demo_Order_Executor_v1.mq5"
if (-not (Test-Path $source)) {
    throw "SER8 demo order executor source not found: $source"
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
    $target = Join-Path $targetDir "TradeMind_Demo_Order_Executor_v1.mq5"
    Copy-Item -Path $source -Destination $target -Force
    $installed += $target
}

Write-Host "`n=== TRADEMIND SER8 UNIFIED MT5 DEMO ORDER EXECUTOR INSTALL ===" -ForegroundColor Cyan
Write-Host "Copied unified executor (order execution + read-only risk snapshot) to:" -ForegroundColor Green
$installed | ForEach-Object { Write-Host "  $_" }
Write-Host "`nNext: compile TradeMind_Demo_Order_Executor_v1.mq5 in MetaEditor and attach it to ONE chart" -ForegroundColor Yellow
Write-Host "on ONE DEMO/PAPER account. This is the ONLY EA required for the SER8 demo path -- do NOT" -ForegroundColor Yellow
Write-Host "also attach TradeMind_MT5_Risk_Snapshot_Exporter.mq5 for this account; it remains only for" -ForegroundColor Yellow
Write-Host "other, non-SER8 pipelines that still read the same mt5_risk_*_utc_*.csv files." -ForegroundColor Yellow
Write-Host "Keep InpOutputFolder=TradeMindAI. Never attach this EA to a live account." -ForegroundColor Green

if ($OpenFolder) {
    Start-Process explorer.exe -ArgumentList "/select,`"$($installed[0])`""
}
