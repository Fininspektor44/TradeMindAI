param(
    [string]$Symbols = "BTCUSDT,ETHUSDT",
    [string]$PaperStart = "2021-01-01T00:00:00+00:00",
    [string]$PaperEnd = "2024-08-01T00:00:00+00:00",
    [string]$HoldoutEnd = "2026-08-01T00:00:00+00:00",
    [switch]$Refresh
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo
$python = Join-Path $repo ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { throw "Python environment not found: $python" }

Write-Host "TradeMind v1.36 DGT PAPER -> FROZEN HOLDOUT" -ForegroundColor Cyan
Write-Host "Paper: arXiv:2506.11921 | Binance spot 1m | read-only" -ForegroundColor DarkGray
Write-Host "This can take time on the first run because monthly 1m archives are downloaded and cached." -ForegroundColor Yellow

$argsList = @(
    "-m", "trademind.dgt_v136",
    "--symbols", $Symbols,
    "--paper-start", $PaperStart,
    "--paper-end", $PaperEnd,
    "--holdout-end", $HoldoutEnd
)
if ($Refresh) { $argsList += "--refresh" }

& $python @argsList
if ($LASTEXITCODE -ne 0) { throw "v1.36 DGT reproduction failed with exit code $LASTEXITCODE" }

Write-Host "`nDONE. Existing TradeMind v1.32 runtime was not modified." -ForegroundColor Green
