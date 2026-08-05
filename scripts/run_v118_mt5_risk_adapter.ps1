param(
    [Parameter(Mandatory=$true)]
    [string]$Login,

    [Parameter(Mandatory=$false)]
    [string]$Symbol = "",

    [Parameter(Mandatory=$false)]
    [ValidateSet("BUY", "SELL", "")]
    [string]$Action = "",

    [Parameter(Mandatory=$false)]
    [string]$Passport = "",

    [Parameter(Mandatory=$false)]
    [string]$CommonFilesRoot = "",

    [Parameter(Mandatory=$false)]
    [string]$OutputDir = "",

    [Parameter(Mandatory=$false)]
    [string]$Correlations = ".\config\mt5\correlation_groups_v1.json",

    [Parameter(Mandatory=$false)]
    [double]$MaximumAgeSeconds = 120,

    [switch]$RunTests
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo

$python = Join-Path $repo ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    throw "Python environment not found: $python"
}

if ([string]::IsNullOrWhiteSpace($CommonFilesRoot)) {
    $CommonFilesRoot = Join-Path $env:APPDATA "MetaQuotes\Terminal\Common\Files\TradeMindAI"
}
if ([string]::IsNullOrWhiteSpace($OutputDir)) {
    $OutputDir = Join-Path $repo "data\mt5_risk_adapter_v1\$Login"
}

$accountCsv = Join-Path $CommonFilesRoot "mt5_risk_account_$Login.csv"
$positionsCsv = Join-Path $CommonFilesRoot "mt5_risk_positions_$Login.csv"
$symbolsCsv = Join-Path $CommonFilesRoot "mt5_risk_symbols_$Login.csv"
foreach ($path in @($accountCsv, $positionsCsv, $symbolsCsv)) {
    if (-not (Test-Path $path)) {
        throw "Required MT5 risk snapshot not found: $path"
    }
}
if ($Correlations -and -not (Test-Path $Correlations)) {
    throw "Correlation mapping not found: $Correlations"
}

if ($RunTests) {
    & $python -m pytest -q `
        ".\tests\test_mt5_risk_adapter.py" `
        ".\tests\test_risk_manager.py" `
        ".\tests\test_signal_intelligence.py"
    if ($LASTEXITCODE -ne 0) {
        throw "MT5 risk adapter tests failed"
    }
}

$arguments = @(
    "-m", "trademind.mt5_risk_adapter",
    "--account-csv", $accountCsv,
    "--positions-csv", $positionsCsv,
    "--symbols-csv", $symbolsCsv,
    "--maximum-age-seconds", $MaximumAgeSeconds.ToString([System.Globalization.CultureInfo]::InvariantCulture),
    "--output-dir", $OutputDir
)
if ($Correlations) {
    $arguments += @("--correlations", $Correlations)
}
if (-not [string]::IsNullOrWhiteSpace($Passport)) {
    if (-not (Test-Path $Passport)) {
        throw "Signal passport not found: $Passport"
    }
    $arguments += @("--passport", $Passport)
}
else {
    if ([string]::IsNullOrWhiteSpace($Symbol) -or [string]::IsNullOrWhiteSpace($Action)) {
        throw "Provide either -Passport or both -Symbol and -Action."
    }
    $arguments += @("--symbol", $Symbol, "--action", $Action)
}

& $python @arguments
if ($LASTEXITCODE -ne 0) {
    throw "MT5 risk adapter execution failed"
}

Write-Host "`nMT5 risk adapter output: $OutputDir" -ForegroundColor Cyan
Write-Host "Read-only. Orders OFF. Broker API not called." -ForegroundColor Green
