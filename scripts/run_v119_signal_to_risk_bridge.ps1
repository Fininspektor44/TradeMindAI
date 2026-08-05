param(
    [Parameter(Mandatory=$true)]
    [string]$Login,

    [Parameter(Mandatory=$false)]
    [string]$Passport = "",

    [Parameter(Mandatory=$false)]
    [string]$PassportsDir = ".\data\signal_intelligence_v1_16\passports",

    [Parameter(Mandatory=$false)]
    [string]$CommonFilesRoot = "",

    [Parameter(Mandatory=$false)]
    [string]$Profile = ".\config\risk_profiles\standard_v1.json",

    [Parameter(Mandatory=$false)]
    [string]$Correlations = ".\config\mt5\correlation_groups_v1.json",

    [Parameter(Mandatory=$false)]
    [string]$OutputDir = "",

    [Parameter(Mandatory=$false)]
    [string]$Journal = ".\data\signal_intelligence_v1_16\events.jsonl",

    [Parameter(Mandatory=$false)]
    [double]$RequestedRiskPct = -1,

    [Parameter(Mandatory=$false)]
    [double]$CostR = 0.04,

    [Parameter(Mandatory=$false)]
    [double]$MaximumMT5AgeSeconds = 120,

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
    $OutputDir = Join-Path $repo "data\signal_to_risk_bridge_v1\$Login"
}

$accountCsv = Join-Path $CommonFilesRoot "mt5_risk_account_utc_$Login.csv"
$positionsCsv = Join-Path $CommonFilesRoot "mt5_risk_positions_utc_$Login.csv"
$symbolsCsv = Join-Path $CommonFilesRoot "mt5_risk_symbols_utc_$Login.csv"
foreach ($path in @($accountCsv, $positionsCsv, $symbolsCsv, $Profile)) {
    if (-not (Test-Path $path)) {
        throw "Required bridge input not found: $path"
    }
}
if ($Correlations -and -not (Test-Path $Correlations)) {
    throw "Correlation mapping not found: $Correlations"
}
if (-not [string]::IsNullOrWhiteSpace($Passport)) {
    if (-not (Test-Path $Passport)) {
        throw "Signal passport not found: $Passport"
    }
}
else {
    New-Item -ItemType Directory -Path $PassportsDir -Force | Out-Null
}

if ($RunTests) {
    & $python -m pytest -q `
        ".\tests\test_signal_to_risk_bridge.py" `
        ".\tests\test_mt5_risk_adapter.py" `
        ".\tests\test_risk_manager.py" `
        ".\tests\test_signal_intelligence.py"
    if ($LASTEXITCODE -ne 0) {
        throw "Signal-to-Risk Bridge tests failed"
    }
}

$arguments = @(
    "-m", "trademind.signal_to_risk_bridge",
    "--login", $Login,
    "--account-csv", $accountCsv,
    "--positions-csv", $positionsCsv,
    "--symbols-csv", $symbolsCsv,
    "--profile", $Profile,
    "--cost-r", $CostR.ToString([System.Globalization.CultureInfo]::InvariantCulture),
    "--maximum-mt5-age-seconds", $MaximumMT5AgeSeconds.ToString([System.Globalization.CultureInfo]::InvariantCulture),
    "--output-dir", $OutputDir
)
if (-not [string]::IsNullOrWhiteSpace($Passport)) {
    $arguments += @("--passport", $Passport)
}
else {
    $arguments += @("--passports-dir", $PassportsDir)
}
if ($Correlations) {
    $arguments += @("--correlations", $Correlations)
}
if ($Journal) {
    $arguments += @("--journal", $Journal)
}
if ($RequestedRiskPct -gt 0) {
    $arguments += @(
        "--requested-risk-pct",
        $RequestedRiskPct.ToString([System.Globalization.CultureInfo]::InvariantCulture)
    )
}

& $python @arguments
if ($LASTEXITCODE -ne 0) {
    throw "Signal-to-Risk Bridge execution failed"
}

Write-Host "`nSignal-to-Risk Bridge output: $OutputDir" -ForegroundColor Cyan
Write-Host "Read-only. Orders OFF. Publication OFF. Broker API not called." -ForegroundColor Green
