param(
    [string]$Symbols = "BTCUSDT,ETHUSDT",
    [string]$Start = "2023-01-01T00:00:00+00:00",
    [string]$HoldoutStart = "2025-01-01T00:00:00+00:00",
    [string]$End = "2026-08-01T00:00:00+00:00",
    [double]$Threshold = 0.0001,
    [double]$FeeBps = 5.5,
    [double]$SlippageBps = 1.0,
    [switch]$Refresh
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) { throw "Python venv not found: $Python" }

$argsList = @(
    "-m", "trademind.funding_carry_v137",
    "--symbols", $Symbols,
    "--start", $Start,
    "--holdout-start", $HoldoutStart,
    "--end", $End,
    "--threshold", $Threshold,
    "--fee-bps", $FeeBps,
    "--slippage-bps", $SlippageBps
)
if ($Refresh) { $argsList += "--refresh" }

& $Python @argsList
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
