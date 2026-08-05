param(
    [Parameter(Mandatory=$false)]
    [string]$Observations = "data\fx_research_v1_4_2\observations.csv",

    [Parameter(Mandatory=$false)]
    [string]$Bars = "data\volume_v1_4\volume_bars.csv",

    [Parameter(Mandatory=$false)]
    [string]$OutputRoot = "data\signal_intelligence_v1_16",

    [Parameter(Mandatory=$false)]
    [int]$CandidateLimit = 0,

    [Parameter(Mandatory=$false)]
    [int]$MaxBars = 72,

    [Parameter(Mandatory=$false)]
    [double]$CostR = 0.04,

    [switch]$RunTests,

    [switch]$OpenDashboard
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo

$python = Join-Path $repo ".venv\Scripts\python.exe"
if (!(Test-Path $python)) {
    throw "Python environment not found: $python"
}

$observationsPath = [System.IO.Path]::GetFullPath((Join-Path $repo $Observations))
$barsPath = [System.IO.Path]::GetFullPath((Join-Path $repo $Bars))
$outputDir = [System.IO.Path]::GetFullPath((Join-Path $repo $OutputRoot))
$candidates = Join-Path $outputDir "candidates.jsonl"
$outcomes = Join-Path $outputDir "outcomes.jsonl"
$journal = Join-Path $outputDir "events.jsonl"
$errors = Join-Path $outputDir "candidate_errors.json"
$report = Join-Path $outputDir "report"
$dashboard = Join-Path $report "dashboard\index.html"
New-Item -ItemType Directory -Force -Path $outputDir, $report | Out-Null

if (!(Test-Path $observationsPath)) {
    throw "FX observations not found: $observationsPath"
}
if (!(Test-Path $barsPath)) {
    throw "Canonical M5 bars not found: $barsPath"
}

if ($RunTests) {
    & $python -m pytest -q `
        .\tests\test_signal_intelligence.py `
        .\tests\test_signal_evidence.py `
        .\tests\test_fx_signal_adapter.py `
        .\tests\test_signal_shadow.py `
        .\tests\test_signal_research_report.py
    if ($LASTEXITCODE -ne 0) {
        throw "TradeMind v1.16 signal tests failed"
    }
}

Write-Host "`n=== TRADEMIND v1.16 SIGNAL RESEARCH ===" -ForegroundColor Cyan
Write-Host "Observations: $observationsPath"
Write-Host "Bars:         $barsPath"
Write-Host "Output:       $outputDir"
Write-Host "Mode:         SHADOW ONLY"

$adapterArgs = @(
    "-m", "trademind.fx_signal_adapter",
    "--observations", $observationsPath,
    "--output", $candidates,
    "--errors", $errors
)
if ($CandidateLimit -gt 0) {
    $adapterArgs += @("--limit", "$CandidateLimit")
}
& $python @adapterArgs
if ($LASTEXITCODE -ne 0) {
    throw "FX candidate adapter failed"
}

& $python -m trademind.signal_shadow `
    --candidates $candidates `
    --bars $barsPath `
    --outcomes $outcomes `
    --journal $journal `
    --max-bars $MaxBars `
    --cost-r $CostR
if ($LASTEXITCODE -ne 0) {
    throw "Shadow outcome evaluation failed"
}

& $python -m trademind.signal_research_report `
    --candidates $candidates `
    --outcomes $outcomes `
    --output-dir $report
if ($LASTEXITCODE -ne 0) {
    throw "Signal research report failed"
}

Write-Host "`nSignal research pipeline completed." -ForegroundColor Green
Write-Host "Candidates: $candidates"
Write-Host "Outcomes:   $outcomes"
Write-Host "Journal:    $journal"
Write-Host "Dashboard:  $dashboard"
Write-Host "Orders OFF. Publication OFF. Grid robots are not signal sources." `
    -ForegroundColor Green

if ($OpenDashboard -and (Test-Path $dashboard)) {
    Start-Process $dashboard
}
