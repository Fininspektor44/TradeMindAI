param(
    [Parameter(Mandatory=$false)]
    [string]$Login = "37365712",

    [Parameter(Mandatory=$false)]
    [string]$VolumeSourceDir = "",

    [Parameter(Mandatory=$false)]
    [string]$CommonFilesRoot = "",

    [Parameter(Mandatory=$false)]
    [string]$CanonicalVolume = ".\data\volume_v1_4\volume_bars.csv",

    [Parameter(Mandatory=$false)]
    [string]$HistoricalOutcomes = ".\data\signal_intelligence_v1_16\outcomes.jsonl",

    [Parameter(Mandatory=$false)]
    [string]$RuntimeRoot = ".\data\live_signal_runtime_v1",

    [Parameter(Mandatory=$false)]
    [string]$Profile = ".\config\risk_profiles\standard_v1.json",

    [Parameter(Mandatory=$false)]
    [string]$Correlations = ".\config\mt5\correlation_groups_v1.json",

    [Parameter(Mandatory=$false)]
    [int]$ServerUTCOffsetHours = 3,

    [Parameter(Mandatory=$false)]
    [double]$CloseGraceSeconds = 5,

    [Parameter(Mandatory=$false)]
    [double]$BootstrapLookbackSeconds = 900,

    [Parameter(Mandatory=$false)]
    [double]$MaximumCandidateAgeSeconds = 900,

    [Parameter(Mandatory=$false)]
    [double]$MaximumMT5AgeSeconds = 120,

    [Parameter(Mandatory=$false)]
    [int]$MaxBars = 72,

    [Parameter(Mandatory=$false)]
    [double]$CostR = 0.04,

    [Parameter(Mandatory=$false)]
    [int]$DashboardCandidateLimit = 60,

    [switch]$RunTests,

    [switch]$OpenDashboard
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo

$python = Join-Path $repo ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    throw "Python environment not found: $python"
}
if ([string]::IsNullOrWhiteSpace($VolumeSourceDir)) {
    $VolumeSourceDir = Join-Path $env:APPDATA "MetaQuotes\Terminal\Common\Files\TradeMindAI_Volume_v1_4"
}
if ([string]::IsNullOrWhiteSpace($CommonFilesRoot)) {
    $CommonFilesRoot = Join-Path $env:APPDATA "MetaQuotes\Terminal\Common\Files\TradeMindAI"
}

$accountCsv = Join-Path $CommonFilesRoot "mt5_risk_account_utc_$Login.csv"
$positionsCsv = Join-Path $CommonFilesRoot "mt5_risk_positions_utc_$Login.csv"
$symbolsCsv = Join-Path $CommonFilesRoot "mt5_risk_symbols_utc_$Login.csv"
$journal = Join-Path $RuntimeRoot "events.jsonl"
$dashboard = Join-Path $RuntimeRoot "dashboard\index.html"

foreach ($path in @($HistoricalOutcomes, $Profile, $accountCsv, $positionsCsv, $symbolsCsv)) {
    if (-not (Test-Path $path)) {
        throw "Required live runtime input not found: $path"
    }
}
if (-not (Test-Path $VolumeSourceDir)) {
    throw "Volume source directory not found: $VolumeSourceDir"
}
if ($Correlations -and -not (Test-Path $Correlations)) {
    throw "Correlation mapping not found: $Correlations"
}
if ($ServerUTCOffsetHours -lt -14 -or $ServerUTCOffsetHours -gt 14) {
    throw "ServerUTCOffsetHours must be between -14 and 14"
}
if ($DashboardCandidateLimit -lt 1) {
    throw "DashboardCandidateLimit must be positive"
}

if ($RunTests) {
    & $python -m pytest -q `
        ".\tests\test_live_signal_dashboard.py" `
        ".\tests\test_live_signal_runtime.py" `
        ".\tests\test_signal_passport_factory.py" `
        ".\tests\test_signal_to_risk_bridge.py" `
        ".\tests\test_mt5_risk_adapter.py" `
        ".\tests\test_risk_manager.py" `
        ".\tests\test_signal_intelligence.py"
    if ($LASTEXITCODE -ne 0) {
        throw "Live Signal Runtime and Dashboard tests failed"
    }
}

$arguments = @(
    "-m", "trademind.live_signal_runtime",
    "--login", $Login,
    "--volume-source-dir", $VolumeSourceDir,
    "--canonical-volume", $CanonicalVolume,
    "--historical-outcomes", $HistoricalOutcomes,
    "--runtime-root", $RuntimeRoot,
    "--account-csv", $accountCsv,
    "--positions-csv", $positionsCsv,
    "--symbols-csv", $symbolsCsv,
    "--profile", $Profile,
    "--journal", $journal,
    "--server-utc-offset-hours", $ServerUTCOffsetHours,
    "--close-grace-seconds", $CloseGraceSeconds.ToString([System.Globalization.CultureInfo]::InvariantCulture),
    "--bootstrap-lookback-seconds", $BootstrapLookbackSeconds.ToString([System.Globalization.CultureInfo]::InvariantCulture),
    "--maximum-candidate-age-seconds", $MaximumCandidateAgeSeconds.ToString([System.Globalization.CultureInfo]::InvariantCulture),
    "--maximum-mt5-age-seconds", $MaximumMT5AgeSeconds.ToString([System.Globalization.CultureInfo]::InvariantCulture),
    "--max-bars", $MaxBars,
    "--cost-r", $CostR.ToString([System.Globalization.CultureInfo]::InvariantCulture)
)
if ($Correlations) {
    $arguments += @("--correlations", $Correlations)
}

& $python @arguments
if ($LASTEXITCODE -ne 0) {
    throw "Live Signal Runtime execution failed"
}

& $python -m trademind.live_signal_dashboard `
    --runtime-root $RuntimeRoot `
    --login $Login `
    --candidate-limit $DashboardCandidateLimit
if ($LASTEXITCODE -ne 0) {
    throw "Live Signal Dashboard execution failed"
}

Write-Host "`nLive Signal Runtime output: $RuntimeRoot" -ForegroundColor Cyan
Write-Host "Dashboard: $dashboard" -ForegroundColor Cyan
Write-Host "Read-only. Orders OFF. Publication OFF. Historical archive unchanged." -ForegroundColor Green

if ($OpenDashboard -and (Test-Path $dashboard)) {
    Start-Process $dashboard
}
