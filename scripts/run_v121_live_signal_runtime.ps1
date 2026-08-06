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
    [string]$BybitBars = ".\data\bybit_v1_9\bybit_bars.csv",

    [Parameter(Mandatory=$false)]
    [string]$BybitShadowDir = ".\data\bybit_shadow_v1_10",

    [Parameter(Mandatory=$false)]
    [string]$CryptoRoot = ".\data\crypto_signal_intelligence_v1_25",

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

    [Parameter(Mandatory=$false)]
    [int]$ProductSignalLimit = 24,

    [Parameter(Mandatory=$false)]
    [int]$CryptoSignalLimit = 24,

    [Parameter(Mandatory=$false)]
    [int]$CryptoStructureBatchSize = 400,

    [Parameter(Mandatory=$false)]
    [int]$ProductCandleLimit = 48,

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
$technicalDashboard = Join-Path $RuntimeRoot "dashboard\index.html"
$productUi = Join-Path $RuntimeRoot "product\index.html"
$bybitDecisions = Join-Path $BybitShadowDir "decisions.csv"
$bybitSignals = Join-Path $BybitShadowDir "signals.csv"
$cryptoCandidates = Join-Path $CryptoRoot "candidates.jsonl"
$cryptoOutcomes = Join-Path $CryptoRoot "outcomes.jsonl"
$cryptoFactory = Join-Path $CryptoRoot "factory"
$cryptoPassports = Join-Path $cryptoFactory "passports"

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
if ($ProductSignalLimit -lt 1) {
    throw "ProductSignalLimit must be positive"
}
if ($CryptoSignalLimit -lt 1) {
    throw "CryptoSignalLimit must be positive"
}
if ($CryptoStructureBatchSize -lt 1) {
    throw "CryptoStructureBatchSize must be positive"
}
if ($ProductCandleLimit -lt 1) {
    throw "ProductCandleLimit must be positive"
}

if ($RunTests) {
    & $python -m pytest -q `
        ".\tests\test_crypto_market_structure.py" `
        ".\tests\test_crypto_signal_adapter_v125.py" `
        ".\tests\test_crypto_structure_incremental.py" `
        ".\tests\test_product_ui_v1252.py" `
        ".\tests\test_product_ui_v125.py" `
        ".\tests\test_crypto_signal_adapter.py" `
        ".\tests\test_product_ui_v124.py" `
        ".\tests\test_product_ui_v1235.py" `
        ".\tests\test_product_ui_v1234.py" `
        ".\tests\test_product_ui_v1233.py" `
        ".\tests\test_product_ui_v1232.py" `
        ".\tests\test_product_ui_v1231.py" `
        ".\tests\test_product_ui.py" `
        ".\tests\test_live_signal_runtime_v122.py" `
        ".\tests\test_live_signal_dashboard.py" `
        ".\tests\test_live_signal_runtime.py" `
        ".\tests\test_signal_passport_factory.py" `
        ".\tests\test_signal_to_risk_bridge.py" `
        ".\tests\test_mt5_risk_adapter.py" `
        ".\tests\test_risk_manager.py" `
        ".\tests\test_signal_intelligence.py"
    if ($LASTEXITCODE -ne 0) {
        throw "Live Signal Runtime, incremental Crypto Structure and Product UI tests failed"
    }
}

$arguments = @(
    "-m", "trademind.live_signal_runtime_v122",
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
    throw "Live Signal Runtime v1.22.1 execution failed"
}

& $python -m trademind.live_signal_dashboard `
    --runtime-root $RuntimeRoot `
    --login $Login `
    --candidate-limit $DashboardCandidateLimit
if ($LASTEXITCODE -ne 0) {
    throw "Live Signal Dashboard execution failed"
}

$cryptoSourceReady = (
    (Test-Path $BybitBars) -and
    (Test-Path $bybitDecisions) -and
    (Test-Path $bybitSignals)
)
if ($cryptoSourceReady) {
    & $python -m trademind.crypto_structure_incremental `
        --decisions $bybitDecisions `
        --signals $bybitSignals `
        --bars $BybitBars `
        --output-dir $CryptoRoot `
        --cost-r $CostR.ToString([System.Globalization.CultureInfo]::InvariantCulture) `
        --batch-size $CryptoStructureBatchSize
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "Incremental Crypto Market Structure failed. Forex runtime remains available."
    }
    elseif ((Test-Path $cryptoCandidates) -and (Test-Path $cryptoOutcomes)) {
        & $python -m trademind.signal_passport_factory `
            --candidates $cryptoCandidates `
            --outcomes $cryptoOutcomes `
            --output-dir $cryptoFactory `
            --passports-dir $cryptoPassports `
            --cost-r $CostR.ToString([System.Globalization.CultureInfo]::InvariantCulture) `
            --maximum-candidate-age-seconds $MaximumCandidateAgeSeconds.ToString([System.Globalization.CultureInfo]::InvariantCulture) `
            --candidate-limit 200
        if ($LASTEXITCODE -ne 0) {
            Write-Warning "Crypto Passport Factory failed. Crypto remains shadow-only in Product UI."
        }
    }
}
else {
    Write-Warning "Bybit shadow sources not found. Product UI will show Forex only until crypto files appear."
}

& $python -m trademind.product_ui_v125 `
    --runtime-root $RuntimeRoot `
    --crypto-root $CryptoRoot `
    --bybit-bars $BybitBars `
    --fx-limit $ProductSignalLimit `
    --crypto-limit $CryptoSignalLimit `
    --candle-limit $ProductCandleLimit
if ($LASTEXITCODE -ne 0) {
    throw "TradeMind Product UI v1.25.2 execution failed"
}

Write-Host "`nLive Signal Runtime output: $RuntimeRoot" -ForegroundColor Cyan
Write-Host "Crypto Structure Intelligence: $CryptoRoot" -ForegroundColor Cyan
Write-Host "Product UI: $productUi" -ForegroundColor Cyan
Write-Host "Technical dashboard: $technicalDashboard" -ForegroundColor DarkGray
Write-Host "Read-only. Crypto sizing not calculated. Orders OFF. Publication OFF. Source archives unchanged." -ForegroundColor Green

if ($OpenDashboard -and (Test-Path $productUi)) {
    Start-Process $productUi
}
