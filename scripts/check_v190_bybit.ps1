param(
    [string]$TaskName = "TradeMindAI-v1.9-Bybit",
    [switch]$OpenDashboard
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$outputDir = Join-Path $projectRoot "data\bybit_v1_9"
$statusPath = Join-Path $outputDir "status.json"
$universePath = Join-Path $outputDir "universe.csv"
$latestPath = Join-Path $outputDir "latest.csv"
$dashboard = Join-Path $outputDir "dashboard\index.html"

if (-not (Test-Path $statusPath)) {
    throw "Bybit status not found: $statusPath"
}

$status = Get-Content $statusPath -Raw | ConvertFrom-Json
Write-Host "`n=== BYBIT STATUS ===" -ForegroundColor Cyan
$status | Select-Object state,updated_at,last_event_at,messages,bars_written,reconnects,orders_enabled | Format-List

if (Test-Path $universePath) {
    Write-Host "=== FIXED BYBIT UNIVERSE ===" -ForegroundColor Yellow
    Import-Csv $universePath | Select-Object rank,symbol,turnover24h,lastPrice,price24hPcnt | Format-Table -AutoSize
}

if (Test-Path $latestPath) {
    Write-Host "=== LATEST CLOSED M5 BARS ===" -ForegroundColor Green
    Import-Csv $latestPath | Select-Object symbol,close,trade_count,delta_turnover,cvd_turnover,book_imbalance_10,spread_bps,funding_rate,open_interest_value,received_at | Format-Table -AutoSize
}

Write-Host "=== WINDOWS TASK ===" -ForegroundColor Magenta
Get-ScheduledTaskInfo -TaskName $TaskName | Format-List LastRunTime,LastTaskResult,NextRunTime

if ($OpenDashboard -and (Test-Path $dashboard)) {
    Start-Process $dashboard
}
