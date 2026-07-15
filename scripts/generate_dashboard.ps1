param(
    [string]$ProjectDir = (Split-Path -Parent $PSScriptRoot),
    [string]$DataDir = "$env:APPDATA\MetaQuotes\Terminal\Common\Files\TradeMindAI_ECN",
    [string]$Symbols = "XAUUSD,XAGUSD,.USTECHCash,.US500Cash,.US30Cash,WTI,BRENT",
    [string]$Timeframe = "M5",
    [int]$MinimumSample = 300,
    [int]$MaxAgeMinutes = 30,
    [string]$Output = "",
    [switch]$Open
)

$ErrorActionPreference = "Stop"
$projectPath = (Resolve-Path $ProjectDir).Path
Set-Location $projectPath

$dashboardExe = Join-Path $projectPath ".venv\Scripts\trademind-dashboard.exe"
$journalFile = Join-Path $projectPath "data\journal_ecn\signals.csv"

if (-not (Test-Path $dashboardExe -PathType Leaf)) {
    throw "TradeMind dashboard executable not found: $dashboardExe"
}

if ([string]::IsNullOrWhiteSpace($Output)) {
    $Output = Join-Path $projectPath "data\dashboard\index.html"
}

& $dashboardExe `
    --data-dir $DataDir `
    --journal $journalFile `
    --output $Output `
    --symbols $Symbols `
    --timeframe $Timeframe `
    --min-sample $MinimumSample `
    --max-age-minutes $MaxAgeMinutes

if ($LASTEXITCODE -ne 0) {
    throw "TradeMind dashboard exited with code $LASTEXITCODE"
}

Write-Host "TradeMind dashboard ready: $Output"

if ($Open) {
    Start-Process $Output
}
