param(
    [string]$Decisions = ".\data\bybit_shadow_v1_10\decisions.csv",
    [string]$Bars = ".\data\bybit_v1_9\bybit_bars.csv",
    [string]$OutputDir = ".\data\crypto_signal_intelligence_v1_33_1_shadow",
    [int]$BatchSize = 400,
    [switch]$RunTests
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo
$python = Join-Path $repo ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { throw "Python environment not found: $python" }
if (-not (Test-Path $Decisions)) { throw "Decisions not found: $Decisions" }
if (-not (Test-Path $Bars)) { throw "Bybit bars not found: $Bars" }

if ($RunTests) {
    & $python -m pytest -q ".\tests\test_crypto_h1_swing_filter_v133.py"
    if ($LASTEXITCODE -ne 0) { throw "v1.33.1 tests failed" }
}

& $python -m trademind.crypto_h1_swing_incremental_v133 `
    --decisions $Decisions `
    --bars $Bars `
    --output-dir $OutputDir `
    --batch-size $BatchSize
if ($LASTEXITCODE -ne 0) { throw "v1.33.1 crypto shadow run failed" }

$statusPath = Join-Path $OutputDir "status.json"
$rejectionsPath = Join-Path $OutputDir "rejections.jsonl"
$candidatesPath = Join-Path $OutputDir "candidates.jsonl"

Write-Host "`n===== v1.33.1 SHADOW SUMMARY =====" -ForegroundColor Cyan
if (Test-Path $statusPath) {
    Get-Content $statusPath -Raw | ConvertFrom-Json |
        Select-Object state,updated_at,processed_batch,eligible_candidates,rejected_decisions,errors,remaining_decisions |
        Format-List
}

if (Test-Path $rejectionsPath) {
    Write-Host "TOP REJECTION REASONS:" -ForegroundColor Yellow
    Get-Content $rejectionsPath | ForEach-Object { $_ | ConvertFrom-Json } |
        ForEach-Object { $_.reasons } |
        Group-Object |
        Sort-Object Count -Descending |
        Select-Object -First 12 Count,Name |
        Format-Table -AutoSize
}

if (Test-Path $candidatesPath) {
    Write-Host "LATEST ELIGIBLE:" -ForegroundColor Green
    Get-Content $candidatesPath | ForEach-Object { $_ | ConvertFrom-Json } |
        Sort-Object observed_at -Descending |
        Select-Object -First 12 symbol,observed_at,@{N="Action";E={$_.plan.action}},@{N="RR";E={[math]::Round([double]$_.market_features.custom.target_rr,2)}} |
        Format-Table -AutoSize
}

Write-Host "READ-ONLY LEARNING SHADOW. Existing v1.32/v1.26 runtime state was not modified." -ForegroundColor Green
