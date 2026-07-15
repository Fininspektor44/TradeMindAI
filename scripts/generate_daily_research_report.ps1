param(
    [string]$ProjectDir = (Split-Path -Parent $PSScriptRoot),
    [string]$DataDir = "$env:APPDATA\MetaQuotes\Terminal\Common\Files\TradeMindAI_ECN",
    [string]$Symbols = "XAUUSD,XAGUSD,.USTECHCash,.US500Cash,.US30Cash,WTI,BRENT",
    [string]$Timeframe = "M5",
    [int]$MaxAgeMinutes = 30,
    [int]$CandidateMinimum = 30,
    [int]$MinimumSample = 300,
    [string]$ReportsDir = "",
    [string]$HistoryDir = ""
)

$ErrorActionPreference = "Stop"
$projectPath = (Resolve-Path $ProjectDir).Path
Set-Location $projectPath

$healthExe = Join-Path $projectPath ".venv\Scripts\trademind-health.exe"
$smcExe = Join-Path $projectPath ".venv\Scripts\trademind-smc-stats.exe"
$validationExe = Join-Path $projectPath ".venv\Scripts\trademind-validate.exe"
$candidateWatchExe = Join-Path $projectPath ".venv\Scripts\trademind-candidate-watch.exe"
$dashboardScript = Join-Path $projectPath "scripts\generate_dashboard.ps1"
$journalFile = Join-Path $projectPath "data\journal_ecn\signals.csv"

if (-not (Test-Path $healthExe -PathType Leaf)) {
    throw "TradeMind health executable not found: $healthExe"
}
if (-not (Test-Path $smcExe -PathType Leaf)) {
    throw "TradeMind SMC report executable not found: $smcExe"
}
if (-not (Test-Path $validationExe -PathType Leaf)) {
    throw "TradeMind validation executable not found: $validationExe"
}
if (-not (Test-Path $candidateWatchExe -PathType Leaf)) {
    throw "TradeMind candidate watcher executable not found: $candidateWatchExe"
}
if (-not (Test-Path $dashboardScript -PathType Leaf)) {
    throw "TradeMind dashboard script not found: $dashboardScript"
}

if ([string]::IsNullOrWhiteSpace($ReportsDir)) {
    $ReportsDir = Join-Path $projectPath "data\research_reports"
}
if ([string]::IsNullOrWhiteSpace($HistoryDir)) {
    $HistoryDir = Join-Path $projectPath "data\candidate_history"
}

$dayFolder = Join-Path $ReportsDir (Get-Date -Format "yyyy-MM-dd")
New-Item -ItemType Directory -Path $dayFolder -Force | Out-Null
$timestamp = Get-Date -Format "yyyy-MM-dd_HHmmss"
$reportPath = Join-Path $dayFolder "research_$timestamp.txt"
$latestPath = Join-Path $ReportsDir "latest.txt"

@(
    "TradeMind daily ECN research report"
    "Generated local: $((Get-Date).ToString('o'))"
    "Project: $projectPath"
    "Data: $DataDir"
    "Journal: $journalFile"
    "Symbols: $Symbols"
    "Timeframe: $Timeframe"
    "Candidate minimum: $CandidateMinimum"
    "Research minimum: $MinimumSample"
    "Candidate history: $HistoryDir"
    ""
    "============================================================"
    "DATA HEALTH"
    "============================================================"
) | Set-Content $reportPath -Encoding utf8

$healthOutput = & $healthExe `
    --data-dir $DataDir `
    --journal $journalFile `
    --symbols $Symbols `
    --timeframe $Timeframe `
    --max-age-minutes $MaxAgeMinutes 2>&1
$healthCode = $LASTEXITCODE
$healthOutput | Add-Content $reportPath -Encoding utf8

foreach ($horizon in @(3, 6, 12)) {
    @(
        ""
        "============================================================"
        "SMC RESEARCH, HORIZON $horizon CANDLES"
        "============================================================"
    ) | Add-Content $reportPath -Encoding utf8

    $smcOutput = & $smcExe `
        --journal $journalFile `
        --horizon $horizon `
        --non-overlap `
        --by-symbol `
        --min-sample $MinimumSample 2>&1
    $smcCode = $LASTEXITCODE
    $smcOutput | Add-Content $reportPath -Encoding utf8
    if ($smcCode -ne 0) {
        "SMC report exited with code $smcCode" | Add-Content $reportPath -Encoding utf8
    }
}

@(
    ""
    "============================================================"
    "PER-SYMBOL STABILITY VALIDATION"
    "============================================================"
) | Add-Content $reportPath -Encoding utf8

$validationOutput = & $validationExe `
    --journal $journalFile `
    --candidate-min $CandidateMinimum `
    --min-sample $MinimumSample 2>&1
$validationCode = $LASTEXITCODE
$validationOutput | Add-Content $reportPath -Encoding utf8
if ($validationCode -ne 0) {
    "Validation exited with code $validationCode" | Add-Content $reportPath -Encoding utf8
}

@(
    ""
    "============================================================"
    "CANDIDATE WATCHER"
    "============================================================"
) | Add-Content $reportPath -Encoding utf8

$candidateOutput = & $candidateWatchExe `
    --journal $journalFile `
    --history-dir $HistoryDir `
    --symbols $Symbols `
    --candidate-min $CandidateMinimum `
    --min-sample $MinimumSample 2>&1
$candidateCode = $LASTEXITCODE
$candidateOutput | Add-Content $reportPath -Encoding utf8
if ($candidateCode -ne 0) {
    "Candidate watcher exited with code $candidateCode" | Add-Content $reportPath -Encoding utf8
}

Copy-Item $reportPath $latestPath -Force
Write-Host "TradeMind research report saved: $reportPath"
Write-Host "Latest report copy: $latestPath"
Write-Host "Health exit code: $healthCode"
Write-Host "Validation exit code: $validationCode"
Write-Host "Candidate watcher exit code: $candidateCode"

& $dashboardScript `
    -ProjectDir $projectPath `
    -DataDir $DataDir `
    -Symbols $Symbols `
    -Timeframe $Timeframe `
    -CandidateMinimum $CandidateMinimum `
    -MinimumSample $MinimumSample `
    -MaxAgeMinutes $MaxAgeMinutes `
    -HistoryDir $HistoryDir `
    -SkipCandidateWatch

if ($healthCode -ge 2) {
    Write-Warning "Research data health contains ERROR items. Open the report before trusting statistics."
    exit $healthCode
}
if ($validationCode -ne 0) {
    throw "TradeMind validation failed with exit code $validationCode"
}
if ($candidateCode -ne 0) {
    throw "TradeMind candidate watcher failed with exit code $candidateCode"
}

Write-Host "TradeMind daily research report completed successfully."
