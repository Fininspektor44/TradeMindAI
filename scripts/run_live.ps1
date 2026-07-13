param(
    [string]$ProjectDir = (Split-Path -Parent $PSScriptRoot),
    [string]$DataDir = "$env:APPDATA\MetaQuotes\Terminal\Common\Files\TradeMindAI",
    [string]$Symbols = "XAUUSD,EURUSD,GBPUSD",
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

if (-not (Test-Path $trademindExe)) {
    throw "TradeMind executable not found: $trademindExe"
}

if (-not (Test-Path $DataDir -PathType Container)) {
    throw "MT5 data directory not found: $DataDir"
}

$primarySymbol = ($Symbols -split ",")[0].Trim().ToUpperInvariant()
$timeframeName = $Timeframe.Trim().ToUpperInvariant()
$primaryCsv = Join-Path $DataDir "${primarySymbol}_${timeframeName}.csv"

$env:TRADEMIND_PROVIDER = "csv"
$env:TRADEMIND_DATA_DIR = $DataDir
$env:TRADEMIND_SYMBOLS = $Symbols
$env:TRADEMIND_TIMEFRAME = $timeframeName
$env:TRADEMIND_MAX_DATA_AGE_SECONDS = [string]$MaxDataAgeSeconds

Write-Host "TradeMind live watcher started"
Write-Host "Project: $projectPath"
Write-Host "Data:    $DataDir"
Write-Host "Trigger: $primaryCsv"
Write-Host "Press Ctrl+C to stop."

$lastProcessedCandle = $null

while ($true) {
    try {
        if (-not (Test-Path $primaryCsv -PathType Leaf)) {
            throw "Trigger CSV not found: $primaryCsv"
        }

        $latestRow = Import-Csv $primaryCsv | Select-Object -Last 1
        $latestCandle = $latestRow.time

        if ([string]::IsNullOrWhiteSpace($latestCandle)) {
            throw "Latest candle timestamp is missing in $primaryCsv"
        }

        if ($latestCandle -ne $lastProcessedCandle) {
            Write-Host "`nNew closed candle detected: $latestCandle"
            & $trademindExe

            if ($LASTEXITCODE -eq 0) {
                $lastProcessedCandle = $latestCandle
            }
            else {
                Write-Warning "TradeMind exited with code $LASTEXITCODE. The candle will be retried."
            }
        }
    }
    catch {
        Write-Warning $_.Exception.Message
    }

    Start-Sleep -Seconds $PollSeconds
}
