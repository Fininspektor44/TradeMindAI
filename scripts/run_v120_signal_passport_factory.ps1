param(
    [Parameter(Mandatory=$false)]
    [string]$Candidates = ".\data\signal_intelligence_v1_16\candidates.jsonl",

    [Parameter(Mandatory=$false)]
    [string]$Outcomes = ".\data\signal_intelligence_v1_16\outcomes.jsonl",

    [Parameter(Mandatory=$false)]
    [string]$OutputDir = ".\data\signal_passport_factory_v1",

    [Parameter(Mandatory=$false)]
    [string]$PassportsDir = "",

    [Parameter(Mandatory=$false)]
    [string]$Journal = ".\data\signal_intelligence_v1_16\events.jsonl",

    [Parameter(Mandatory=$false)]
    [double]$CostR = 0.04,

    [Parameter(Mandatory=$false)]
    [int]$RecentWindow = 30,

    [Parameter(Mandatory=$false)]
    [double]$MaximumCandidateAgeSeconds = 900,

    [Parameter(Mandatory=$false)]
    [double]$MaximumClockSkewSeconds = 30,

    [Parameter(Mandatory=$false)]
    [int]$CandidateLimit = 0,

    [switch]$RunTests
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo

$python = Join-Path $repo ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    throw "Python environment not found: $python"
}
foreach ($path in @($Candidates, $Outcomes)) {
    if (-not (Test-Path $path)) {
        throw "Required Signal Passport Factory input not found: $path"
    }
}
if ([string]::IsNullOrWhiteSpace($PassportsDir)) {
    $PassportsDir = Join-Path $OutputDir "passports"
}
New-Item -ItemType Directory -Path $OutputDir, $PassportsDir -Force | Out-Null

if ($RunTests) {
    & $python -m pytest -q `
        ".\tests\test_signal_passport_factory.py" `
        ".\tests\test_signal_to_risk_bridge.py" `
        ".\tests\test_mt5_risk_adapter.py" `
        ".\tests\test_risk_manager.py" `
        ".\tests\test_signal_intelligence.py"
    if ($LASTEXITCODE -ne 0) {
        throw "Signal Passport Factory tests failed"
    }
}

$arguments = @(
    "-m", "trademind.signal_passport_factory",
    "--candidates", $Candidates,
    "--outcomes", $Outcomes,
    "--output-dir", $OutputDir,
    "--passports-dir", $PassportsDir,
    "--cost-r", $CostR.ToString([System.Globalization.CultureInfo]::InvariantCulture),
    "--recent-window", "$RecentWindow",
    "--maximum-candidate-age-seconds",
    $MaximumCandidateAgeSeconds.ToString([System.Globalization.CultureInfo]::InvariantCulture),
    "--maximum-clock-skew-seconds",
    $MaximumClockSkewSeconds.ToString([System.Globalization.CultureInfo]::InvariantCulture)
)
if ($Journal) {
    $arguments += @("--journal", $Journal)
}
if ($CandidateLimit -gt 0) {
    $arguments += @("--candidate-limit", "$CandidateLimit")
}

& $python @arguments
if ($LASTEXITCODE -ne 0) {
    throw "Signal Passport Factory execution failed"
}

Write-Host "`nSignal Passport Factory output: $OutputDir" -ForegroundColor Cyan
Write-Host "Bridge inbox: $PassportsDir" -ForegroundColor Cyan
Write-Host "Read-only. Orders OFF. Publication OFF. No future-data leakage." `
    -ForegroundColor Green
