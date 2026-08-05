param(
    [Parameter(Mandatory=$true)]
    [string]$Passport,

    [Parameter(Mandatory=$true)]
    [string]$Account,

    [Parameter(Mandatory=$true)]
    [string]$Instrument,

    [Parameter(Mandatory=$false)]
    [string]$Profile = ".\config\risk_profiles\standard_v1.json",

    [Parameter(Mandatory=$false)]
    [string]$Portfolio = "",

    [Parameter(Mandatory=$false)]
    [string]$Output = ".\data\risk_manager_v1\decision.json",

    [Parameter(Mandatory=$false)]
    [string]$Journal = ".\data\signal_intelligence\events.jsonl",

    [Parameter(Mandatory=$false)]
    [double]$RequestedRiskPct = -1,

    [switch]$RunTests
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo

$python = Join-Path $repo ".venv\Scripts\python.exe"
if (!(Test-Path $python)) {
    throw "Python environment not found: $python"
}

foreach ($path in @($Passport, $Account, $Instrument, $Profile)) {
    if (!(Test-Path $path)) {
        throw "Required input not found: $path"
    }
}
if ($Portfolio -and !(Test-Path $Portfolio)) {
    throw "Portfolio input not found: $Portfolio"
}

if ($RunTests) {
    & $python -m pytest -q `
        ".\tests\test_risk_manager.py" `
        ".\tests\test_signal_intelligence.py"
    if ($LASTEXITCODE -ne 0) {
        throw "Risk Manager tests failed"
    }
}

$arguments = @(
    "-m", "trademind.risk_manager",
    "--passport", $Passport,
    "--account", $Account,
    "--instrument", $Instrument,
    "--profile", $Profile,
    "--output", $Output
)
if ($Portfolio) {
    $arguments += @("--portfolio", $Portfolio)
}
if ($Journal) {
    $arguments += @("--journal", $Journal)
}
if ($RequestedRiskPct -gt 0) {
    $arguments += @("--requested-risk-pct", $RequestedRiskPct.ToString(
        [System.Globalization.CultureInfo]::InvariantCulture
    ))
}

& $python @arguments
if ($LASTEXITCODE -ne 0) {
    throw "Risk Manager execution failed"
}

Write-Host "`nRisk Manager decision: $Output" -ForegroundColor Cyan
Write-Host "Read-only. Orders OFF. Broker API not called." -ForegroundColor Green
