param(
    [string]$ProjectDir = (Split-Path -Parent $PSScriptRoot),
    [string]$DataDir = "$env:APPDATA\MetaQuotes\Terminal\Common\Files\TradeMindAI_ECN",
    [string]$Symbols = "XAUUSD,XAGUSD,.USTECHCash,.US500Cash,.US30Cash,WTI,BRENT",
    [string]$Timeframe = "M5",
    [int]$MaxAgeMinutes = 30,
    [int]$MinimumSample = 300,
    [string]$ReportsDir = ""
)

$ErrorActionPreference = "Stop"
$projectPath = (Resolve-Path $ProjectDir).Path
Set-Location $projectPath

$healthExe = Join-Path $projectPath ".venv\Scripts\trademind-health.exe"
$smcExe = Join-Path $projectPath ".venv\Scripts\trademind-smc-stats.exe"
$journalFile = Join-Path $projectPath "data\journal_ecn\signals.csv"

if (-not (Test-Path $healthExe -PathType Leaf)) {
    throw "TradeMind health executable not found: $healthExe"
}
if (-not (Test-Path $smcExe -PathType Leaf)) {
    throw "TradeMind SMC report executable not found: $smcExe"
}

if ([string]::IsNullOrWhiteSpace($ReportsDir)) {
    $ReportsDir = Join-Path $projectPath "data\research_reports"
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

Copy-Item $reportPath $latestPath -Force
Write-Host "TradeMind research report saved: $reportPath"
Write-Host "Latest report copy: $latestPath"
Write-Host "Health exit code: $healthCode"

if ($healthCode -ge 2) {
    Write-Warning "Research data health contains ERROR items. Open the report before trusting statistics."
    exit $healthCode
}

Write-Host "TradeMind daily research report completed successfully."
