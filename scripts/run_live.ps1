param(
    [string]$ProjectDir = (Split-Path -Parent $PSScriptRoot),
    [string]$DataDir = "$env:APPDATA\MetaQuotes\Terminal\Common\Files\TradeMindAI",
    [string]$Symbols = "XAUUSD",
    [string]$Timeframe = "M5",
    [int]$PollSeconds = 10,
    [int]$MaxDataAgeSeconds = 900
)

$ErrorActionPreference = "Stop"

if ($PollSeconds -lt 1) {
    throw "PollSeconds must be at least 1."
}

$projectPath = (Resolve-Path $ProjectDir).Path
$trademindExe = Join-Path $projectPath ".venv\Scripts\trademind.exe"
$journalPath = Join-Path $projectPath "data\journal"

if (-not (Test-Path $trademindExe)) {
    throw "TradeMind executable not found: $trademindExe"
}

if (-not (Test-Path $DataDir -PathType Container)) {
    throw "MT5 data directory not found: $DataDir"
}

$symbolNames = @(
    $Symbols -split "," |
        ForEach-Object { $_.Trim().ToUpperInvariant() } |
        Where-Object { -not [string]::IsNullOrWhiteSpace($_) }
)
$timeframeName = $Timeframe.Trim().ToUpperInvariant()

$env:TRADEMIND_PROVIDER = "csv"
$env:TRADEMIND_DATA_DIR = $DataDir
$env:TRADEMIND_SYMBOLS = ($symbolNames -join ",")
$env:TRADEMIND_TIMEFRAME = $timeframeName
$env:TRADEMIND_MAX_DATA_AGE_SECONDS = [string]$MaxDataAgeSeconds
$env:TRADEMIND_JOURNAL_DIR = $journalPath
$env:TRADEMIND_EVAL_HORIZONS = "3,6,12"
$env:TRADEMIND_POINT_SIZES = "XAUUSD=0.01,EURUSD=0.00001,GBPUSD=0.00001"

Write-Host "TradeMind live watcher started"
Write-Host "Project: $projectPath"
Write-Host "Data:    $DataDir"
Write-Host "Journal: $journalPath"
Write-Host "Watching: $($symbolNames -join ',') $timeframeName"
Write-Host "Press Ctrl+C to stop."

$lastProcessedSignature = $null

while ($true) {
    try {
        $signatureParts = foreach ($symbol in $symbolNames) {
            $csvPath = Join-Path $DataDir "${symbol}_${timeframeName}.csv"
            if (-not (Test-Path $csvPath -PathType Leaf)) {
                "${symbol}=MISSING"
                continue
            }

            $latestRow = Import-Csv $csvPath | Select-Object -Last 1
            $latestCandle = $latestRow.time
            if ([string]::IsNullOrWhiteSpace($latestCandle)) {
                "${symbol}=EMPTY"
            }
            else {
                "${symbol}=${latestCandle}"
            }
        }
        $currentSignature = $signatureParts -join ";"

        if ($currentSignature -ne $lastProcessedSignature) {
            Write-Host "`nMarket-data update detected: $currentSignature"
            & $trademindExe

            if ($LASTEXITCODE -eq 0) {
                $lastProcessedSignature = $currentSignature
            }
            else {
                Write-Warning "TradeMind exited with code $LASTEXITCODE. Data will be retried."
            }
        }
    }
    catch {
        Write-Warning $_.Exception.Message
    }

    Start-Sleep -Seconds $PollSeconds
}
