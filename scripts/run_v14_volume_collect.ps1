param(
    [string]$SourceDir = "$env:APPDATA\MetaQuotes\Terminal\Common\Files\TradeMindAI_Volume_v1_4",
    [string]$Output = ".\data\volume_v1_4\volume_bars.csv"
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $projectRoot

& ".\.venv\Scripts\trademind-volume-collect.exe" `
    --source-dir $SourceDir `
    --output $Output
exit $LASTEXITCODE
