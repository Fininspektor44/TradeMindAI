param(
    [ValidateRange(2, 50)]
    [int]$TopN = 10,
    [double]$MinTurnover = 5000000,
    [ValidateRange(0.25, 24)]
    [double]$RefreshHours = 6,
    [double]$RunSeconds = 0,
    [switch]$DiscoverOnly,
    [switch]$OpenDashboard
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    throw "Python environment not found: $python"
}

$outputDir = Join-Path $projectRoot "data\bybit_v1_9"
$logDir = Join-Path $outputDir "logs"
New-Item -ItemType Directory -Path $logDir -Force | Out-Null
$logPath = Join-Path $logDir ("bybit_" + (Get-Date -Format "yyyyMMdd") + ".log")

$arguments = @(
    "-m", "trademind.bybit_intelligence",
    "--output-dir", $outputDir,
    "--top-n", $TopN,
    "--min-turnover", $MinTurnover,
    "--refresh-hours", $RefreshHours
)
if ($DiscoverOnly) {
    $arguments += "--discover-only"
}
if ($RunSeconds -gt 0) {
    $arguments += @("--run-seconds", $RunSeconds)
}

Write-Host "TradeMind v1.9 Bybit Market Intelligence"
Write-Host "Mode: public read-only market data"
Write-Host "Universe: BTC/ETH plus liquid altcoins, total $TopN"
Write-Host "Output: $outputDir"
Write-Host "No API key and no order function are used."

& $python @arguments 2>&1 | Tee-Object -FilePath $logPath -Append
if ($LASTEXITCODE -ne 0) {
    throw "Bybit collector exited with code $LASTEXITCODE"
}

$dashboard = Join-Path $outputDir "dashboard\index.html"
if ($OpenDashboard -and (Test-Path $dashboard)) {
    Start-Process $dashboard
}
