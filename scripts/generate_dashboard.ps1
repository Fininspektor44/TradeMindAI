param(
    [string]$ProjectDir = (Split-Path -Parent $PSScriptRoot),
    [string]$DataDir = "$env:APPDATA\MetaQuotes\Terminal\Common\Files\TradeMindAI_ECN",
    [string]$Symbols = "XAUUSD,XAGUSD,.USTECHCash,.US500Cash,.US30Cash,WTI,BRENT",
    [string]$Timeframe = "M5",
    [int]$CandidateMinimum = 30,
    [int]$MinimumSample = 300,
    [int]$MaxAgeMinutes = 30,
    [string]$HistoryDir = "",
    [string]$Output = "",
    [switch]$SkipCandidateWatch,
    [switch]$Open
)

$ErrorActionPreference = "Stop"
$projectPath = (Resolve-Path $ProjectDir).Path
Set-Location $projectPath

$dashboardExe = Join-Path $projectPath ".venv\Scripts\trademind-dashboard.exe"
$candidateWatchExe = Join-Path $projectPath ".venv\Scripts\trademind-candidate-watch.exe"
$journalFile = Join-Path $projectPath "data\journal_ecn\signals.csv"

if (-not (Test-Path $dashboardExe -PathType Leaf)) {
    throw "TradeMind dashboard executable not found: $dashboardExe"
}

if ([string]::IsNullOrWhiteSpace($HistoryDir)) {
    $HistoryDir = Join-Path $projectPath "data\candidate_history"
}
if ([string]::IsNullOrWhiteSpace($Output)) {
    $Output = Join-Path $projectPath "data\dashboard\index.html"
}

if (-not $SkipCandidateWatch) {
    if (-not (Test-Path $candidateWatchExe -PathType Leaf)) {
        throw "TradeMind candidate watcher executable not found: $candidateWatchExe"
    }

    Write-Host "Updating TradeMind candidate history..."
    & $candidateWatchExe `
        --journal $journalFile `
        --history-dir $HistoryDir `
        --symbols $Symbols `
        --candidate-min $CandidateMinimum `
        --min-sample $MinimumSample

    if ($LASTEXITCODE -ne 0) {
        throw "TradeMind candidate watcher exited with code $LASTEXITCODE"
    }
}

& $dashboardExe `
    --data-dir $DataDir `
    --journal $journalFile `
    --output $Output `
    --symbols $Symbols `
    --timeframe $Timeframe `
    --candidate-min $CandidateMinimum `
    --min-sample $MinimumSample `
    --max-age-minutes $MaxAgeMinutes

if ($LASTEXITCODE -ne 0) {
    throw "TradeMind dashboard exited with code $LASTEXITCODE"
}

Write-Host "TradeMind validation dashboard ready: $Output"

if ($Open) {
    Start-Process $Output
}
