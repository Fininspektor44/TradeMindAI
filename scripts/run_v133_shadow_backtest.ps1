param(
    [string]$Candidates = ".\data\crypto_signal_intelligence_v1_33_1_shadow\candidates.jsonl",
    [string]$Bars = ".\data\bybit_v1_9\bybit_bars.csv",
    [string]$OutputDir = ".\data\crypto_signal_intelligence_v1_33_1_backtest",
    [int]$MaxBars = 72,
    [double]$CostR = 0.04
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo
$python = Join-Path $repo ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { throw "Python environment not found: $python" }
if (-not (Test-Path $Candidates)) { throw "Candidates not found: $Candidates" }
if (-not (Test-Path $Bars)) { throw "Bars not found: $Bars" }

& $python -m trademind.crypto_v133_shadow_backtest `
    --candidates $Candidates `
    --bars $Bars `
    --output-dir $OutputDir `
    --max-bars $MaxBars `
    --cost-r $CostR.ToString([System.Globalization.CultureInfo]::InvariantCulture)
if ($LASTEXITCODE -ne 0) { throw "v1.33.1 historical shadow diagnostic failed" }

$status = Get-Content (Join-Path $OutputDir "status.json") -Raw | ConvertFrom-Json
Write-Host "`n===== BY SYMBOL =====" -ForegroundColor Cyan
$status.symbols |
    Sort-Object n -Descending |
    Select-Object symbol,n,wins,@{N="WR%";E={[math]::Round(100*[double]$_.win_rate,1)}},@{N="EV_R";E={[math]::Round([double]$_.ev_r,3)}},@{N="Net_R";E={[math]::Round([double]$_.net_r,2)}} |
    Format-Table -AutoSize

Write-Host "READ-ONLY. No orders, no publication, no exchange API." -ForegroundColor Green
Write-Host "This is in-sample historical diagnostics. Forward proof must be collected separately." -ForegroundColor Yellow
