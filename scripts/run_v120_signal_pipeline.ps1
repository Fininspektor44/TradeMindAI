param(
    [Parameter(Mandatory=$true)]
    [string]$Login,

    [Parameter(Mandatory=$false)]
    [string]$Candidates = ".\data\signal_intelligence_v1_16\candidates.jsonl",

    [Parameter(Mandatory=$false)]
    [string]$Outcomes = ".\data\signal_intelligence_v1_16\outcomes.jsonl",

    [Parameter(Mandatory=$false)]
    [string]$FactoryOutputDir = ".\data\signal_passport_factory_v1",

    [Parameter(Mandatory=$false)]
    [string]$CommonFilesRoot = "",

    [Parameter(Mandatory=$false)]
    [string]$Profile = ".\config\risk_profiles\standard_v1.json",

    [Parameter(Mandatory=$false)]
    [string]$Correlations = ".\config\mt5\correlation_groups_v1.json",

    [Parameter(Mandatory=$false)]
    [double]$RequestedRiskPct = -1,

    [Parameter(Mandatory=$false)]
    [double]$CostR = 0.04,

    [Parameter(Mandatory=$false)]
    [double]$MaximumCandidateAgeSeconds = 900,

    [Parameter(Mandatory=$false)]
    [double]$MaximumMT5AgeSeconds = 120,

    [switch]$RunTests
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo
$passportsDir = Join-Path $FactoryOutputDir "passports"
$journal = ".\data\signal_intelligence_v1_16\events.jsonl"

$factoryArguments = @{
    Candidates = $Candidates
    Outcomes = $Outcomes
    OutputDir = $FactoryOutputDir
    PassportsDir = $passportsDir
    Journal = $journal
    CostR = $CostR
    MaximumCandidateAgeSeconds = $MaximumCandidateAgeSeconds
}
if ($RunTests) {
    $factoryArguments["RunTests"] = $true
}
& "$PSScriptRoot\run_v120_signal_passport_factory.ps1" @factoryArguments
if ($LASTEXITCODE -ne 0) {
    throw "Signal Passport Factory pipeline stage failed"
}

$bridgeArguments = @{
    Login = $Login
    PassportsDir = $passportsDir
    Profile = $Profile
    Correlations = $Correlations
    Journal = $journal
    CostR = $CostR
    MaximumMT5AgeSeconds = $MaximumMT5AgeSeconds
}
if (-not [string]::IsNullOrWhiteSpace($CommonFilesRoot)) {
    $bridgeArguments["CommonFilesRoot"] = $CommonFilesRoot
}
if ($RequestedRiskPct -gt 0) {
    $bridgeArguments["RequestedRiskPct"] = $RequestedRiskPct
}
& "$PSScriptRoot\run_v119_signal_to_risk_bridge.ps1" @bridgeArguments
if ($LASTEXITCODE -ne 0) {
    throw "Signal-to-Risk Bridge pipeline stage failed"
}

Write-Host "`nTradeMind v1.20 signal pipeline completed." -ForegroundColor Cyan
Write-Host "Candidate -> Passport Factory -> MT5 Risk Bridge" -ForegroundColor Cyan
Write-Host "Orders OFF. Publication OFF. Manual approval not requested." `
    -ForegroundColor Green
