param(
    [string]$SourceDir = "$env:APPDATA\MetaQuotes\Terminal\Common\Files\TradeMindAI_Volume_v1_4",
    [string]$Volume = ".\data\volume_v1_4\volume_bars.csv",
    [string]$OutputDir = ".\data\fx_research_v1_4_2",
    [ValidateRange(-14, 14)]
    [int]$ServerUtcOffsetHours = 0
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $projectRoot

& ".\.venv\Scripts\trademind-volume-collect.exe" `
    --source-dir $SourceDir `
    --output $Volume
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

& ".\.venv\Scripts\trademind-fx-research.exe" `
    --volume $Volume `
    --observations (Join-Path $OutputDir "observations.csv") `
    --states (Join-Path $OutputDir "latest.csv") `
    --server-utc-offset-hours $ServerUtcOffsetHours
exit $LASTEXITCODE
