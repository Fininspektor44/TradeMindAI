param(
    [Parameter(Mandatory=$true)]
    [string]$Login,

    [Parameter(Mandatory=$false)]
    [string]$DealsCsv = "",

    [Parameter(Mandatory=$false)]
    [string]$ShadowState = "",

    [Parameter(Mandatory=$false)]
    [string]$OutputDir = "",

    [switch]$RunTests
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo

$python = Join-Path $repo ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    throw "Python environment not found: $python"
}

if ([string]::IsNullOrWhiteSpace($ShadowState)) {
    $ShadowState = Join-Path $repo "data\breakeven_stat_monitor_v1\$Login\state.json"
}
if (-not (Test-Path $ShadowState)) {
    throw "v1.28 shadow state not found: $ShadowState"
}

if ([string]::IsNullOrWhiteSpace($OutputDir)) {
    $OutputDir = Join-Path $repo "data\breakeven_counterfactual_v1\$Login"
}

function Test-TradeMindDealCsv([string]$Path) {
    if (-not (Test-Path $Path -PathType Leaf)) { return $false }
    try {
        $header = Get-Content $Path -TotalCount 1 -ErrorAction Stop
        $required = @("ticket","position_id","time_msc","symbol","magic","deal_type","entry","volume","price")
        foreach ($name in $required) {
            if ($header -notmatch "(^|,)$name(,|$)") { return $false }
        }
        return $true
    }
    catch { return $false }
}

if ([string]::IsNullOrWhiteSpace($DealsCsv)) {
    $roots = @(
        (Join-Path $repo "data"),
        (Join-Path $env:APPDATA "MetaQuotes\Terminal\Common\Files\TradeMindAI")
    ) | Where-Object { Test-Path $_ }

    $candidates = @()
    foreach ($root in $roots) {
        $candidates += Get-ChildItem $root -Recurse -File -Filter "*.csv" -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -match $Login -or $_.FullName -match "grid_deals|deals" } |
            Sort-Object LastWriteTime -Descending
    }
    $match = $candidates | Where-Object { Test-TradeMindDealCsv $_.FullName } | Select-Object -First 1
    if ($null -eq $match) {
        throw "Read-only MT5 deal CSV not auto-detected. Pass -DealsCsv explicitly. Exporter settings were not changed."
    }
    $DealsCsv = $match.FullName
}

if (-not (Test-TradeMindDealCsv $DealsCsv)) {
    throw "Invalid MT5 deal CSV schema: $DealsCsv"
}

if ($RunTests) {
    & $python -m pytest -q ".\tests\test_breakeven_counterfactual.py"
    if ($LASTEXITCODE -ne 0) {
        throw "v1.29 counterfactual tests failed"
    }
}

Write-Host "Deals CSV: $DealsCsv" -ForegroundColor Cyan
& $python -m trademind.breakeven_counterfactual `
    --shadow-state $ShadowState `
    --deals $DealsCsv `
    --output-dir $OutputDir `
    --login $Login
if ($LASTEXITCODE -ne 0) {
    throw "v1.29 counterfactual execution failed"
}

Write-Host "`nBreakEven counterfactual: $OutputDir" -ForegroundColor Cyan
Write-Host "READ-ONLY. Shadow comparison only. Orders OFF. Robot/exporter settings unchanged." -ForegroundColor Green
