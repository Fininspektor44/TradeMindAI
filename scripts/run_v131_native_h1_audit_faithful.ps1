param(
    [string]$Python = ".\.venv\Scripts\python.exe",
    [string]$Bars = "data\bybit_v1_9\bybit_bars.csv",
    [string]$OutputDir = "data\crypto_v131_native_h1_audit_faithful",
    [int]$MaxBars = 72,
    [double]$CostR = 0.04
)

$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)

if (-not (Test-Path $Python)) { throw "Python not found: $Python" }
if (-not (Test-Path $Bars)) { throw "Bybit bars not found: $Bars" }

& $Python -m trademind.crypto_v131_native_h1_audit_faithful --bars $Bars --output-dir $OutputDir --max-bars $MaxBars --cost-r $CostR
if ($LASTEXITCODE -ne 0) { throw "Faithful native H1 audit failed with exit code $LASTEXITCODE" }
